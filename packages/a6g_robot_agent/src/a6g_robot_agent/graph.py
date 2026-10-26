"""Robot agent graph (SPEC 8.2).

One invocation per event, ``thread_id = f"{robot_id}:{envelope_id}"``. Invocations are
short: ``execute_next`` never waits for a skill. It writes a ``dispatch`` instruction;
the runtime runs the MCP call and feeds the ``SkillResult`` back as a new event. So link
changes, kill-switch and contract-net messages are never blocked behind a running skill.

State is plain JSON (pydantic models are dumped) so checkpoints are portable.

    START -> route_event -+-> evaluate_envelope -+-> sequence_tasks -> execute_next -> report -> END
                          |                      +----------------------------------> report
                          +-> execute_next (skill_result, tick, resume, operator)
                          +-> handle_link_loss -> execute_next
                          +-> triage_observation -> report
                          +-> negotiate -+-> sequence_tasks (award) / report (cfp)
"""

import contextlib
import copy
import json
import logging
import math
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from a6g_allocator.cost import (
    Estimate,
    Infeasible,
    after_task,
    estimate,
    evaluate_sequence,
    lap_reversed,
    order_nearest_first,
    region_covers,
    task_path,
    virtual_state,
)
from a6g_common.clock import Clock
from a6g_common.config import (
    AgentConfig,
    AllocatorConfig,
    CampusConfig,
    InteractionsConfig,
    KnowledgeConfig,
    LinkLossPolicy,
    RobotManifest,
)
from a6g_common.ids import IdFactory
from a6g_common.models import one_sentence
from a6g_common.schemas import (
    Award,
    Bid,
    CallForProposals,
    DecisionKind,
    Envelope,
    EnvelopeReply,
    LinkQuality,
    PoiTarget,
    Pose2D,
    PoseTarget,
    RefusalReason,
    Report,
    ReportKind,
    RobotState,
    SectorTarget,
    SkillName,
    SkillOutcome,
    SkillResult,
    TaskKind,
    TaskSpec,
    TrafficClass,
)
from a6g_common.statusvocab import match_subject, status_from_label
from a6g_interactions import language as lang_
from a6g_observability.decision_log import DecisionSink, make_decision
from a6g_transport.budget import CLASS_PRIORITY, Admission, UplinkBudget
from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict  # pydantic needs it on Python < 3.12

from a6g_robot_agent import guidance
from a6g_robot_agent.cognition import (
    Deliberation,
    Participation,
    Replan,
    Summary,
    ask_once,
    check_deliberation,
    check_participation,
    check_replan,
    check_summary,
    propose,
    untrusted,
)
from a6g_robot_agent.perception import Candidate, PerceptionPipeline, image_size
from a6g_robot_agent.uplink import Uplink

log = logging.getLogger(__name__)

#: Pseudo task id for the link-loss ``return_home`` policy.
RETURN_HOME_TASK = "__link_loss_return_home__"
#: Pseudo task for hold_then_recover: drive to a link recovery point.
RECOVERY_TASK = "__link_recovery__"
PSEUDO_TASKS = frozenset({RETURN_HOME_TASK, RECOVERY_TASK})

ACTIVE_STATUSES = frozenset({"accepted", "executing", "holding"})

#: ``follow_route`` takes at most this many waypoints (SPEC 6.1): longer routes (a survey
#: along the footpaths) are walked in consecutive legs that share their boundary waypoint.
MAX_ROUTE_WAYPOINTS = 20


def route_legs(waypoints: list[Any], size: int = MAX_ROUTE_WAYPOINTS) -> tuple[list, list[int]]:
    """(legs, index of each leg's first waypoint in the whole route)."""
    legs, starts, i = [], [], 0
    while True:
        leg = waypoints[i : i + size]
        legs.append(leg)
        starts.append(i)
        if i + len(leg) >= len(waypoints):
            return legs, starts
        i += len(leg) - 1


class AgentState(TypedDict, total=False):
    robot_id: str
    event: dict[str, Any]
    envelope: dict[str, Any] | None
    mission_id: str | None
    status: str  # new | accepted | refused | executing | holding | completed
    tasks: dict[str, dict[str, Any]]  # task_id -> {spec, status, outcome, detail}
    sequence: list[str]
    current_task_id: str | None
    dispatch: dict[str, Any] | None  # instruction to the runtime: {task_id, tool, args}
    link_quality: str
    holding: bool
    hold_reason: str | None
    returned_home: bool
    recovery_stage: str | None  # hold_then_recover: holding | recovering | returning | home
    link_lost_since: str | None  # ISO time the link-loss hold started (profile clock)
    recovery_target: dict[str, Any] | None
    pose: dict[str, Any] | None
    outbox: list[dict[str, Any]]  # queued Reports (+ "nbytes")
    sent_count: int
    dropped_count: int
    reply: dict[str, Any] | None  # direct A2A reply (EnvelopeReply / Bid / ack)
    last_reply: dict[str, Any] | None
    cfps: dict[str, dict[str, Any]]  # cfp_id -> CallForProposals seen
    awards: list[str]  # award_ids already accepted (idempotency)
    awards_made: list[dict[str, Any]]  # awards this robot issued as contract-net initiator
    cancel_current: bool  # instruction to the runtime: the running task was preempted
    yielding: str | None  # running task cancelled for a higher-priority one: back to pending
    followups: list[dict[str, Any]]  # events the runtime should feed back
    branch: str
    # Cognitive core (SPEC A2.3): the model proposes, validate_proposal disposes.
    deliberation: dict[str, Any] | None  # envelope + feasibility table being deliberated
    replan_trigger: dict[str, Any] | None  # what happened (skill failed, observation, link)
    proposal: dict[str, Any] | None  # {function, value, attempt, history, stats}
    model_order: list[str] | None  # validated task order (within priority and lap)
    observations: list[dict[str, Any]]  # recent own observations (deliberation input)
    hold_until: str | None  # a replan hold ends at this (ISO) time
    #: An interaction thread: {interaction_id, stage}. The content lives only in the
    #: InteractionStore and the robot's in-memory copy (rule 28).
    conversation: dict[str, Any] | None
    releasing: str | None  # survey released to the fleet for a person's escort (A3.4.4)
    actions: list[dict[str, Any]]  # instructions to the runtime (request_help ...)


@dataclass
class AgentDeps:
    robot_id: str
    manifest: RobotManifest
    campus: CampusConfig
    allocator: AllocatorConfig
    agent_cfg: AgentConfig
    link_policy: LinkLossPolicy
    uplink: Uplink
    budget: UplinkBudget
    decisions: DecisionSink
    perception: PerceptionPipeline
    clock: Clock
    ids: IdFactory
    model: BaseChatModel | None = None
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    #: Knowledge server (SPEC A3.3; P10): places and people routes. None: no routes.
    kb: Any = None
    #: InteractionStore client (SPEC A3.5). None: the robot's copy only.
    interactions: Any = None
    icfg: InteractionsConfig = field(default_factory=InteractionsConfig)
    #: The robot's in-memory copies of its interactions: never checkpointed (rule 28).
    local: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Speech output (simulation: the console; Gazebo: an on-screen label). Never logged.
    speak: Callable[[str], None] = field(default=lambda _text: None)

    @property
    def actor(self) -> str:
        return f"{self.robot_id}.agent"


# --------------------------------------------------------------------------- helpers


def route_headings(waypoints: list[Pose2D]) -> list[Pose2D]:
    """Each waypoint faces along the leg that arrives at it (the first, towards the second).

    Campus waypoints carry no meaningful yaw (0 = east); goals with it make every corner a
    turn in place to face east, which is slow (a 180 deg static-walk turn takes the Go2 ~30 s)
    and, next to walls, hard for the Summit's padded footprint.
    """
    if len(waypoints) < 2:
        return list(waypoints)
    out = []
    for i, w in enumerate(waypoints):
        a, b = (w, waypoints[1]) if i == 0 else (waypoints[i - 1], w)
        yaw = math.atan2(b.y - a.y, b.x - a.x) if (a.x, a.y) != (b.x, b.y) else w.yaw
        out.append(w.model_copy(update={"yaw": round(yaw, 6)}))
    return out


_LAP = re.compile(r"-lap(\d+)$")


def lap_number(task_id: str) -> int:
    """Patrol lap of a task (the coordinator names laps "<task>-lap<n>"); 1 otherwise."""
    m = _LAP.search(task_id)
    return int(m.group(1)) if m else 1


def task_to_skill(
    task: TaskSpec, campus: CampusConfig, agent_cfg: AgentConfig, deps: AgentDeps
) -> tuple[SkillName, dict[str, Any]]:
    """Map a task to one coarse MCP skill call."""
    target = task.target
    if task.kind is TaskKind.RETURN_HOME:
        return SkillName.RETURN_HOME, {}
    if task.kind is TaskKind.SURVEY_SECTOR:
        waypoints = route_headings(task_path(task, campus))
        return SkillName.FOLLOW_ROUTE, {"waypoints": [w.model_dump() for w in waypoints]}
    if task.kind is TaskKind.INSPECT_POI and isinstance(target, PoiTarget):
        dwell = min(agent_cfg.inspect_dwell_s, task.est_duration_s or agent_cfg.inspect_dwell_s)
        args: dict[str, Any] = {"poi_id": target.poi_id, "dwell_s": dwell}
        if target.slot:  # several robots at this POI: its own spot beside the viewpoint
            args["viewpoint"] = task_path(task, campus)[0].model_dump()
        return SkillName.INSPECT, args
    if task.kind is TaskKind.HOLD_POST:
        pose = task_path(task, campus)[0]
        until = deps.clock.now() + timedelta(seconds=max(1.0, task.est_duration_s))
        return SkillName.HOLD_POST, {"pose": pose.model_dump(), "until": until.isoformat()}
    # go_to, and escort (a placeholder, SPEC 14: go to the meeting point).
    if isinstance(target, PoiTarget) and target.slot:
        return SkillName.NAVIGATE_TO, {"pose": task_path(task, campus)[0].model_dump()}
    if isinstance(target, PoiTarget):
        return SkillName.NAVIGATE_TO, {"poi_id": target.poi_id}
    if isinstance(target, PoseTarget):
        return SkillName.NAVIGATE_TO, {"pose": target.pose.model_dump()}
    if isinstance(target, SectorTarget):
        return SkillName.NAVIGATE_TO, {"pose": task_path(task, campus)[0].model_dump()}
    raise ValueError(f"cannot map task {task.task_id}")


def oriented(
    spec: TaskSpec, entry: dict[str, Any], campus: CampusConfig, pose: Any = None
) -> TaskSpec:
    """A patrol lap free to run from either end: fixed, when it starts, to the end nearer
    the robot, and kept with the task (resumes and remainders follow the same direction)."""
    t = spec.target
    if not isinstance(t, SectorTarget) or t.reverse is not None:
        return spec
    if entry.get("reverse") is None:
        start = Pose2D(x=float(pose["x"]), y=float(pose["y"])) if pose else None
        entry["reverse"] = lap_reversed(spec, campus, start)
    return spec.model_copy(update={"target": t.model_copy(update={"reverse": entry["reverse"]})})


def _left_of(spec: TaskSpec, entry: dict[str, Any]) -> TaskSpec:
    """What is left of a task under way: a survey from the start of its current leg."""
    legs = entry.get("route_legs")
    if not isinstance(spec.target, SectorTarget) or not legs:
        return spec
    done = spec.target.from_waypoint + legs["starts"][legs["leg"]]
    reverse = spec.target.reverse if entry.get("reverse") is None else entry["reverse"]
    target = spec.target.model_copy(update={"from_waypoint": done, "reverse": bool(reverse)})
    return spec.model_copy(update={"target": target})


def _class_rank(item: dict[str, Any]) -> int:
    return CLASS_PRIORITY.index(TrafficClass(item["traffic_class"]))


class _Ctx:
    """Per-node helper bound to the deps and the current state."""

    def __init__(self, deps: AgentDeps, state: AgentState) -> None:
        self.deps = deps
        self.state = state
        self.outbox: list[dict[str, Any]] = list(state.get("outbox") or [])

    @property
    def mission_id(self) -> str | None:
        return self.state.get("mission_id")

    @property
    def envelope_id(self) -> str | None:
        env = self.state.get("envelope")
        return env["envelope_id"] if env else None

    def report(
        self,
        kind: ReportKind,
        payload: dict[str, Any],
        traffic_class: TrafficClass = TrafficClass.CONTROL_MSGS,
        extra_bytes: int = 0,
        mission_id: str | None = None,
    ) -> None:
        rep = Report(
            report_id=self.deps.ids("rep"),
            robot_id=self.deps.robot_id,
            mission_id=mission_id or self.mission_id,
            envelope_id=self.envelope_id,
            kind=kind,
            traffic_class=traffic_class,
            payload=payload,
            stamp=self.deps.clock.now(),
        )
        item = rep.model_dump(mode="json")
        item["nbytes"] = len(json.dumps(item)) + extra_bytes
        self.outbox.append(item)

    def decide(
        self,
        kind: DecisionKind,
        output: dict[str, Any],
        *,
        policy: dict[str, Any] | None = None,
        justification: str = "",
        inputs_ref: str = "",
        mission_id: str | None = None,
    ) -> None:
        decision = self.deps.decisions.record(
            make_decision(
                self.deps.actor,
                kind,
                mission_id=mission_id or self.mission_id,
                inputs_ref=inputs_ref,
                output=output,
                policy=policy,
                justification=justification,
                clock=self.deps.clock,
                ids=self.deps.ids,
            )
        )
        self.report(ReportKind.DECISION, decision.model_dump(mode="json"))


def _policy(deps: AgentDeps, floor: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "cost_weights": deps.allocator.weights.model_dump(),
        "battery_margin_pct": deps.allocator.battery_margin_pct,
    }
    if floor is not None:
        out["battery_floor_pct"] = floor
    return out


# --------------------------------------------------------------------------- graph


def build_graph(deps: AgentDeps) -> StateGraph:
    def _thinking() -> bool:
        """The cognitive core is on (a model, and cognition enabled)."""
        return deps.model is not None and deps.agent_cfg.cognition

    async def route_event(state: AgentState) -> dict[str, Any]:
        ev = state["event"]
        update: dict[str, Any] = {
            "robot_id": deps.robot_id,
            "dispatch": None,
            "reply": None,
            "followups": [],
            "cancel_current": False,
            "actions": [],
            "proposal": None,
        }
        if ev["type"] == "coordinator":
            ctx = _Ctx(deps, state)
            ctx.decide(
                DecisionKind.FALLBACK,
                {"coordinator_reachable": ev["reachable"]},
                policy={"rule": "coordinator_loss", "p2p": "contract_net on swappable tasks"},
                justification=(
                    "coordinator back: reconcile envelopes and awards"
                    if ev["reachable"]
                    else "coordinator lost: keep executing envelopes; redistribute peer-to-peer"
                ),
            )
            update["outbox"] = ctx.outbox
        if ev["type"] == "work_request":
            # Nothing left to do in any envelope: ask the coordinator for work. It may move
            # here tasks another robot has queued but not started (load balancing).
            ctx = _Ctx(deps, state)
            ctx.report(
                ReportKind.ENVELOPE_STATUS,
                {"status": "idle", "pose": ev.get("pose"), "battery_pct": ev.get("battery_pct")},
            )
            update["outbox"] = ctx.outbox
        if ev["type"] == "reconcile":
            # Coordinator is back (SPEC 9.4): send the current envelope and our awards.
            ctx = _Ctx(deps, state)
            env = state.get("envelope") or {}
            ctx.report(
                ReportKind.RECONCILE,
                {
                    "envelope_id": env.get("envelope_id"),
                    "version": env.get("version"),
                    "tasks": {
                        tid: {"status": t["status"], "spec": t["spec"]}
                        for tid, t in (state.get("tasks") or {}).items()
                    },
                    "awards_accepted": list(state.get("awards") or []),
                    "awards_made": list(state.get("awards_made") or []),
                },
            )
            update["outbox"] = ctx.outbox
        if ev["type"] in ("utterance", "interaction_end") and ev.get("mission_id"):
            update["mission_id"] = ev["mission_id"]  # the mission the robot is serving
        if "robot_state" in ev:
            rs = ev["robot_state"]
            update["pose"] = rs["pose"]
            # An active thread follows link changes (link_change events); an idle one does
            # not get them, so it takes the live reading (2026-10-22: the summit refused work
            # at the base, "no connection", from a loss remembered by its finished envelope).
            active = state.get("status") in ACTIVE_STATUSES
            update["link_quality"] = (active and state.get("link_quality")) or rs["link_quality"]
        if ev["type"] == "operator":
            ctx = _Ctx(deps, state)
            command = ev["command"]
            if command == "hold":
                update.update(holding=True, hold_reason=ev.get("reason", "operator"))
            elif command == "resume" and state.get("hold_reason") != "link_lost":
                update.update(holding=False, hold_reason=None)
            ctx.decide(
                DecisionKind.FALLBACK,
                {"command": command, "reason": ev.get("reason", "operator")},
                policy={"rule": "operator_command"},
                justification=f"operator command {command}",
            )
            update["outbox"] = ctx.outbox
        return update

    def pick_branch(state: AgentState) -> str:
        return {
            "envelope": "evaluate_envelope",
            "skill_result": "execute_next",
            "tick": "execute_next",
            "resume": "execute_next",
            "operator": "execute_next",
            "link_change": "handle_link_loss",
            "battery_low": "handle_battery",
            "battery_charged": "handle_battery",
            "link_recover": "execute_next",
            "observation_candidate": "triage_observation",
            "utterance": "converse",
            "interaction_end": "end_interaction",
            "route_status": "execute_next",
            "person_end": "execute_next",
            "cfp": "negotiate",
            "award": "negotiate",
            "award_made": "negotiate",
        }.get(state["event"]["type"], "report")  # flush, reconcile -> report

    async def evaluate_envelope(state: AgentState) -> dict[str, Any]:
        """Code: the feasibility and cost table (SPEC A2.3.2). The model, if any, decides in
        ``deliberate``; without one the table's verdicts are the decision."""
        ev = state["event"]
        env = Envelope.model_validate(ev["envelope"])
        prev = state.get("envelope")
        if prev and prev["envelope_id"] == env.envelope_id and prev["version"] >= env.version:
            return {"reply": state.get("last_reply"), "branch": "report"}  # idempotent

        robot_state = RobotState.model_validate(ev["robot_state"])
        floor = env.budgets.battery_floor_pct
        ordered = order_nearest_first(
            Pose2D(x=robot_state.pose.x, y=robot_state.pose.y), env.tasks, deps.campus
        )
        link = LinkQuality(state.get("link_quality") or robot_state.link_quality)
        blocking = [c for c in ev.get("commitments", []) if c["priority"] > env.priority]

        results: list[tuple[TaskSpec, Estimate | Infeasible]]
        if ev.get("recharging"):
            results = [
                (t, Infeasible(RefusalReason.BATTERY, "recharging at the staging area"))
                for t in ordered
            ]
        elif link is LinkQuality.LOST:
            results = [(t, Infeasible(RefusalReason.LINK, "link lost")) for t in ordered]
        elif blocking:
            detail = f"committed to higher-priority envelope {blocking[0]['envelope_id']}"
            results = [(t, Infeasible(RefusalReason.CONFLICT, detail)) for t in ordered]
        else:
            # A task under way (a new version, a resync after a shadow) is not judged again
            # from its start: only what is left of it counts, and it is kept (the low-battery
            # rule covers the rest). Refusing it stopped the robot halfway through a lap.
            old = state.get("tasks") or {}
            running = [t for t in ordered if old.get(t.task_id, {}).get("status") == "dispatched"]
            start = virtual_state(robot_state, deps.manifest, deps.allocator)
            results = []
            for t in running:
                left = _left_of(t, old[t.task_id])
                r = estimate(start, left, deps.manifest, deps.campus, deps.allocator, -math.inf)
                if isinstance(r, Infeasible):
                    r = Estimate(0.0, 0.0, 0.0, 0.0, 0.0, start.battery_pct, start.pose)
                start = after_task(start, r)
                results.append((t, r))
            results += evaluate_sequence(
                start,
                [t for t in ordered if t not in running],
                deps.manifest,
                deps.campus,
                deps.allocator,
                floor,
                env.region,
            )
        table = {
            t.task_id: (
                {
                    "feasible": True,
                    "cost": round(r.cost, 2),
                    "battery_after_pct": round(r.battery_after_pct, 1),
                }
                if isinstance(r, Estimate)
                else {"feasible": False, "reason": r.reason.value, "detail": r.detail}
            )
            for t, r in results
        }
        plan = {
            "envelope": env.model_dump(mode="json"),
            "table": table,
            "link": link.value,
            "battery_pct": robot_state.battery_pct,
            "pose": robot_state.pose.model_dump(mode="json"),
        }
        feasible = [tid for tid, row in table.items() if row["feasible"]]
        if _thinking() and feasible:
            return {"deliberation": plan, "proposal": None, "branch": "deliberate"}
        rationale = None
        if deps.model is not None and deps.agent_cfg.llm_justifications and feasible:
            # Cognition off: the model only words the code's decision (pre-A2 behaviour).
            rationale, _ = await one_sentence(
                deps.model,
                "You justify a robot's task-allocation decision in one short sentence. "
                "The decision is already made by deterministic code; do not change it.",
                f"Feasibility table: {json.dumps(table)}",
                "",
            )
        return _settle_envelope(
            state, plan, None, {"model_policy": "fallback_no_model"}, rationale=rationale or None
        )

    def _escort_changes(
        old_tasks: dict[str, dict[str, Any]], tasks: dict[str, dict[str, Any]], current: str | None
    ) -> bool:
        """Escorts of people across envelope versions (SPEC A3.4): one handed over to another
        robot leaves (the person is told; the robot forgets its copy); one retargeted (a
        posture change concludes it at a shelter) replans. True: cancel the running leg."""
        retarget = False
        for tid, old in old_tasks.items():
            iid = old["spec"].get("interaction_id")
            if not iid or old["status"] not in ("pending", "dispatched"):
                continue
            local = deps.local.get(iid)
            if tid not in tasks:
                if local is not None and local.get("stage") != "ended":
                    deps.speak(lang_.say("handover_leave", local["content"].get("lang")))
                    deps.local.pop(iid, None)
                continue
            new = tasks[tid]
            if new["spec"]["target"] != old["spec"]["target"] and old.get("escort"):
                new["escort"] = {**old["escort"], "replan": True}
                retarget = retarget or tid == current
                if local is not None:
                    dest = guidance.dest_of(TaskSpec.model_validate(new["spec"]))
                    if dest and dest.get("kind") == "poi":
                        name = guidance.display_name(deps.campus, "poi", dest["id"])
                        deps.speak(lang_.say("shelter", local["content"].get("lang"), place=name))
        for t in tasks.values():  # the escort arrived: the P7 round trip ends here
            iid = t["spec"].get("interaction_id")
            local = deps.local.get(iid) if iid else None
            if local is not None:
                local["content"].setdefault("t_envelope", deps.clock.now().isoformat())
        return retarget

    def _settle_envelope(
        state: AgentState,
        plan: dict[str, Any],
        chosen: Deliberation | None,
        model_policy: dict[str, Any],
        *,
        rationale: str | None = None,
    ) -> dict[str, Any]:
        """Decide (the validated proposal, else the table's verdicts) and build the reply,
        the task states, the decision record and the yield to higher priorities."""
        env = Envelope.model_validate(plan["envelope"])
        ctx = _Ctx(deps, {**state, "envelope": plan["envelope"], "mission_id": env.mission_id})
        table = plan["table"]
        feasible = [tid for tid, row in table.items() if row["feasible"]]
        refusals = {
            tid: RefusalReason(row["reason"]) for tid, row in table.items() if not row["feasible"]
        }
        details = {tid: row["detail"] for tid, row in table.items() if not row["feasible"]}
        accepted = list(feasible)
        if chosen is not None and chosen.action != "accept_all":
            keep = set(chosen.task_ids) if chosen.action == "accept_subset" else set()
            for tid in feasible:
                if tid not in keep:
                    refusals[tid] = chosen.reason or RefusalReason.CONFLICT
                    details[tid] = f"declined by the robot: {chosen.rationale or 'deliberation'}"
            accepted = [tid for tid in feasible if tid in keep]
        decision = "accept" if not refusals else "refuse" if not accepted else "renegotiate"

        template = {
            "accept": f"Accepting all {len(accepted)} tasks: feasible within the envelope.",
            "refuse": "Refusing the envelope: "
            + "; ".join(f"{k}: {details[k]}" for k in sorted(details)),
            "renegotiate": f"Proposing {len(accepted)} of {len(env.tasks)} tasks; refused "
            + ", ".join(f"{k} ({refusals[k].value})" for k in sorted(refusals)),
        }[decision]
        justification = (
            (chosen.rationale.strip() if chosen is not None else "") or rationale or template
        )

        kind = {
            "accept": DecisionKind.ACCEPT,
            "refuse": DecisionKind.REFUSE,
            "renegotiate": DecisionKind.RENEGOTIATE,
        }[decision]
        ctx.decide(
            kind,
            {
                "envelope_id": env.envelope_id,
                "version": env.version,
                "accepted": accepted,
                "refusals": {k: v.value for k, v in refusals.items()},
                "details": details,
                "estimates": {
                    tid: {"cost": row["cost"], "battery_after_pct": row["battery_after_pct"]}
                    for tid, row in table.items()
                    if row["feasible"]
                },
            },
            policy={**_policy(deps, env.budgets.battery_floor_pct), **model_policy},
            justification=justification,
            inputs_ref=f"envelope:{env.envelope_id}:v{env.version}",
            mission_id=env.mission_id,
        )
        reply = EnvelopeReply(
            envelope_id=env.envelope_id,
            version=env.version,
            robot_id=deps.robot_id,
            mission_id=env.mission_id,
            decision=decision,
            accepted_task_ids=accepted,
            refusals=refusals,
            refusal_details={k: v for k, v in details.items() if k in refusals},
            justification=justification,
            stamp=deps.clock.now(),
        ).model_dump(mode="json")

        old_tasks = state.get("tasks") or {}
        tasks: dict[str, dict[str, Any]] = {}
        for t in env.tasks:
            old = old_tasks.get(t.task_id)
            if old and old["status"] in ("succeeded", "failed", "dispatched", "transferred"):
                tasks[t.task_id] = {**old, "spec": t.model_dump(mode="json")}
            else:
                status = "pending" if t.task_id in accepted else "refused"
                tasks[t.task_id] = {
                    "spec": t.model_dump(mode="json"),
                    "status": status,
                    "outcome": None,
                    "detail": details.get(t.task_id),
                }
        # A new version that drops the running task preempts it (SPEC 10.2 arbitrate).
        current = state.get("current_task_id")
        preempted = current is not None and current != RETURN_HOME_TASK and current not in tasks
        if preempted:
            ctx.report(ReportKind.TASK_STATUS, {"task_id": current, "status": "preempted"})
        retarget = _escort_changes(old_tasks, tasks, current)
        # A higher-priority task (an escort, a post) interrupts lower-priority running work
        # (a patrol lap, an inspection): the robot yields and resumes it afterwards.
        yielding = releasing = None
        running = tasks.get(current) if current else None
        if running and running["status"] == "dispatched" and not preempted:
            spec = TaskSpec.model_validate(running["spec"])
            urgent = [
                tid
                for tid, t in tasks.items()
                if t["status"] == "pending"
                and TaskSpec.model_validate(t["spec"]).priority > spec.priority
            ]
            if urgent and spec.kind not in deps.allocator.non_preemptible_kinds:
                yielding = current
                # A person's escort releases the survey: its uncovered remainder goes to
                # the other robots (SPEC A3.4.4) instead of waiting for this one.
                for_person = any(
                    TaskSpec.model_validate(tasks[u]["spec"]).interaction_id for u in urgent
                )
                if for_person and spec.kind is TaskKind.SURVEY_SECTOR:
                    releasing = current
                ctx.report(ReportKind.TASK_STATUS, {"task_id": current, "status": "paused"})
                ctx.decide(
                    DecisionKind.FALLBACK,
                    {"paused": current, "for": sorted(urgent)},
                    policy={"rule": "higher priority first"},
                    justification=f"{current} yields to {', '.join(sorted(urgent))}",
                    inputs_ref=f"envelope:{env.envelope_id}:v{env.version}",
                    mission_id=env.mission_id,
                )
        keep = decision != "refuse"
        return {
            "envelope": env.model_dump(mode="json"),
            "mission_id": env.mission_id,
            "tasks": tasks,
            "status": ("executing" if current else "accepted") if keep else "refused",
            "reply": reply,
            "last_reply": reply,
            "outbox": ctx.outbox,
            "link_quality": plan["link"],
            "cancel_current": preempted or yielding is not None or retarget,
            "yielding": yielding,
            "releasing": releasing,
            "deliberation": None,
            "proposal": None,
            "model_order": (
                chosen.order if chosen is not None and chosen.order else None
            ),  # [] = none
            "branch": "sequence" if keep else "report",
        }

    # ------------------------------------------------------------- cognitive core (A2.3)

    def _deliberation_facts(state: AgentState, plan: dict[str, Any]) -> str:
        env = plan["envelope"]
        pinned = set(env.get("pinned_task_ids") or [])
        rows = []
        for t in env["tasks"]:
            spec = TaskSpec.model_validate(t)
            row = plan["table"][spec.task_id]
            verdict = (
                f"feasible, cost {row['cost']}, battery after {row['battery_after_pct']} %"
                if row["feasible"]
                else f"INFEASIBLE ({row['reason']}: {row['detail']})"
            )
            rows.append(
                f"- {spec.task_id}: {spec.kind.value} priority {spec.priority}"
                + (" PINNED" if spec.task_id in pinned else "")
                + f"; {verdict}"
            )
        obs = "\n".join(
            f"- {o['obs_id']}: {o['label']} (confidence {o['confidence']:.2f})"
            for o in (state.get("observations") or [])[-5:]
        )
        return (
            f"Robot {deps.robot_id} ({deps.manifest.platform.value}), battery "
            f"{plan['battery_pct']} %, link {plan['link']}.\n"
            f"Envelope {env['envelope_id']} v{env['version']}, mission {env['mission_id']}, "
            f"priority {env['priority']}, swappable {env['swappable']}.\n"
            f"Feasibility and cost table (computed by code):\n"
            + "\n".join(rows)
            + "\nTask ids: "
            + ", ".join(plan["table"])
            + ("\nRecent observations:\n" + obs if obs else "")
        )

    async def deliberate(state: AgentState) -> dict[str, Any]:
        """Model: accept_all / accept_subset / refuse (+ order, rationale)."""
        plan = state["deliberation"] or {}
        prop = state.get("proposal") or {"function": "deliberation", "attempt": 0, "history": []}
        attempt = prop["attempt"] + 1
        value, stats = await ask_once(
            deps.model,
            "deliberation",
            Deliberation,
            _deliberation_facts(state, plan),
            history=[tuple(h) for h in prop["history"]],
            timeout_s=deps.agent_cfg.model_timeouts.deliberation,
            agent_id=deps.robot_id,
            mission_id=plan["envelope"]["mission_id"],
            attempt=attempt,
        )
        return {"proposal": _proposal(prop, "deliberation", attempt, value, stats)}

    async def replan(state: AgentState) -> dict[str, Any]:
        """Model: continue / resequence / skip / escalate / request_help / hold."""
        trigger = state.get("replan_trigger") or {}
        prop = state.get("proposal") or {"function": "replanning", "attempt": 0, "history": []}
        attempt = prop["attempt"] + 1
        tasks = state.get("tasks") or {}
        pending = [
            tid
            for tid in state.get("sequence") or []
            if (tasks.get(tid) or {}).get("status") == "pending"
        ]
        env = state.get("envelope") or {}
        obs = [o for o in state.get("observations") or []][-5:]
        facts = (
            f"Robot {deps.robot_id}, envelope {env.get('envelope_id')} (swappable "
            f"{env.get('swappable')}). Event: {json.dumps(trigger)}.\n"
            f"Pending tasks in order: {pending or 'none'}.\n"
            + "".join(
                f"Observation {o['obs_id']}: {o['label']} ({o['confidence']:.2f})\n" for o in obs
            )
        )
        value, stats = await ask_once(
            deps.model,
            "replanning",
            Replan,
            facts,
            history=[tuple(h) for h in prop["history"]],
            timeout_s=deps.agent_cfg.model_timeouts.replanning,
            agent_id=deps.robot_id,
            mission_id=state.get("mission_id"),
            attempt=attempt,
        )
        return {"proposal": _proposal(prop, "replanning", attempt, value, stats)}

    def _proposal(
        prop: dict[str, Any], function: str, attempt: int, value: Any, stats: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "function": function,
            "attempt": attempt,
            "history": prop.get("history") or [],
            "value": value.model_dump(mode="json") if value is not None else None,
            "latency_s": (prop.get("latency_s") or 0.0) + stats["latency_s"],
            "prompt": stats["prompt"],
            "rejections": prop.get("rejections") or [],
        }

    def _model_policy(prop: dict[str, Any], policy: str) -> dict[str, Any]:
        out: dict[str, Any] = {"model_policy": policy, "prompt": prop.get("prompt")}
        if prop.get("attempt"):
            out.update(model_attempts=prop["attempt"], model_latency_s=round(prop["latency_s"], 3))
        if prop.get("rejections"):
            out["validator_rejections"] = prop["rejections"]
        return out

    async def validate_proposal(state: AgentState) -> dict[str, Any]:
        """Code: accept the proposal, send it back once with the reason, or fall back."""
        prop = state.get("proposal") or {}
        function = prop.get("function")
        value = prop.get("value")
        if function == "deliberation":
            plan = state["deliberation"] or {}
            table = plan["table"]
            feasible = {t for t, r in table.items() if r["feasible"]}
            pinned = set(plan["envelope"].get("pinned_task_ids") or [])
            if value is None:  # no model answer: the table decides
                return _settle_envelope(state, plan, None, _model_policy(prop, "fallback_no_model"))
            chosen = Deliberation.model_validate(value)
            verdict = check_deliberation(chosen, feasible, set(table) - feasible, pinned)
            if verdict is None:
                return _settle_envelope(state, plan, chosen, _model_policy(prop, "model"))
            return _reject(state, prop, value, verdict, _settle_envelope_fallback)
        # replanning
        if value is None:
            return _finish_replan(state, None, _model_policy(prop, "fallback_no_model"))
        chosen_r = Replan.model_validate(value)
        tasks = state.get("tasks") or {}
        pending = [
            tid
            for tid in state.get("sequence") or []
            if (tasks.get(tid) or {}).get("status") == "pending"
        ]
        verdict = check_replan(
            chosen_r,
            pending,
            swappable=bool((state.get("envelope") or {}).get("swappable")),
            observations={o["obs_id"] for o in state.get("observations") or []},
            max_hold_s=deps.agent_cfg.max_hold_s,
        )
        if verdict is None:
            return _finish_replan(state, chosen_r, _model_policy(prop, "model"))
        return _reject(state, prop, value, verdict, _finish_replan_fallback)

    def _reject(state, prop, value, verdict, fallback) -> dict[str, Any]:
        prop = {
            **prop,
            "history": [*prop.get("history", []), [json.dumps(value), verdict]],
            "rejections": [*prop.get("rejections", []), verdict],
        }
        if prop["attempt"] < 2:  # back to the model once, with the reason
            return {"proposal": prop}
        return fallback({**state, "proposal": prop}, _model_policy(prop, "fallback_rejected"))

    def _settle_envelope_fallback(state: AgentState, policy: dict[str, Any]) -> dict[str, Any]:
        return _settle_envelope(state, state["deliberation"] or {}, None, policy)

    def _finish_replan_fallback(state: AgentState, policy: dict[str, Any]) -> dict[str, Any]:
        return _finish_replan(state, None, policy)

    def _finish_replan(
        state: AgentState, chosen: Replan | None, policy: dict[str, Any]
    ) -> dict[str, Any]:
        """Apply a validated replan (the code's default is ``continue``)."""
        ctx = _Ctx(deps, state)
        tasks = copy.deepcopy(state.get("tasks") or {})
        update: dict[str, Any] = {"replan_trigger": None, "proposal": None, "branch": "execute"}
        action = chosen.action if chosen is not None else "continue"
        if action == "resequence" and chosen is not None:
            rest = [t for t in state.get("sequence") or [] if t not in (chosen.order or [])]
            update["sequence"] = [*rest, *(chosen.order or [])]
            update["model_order"] = list(chosen.order or [])
        elif action == "skip" and chosen is not None and chosen.task_id:
            tasks[chosen.task_id].update(status="failed", outcome="skipped", detail=chosen.reason)
            ctx.report(
                ReportKind.TASK_STATUS,
                {
                    "task_id": chosen.task_id,
                    "status": "failed",
                    "outcome": "skipped",
                    "reason": chosen.reason,
                },
            )
            update["tasks"] = tasks
        elif action == "escalate" and chosen is not None:
            obs = next(o for o in state.get("observations") or [] if o["obs_id"] == chosen.obs_id)
            ctx.report(
                ReportKind.OBSERVATION,
                {**obs["observation"], "requires_confirmation": True},
                TrafficClass.EVENT_IMAGE,
            )
        elif action == "request_help" and chosen is not None:
            update["actions"] = [
                *(state.get("actions") or []),
                {"type": "request_help", "task_id": chosen.task_id},
            ]
        elif action == "hold" and chosen is not None:
            until = deps.clock.now() + timedelta(seconds=chosen.hold_s or 0)
            update.update(holding=True, hold_reason="replan", hold_until=until.isoformat())
        ctx.decide(
            DecisionKind.FALLBACK,
            {
                "replan": action,
                **({"proposal": chosen.model_dump(mode="json")} if chosen else {}),
                "trigger": state.get("replan_trigger"),
            },
            policy={"rule": "local replanning", **policy},
            justification=(
                chosen.rationale if chosen and chosen.rationale else f"replan: {action}"
            ),
            inputs_ref=f"replan:{(state.get('replan_trigger') or {}).get('type')}",
        )
        update["outbox"] = ctx.outbox
        return update

    def after_evaluate(state: AgentState) -> str:
        return {"sequence": "sequence_tasks", "deliberate": "deliberate"}.get(
            state.get("branch") or "", "report"
        )

    def after_validate(state: AgentState) -> str:
        prop = state.get("proposal")
        if prop is not None:  # rejected once: back to the model with the reason
            return "deliberate" if prop["function"] == "deliberation" else "replan"
        return {"sequence": "sequence_tasks", "execute": "execute_next"}.get(
            state.get("branch") or "", "report"
        )

    def after_execute(state: AgentState) -> str:
        return "replan" if state.get("branch") == "replan" else "report"

    async def sequence_tasks(state: AgentState) -> dict[str, Any]:
        tasks = state.get("tasks") or {}
        pending = [
            TaskSpec.model_validate(v["spec"]) for v in tasks.values() if v["status"] == "pending"
        ]
        pose = state.get("pose") or deps.campus.staging.model_dump()
        start = Pose2D(x=pose["x"], y=pose["y"])
        # Strictly by priority (an escort before a patrol lap); within a priority, lap by lap
        # (every sector of lap n before any of lap n+1, or the nearest sectors' new laps
        # starve a far one), nearest-first within a lap.
        order: list[str] = []
        # A validated model order (A2.3.2) replaces nearest-first within a priority and lap.
        preferred = {tid: i for i, tid in enumerate(state.get("model_order") or [])}
        for tier in sorted({t.priority for t in pending}, reverse=True):
            same = [t for t in pending if t.priority == tier]
            for lap in sorted({lap_number(t.task_id) for t in same}):
                batch = [t for t in same if lap_number(t.task_id) == lap]
                nearest = [t.task_id for t in order_nearest_first(start, batch, deps.campus)]
                if preferred:
                    nearest.sort(key=lambda tid: preferred.get(tid, len(preferred)))
                order += nearest
        done = [tid for tid in state.get("sequence") or [] if tid not in order]
        return {"sequence": done + order}

    def _hold_then_recover(
        state: AgentState, ctx: _Ctx, ev: dict[str, Any], stage_now: str | None
    ) -> dict[str, Any]:
        """SPEC A1 12.1: hold, then seek a link recovery point, then return home."""
        stage = stage_now or state.get("recovery_stage")
        policy = {
            "link_loss_policy": deps.link_policy.value,
            "hold_s": deps.agent_cfg.link_loss_hold_s,
        }
        home = {
            "task_id": RETURN_HOME_TASK,
            "tool": "return_home",
            "args": {},
            "mission_id": ctx.mission_id,
        }
        if stage is None:
            ctx.decide(
                DecisionKind.FALLBACK,
                {"step": "hold", "hold_s": deps.agent_cfg.link_loss_hold_s},
                policy=policy,
                justification=(
                    f"link lost: holding {deps.agent_cfg.link_loss_hold_s:.0f} s "
                    "before seeking coverage"
                ),
            )
            return {"recovery_stage": "holding", "link_lost_since": deps.clock.now().isoformat()}
        if stage == "holding" and ev["type"] == "link_recover":
            point = _nearest_recovery_point(state, ev)
            if point is not None:
                ctx.decide(
                    DecisionKind.FALLBACK,
                    {
                        "step": "seek_coverage",
                        "recovery_point": point["point_id"],
                        "cost": point["cost"],
                    },
                    policy=policy,
                    justification=f"still no link after the hold: driving to {point['point_id']}",
                )
                return {
                    "recovery_stage": "recovering",
                    "recovery_target": point,
                    "current_task_id": RECOVERY_TASK,
                    "dispatch": {
                        "task_id": RECOVERY_TASK,
                        "tool": "navigate_to",
                        "args": {"pose": point["pose"]},
                        "mission_id": ctx.mission_id,
                    },
                }
            ctx.decide(
                DecisionKind.FALLBACK,
                {"step": "return_home", "reason": "no reachable recovery point"},
                policy=policy,
                justification="no reachable link recovery point: returning home",
            )
            return {
                "recovery_stage": "returning",
                "current_task_id": RETURN_HOME_TASK,
                "dispatch": home,
            }
        if stage == "arrived":
            ctx.decide(
                DecisionKind.FALLBACK,
                {"step": "return_home", "reason": "still no link at the recovery point"},
                policy=policy,
                justification="still no link at the recovery point: returning home",
            )
            return {
                "recovery_stage": "returning",
                "current_task_id": RETURN_HOME_TASK,
                "dispatch": home,
            }
        return {}

    def _nearest_recovery_point(state: AgentState, ev: dict[str, Any]) -> dict[str, Any] | None:
        if "robot_state" not in ev:
            return None
        robot_state = RobotState.model_validate(ev["robot_state"])
        env = state.get("envelope") or {}
        floor = (env.get("budgets") or {}).get(
            "battery_floor_pct", deps.allocator.default_battery_floor_pct
        )
        start = virtual_state(robot_state, deps.manifest, deps.allocator)
        best: dict[str, Any] | None = None
        for point in sorted(deps.campus.link_recovery_points, key=lambda p: p.point_id):
            probe = TaskSpec(
                task_id=f"probe-{point.point_id}",
                kind=TaskKind.RETURN_HOME,
                target=PoseTarget(pose=point.pose),
                est_duration_s=0,
            )
            est = estimate(start, probe, deps.manifest, deps.campus, deps.allocator, floor)
            if isinstance(est, Estimate) and (best is None or est.cost < best["cost"]):
                best = {
                    "point_id": point.point_id,
                    "pose": point.pose.model_dump(),
                    "cost": est.cost,
                }
        return best

    # ------------------------------------------------------------- people escorts (A3.4)

    def _pose_now(state: AgentState, ev: dict[str, Any]) -> dict[str, Any]:
        rs = ev.get("robot_state") or {}
        return rs.get("pose") or state.get("pose") or deps.campus.staging.model_dump()

    def _segment(esc: dict[str, Any]) -> None:
        """The leg just driven, for route compliance (only legs with the person)."""
        if esc["leg"] >= esc["person_from"]:
            esc["segments"].append(
                {
                    "edges": esc["leg_edges"][esc["leg"]],
                    "at": esc["leg_started"],
                    "start": esc["start_node"],
                    "goal": esc["goal_node"],
                }
            )

    async def _fetch(ctx: _Ctx, iid: str, tid: str | None = None) -> dict[str, Any] | None:
        """The interaction by id (store, else my copy, which re-creates a lost entry);
        any fallback is a decision (SPEC A3.5)."""
        local, how = await guidance.fetch(deps, iid)
        if how != "store" and deps.interactions is not None:
            ctx.decide(
                DecisionKind.FALLBACK,
                {"interaction_id": iid, "task_id": tid, "entry": how},
                policy={"rule": f"interaction_{how}"},
                justification={
                    "restored": "the store lost the interaction: re-created from my copy",
                    "local": "interaction store unreachable: using my copy",
                    "lost": "interaction lost in the store and in my copy: asking again",
                }[how],
            )
        return local

    async def _compliance(ctx: _Ctx, iid: str, esc: dict[str, Any] | None) -> bool | None:
        if not esc or not esc.get("segments") or deps.kb is None:
            return None
        local = await _fetch(ctx, iid)
        profile = ((local or {}).get("content") or {}).get("profile") or "default"
        try:
            out = await deps.kb.route_compliance(esc["segments"], profile)
        except Exception:
            return None
        return bool(out["compliant"])

    async def _depart(iid: str, esc: dict[str, Any], replanned: bool) -> None:
        """Say the route aloud (spoken only, never logged: rule 28)."""
        local = deps.local.get(iid)
        if local is None:
            return
        content = local["content"]
        lng = content.get("lang")
        lines = []
        if replanned:
            lines.append(lang_.say("replan", lng))
        elif content.get("handovers") and not esc.get("greeted"):
            place = content.get("place_said") or "?"
            lines.append(lang_.say("handover_greet", lng, robot=deps.robot_id, place=place))
            esc["greeted"] = True
        ex = esc["explain"]
        lines.append(
            lang_.route_explanation(
                lng,
                ex["length_m"],
                ex["est_time_s"],
                content.get("profile") or "default",
                ex["avoided"],
                ex["notices"],
            )
        )
        deps.speak(" ".join(lines))
        if not content.get("t_departure"):
            await guidance.note(deps, iid, t_departure=deps.clock.now().isoformat())

    def _escort_fail(
        ctx: _Ctx, tasks: dict[str, dict[str, Any]], tid: str, iid: str, reason: str
    ) -> dict[str, Any]:
        """The escort cannot go on: the interaction ends as failed (the coordinator closes
        the task and deletes the entry); the robot forgets its copy."""
        tasks[tid].update(status="failed", detail=reason)
        local = deps.local.pop(iid, None) or {}
        content = local.get("content") or {}
        ctx.report(
            ReportKind.INTERACTION_END,
            {
                "interaction_id": iid,
                "robot_id": deps.robot_id,
                "task_id": tid,
                "outcome": "failed",
                "reason": reason,
                **guidance.metrics(content),
            },
        )
        return {
            "tasks": tasks,
            "outbox": ctx.outbox,
            "followups": [{"type": "tick"}],
            "actions": [{"type": "forget_interaction", "interaction_id": iid}],
        }

    async def _escort_go(
        state: AgentState,
        ctx: _Ctx,
        tasks: dict[str, dict[str, Any]],
        tid: str,
        pose: dict[str, Any],
        replanned: bool,
    ) -> dict[str, Any]:
        """Plan the person's route (``plan_route`` with the profile fetched by
        interaction_id) and dispatch its first leg (``follow_route``)."""
        entry = tasks[tid]
        spec = TaskSpec.model_validate(entry["spec"])
        iid = str(spec.interaction_id)
        esc = dict(entry.get("escort") or {"segments": [], "replans": -1, "met": False})
        local = await _fetch(ctx, iid, tid)
        if local is None:
            deps.speak(lang_.say("lost", None))
            return _escort_fail(ctx, tasks, tid, iid, "interaction_lost")
        content = local["content"]
        local["stage"] = "escorting"
        content.setdefault("mission_id", ctx.mission_id)
        profile = content.get("profile") or "default"
        start = {"x": float(pose["x"]), "y": float(pose["y"])}
        legs: list[list[dict[str, float]]] = []
        leg_edges: list[list[str]] = []
        meet = content.get("meeting_pose")
        route = None
        try:
            if (
                meet
                and not esc.get("met")
                and guidance.distance(start, meet) > guidance.MEET_RADIUS_M
            ):
                approach = await deps.kb.plan_route(start, meet, "default")  # without the person
                if approach.get("ok"):
                    legs += approach["legs"]
                    leg_edges += [[] for _ in approach["legs"]]
                start = {"x": float(meet["x"]), "y": float(meet["y"])}
            else:
                esc["met"] = True
            dest = guidance.dest_of(spec) or {}
            goal = (
                {"x": dest["x"], "y": dest["y"]}
                if dest.get("kind") == "pose"
                else guidance.goal_ref(deps, dest)
            )
            route = await deps.kb.plan_route(start, goal, profile)
        except Exception:
            route = None
        if route is None:
            ctx.decide(
                DecisionKind.FALLBACK,
                {"interaction_id": iid, "task_id": tid},
                policy={"rule": "fallback_no_kb"},
                justification="knowledge server unavailable: driving to the destination "
                "without a people route",
            )
            entry.pop("escort", None)
            tool, args = task_to_skill(spec, deps.campus, deps.agent_cfg, deps)
            if entry["status"] != "dispatched":
                entry["status"] = "dispatched"
                ctx.report(ReportKind.TASK_STATUS, {"task_id": tid, "status": "started"})
            return {
                "tasks": tasks,
                "dispatch": {
                    "task_id": tid,
                    "tool": tool.value,
                    "args": args,
                    "mission_id": ctx.mission_id,
                },
                "current_task_id": tid,
                "status": "executing",
                "outbox": ctx.outbox,
            }
        if not route["ok"]:
            deps.speak(lang_.say("no_route", content.get("lang")))
            ctx.decide(
                DecisionKind.FALLBACK,
                {"interaction_id": iid, "task_id": tid, "blocking": len(route["blocking"])},
                policy={"rule": "no_people_route"},
                justification="no route satisfies the person's profile and the statuses in force",
            )
            return _escort_fail(ctx, tasks, tid, iid, "no_route")
        person_from = len(legs)
        legs += route["legs"]
        leg_edges += route["leg_edges"]
        esc.update(
            route_id=route["route_id"],
            legs=legs,
            leg_edges=leg_edges,
            leg=0,
            person_from=person_from,
            start_node=route["start_node"],
            goal_node=route["goal_node"],
            edge_nodes=route["edge_nodes"],
            leg_started=deps.clock.now().isoformat(),
            replans=int(esc.get("replans", -1)) + 1,
            replan=False,
            explain={
                "length_m": route["length_m"],
                "est_time_s": route["est_time_s"],
                "avoided": len(route["statuses_applied"]),
                "notices": [n["text"] for n in route.get("notices") or []],
            },
        )
        entry["escort"] = esc
        if entry["status"] != "dispatched":
            entry["status"] = "dispatched"
            ctx.report(ReportKind.TASK_STATUS, {"task_id": tid, "status": "started"})
        ctx.decide(
            DecisionKind.SKILL,
            {
                "interaction_id": iid,
                "task_id": tid,
                "route_id": route["route_id"],
                "statuses_applied": route["statuses_applied"],
                "notices_applied": route["notices_applied"],
                "length_m": route["length_m"],
                "replans": esc["replans"],
            },
            policy={"rule": "constraint-aware people route (SPEC A3.4.5, rule 26)"},
            justification="people route planned by code on the campus graph",
        )
        if person_from == 0:
            await _depart(iid, esc, replanned)
        return {
            "tasks": tasks,
            "dispatch": {
                "task_id": tid,
                "tool": SkillName.FOLLOW_ROUTE.value,
                "args": {"waypoints": legs[0]},
                "mission_id": ctx.mission_id,
            },
            "current_task_id": tid,
            "status": "executing",
            "outbox": ctx.outbox,
        }

    async def _restricted_ahead(esc: dict[str, Any]) -> bool:
        """A restrictive status on what is left of the route (checked at each leg)."""
        if deps.kb is None:
            return False
        ahead = sorted(guidance.route_elements(esc, esc["leg"]))
        try:
            statuses = await deps.kb.status(ahead)
        except Exception:
            return False
        return any(s["restrictive"] for s in statuses.values())

    async def _escort_result(
        state: AgentState,
        ev: dict[str, Any],
        ctx: _Ctx,
        tasks: dict[str, dict[str, Any]],
        current: str,
    ) -> dict[str, Any] | None:
        """A leg of a person's escort ended: the next leg, a replan, or (None) the normal
        task handling (the last leg arrived; failures; holds)."""
        entry = tasks[current]
        esc = copy.deepcopy(entry["escort"])
        iid = str(entry["spec"].get("interaction_id"))
        result = SkillResult.model_validate(ev["result"])
        if result.outcome is SkillOutcome.SUCCEEDED:
            _segment(esc)
            if esc["leg"] + 1 < len(esc["legs"]):
                esc["leg"] += 1
                await _fetch(ctx, iid, current)  # the profile by id at every leg (A3.5)
                if esc["leg"] == esc["person_from"] and not esc.get("met"):
                    esc["met"] = True  # with the person: the guided part starts
                    entry["escort"] = esc
                    await _depart(iid, esc, False)
                if await _restricted_ahead(esc):
                    entry["escort"] = esc
                    return await _escort_go(state, ctx, tasks, current, _pose_now(state, ev), True)
                esc["leg_started"] = deps.clock.now().isoformat()
                entry["escort"] = esc
                return {
                    "tasks": tasks,
                    "dispatch": {
                        "task_id": current,
                        "tool": SkillName.FOLLOW_ROUTE.value,
                        "args": {"waypoints": esc["legs"][esc["leg"]]},
                        "mission_id": ctx.mission_id,
                    },
                    "current_task_id": current,
                    "status": "executing",
                    "outbox": ctx.outbox,
                }
            entry["escort"] = esc
            await _arrived(ctx, iid, entry, esc)
            return None
        if result.outcome is SkillOutcome.CANCELLED and esc.get("replan"):
            _segment(esc)  # driven up to the cancellation
            entry["escort"] = esc
            return await _escort_go(state, ctx, tasks, current, _pose_now(state, ev), True)
        return None

    async def _arrived(ctx: _Ctx, iid: str, entry: dict[str, Any], esc: dict[str, Any]) -> None:
        compliant = await _compliance(ctx, iid, esc)
        local = deps.local.get(iid)
        await guidance.note(deps, iid, route_compliant=compliant)
        if local is None:
            return
        content = local["content"]
        spec = TaskSpec.model_validate(entry["spec"])
        dest = guidance.dest_of(spec) or {}
        if content.get("conclude_at") and content.get("conclude_at") == dest.get("id"):
            local["outcome"] = "concluded_at_shelter"
            place = guidance.display_name(deps.campus, "poi", dest["id"])
        else:
            local["outcome"] = "arrived"
            place = content.get("place_said") or "?"
        local["stage"] = "arrived"
        # People answer in real time, even when the simulator runs faster: wall clock.
        local["arrival_deadline"] = time.time() + deps.icfg.arrival_confirm_s
        deps.speak(lang_.say("arrived", content.get("lang"), place=place))

    async def _escort_event(
        state: AgentState,
        ev: dict[str, Any],
        ctx: _Ctx,
        tasks: dict[str, dict[str, Any]],
        current: str | None,
    ) -> dict[str, Any]:
        if ev["type"] == "route_status":
            # A restrictive status reached the remaining route: stop the leg and replan.
            entry = tasks.get(current) if current else None
            if entry and entry.get("escort") and ev.get("restrictive"):
                esc = entry["escort"]
                if ev.get("subject") in guidance.route_elements(esc, esc["leg"]):
                    entry["escort"] = {**esc, "replan": True}
                    return {"tasks": tasks, "cancel_current": True, "outbox": ctx.outbox}
            return {}
        iid = ev.get("interaction_id")  # person_end: the person ended the escort
        for tid, entry in tasks.items():
            if entry["spec"].get("interaction_id") != iid:
                continue
            if entry["status"] not in ("pending", "dispatched"):
                continue
            esc = entry.get("escort")
            if esc and tid == current:
                esc = copy.deepcopy(esc)
                _segment(esc)
                entry["escort"] = esc
            await guidance.note(deps, iid, route_compliant=await _compliance(ctx, iid, esc))
            entry.update(status="ended", detail="ended by the person")
            update: dict[str, Any] = {"tasks": tasks, "outbox": ctx.outbox}
            if tid == current:
                update.update(
                    cancel_current=True, current_task_id=None, followups=[{"type": "tick"}]
                )
            return update
        return {}

    async def execute_next(state: AgentState) -> dict[str, Any]:
        ev = state["event"]
        ctx = _Ctx(deps, state)
        tasks = copy.deepcopy(state.get("tasks") or {})
        current = state.get("current_task_id")
        update: dict[str, Any] = {}
        followups: list[dict[str, Any]] = []

        if ev["type"] in ("route_status", "person_end"):
            return await _escort_event(state, ev, ctx, tasks, current)
        long_route = (tasks.get(current) or {}).get("route_legs") if current else None
        if (
            long_route
            and ev["type"] == "skill_result"
            and ev.get("task_id") == current
            and SkillResult.model_validate(ev["result"]).outcome is SkillOutcome.SUCCEEDED
            and long_route["leg"] + 1 < len(long_route["legs"])
        ):
            long_route["leg"] += 1  # the next leg of a long route (a survey)
            return {
                "tasks": tasks,
                "dispatch": {
                    "task_id": current,
                    "tool": SkillName.FOLLOW_ROUTE.value,
                    "args": {"waypoints": long_route["legs"][long_route["leg"]]},
                    "mission_id": ctx.mission_id,
                },
                "current_task_id": current,
                "status": "executing",
                "outbox": ctx.outbox,
            }
        held = tasks.get(current) if current else None
        if (
            held
            and held["spec"].get("until_cleared")
            and ev["type"] == "skill_result"
            and ev.get("task_id") == current
            and SkillResult.model_validate(ev["result"]).outcome is SkillOutcome.SUCCEEDED
        ):
            # An emergency post: one hold period is over, the post is not: hold the next
            # (it ends with the all clear, which drops it from the envelope).
            spec = TaskSpec.model_validate(held["spec"])
            tool, args = task_to_skill(spec, deps.campus, deps.agent_cfg, deps)
            return {
                "tasks": tasks,
                "dispatch": {
                    "task_id": current,
                    "tool": tool.value,
                    "args": args,
                    "mission_id": ctx.mission_id,
                },
                "current_task_id": current,
                "status": "executing",
                "outbox": ctx.outbox,
            }
        if (
            ev["type"] == "skill_result"
            and ev.get("task_id") == current
            and current in tasks
            and tasks[current].get("escort")
        ):
            handled = await _escort_result(state, ev, ctx, tasks, current)
            if handled is not None:
                return handled

        if (
            ev["type"] == "skill_result"
            and ev.get("task_id") == current
            and current not in tasks
            and current not in PSEUDO_TASKS  # the recovery drive and return home are not tasks
        ):
            current = None  # preempted by a new envelope version: its result is moot
        elif ev["type"] == "skill_result" and ev.get("task_id") == current:
            result = SkillResult.model_validate(ev["result"])
            interrupted = result.outcome is SkillOutcome.CANCELLED and (
                state.get("holding") or ev.get("interrupted_by_hold")
            )
            yielded = result.outcome is SkillOutcome.CANCELLED and state.get("yielding") == current
            update["yielding"] = None
            update["releasing"] = None
            if current not in PSEUDO_TASKS and yielded and state.get("releasing") == current:
                # Released for a person's escort: the fleet covers the rest (SPEC A3.4.4).
                spec = TaskSpec.model_validate(tasks[current]["spec"])
                done = re.match(r"waypoint (\d+)", result.detail or "")
                offset = getattr(spec.target, "from_waypoint", 0)
                legs = tasks[current].get("route_legs")
                if legs:  # the index within the leg, from the leg's start in the route
                    offset += legs["starts"][legs["leg"]]
                remainder = offset + (int(done.group(1)) if done else 0)
                reverse = bool(oriented(spec, tasks[current], deps.campus).target.reverse)  # type: ignore[union-attr]
                tasks[current].update(status="released", detail="released for an escort")
                ctx.report(
                    ReportKind.TASK_STATUS,
                    {
                        "task_id": current,
                        "status": "released",
                        "remainder_from": remainder,
                        "remainder_reverse": reverse,
                    },
                )
                ctx.decide(
                    DecisionKind.FALLBACK,
                    {"released": current, "remainder_from": remainder},
                    policy={"rule": "remainder survey to the fleet (SPEC A3.4.4)"},
                    justification=f"{current} released for a person's escort; the uncovered "
                    f"remainder (from waypoint {remainder}) goes to the other robots",
                )
            elif current not in PSEUDO_TASKS and yielded:
                # Gave way to a higher-priority task: resume it afterwards (from the start).
                tasks[current].update(status="pending", detail="paused for a higher priority")
                tasks[current].pop("route_legs", None)
                tasks[current].pop("reverse", None)  # from the nearer end again
            elif current not in PSEUDO_TASKS and interrupted:
                # Stopped by kill-switch/operator hold: re-run it after resume.
                tasks[current].update(status="pending", detail="interrupted by hold")
            elif current == RECOVERY_TASK:
                still_lost = LinkQuality(state.get("link_quality") or "ok") is LinkQuality.LOST
                update["recovery_stage"] = "arrived" if still_lost else None
            elif current == RETURN_HOME_TASK:
                update["recovery_stage"] = "home"
            elif current not in PSEUDO_TASKS:
                ok = result.outcome is SkillOutcome.SUCCEEDED
                tasks[current].update(
                    status="succeeded" if ok else "failed",
                    outcome=result.outcome.value,
                    detail=result.detail,
                )
                spec = TaskSpec.model_validate(tasks[current]["spec"])
                ctx.report(
                    ReportKind.SKILL_RESULT,
                    {"task_id": current, "result": result.model_dump(mode="json")},
                )
                if not ok:
                    # P3: the coordinator reallocates; if unreachable, contract-net.
                    ctx.report(
                        ReportKind.TASK_STATUS,
                        {"task_id": current, "status": "failed", "outcome": result.outcome.value},
                    )
                    update["replan_trigger"] = {
                        "type": "skill_failed",
                        "task_id": current,
                        "outcome": result.outcome.value,
                        "detail": result.detail,
                    }
                elif result.artifact_uri and isinstance(spec.target, PoiTarget):
                    followups.append(
                        {
                            "type": "observation_candidate",
                            "task_id": current,
                            "poi_id": spec.target.poi_id,
                            "image_uri": result.artifact_uri,
                        }
                    )
            current = None
        elif ev["type"] == "resume" and current is not None:
            # Process restart: the skill call died with the old process; re-issue it.
            if current == RETURN_HOME_TASK:
                update["dispatch"] = {
                    "task_id": current,
                    "tool": "return_home",
                    "args": {},
                    "mission_id": ctx.mission_id,
                }
            elif current == RECOVERY_TASK:
                update["dispatch"] = {
                    "task_id": current,
                    "tool": "navigate_to",
                    "args": {"pose": (state.get("recovery_target") or {}).get("pose")},
                    "mission_id": ctx.mission_id,
                }
            elif tasks[current].get("route_legs"):  # a long route: the current leg again
                lr = tasks[current]["route_legs"]
                update["dispatch"] = {
                    "task_id": current,
                    "tool": SkillName.FOLLOW_ROUTE.value,
                    "args": {"waypoints": lr["legs"][lr["leg"]]},
                    "mission_id": ctx.mission_id,
                }
            elif tasks[current].get("escort"):  # a person's escort: the current leg again
                esc = tasks[current]["escort"]
                update["dispatch"] = {
                    "task_id": current,
                    "tool": SkillName.FOLLOW_ROUTE.value,
                    "args": {"waypoints": esc["legs"][esc["leg"]]},
                    "mission_id": ctx.mission_id,
                }
            else:
                spec = TaskSpec.model_validate(tasks[current]["spec"])
                spec = oriented(spec, tasks[current], deps.campus, state.get("pose"))
                tool, args = task_to_skill(spec, deps.campus, deps.agent_cfg, deps)
                update["dispatch"] = {
                    "task_id": current,
                    "tool": tool.value,
                    "args": args,
                    "mission_id": ctx.mission_id,
                }
            return {**update, "outbox": ctx.outbox}

        update.update(tasks=tasks, current_task_id=current, followups=followups, branch=None)
        if current is not None:  # a skill is still running
            update["outbox"] = ctx.outbox
            return update
        trigger = update.get("replan_trigger") or state.get("replan_trigger")
        if trigger and _thinking():
            # Replan (model) before dispatching anything else; validate_proposal comes back.
            update.update(replan_trigger=trigger, proposal=None, branch="replan", outbox=ctx.outbox)
            return update
        update["replan_trigger"] = None
        hold_until = state.get("hold_until")
        if (
            state.get("hold_reason") == "replan"
            and hold_until
            and deps.clock.now() >= datetime.fromisoformat(hold_until)
        ):
            update.update(holding=False, hold_reason=None, hold_until=None)
            state = {**state, "holding": False}

        link = LinkQuality(state.get("link_quality") or "ok")
        if link is LinkQuality.LOST and deps.link_policy is LinkLossPolicy.HOLD_THEN_RECOVER:
            update.update(_hold_then_recover(state, ctx, ev, update.get("recovery_stage")))
            update.setdefault("holding", True)
            update.update(hold_reason="link_lost", status="holding", outbox=ctx.outbox)
            return update
        if link is LinkQuality.LOST:
            if deps.link_policy is LinkLossPolicy.RETURN_HOME and not state.get("returned_home"):
                update.update(
                    dispatch={
                        "task_id": RETURN_HOME_TASK,
                        "tool": "return_home",
                        "args": {},
                        "mission_id": ctx.mission_id,
                    },
                    current_task_id=RETURN_HOME_TASK,
                    returned_home=True,
                )
            update.update(holding=True, hold_reason="link_lost", status="holding")
            update["outbox"] = ctx.outbox
            return update
        if state.get("holding"):
            update.update(status="holding", outbox=ctx.outbox)
            return update

        def priority(tid: str) -> tuple[int, int]:
            entry = tasks.get(tid)
            return (-int(entry["spec"].get("priority", 0)) if entry else 0, lap_number(tid))

        # Highest priority first (a yielded patrol waits for the escort that interrupted it);
        # the stored sequence (nearest-first) breaks ties.
        for tid in sorted(state.get("sequence") or [], key=priority):
            entry = tasks.get(tid)
            if not entry or entry["status"] != "pending":
                continue
            spec = TaskSpec.model_validate(entry["spec"])
            deps_status = [tasks[d]["status"] for d in spec.depends_on if d in tasks]
            if any(s in ("failed", "refused", "blocked") for s in deps_status):
                entry.update(status="blocked", detail="dependency did not succeed")
                ctx.report(ReportKind.TASK_STATUS, {"task_id": tid, "status": "blocked"})
                continue
            if any(s != "succeeded" for s in deps_status):
                continue
            if spec.kind is TaskKind.ESCORT and spec.interaction_id and deps.kb is not None:
                go = await _escort_go(state, ctx, tasks, tid, _pose_now(state, ev), False)
                return {**update, **go, "outbox": ctx.outbox}
            spec = oriented(spec, entry, deps.campus, _pose_now(state, ev))
            tool, args = task_to_skill(spec, deps.campus, deps.agent_cfg, deps)
            if tool is SkillName.FOLLOW_ROUTE and (
                entry.get("route_legs") or len(args["waypoints"]) > MAX_ROUTE_WAYPOINTS
            ):
                if not entry.get("route_legs"):
                    legs, starts = route_legs(args["waypoints"])
                    entry["route_legs"] = {"legs": legs, "starts": starts, "leg": 0}
                lr = entry["route_legs"]  # after a hold: the leg it was on
                args = {"waypoints": lr["legs"][lr["leg"]]}
            entry["status"] = "dispatched"
            started: dict[str, Any] = {"task_id": tid, "status": "started"}
            if entry.get("reverse") is not None:  # the next lap starts at the other end
                started["reverse"] = entry["reverse"]
            ctx.report(ReportKind.TASK_STATUS, started)
            update.update(
                dispatch={
                    "task_id": tid,
                    "tool": tool.value,
                    "args": args,
                    "mission_id": ctx.mission_id,
                },
                current_task_id=tid,
                status="executing",
            )
            update["outbox"] = ctx.outbox
            return update

        open_tasks = [t for t in tasks.values() if t["status"] in ("pending", "dispatched")]
        if not open_tasks and state.get("status") in ("accepted", "executing", "holding"):
            summary = Counter(t["status"] for t in tasks.values())
            ctx.report(
                ReportKind.ENVELOPE_STATUS,
                {
                    "status": "completed",
                    "tasks": dict(sorted(summary.items())),
                    # Where it stands idle, and its battery: the coordinator gives it the
                    # nearest queued work it can finish.
                    "pose": _pose_now(state, ev),
                    "battery_pct": (ev.get("robot_state") or {}).get("battery_pct"),
                },
            )
            update["status"] = "completed"
        update["outbox"] = ctx.outbox
        return update

    async def handle_link_loss(state: AgentState) -> dict[str, Any]:
        ev = state["event"]
        new = LinkQuality(ev["link_quality"])
        old = LinkQuality(state.get("link_quality") or "ok")
        if new == old:
            return {}
        ctx = _Ctx(deps, state)
        update: dict[str, Any] = {"link_quality": new.value}
        if state.get("envelope"):
            update["replan_trigger"] = {"type": "link_change", "from": old.value, "to": new.value}
        if new is LinkQuality.LOST:
            current = state.get("current_task_id")
            stop_now = False
            if (
                deps.link_policy is LinkLossPolicy.HOLD_THEN_RECOVER
                and current not in (None, *PSEUDO_TASKS)
                and state.get("envelope")
                and current in (state.get("tasks") or {})
            ):
                spec = TaskSpec.model_validate(state["tasks"][current]["spec"])
                stop_now = not region_covers(state["envelope"]["region"], spec, deps.campus)
            if stop_now:  # it would leave the envelope region: stop rather than finish
                update.update(cancel_current=True, holding=True, hold_reason="link_lost")
            ctx.decide(
                DecisionKind.FALLBACK,
                {"link_quality": new.value, "current_task_id": current, "stopped": stop_now},
                policy={"link_loss_policy": deps.link_policy.value},
                justification={
                    LinkLossPolicy.FINISH_CURRENT_THEN_HOLD: (
                        "link lost: finish the current task, then hold; no new tasks accepted"
                    ),
                    LinkLossPolicy.RETURN_HOME: (
                        "link lost: finish the current task, then return home and hold"
                    ),
                    LinkLossPolicy.HOLD_THEN_RECOVER: (
                        "link lost: "
                        + (
                            "stop (task leaves the envelope region)"
                            if stop_now
                            else "finish the current task"
                        )
                        + ", hold, then seek coverage; no new tasks accepted"
                    ),
                }[deps.link_policy],
            )
        elif old is LinkQuality.LOST:
            if state.get("hold_reason") == "link_lost":
                update.update(holding=False, hold_reason=None, returned_home=False)
            update.update(recovery_stage=None, link_lost_since=None, recovery_target=None)
            if state.get("current_task_id") in PSEUDO_TASKS:
                update["cancel_current"] = True  # coverage is back: stop the recovery drive
            ctx.decide(
                DecisionKind.FALLBACK,
                {"link_quality": new.value, "queued_reports": len(ctx.outbox)},
                policy={"link_loss_policy": deps.link_policy.value},
                justification="link restored: resume the envelope and flush queued reports",
            )
        update["outbox"] = ctx.outbox
        return update

    async def handle_battery(state: AgentState) -> dict[str, Any]:
        """Low battery: release every open task, stop, recharge at the staging area (the
        runtime drives there); the coordinator hands important work to other robots."""
        ev = state["event"]
        ctx = _Ctx(deps, state)
        pct = ev.get("battery_pct")
        if ev["type"] == "battery_charged":
            ctx.report(ReportKind.ENVELOPE_STATUS, {"status": "charged", "battery_pct": pct})
            return {"status": "completed", "outbox": ctx.outbox}
        tasks = copy.deepcopy(state.get("tasks") or {})
        released = []
        for tid, t in sorted(tasks.items()):
            if t["status"] in ("pending", "dispatched"):
                t.update(status="released", detail="battery low: recharging")
                released.append(tid)
                iid = t["spec"].get("interaction_id")
                if iid and iid in deps.local:  # another robot continues from here
                    pose = state.get("pose") or deps.campus.staging.model_dump()
                    meet = {"x": float(pose["x"]), "y": float(pose["y"])}
                    local = await guidance.release(deps, iid, meeting_pose=meet)
                    lang = (local or {}).get("content", {}).get("lang")
                    deps.speak(lang_.say("handover_leave", lang))
        ctx.report(
            ReportKind.ENVELOPE_STATUS,
            {"status": "recharging", "battery_pct": pct, "released": released},
        )
        ctx.decide(
            DecisionKind.FALLBACK,
            {"battery_pct": pct, "released": released, "step": "recharge"},
            policy={
                "low_battery_pct": deps.allocator.default_battery_floor_pct
                + deps.allocator.battery_margin_pct,
                "resume_pct": deps.allocator.recharge_resume_pct,
            },
            justification=f"battery {pct} %: release the tasks and recharge at the staging area",
        )
        return {
            "tasks": tasks,
            "status": "recharging",
            "current_task_id": None,
            "cancel_current": state.get("current_task_id") is not None,
            "holding": False,
            "outbox": ctx.outbox,
        }

    async def triage_observation(state: AgentState) -> dict[str, Any]:
        ev = state["event"]
        ctx = _Ctx(deps, state)
        pose = state.get("pose") or deps.campus.staging.model_dump()
        # A camera event at an inspected POI sees that POI: anchor the observation there
        # (the last robot_state may predate the arrival).
        poi = next((p for p in deps.campus.pois if p.poi_id == ev.get("poi_id")), None)
        if poi is not None:
            pose = poi.pose.model_dump()
        candidate = Candidate(
            robot_id=deps.robot_id,
            mission_id=state.get("mission_id"),
            pose=Pose2D(x=pose["x"], y=pose["y"], yaw=pose.get("yaw", 0.0)),
            image_uri=ev.get("image_uri"),
            poi_id=ev.get("poi_id"),
            stamp=deps.clock.now(),
        )
        obs = await deps.perception.run(candidate)
        if obs is None:
            return {}
        # A label in the closed status vocabulary, close to a known element: propose that
        # element's status (SPEC A3.3.2); the coordinator re-validates and writes it.
        status = status_from_label(obs.label)
        if status is not None:
            subject = match_subject(
                deps.campus, obs.pose.x, obs.pose.y, deps.knowledge.status_match_radius_m
            )
            if subject is not None:
                obs = obs.model_copy(update={"subject": subject})
        recent = [
            *(state.get("observations") or []),
            {
                "obs_id": obs.obs_id,
                "label": obs.label,
                "confidence": obs.confidence,
                "observation": obs.model_dump(mode="json"),
            },
        ][-10:]
        ctx.report(
            ReportKind.OBSERVATION,
            obs.model_dump(mode="json"),
            TrafficClass.EVENT_IMAGE,
            extra_bytes=image_size(obs.evidence.image_uri),
        )
        if obs.requires_confirmation:
            ctx.decide(
                DecisionKind.ESCALATE,
                {
                    "obs_id": obs.obs_id,
                    "label": obs.label,
                    "confidence": obs.confidence,
                    # For the DS09 export: images go out only without a person in view.
                    "evidence": obs.evidence.model_dump(mode="json"),
                    "person_present": obs.person_present,
                    "pose": obs.pose.model_dump(mode="json"),
                },
                policy={"triage_confidence_threshold": deps.agent_cfg.triage_confidence_threshold},
                justification=obs.reasoning or f"novel observation: {obs.label}",
                inputs_ref=f"task:{ev.get('task_id')}",
            )
        update: dict[str, Any] = {"outbox": ctx.outbox, "observations": recent, "branch": None}
        if _thinking() and state.get("envelope") and not state.get("current_task_id"):
            update.update(
                replan_trigger={"type": "observation", "obs_id": obs.obs_id, "label": obs.label},
                branch="replan",
            )
        return update

    async def negotiate(state: AgentState) -> dict[str, Any]:
        ev = state["event"]
        ctx = _Ctx(deps, state)
        if ev["type"] == "award_made":
            # This robot ran a contract-net (coordinator unreachable) and awarded a task.
            award = Award.model_validate(ev["award"])
            tasks = copy.deepcopy(state.get("tasks") or {})
            if award.task_id in tasks and ev.get("from_robot") == deps.robot_id:
                tasks[award.task_id].update(
                    status="transferred", detail=f"awarded to {award.awarded_to}"
                )
            ctx.decide(
                DecisionKind.AWARD,
                {
                    "award": award.model_dump(mode="json"),
                    "from_robot": ev.get("from_robot"),
                    "bids": ev.get("bids", []),
                },
                policy={"rule": "contract_net", "tie_break": "lowest robot_id"},
                justification=(
                    f"coordinator unreachable: {award.task_id} of {ev.get('from_robot')} "
                    f"awarded to {award.awarded_to} (lowest cost {award.cost:.1f})"
                ),
                inputs_ref=f"cfp:{award.cfp_id}",
                mission_id=award.mission_id,
            )
            made = [*(state.get("awards_made") or []), award.model_dump(mode="json")]
            return {"tasks": tasks, "awards_made": made, "outbox": ctx.outbox, "branch": "report"}
        if ev["type"] == "cfp":
            cfp = CallForProposals.model_validate(ev["cfp"])
            robot_state = RobotState.model_validate(ev["robot_state"])
            env = state.get("envelope")
            link = LinkQuality(state.get("link_quality") or robot_state.link_quality)
            result: Estimate | Infeasible
            if link is LinkQuality.LOST:
                result = Infeasible(RefusalReason.LINK, "link lost")
            elif env is None or not region_covers(env["region"], cfp.task, deps.campus):
                result = Infeasible(RefusalReason.REGION, "task outside my envelope region")
            else:
                floor = env["budgets"]["battery_floor_pct"]
                start = virtual_state(robot_state, deps.manifest, deps.allocator)
                result = estimate(
                    start, cfp.task, deps.manifest, deps.campus, deps.allocator, floor
                )
            feasible = isinstance(result, Estimate)
            note = (ev.get("nl") or {}).get("summary")
            participation = None
            policy_extra: dict[str, Any] = {}
            if _thinking() and feasible:
                # Bid values are the cost function's; the model only decides to take part,
                # within the contract-net deadline (P3 completes in < 1.5 s).
                budget = min(deps.agent_cfg.model_timeouts.negotiation, cfp.deadline_ms / 2000)
                out = await propose(
                    deps.model,
                    "negotiation",
                    Participation,
                    f"Task {cfp.task.task_id} ({cfp.task.kind.value}) from {cfp.initiator}; your "
                    f"computed cost bid {result.cost:.1f}.\n"
                    + untrusted("initiator summary", note),
                    lambda p: check_participation(p, feasible),
                    timeout_s=budget,
                    agent_id=deps.robot_id,
                    mission_id=cfp.mission_id,
                    untrusted_inputs=[("initiator summary", note)],
                )
                participation = out.value
                policy_extra = out.policy_record()
            declined = participation is not None and not participation.participate
            bid = Bid(
                cfp_id=cfp.cfp_id,
                bidder=deps.robot_id,
                cost=result.cost if feasible and not declined else None,
                refusal_reason=(
                    (participation.reason or RefusalReason.CONFLICT).value
                    if declined
                    else result.reason.value
                    if isinstance(result, Infeasible)
                    else None
                ),
                stamp=deps.clock.now(),
            )
            ctx.decide(
                DecisionKind.BID,
                bid.model_dump(mode="json"),
                policy={**_policy(deps), **policy_extra},
                justification=(
                    participation.note or "declined"
                    if declined
                    else result.detail
                    if isinstance(result, Infeasible)
                    else "cost bid"
                ),
                inputs_ref=f"cfp:{cfp.cfp_id}",
                mission_id=cfp.mission_id,
            )
            cfps = {**(state.get("cfps") or {}), cfp.cfp_id: cfp.model_dump(mode="json")}
            return {
                "reply": bid.model_dump(mode="json"),
                "cfps": cfps,
                "outbox": ctx.outbox,
                "branch": "report",
            }

        award = Award.model_validate(ev["award"])
        awards = list(state.get("awards") or [])
        if award.award_id in awards:  # idempotent acceptance
            return {"reply": {"award_id": award.award_id, "accepted": True}, "branch": "report"}
        cfp = (state.get("cfps") or {}).get(award.cfp_id)
        if cfp is None or award.awarded_to != deps.robot_id:
            return {"reply": {"award_id": award.award_id, "accepted": False}, "branch": "report"}
        task = TaskSpec.model_validate(cfp["task"])
        tasks = copy.deepcopy(state.get("tasks") or {})
        tasks[task.task_id] = {
            "spec": task.model_dump(mode="json"),
            "status": "pending",
            "outcome": None,
            "detail": f"awarded by {award.awarded_by}",
        }
        ctx.decide(
            DecisionKind.AWARD,
            {"award_id": award.award_id, "task_id": task.task_id, "accepted": True},
            policy={"rule": "contract_net_award"},
            justification=f"accepted transfer of {task.task_id} from {award.awarded_by}",
            inputs_ref=f"award:{award.award_id}",
            mission_id=award.mission_id,
        )
        return {
            "tasks": tasks,
            "awards": [*awards, award.award_id],
            "status": "executing" if state.get("status") == "completed" else state.get("status"),
            "reply": {"award_id": award.award_id, "accepted": True},
            "outbox": ctx.outbox,
            "branch": "sequence",
        }

    def after_negotiate(state: AgentState) -> str:
        return "sequence_tasks" if state.get("branch") == "sequence" else "report"

    # ------------------------------------------------------------- human interaction

    async def converse(state: AgentState) -> dict[str, Any]:
        """A person talks to the robot (SPEC A3.4): disclose, understand, confirm.

        Runs on the interaction's own thread. The utterance comes from the robot's
        in-memory copy, never through the graph; the state gets the interaction_id only.
        The model proposes the objective (as said), the declared constraint and the
        profile; code resolves the place (knowledge server) and maps the constraint to a
        profile, never dropping it (NFR-A6G-14)."""
        ev = state["event"]
        iid = str(ev.get("interaction_id"))
        ctx = _Ctx(deps, state)
        local = deps.local.get(iid)
        if local is None:
            return {"conversation": None}
        text = str(local.pop("utterance", "") or "")
        content = local["content"]
        lng = content.get("lang") or lang_.detect_lang(text, deps.icfg.languages)
        content["lang"] = lng
        lines = [lang_.say("disclosure", lng, robot=deps.robot_id)]  # AI Act Art. 50
        if lang_.aimed_at_others(text):
            ctx.decide(
                DecisionKind.ESCALATE,
                {"interaction_id": iid},
                policy={"rule": "injection_suspected"},
                justification="instructions aimed at other robots, the mission or the network "
                "ignored; an utterance creates at most the speaker's escort (text not logged)",
                inputs_ref=f"interaction:{iid}",
            )
            lines.append(lang_.say("only_you", lng))
        profiles = guidance.profiles(deps.campus)
        names = sorted(
            {
                guidance.display_name(deps.campus, k, i)
                for k, i in guidance.place_names(deps.campus).values()
            }
        )
        resolved: dict[str, Any] = {}

        async def validate(p: guidance.Request) -> str | None:
            verdict = guidance.check_request(p, text, set(profiles))
            if verdict or p.objective_kind == "none":
                return verdict
            place = await guidance.resolve(deps, str(p.objective), state.get("mission_id"))
            if place is None:
                return "the objective is not a campus place: use the words of one of the Places"
            resolved["place"] = place
            return None

        out = await propose(
            deps.model if _thinking() else None,
            "conversation",
            guidance.Request,
            "Places: "
            + ", ".join(names)
            + "\nProfiles: "
            + ", ".join(sorted(profiles))
            + "\n"
            + untrusted("utterance", text),
            validate,
            timeout_s=deps.agent_cfg.model_timeouts.conversation,
            agent_id=deps.robot_id,
            mission_id=state.get("mission_id"),
            untrusted_inputs=[("utterance", text)],
            private=True,
        )
        policy = out.policy_record()
        if "validator_rejections" in policy:  # their text may quote the person: count only
            policy["validator_rejections"] = len(policy["validator_rejections"])
        request, place = out.value, resolved.get("place")
        if request is None:  # rule 20: the deterministic reading
            request = guidance.fallback_request(text)
            place = await guidance.resolve(deps, text, state.get("mission_id"))
        if request.objective_kind == "none" or place is None:
            lines.append(lang_.say("not_understood", lng, places=", ".join(names)))
            deps.speak(" ".join(lines))
            deps.local.pop(iid, None)
            ctx.decide(
                DecisionKind.FALLBACK,
                {"interaction_id": iid, "understood": False},
                policy={"rule": "human_interaction", **policy},
                justification="no campus place understood: the robot lists what it can do",
                inputs_ref=f"interaction:{iid}",
            )
            return {"conversation": {"interaction_id": iid, "stage": "ended"}, "outbox": ctx.outbox}
        constraint = (request.declared_constraint or "").strip() or lang_.stated_constraint(text)
        profile, how = lang_.profile_for(constraint, request.profile, profiles)
        content.update(
            objective_kind="escort",
            destination={"kind": place["kind"], "id": place["id"]},
            place_said=place["said"],
            declared_constraint=constraint or None,
            profile=profile,
            meeting_pose=None,
        )
        local["stage"] = "confirming"
        local["confirm_deadline"] = time.time() + deps.icfg.confirm_timeout_s  # wall clock
        local["confirm_text"] = lang_.confirmation(lng, place["said"], profile, constraint)
        if deps.interactions is not None:
            try:
                await deps.interactions.create(iid, content, [deps.robot_id])
            except Exception:
                ctx.decide(
                    DecisionKind.FALLBACK,
                    {"interaction_id": iid},
                    policy={"rule": "interaction_store_unavailable"},
                    justification="interaction store unavailable: my copy only",
                )
        lines.append(local["confirm_text"])
        deps.speak(" ".join(lines))
        ctx.decide(
            DecisionKind.FALLBACK,
            {"interaction_id": iid, "destination": place["id"], "stage": "confirming"},
            policy={"rule": "human_interaction", "profile_from": how, **policy},
            justification="asking the person to confirm the destination and the constraint",
            inputs_ref=f"interaction:{iid}",
        )
        return {
            "conversation": {"interaction_id": iid, "stage": "confirming"},
            "outbox": ctx.outbox,
        }

    async def confirm(state: AgentState) -> dict[str, Any]:
        """Wait for the person (interrupt); the interrupt and resume values carry only
        ``{interaction_id, confirmed}``. Confirmed: a P7 human request (ids and poses)."""
        conv = state.get("conversation") or {}
        if conv.get("stage") != "confirming":
            return {}
        iid = conv["interaction_id"]
        answer = interrupt({"interaction_id": iid, "stage": "confirm"})
        ctx = _Ctx(deps, state)
        local = deps.local.get(iid)
        if local is None:  # the robot restarted while waiting: the copy is gone
            return {"conversation": {"interaction_id": iid, "stage": "ended"}}
        content = local["content"]
        lng = content.get("lang")
        if not (answer or {}).get("confirmed"):
            timed_out = bool(local.get("timed_out"))
            if not timed_out:
                deps.speak(lang_.say("declined", lng))
            if deps.interactions is not None:
                with contextlib.suppress(Exception):  # else the TTL removes it
                    await deps.interactions.delete(iid)
            deps.local.pop(iid, None)
            ctx.decide(
                DecisionKind.FORGET,
                {
                    "interaction_id": iid,
                    "outcome": "failed" if timed_out else "ended_by_person",
                    **guidance.metrics(content),
                },
                policy={"rule": "rule 28: closed before any request; entry and copy deleted"},
                justification="the person did not confirm: the interaction is forgotten",
            )
            return {"conversation": {"interaction_id": iid, "stage": "ended"}, "outbox": ctx.outbox}
        now = deps.clock.now().isoformat()
        content["t_confirmed"] = now
        await guidance.note(deps, iid, t_request=content.get("t_request"), t_confirmed=now)
        local["stage"] = "requested"
        ctx.report(
            ReportKind.HUMAN_REQUEST,
            {
                "interaction_id": iid,
                "objective_kind": "escort",
                "destination": content["destination"],
                "robot_id": deps.robot_id,
                "pose": local.get("pose"),
            },
            mission_id=content.get("mission_id"),
        )
        lost = local.get("link") == LinkQuality.LOST.value
        alone = local.get("coordinator") is False  # the request waits in the queue
        deps.speak(
            lang_.say("queued" if lost else "coordinator_down" if alone else "requested", lng)
        )
        ctx.decide(
            DecisionKind.FALLBACK,
            {"interaction_id": iid, "confirmed": True, "destination": content["destination"]["id"]},
            policy={"rule": "human_interaction", "queued": lost or alone},
            justification="the person confirmed: guidance requested from the coordinator (P7)",
            inputs_ref=f"interaction:{iid}",
        )
        return {"conversation": {"interaction_id": iid, "stage": "requested"}, "outbox": ctx.outbox}

    async def end_interaction(state: AgentState) -> dict[str, Any]:
        """The interaction ended (arrival, the person, a shelter, a failure): outcome and
        guidance metrics go to the coordinator (which deletes the entry); the robot
        forgets its copy (rule 28)."""
        ev = state["event"]
        iid = str(ev.get("interaction_id"))
        ctx = _Ctx(deps, state)
        local = deps.local.pop(iid, None) or {}
        content = local.get("content") or {}
        ctx.report(
            ReportKind.INTERACTION_END,
            {
                "interaction_id": iid,
                "robot_id": deps.robot_id,
                "outcome": ev.get("outcome") or "failed",
                **guidance.metrics(content),
            },
            mission_id=content.get("mission_id"),
        )
        return {"conversation": {"interaction_id": iid, "stage": "ended"}, "outbox": ctx.outbox}

    # ------------------------------------------------------------- reporting

    def _template_summary(item: dict[str, Any]) -> str:
        p = item.get("payload") or {}
        kind = item.get("kind")
        if kind == "envelope_status":
            return f"{deps.robot_id}: envelope {p.get('status')}"
        if kind == "observation":
            return f"{deps.robot_id} saw {p.get('label')} ({float(p.get('confidence') or 0):.0%})"
        if kind == "skill_result":
            return f"{deps.robot_id}: {p.get('task_id')} {(p.get('result') or {}).get('outcome')}"
        if kind == "task_status":
            return f"{deps.robot_id}: {p.get('task_id')} {p.get('status')}"
        return f"{deps.robot_id}: {kind}"

    async def _summaries(outbox: list[dict[str, Any]], state: AgentState) -> None:
        """nl.summary on each report (A2.3.1 reporting): model-written for the configured
        kinds, a template otherwise. The structured payload stays authoritative."""
        cap = deps.agent_cfg.nl_max_tokens
        for item in outbox:
            if item.get("nl") is not None:
                continue
            summary = _template_summary(item)
            if _thinking() and item.get("kind") in deps.agent_cfg.nl_summary_kinds:
                out = await propose(
                    deps.model,
                    "reporting",
                    Summary,
                    f"Word limit: {cap}. Report (structured, authoritative):\n"
                    + json.dumps({k: item[k] for k in ("kind", "payload") if k in item})[:4000],
                    lambda p: check_summary(p, cap),
                    timeout_s=deps.agent_cfg.model_timeouts.reporting,
                    agent_id=deps.robot_id,
                    mission_id=item.get("mission_id"),
                )
                if out.value is not None:
                    summary = out.value.summary.strip()
            item["nl"] = {"summary": " ".join(summary.split()[:cap])}

    async def report(state: AgentState) -> dict[str, Any]:
        outbox = list(state.get("outbox") or [])
        if not outbox or LinkQuality(state.get("link_quality") or "ok") is LinkQuality.LOST:
            return _bound_queue(outbox, state)
        remaining: list[dict[str, Any]] = []
        sent = dropped = 0
        blocked = False
        for item in sorted(outbox, key=_class_rank):
            if blocked:
                remaining.append(item)
                continue
            admission = deps.budget.admit(TrafficClass(item["traffic_class"]), item["nbytes"])
            if admission is Admission.DROP:
                dropped += 1
                continue
            if admission is Admission.QUEUE:
                remaining.append(item)
                continue
            if item.get("nl") is None:
                await _summaries([item], state)
            report_ = Report.model_validate(
                {k: v for k, v in item.items() if k not in ("nbytes", "nl")}
            )
            if await _send(report_, item.get("nl")):
                sent += 1
            else:
                remaining.append(item)
                blocked = True  # uplink failing: keep the rest queued, in order
        order = {id(item): i for i, item in enumerate(outbox)}
        remaining.sort(key=lambda it: order[id(it)])
        update = _bound_queue(remaining, state)
        update["sent_count"] = (state.get("sent_count") or 0) + sent
        update["dropped_count"] = (
            update.get("dropped_count", state.get("dropped_count") or 0) + dropped
        )
        return update

    async def _send(report_: Report, nl: dict[str, Any] | None) -> bool:
        try:
            return await deps.uplink.send(report_, nl=nl)  # type: ignore[call-arg]
        except TypeError:  # an uplink without NL support (tests, older stand-ins)
            return await deps.uplink.send(report_)

    def _bound_queue(outbox: list[dict[str, Any]], state: AgentState) -> dict[str, Any]:
        limit = deps.agent_cfg.max_queued_reports
        dropped = state.get("dropped_count") or 0
        while len(outbox) > limit:
            # Drop the oldest message of the lowest-priority class present.
            worst = max(range(len(outbox)), key=lambda i: (_class_rank(outbox[i]), -i))
            log.warning("%s report queue full: dropping %s", deps.robot_id, outbox[worst]["kind"])
            outbox.pop(worst)
            dropped += 1
        return {"outbox": outbox, "dropped_count": dropped}

    g = StateGraph(AgentState)
    g.add_node("route_event", route_event)
    g.add_node("evaluate_envelope", evaluate_envelope)
    g.add_node("sequence_tasks", sequence_tasks)
    g.add_node("execute_next", execute_next)
    g.add_node("handle_link_loss", handle_link_loss)
    g.add_node("handle_battery", handle_battery)
    g.add_node("triage_observation", triage_observation)
    g.add_node("negotiate", negotiate)
    g.add_node("deliberate", deliberate)
    g.add_node("replan", replan)
    g.add_node("validate_proposal", validate_proposal)
    g.add_node("converse", converse)
    g.add_node("flush", report)  # the decisions go up before waiting for the person
    g.add_node("confirm", confirm)
    g.add_node("end_interaction", end_interaction)
    g.add_node("report", report)
    g.add_edge(START, "route_event")
    g.add_conditional_edges(
        "route_event",
        pick_branch,
        [
            "evaluate_envelope",
            "execute_next",
            "handle_link_loss",
            "handle_battery",
            "triage_observation",
            "negotiate",
            "converse",
            "end_interaction",
            "report",
        ],
    )
    g.add_edge("handle_battery", "report")
    g.add_conditional_edges(
        "evaluate_envelope", after_evaluate, ["sequence_tasks", "deliberate", "report"]
    )
    g.add_edge("deliberate", "validate_proposal")
    g.add_edge("replan", "validate_proposal")
    g.add_conditional_edges(
        "validate_proposal",
        after_validate,
        ["deliberate", "replan", "sequence_tasks", "execute_next", "report"],
    )
    g.add_conditional_edges("negotiate", after_negotiate, ["sequence_tasks", "report"])
    g.add_edge("sequence_tasks", "execute_next")
    g.add_edge("handle_link_loss", "execute_next")
    g.add_conditional_edges("execute_next", after_execute, ["replan", "report"])
    g.add_conditional_edges("triage_observation", after_execute, ["replan", "report"])
    g.add_edge("converse", "flush")
    g.add_edge("flush", "confirm")
    g.add_edge("confirm", "report")
    g.add_edge("end_interaction", "report")
    g.add_edge("report", END)
    return g
