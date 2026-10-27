"""The shared, pure, deterministic cost function (SPEC 9.1, Rule 3).

The coordinator's allocator, every robot's ``evaluate_envelope`` and every contract-net
bid call the same functions here, so a robot never disagrees with the coordinator about
feasibility for reasons other than fresher state.

    base = w.travel_time * travel_s + w.battery * battery_used_pct + w.commitment * commitment_s
    cost = base * (1 - w.priority * task.priority / 100)

* travel: straight line to the task's start pose at ``max_speed_mps`` plus turn-in-place
  time (a route graph replaces the straight line when ``route_graph_uri`` is available);
* battery: ``battery_drain_pct_per_m`` over travel + work distance; infeasible when the
  remaining battery would fall below ``floor + battery_margin_pct``. Manifests with
  ``battery_reliable: false`` use ``unknown_battery_pct``;
* commitment: seconds until the robot's current commitment ends;
* capability mismatch is infeasible; a target outside the envelope region is infeasible.
"""

import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from a6g_common.config import AllocatorConfig, CampusConfig, RobotManifest
from a6g_common.geometry import point_in_polygon
from a6g_common.routegraph import crosses
from a6g_common.schemas import (
    PoiTarget,
    Pose2D,
    PoseTarget,
    RefusalReason,
    RobotState,
    SectorTarget,
    TaskKind,
    TaskSpec,
)


@dataclass(frozen=True)
class Infeasible:
    reason: RefusalReason
    detail: str


@dataclass(frozen=True)
class Estimate:
    cost: float
    travel_s: float
    work_s: float
    distance_m: float
    battery_used_pct: float
    battery_after_pct: float
    end_pose: Pose2D


@dataclass(frozen=True)
class VirtualState:
    """Where a robot will be (pose, battery) after its earlier tasks; used for sequences."""

    pose: Pose2D
    battery_pct: float
    commitment_s: float = 0.0


def battery_of(state: RobotState, manifest: RobotManifest, cfg: AllocatorConfig) -> float:
    if manifest.battery_reliable and state.battery_pct is not None:
        return state.battery_pct
    return cfg.unknown_battery_pct


def virtual_state(
    state: RobotState, manifest: RobotManifest, cfg: AllocatorConfig, now: datetime | None = None
) -> VirtualState:
    commitment = 0.0
    if state.commitment_until is not None:
        commitment = max(0.0, (state.commitment_until - (now or state.stamp)).total_seconds())
    pose = Pose2D(x=state.pose.x, y=state.pose.y, yaw=state.pose.yaw)
    return VirtualState(pose, battery_of(state, manifest, cfg), commitment)


def lap_reversed(task: TaskSpec, campus: CampusConfig, start: Pose2D | None) -> bool:
    """Whether a survey runs its route from the last waypoint: as the target says, else (a
    whole lap) from the end nearer ``start`` (walking distance on the footpaths)."""
    target = task.target
    if not isinstance(target, SectorTarget):
        return False
    if target.reverse is not None:
        return target.reverse
    wps = campus.sector(target.sector_id).survey_waypoints
    if start is None or target.from_waypoint or len(wps) < 2:
        return False

    def far(p: Pose2D) -> float:
        walk = route_distance(campus, start, p)
        return walk if walk is not None else math.hypot(p.x - start.x, p.y - start.y)

    to_first, to_last = far(wps[0]), far(wps[-1])
    # A robot with coverage does not start at an end in lost coverage when the other end has
    # coverage: coming out of a lost area it would turn back into it (user report,
    # 2026-10-27, South plaza: after recovering at staging, the next lap started at the
    # arcade). The lap then ends in the lost area instead, and the robot recovers once.
    here = campus.link_level_at(start.x, start.y)
    if here is not None and here != "lost":
        lost_first = campus.link_level_at(wps[0].x, wps[0].y) == "lost"
        lost_last = campus.link_level_at(wps[-1].x, wps[-1].y) == "lost"
        if lost_first != lost_last:
            return lost_first
    if target.prefer_reverse is not None:  # the end patrolled longest ago, if not too far
        length = sum(math.hypot(b.x - a.x, b.y - a.y) for a, b in itertools.pairwise(wps))
        stale, fresh = (to_last, to_first) if target.prefer_reverse else (to_first, to_last)
        if stale - fresh <= length / 2:
            return target.prefer_reverse
    return to_last < to_first


def task_path(task: TaskSpec, campus: CampusConfig, start: Pose2D | None = None) -> list[Pose2D]:
    """Poses the task visits, in order: the first is where work starts. ``start``: where
    the robot is, for a lap that may run from either end."""
    target = task.target
    if isinstance(target, SectorTarget):
        sector = campus.sector(target.sector_id)
        if sector.survey_waypoints:
            wps = list(sector.survey_waypoints)
            if lap_reversed(task, campus, start):
                wps.reverse()
            return wps[target.from_waypoint :] or [wps[-1]]
        xs, ys = zip(*sector.polygon, strict=True)
        return [Pose2D(x=sum(xs) / len(xs), y=sum(ys) / len(ys))]
    if isinstance(target, PoiTarget):
        if target.viewpoint is not None:  # relocated for coverage (SPEC A4.7)
            return [target.viewpoint]
        return [campus.poi_slot(target.poi_id, target.slot)]
    if isinstance(target, PoseTarget):
        return [target.pose]
    raise TypeError(f"unknown target {target!r}")


def target_points(task: TaskSpec, campus: CampusConfig) -> list[tuple[float, float]]:
    """Points that must lie inside an envelope region for the task to be covered."""
    target = task.target
    if isinstance(target, SectorTarget):
        sector = campus.sector(target.sector_id)
        if sector.survey_waypoints:  # the points the robot actually walks
            return [(p.x, p.y) for p in task_path(task, campus)]
        return list(sector.polygon)
    return [(p.x, p.y) for p in task_path(task, campus)]


def region_covers(
    region: Sequence[tuple[float, float]], task: TaskSpec, campus: CampusConfig
) -> bool:
    return all(point_in_polygon(p, region) for p in target_points(task, campus))


_ROUTE_CACHE: dict[tuple[int, str, str, bool], float | None] = {}
_FREE_CACHE: dict[tuple[int, int, int, int, int], bool] = {}


def route_distance(
    campus: CampusConfig, a: Pose2D, b: Pose2D, stairs: bool = False
) -> float | None:
    """Distance from ``a`` to ``b`` over the campus route graph (to and from the nearest
    nodes, then the shortest path; stairs only if allowed), or None without a graph or a
    path (SPEC A3.3.3: travel time on the route graph when it exists)."""
    graph = campus.route_graph
    if graph is None or not graph.nodes:
        return None
    straight = math.hypot(b.x - a.x, b.y - a.y)
    if campus.footpaths_only:  # the robots walk on the footpaths only (2026-10-18)
        factors = campus.robot_edge_factors()  # as the robots walk (patrol paths preferred)
        return _walk_length(graph, (a.x, a.y), (b.x, b.y), stairs, factors)
    if straight <= graph.open_space_max_m:
        # Open plaza: a straight move through free space (cached on a 1 m grid).
        key5 = (id(campus), round(a.x), round(a.y), round(b.x), round(b.y))
        if key5 not in _FREE_CACHE:
            if len(_FREE_CACHE) > 200_000:
                _FREE_CACHE.clear()
            _FREE_CACHE[key5] = not crosses((a.x, a.y), (b.x, b.y), campus.obstacles)
        if _FREE_CACHE[key5]:
            return straight

    def nearest(p: Pose2D) -> tuple[str, float]:
        n = min(graph.nodes, key=lambda n: (math.hypot(n.x - p.x, n.y - p.y), n.id))
        return n.id, math.hypot(n.x - p.x, n.y - p.y)

    (na, da), (nb, db) = nearest(a), nearest(b)
    key = (id(graph), na, nb, stairs)
    if key not in _ROUTE_CACHE:
        if len(_ROUTE_CACHE) > 100_000:
            _ROUTE_CACHE.clear()
        found = graph.shortest_path(na, nb, None if stairs else (lambda e: not e.stairs))
        _ROUTE_CACHE[key] = found[0] if found else None
    path = _ROUTE_CACHE[key]
    if path is None:
        return None
    return max(da + path + db, straight)


_WALKERS: dict[tuple[int, bool, int], Any] = {}
_WALK_CACHE: dict[tuple[int, bool, int, int, int, int, int], float | None] = {}


def _walk_length(
    graph: Any,
    a: tuple[float, float],
    b: tuple[float, float],
    stairs: bool,
    factors: dict[str, float] | None = None,
):
    """Walking distance over the footpaths (cached on a 0.5 m grid)."""
    from a6g_common.footpaths import Walker

    wkey = (id(graph), stairs, id(factors))  # the campus caches its factors
    if wkey not in _WALKERS:
        g = (
            graph
            if stairs
            else graph.model_copy(update={"edges": [e for e in graph.edges if not e.stairs]})
        )
        _WALKERS[wkey] = Walker(g, factors=factors)
    key = (*wkey, round(a[0] * 2), round(a[1] * 2), round(b[0] * 2), round(b[1] * 2))
    if key not in _WALK_CACHE:
        if len(_WALK_CACHE) > 200_000:
            _WALK_CACHE.clear()
        found = _WALKERS[wkey].walk(a, b)
        _WALK_CACHE[key] = found[0] if found else None
    return _WALK_CACHE[key]


def _leg(
    a: Pose2D,
    b: Pose2D,
    manifest: RobotManifest,
    campus: CampusConfig | None = None,
    cfg: AllocatorConfig | None = None,
) -> tuple[float, float, float]:
    """(distance m, seconds, heading at arrival) for a turn-then-drive leg; over the route
    graph when ``campus`` has one and ``cfg.route_costs`` (else a straight line)."""
    dist = math.hypot(b.x - a.x, b.y - a.y)
    if dist < 1e-6:
        return 0.0, 0.0, a.yaw
    if campus is not None and cfg is not None and cfg.route_costs:
        dist = route_distance(campus, a, b, manifest.stairs) or dist
    bearing = math.atan2(b.y - a.y, b.x - a.x)
    turn = abs((bearing - a.yaw + math.pi) % (2 * math.pi) - math.pi)
    return dist, dist / manifest.max_speed_mps + turn * manifest.turn_in_place_s_per_rad, bearing


def estimate(
    start: VirtualState,
    task: TaskSpec,
    manifest: RobotManifest,
    campus: CampusConfig,
    cfg: AllocatorConfig,
    battery_floor_pct: float,
    region: Sequence[tuple[float, float]] | None = None,
) -> Estimate | Infeasible:
    """Estimate one task from ``start``. Pure and deterministic."""
    if not manifest.has_capabilities(task.required_capabilities):
        missing = sorted(set(task.required_capabilities) - {c.value for c in manifest.capabilities})
        return Infeasible(RefusalReason.CAPABILITY, f"missing capabilities {missing}")
    if region is not None and not region_covers(region, task, campus):
        return Infeasible(RefusalReason.REGION, "task target outside the envelope region")

    path = task_path(task, campus, start.pose)
    travel_m, travel_s, heading = _leg(start.pose, path[0], manifest, campus, cfg)
    work_m = work_s = 0.0
    here = Pose2D(x=path[0].x, y=path[0].y, yaw=heading)
    for nxt in path[1:]:
        d, s, heading = _leg(here, nxt, manifest)
        work_m, work_s = work_m + d, work_s + s
        here = Pose2D(x=nxt.x, y=nxt.y, yaw=heading)
    work_s = max(work_s, task.est_duration_s)

    used = manifest.battery_drain_pct_per_m * (travel_m + work_m)
    after = start.battery_pct - used
    needed = battery_floor_pct + cfg.battery_margin_pct
    if after < needed:
        return Infeasible(
            RefusalReason.BATTERY,
            f"battery would end at {after:.1f}% (< floor {battery_floor_pct}% + margin "
            f"{cfg.battery_margin_pct}%)",
        )

    w = cfg.weights
    base = w.travel_time * travel_s + w.battery * used + w.commitment * start.commitment_s
    value = base * (1.0 - w.priority * task.priority / 100.0)
    return Estimate(
        cost=round(max(0.0, value), 6),
        travel_s=round(travel_s, 3),
        work_s=round(work_s, 3),
        distance_m=round(travel_m + work_m, 3),
        battery_used_pct=round(used, 3),
        battery_after_pct=round(after, 3),
        end_pose=here,
    )


def cost(
    state: RobotState,
    task: TaskSpec,
    manifest: RobotManifest,
    campus: CampusConfig,
    cfg: AllocatorConfig,
    battery_floor_pct: float | None = None,
    region: Sequence[tuple[float, float]] | None = None,
) -> float | Infeasible:
    """``cost(robot_state, task, manifest, campus) -> float | INFEASIBLE`` (SPEC 9.1)."""
    floor = cfg.default_battery_floor_pct if battery_floor_pct is None else battery_floor_pct
    result = estimate(
        virtual_state(state, manifest, cfg), task, manifest, campus, cfg, floor, region
    )
    return result if isinstance(result, Infeasible) else result.cost


def after_task(start: VirtualState, est: Estimate) -> VirtualState:
    return VirtualState(
        pose=est.end_pose,
        battery_pct=est.battery_after_pct,
        commitment_s=start.commitment_s + est.travel_s + est.work_s,
    )


#: Nominal charging rate for planning a recharge between patrol laps (%/s).
RECHARGE_PCT_PER_S = 0.2


def estimate_next(
    state: VirtualState,
    task: TaskSpec,
    manifest: RobotManifest,
    campus: CampusConfig,
    cfg: AllocatorConfig,
    battery_floor_pct: float,
    region: Sequence[tuple[float, float]] | None = None,
) -> tuple[Estimate | Infeasible, VirtualState]:
    """The next task of a sequence and the state after it. A patrol lap that the battery
    left by earlier tasks cannot cover is planned after a recharge at the staging area (the
    robot recharges on its own when low), if a charged robot can do it."""
    result = estimate(state, task, manifest, campus, cfg, battery_floor_pct, region)
    if (
        isinstance(result, Infeasible)
        and result.reason is RefusalReason.BATTERY
        and task.kind is TaskKind.SURVEY_SECTOR
        and state.commitment_s > 0  # earlier tasks used the battery
    ):
        full = max(state.battery_pct, cfg.recharge_resume_pct)
        if not manifest.battery_reliable:
            full = max(state.battery_pct, cfg.unknown_battery_pct)
        charged = VirtualState(
            pose=campus.staging,
            battery_pct=full,
            commitment_s=state.commitment_s
            + max(0.0, full - state.battery_pct) / RECHARGE_PCT_PER_S,
        )
        again = estimate(charged, task, manifest, campus, cfg, battery_floor_pct, region)
        if isinstance(again, Estimate):
            result = again
            state = charged
    if isinstance(result, Estimate):
        return result, after_task(state, result)
    return result, state


def evaluate_sequence(
    start: VirtualState,
    tasks: Iterable[TaskSpec],
    manifest: RobotManifest,
    campus: CampusConfig,
    cfg: AllocatorConfig,
    battery_floor_pct: float,
    region: Sequence[tuple[float, float]] | None = None,
) -> list[tuple[TaskSpec, Estimate | Infeasible]]:
    """Evaluate tasks in order, carrying pose and battery forward over feasible ones."""
    out: list[tuple[TaskSpec, Estimate | Infeasible]] = []
    state = start
    for task in tasks:
        result, state = estimate_next(state, task, manifest, campus, cfg, battery_floor_pct, region)
        out.append((task, result))
    return out


def order_nearest_first(
    start: Pose2D,
    tasks: Sequence[TaskSpec],
    campus: CampusConfig,
    priority_weight: float = 0.5,
) -> list[TaskSpec]:
    """Greedy nearest-first ordering with priority weighting, respecting dependencies.

    Score = distance * (1 - priority_weight * priority / 100); ties break on task_id.
    Dependencies on tasks outside ``tasks`` are treated as satisfied elsewhere.
    """
    remaining = {t.task_id: t for t in tasks}
    ordered: list[TaskSpec] = []
    here = start
    while remaining:
        ready = [
            t for t in remaining.values() if not any(d in remaining for d in t.depends_on)
        ] or list(remaining.values())  # dependency cycle: fall back to all (validated upstream)

        def score(t: TaskSpec, origin: Pose2D = here) -> tuple[float, str]:
            p = task_path(t, campus, origin)[0]
            d = math.hypot(p.x - origin.x, p.y - origin.y)
            return d * (1.0 - priority_weight * t.priority / 100.0), t.task_id

        best = min(ready, key=score)
        ordered.append(best)
        del remaining[best.task_id]
        here = task_path(best, campus, here)[-1]
    return ordered


def best_bid(bids: Iterable[tuple[str, float]]) -> tuple[str, float] | None:
    """Lowest cost wins; ties go to the lowest robot_id (SPEC 9.3)."""
    ranked = sorted(bids, key=lambda b: (b[1], b[0]))
    return ranked[0] if ranked else None
