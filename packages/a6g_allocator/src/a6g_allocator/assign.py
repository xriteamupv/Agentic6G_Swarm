"""Deterministic task allocation (SPEC 10.2 ``allocate``, Rule 3).

``assign`` solves rounds of the Hungarian assignment problem
(``scipy.optimize.linear_sum_assignment``) on the shared cost matrix. Each round gives a
robot at most one task; the winners' virtual state (pose, battery, commitment) advances
and the next round assigns the remaining tasks, so a robot can receive several tasks.

Determinism: rows are tasks sorted by (-priority, task_id), columns robots sorted by
robot_id, and a tiny tie-breaking term ``rank(robot_id) * 1e-6`` makes equal costs go to
the lowest robot_id. LLMs never take part; they may only phrase justifications.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
from a6g_common.config import AllocatorConfig, CampusConfig, RobotManifest
from a6g_common.schemas import PoiTarget, RefusalReason, TaskKind, TaskSpec
from scipy.optimize import linear_sum_assignment

from a6g_allocator.cost import Estimate, Infeasible, VirtualState, estimate_next

TIE_EPS = 1e-6
_BIG = 1e12


@dataclass(frozen=True)
class Assignment:
    task_id: str
    robot_id: str
    cost: float
    round: int


@dataclass
class AssignResult:
    assignments: list[Assignment] = field(default_factory=list)
    unallocated: dict[str, RefusalReason] = field(default_factory=dict)
    details: dict[str, str] = field(default_factory=dict)
    rounds: int = 0

    def by_robot(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for a in self.assignments:
            out.setdefault(a.robot_id, []).append(a.task_id)
        return out


def visit_key(task: TaskSpec) -> str | None:
    """Tasks of one kind at one POI: a robot does each at most once."""
    target = task.target
    if isinstance(target, PoiTarget) and task.kind is not TaskKind.HOLD_POST:
        return f"{task.kind.value}:{target.poi_id}"
    return None


def assign(
    tasks: Sequence[TaskSpec],
    robots: Mapping[str, VirtualState],
    manifests: Mapping[str, RobotManifest],
    campus: CampusConfig,
    cfg: AllocatorConfig,
    *,
    battery_floor_pct: float | None = None,
    excluded: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
    pinned: Mapping[str, str] | None = None,
    max_rounds: int | None = None,
    holding: set[str] | frozenset[str] = frozenset(),
    visiting: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
) -> AssignResult:
    """Allocate ``tasks`` to ``robots`` (robot_id -> virtual state).

    ``excluded`` holds (task_id, robot_id) pairs that must not be used (refusals);
    ``pinned`` maps task_id -> robot_id for tasks that belong to a specific robot
    (e.g. its own return_home). ``holding`` are robots that already hold a post: a hold
    lasts until its deadline, so a robot holds one post at a time (a second one queued
    behind it would stay uncovered). ``visiting`` holds (robot_id, kind:poi) pairs already
    planned: copies of one task at one POI ("two robots inspect the bus stop") go to
    different robots. Pure and deterministic.
    """
    holding = set(holding)
    visiting = set(visiting)
    floor = cfg.default_battery_floor_pct if battery_floor_pct is None else battery_floor_pct
    pinned = dict(pinned or {})
    states = dict(robots)
    robot_ids = sorted(states)
    rank = {rid: i for i, rid in enumerate(robot_ids)}
    result = AssignResult()

    nxt: dict[tuple[str, str], VirtualState] = {}

    def est(task: TaskSpec, rid: str) -> Estimate | Infeasible:
        if (task.task_id, rid) in excluded:
            return Infeasible(RefusalReason.CONFLICT, f"{rid} refused this task")
        if task.kind is TaskKind.HOLD_POST and rid in holding:
            return Infeasible(RefusalReason.CONFLICT, f"{rid} already holds a post")
        key = visit_key(task)
        if key and (rid, key) in visiting:
            return Infeasible(RefusalReason.CONFLICT, f"{rid} already goes there for this")
        e, after = estimate_next(states[rid], task, manifests[rid], campus, cfg, floor)
        nxt[(task.task_id, rid)] = after  # a recharge between patrol laps included
        return e

    def take(task: TaskSpec, rid: str) -> None:
        if task.kind is TaskKind.HOLD_POST:
            holding.add(rid)
        key = visit_key(task)
        if key:
            visiting.add((rid, key))

    # Pinned tasks first, in priority order.
    remaining: list[TaskSpec] = []
    for task in sorted(tasks, key=lambda t: (-t.priority, t.task_id)):
        rid = pinned.get(task.task_id)
        if rid is None:
            remaining.append(task)
            continue
        if rid not in states:
            result.unallocated[task.task_id] = RefusalReason.CONFLICT
            result.details[task.task_id] = f"pinned robot {rid} unavailable"
            continue
        e = est(task, rid)
        if isinstance(e, Infeasible):
            result.unallocated[task.task_id] = e.reason
            result.details[task.task_id] = e.detail
            continue
        result.assignments.append(Assignment(task.task_id, rid, e.cost, 0))
        states[rid] = nxt[(task.task_id, rid)]
        take(task, rid)

    limit = max_rounds or cfg.max_rounds
    last_reason: dict[str, Infeasible] = {}
    while remaining and robot_ids and result.rounds < limit:
        result.rounds += 1
        matrix = np.full((len(remaining), len(robot_ids)), _BIG)
        estimates: dict[tuple[int, int], Estimate] = {}
        for i, task in enumerate(remaining):
            for j, rid in enumerate(robot_ids):
                e = est(task, rid)
                if isinstance(e, Estimate):
                    matrix[i, j] = e.cost + TIE_EPS * rank[rid]
                    estimates[i, j] = e
                else:
                    # Keep the most informative reason (battery/capability over conflict).
                    prev = last_reason.get(task.task_id)
                    if prev is None or prev.reason is RefusalReason.CONFLICT:
                        last_reason[task.task_id] = e
        rows, cols = linear_sum_assignment(matrix)
        assigned_now: set[int] = set()
        for i, j in zip(rows, cols, strict=True):
            if (i, j) not in estimates:
                continue
            task, rid = remaining[i], robot_ids[j]
            if task.kind is TaskKind.HOLD_POST and rid in holding:
                continue  # took a post earlier in this round's pinned pass
            key = visit_key(task)
            if key and (rid, key) in visiting:
                continue
            take(task, rid)
            e = estimates[i, j]
            result.assignments.append(Assignment(task.task_id, rid, e.cost, result.rounds))
            est(task, rid)  # the state after it, from the robot's current state
            states[rid] = nxt[(task.task_id, rid)]
            assigned_now.add(i)
        if not assigned_now:
            break
        remaining = [t for i, t in enumerate(remaining) if i not in assigned_now]

    for task in remaining:
        reason = last_reason.get(task.task_id)
        result.unallocated[task.task_id] = reason.reason if reason else RefusalReason.CONFLICT
        result.details[task.task_id] = reason.detail if reason else "no robot available"
    return result
