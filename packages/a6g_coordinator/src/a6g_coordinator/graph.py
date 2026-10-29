"""Swarm Coordination Agent graph.

One invocation per event, ``thread_id = mission_id``. Events arrive either as chat
messages (``messages``, from agent-chat-ui or ``a6g goal``) or as structured ``event``s
(robot reports, failures, fleet changes, posture changes). All mission state lives in the
checkpointer; the process holds none.

    START -> ingest -+-> decompose -> validate -> arbitrate -> allocate -> publish_envelopes
                     |                                        ^                    |
                     +-> allocate (failure / fleet change) ---+     handle_reply <-+
                     +-> publish_envelopes (reconcile)              |      |
                     +-> audit                           allocate <-+      v
                                                                         audit -> escalate?
    escalate: interrupt() in agent-chat-ui's HITL format; re-raised at the end of every run
    until the operator answers, so robot reports keep flowing meanwhile.

Allocation and negotiation are deterministic code (Rule 3); the LLM only decomposes goals,
classifies chat and phrases messages.
"""

import asyncio
import contextlib
import copy
import itertools
import json
import logging
import math
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Any

from a6g_allocator.assign import Assignment, AssignResult, assign, visit_key
from a6g_allocator.cost import (
    Estimate,
    VirtualState,
    estimate,
    estimate_next,
    route_distance,
    task_path,
    virtual_state,
)
from a6g_common.clock import Clock
from a6g_common.config import A6GConfig, RobotManifest, load_manifest
from a6g_common.ids import IdFactory
from a6g_common.messages import untrusted
from a6g_common.models import LLMTrace, one_sentence, structured_call
from a6g_common.schemas import (
    Award,
    Decision,
    DecisionKind,
    Envelope,
    EnvelopeBudgets,
    MissionGoal,
    Observation,
    PoiTarget,
    Pose2D,
    PoseTarget,
    Posture,
    RefusalReason,
    Report,
    ReportKind,
    RobotState,
    SectorTarget,
    StatusReport,
    TaskKind,
    TaskSpec,
)
from a6g_common.statusvocab import match_subject, status_from_label
from a6g_observability.decision_log import DecisionSink, make_decision
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_store
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from pydantic import BaseModel, Field
from typing_extensions import TypedDict  # pydantic needs it on Python < 3.12

from a6g_coordinator import agentic
from a6g_coordinator import flows as flow_rules
from a6g_coordinator.decompose import (
    CAPABILITIES_TEXT,
    POSTURE_PRIORITY,
    TAKEOVER,
    _target_key,
    decompose_goal,
    home_task,
    interpret_chat,
    is_correction,
    is_patrol,
    is_why,
    keyword_posture,
    make_task,
    mentions,
    needs_care,
    order_after_all_clear,
    order_robots,
    place_target,
    plan_gaps,
)
from a6g_coordinator.links import RobotLink
from a6g_coordinator.narrate import Narrator, join, plural, polish
from a6g_coordinator.network import NetworkBranchClient

log = logging.getLogger(__name__)

OPEN = ("pending", "assigned", "in_progress")
ACTIVE_ROBOT = "available"
SEEN_REPORTS_MAX = 2000


class CoordState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    event: dict[str, Any] | None
    chat_seen: int
    mission_id: str
    mission: dict[str, Any] | None
    posture: str
    status: str  # idle | active | completed
    tasks: dict[str, dict[str, Any]]
    fleet: dict[str, dict[str, Any]]
    envelopes: dict[str, dict[str, Any]]
    escalations: list[dict[str, Any]]
    escalation_seq: int
    seen_reports: list[str]
    pending_decisions: list[dict[str, Any]]
    notes: list[str]
    dirty: list[str]
    new_tasks: list[str]
    replies: list[dict[str, Any]]
    rounds: int
    reallocating: bool
    branch: str
    pending_call: dict[str, str] | None  # tool call announced to the chat, result pending
    posture_changed: bool  # this event raised the posture (arbitrate announces the switch)
    flows: dict[str, dict[str, Any]]  # flow_id -> FlowRecord
    kpi11_t0: float | None  # wall time a change to emergency was written (reaction time)
    #: The careful path: the order whose assignment the model checks before publication
    order_check: str | None
    recheck: bool  # the check changed the work: allocate again
    #: Step 2: anomaly key ("robot:<id>", "task:<id>") -> when it was last analysed (ISO)
    anomalies: dict[str, str]
    refusal_streak: dict[str, int]  # robot -> envelope replies in a row refusing all its work


@dataclass
class CoordinatorDeps:
    cfg: A6GConfig
    links: Any  # Callable[[robot_id, fleet entry], RobotLink]
    decisions: DecisionSink
    clock: Clock
    ids: IdFactory
    model: BaseChatModel | None = None
    network: NetworkBranchClient = field(default_factory=NetworkBranchClient)
    #: Campus knowledge server client: places, statuses, routes. None: none.
    kb: Any = None
    #: InteractionStore client: hand-overs, shelters, deletion at the end.
    interactions: Any = None
    actor: str = "coordinator"
    state_timeout_s: float = 3.0
    envelope_timeout_s: float | None = None  # None: coordinator.envelope_timeout_s
    # Other mission threads (ending superseded missions): queue an event without waiting
    # for it, and read a thread's state. None: the Agent Server's SDK client (the
    # in-process Coordinator sets its own).
    submit: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None
    peek: Callable[[str], Awaitable[dict[str, Any]]] | None = None
    #: Robots admitted from the staging area (``robot_added``, robot_id -> event): a
    #: mission started later counts them too, not only the configured fleet.
    admitted: dict[str, dict[str, Any]] = field(default_factory=dict)


#: Robots admitted while the Agent Server runs, shared by its gateway and its graph. The
#: server loads the graph file as a module of its own (not ``a6g_coordinator.server``), so a
#: registry kept there by the gateway would be another object: robots added at the staging
#: area before a mission would never join it.
ADMITTED: dict[str, dict[str, Any]] = {}


# --------------------------------------------------------------------------- helpers


def initial_fleet(cfg: A6GConfig) -> dict[str, dict[str, Any]]:
    return {
        r.robot_id: {
            "robot_id": r.robot_id,
            "platform": cfg.manifests[r.robot_id].platform.value,
            "status": ACTIVE_ROBOT,
            "a2a_url": r.a2a_url,
            "manifest": cfg.manifests[r.robot_id].model_dump(mode="json"),
        }
        for r in cfg.fleet.robots
    }


def campus_region(cfg: A6GConfig, margin: float = 5.0) -> list[tuple[float, float]]:
    """Envelope region: the pilot area; else a box around everything tasks can target."""
    campus = cfg.campus
    if campus.pilot_area:
        return [tuple(p) for p in campus.pilot_area]
    pts = [(campus.staging.x, campus.staging.y)]
    pts += [p for s in campus.sectors for p in s.polygon]
    pts += [(p.pose.x, p.pose.y) for p in campus.pois]
    pts += [(r.pose.x, r.pose.y) for r in campus.link_recovery_points]
    xs, ys = [x for x, _ in pts], [y for _, y in pts]
    x0, x1, y0, y1 = min(xs) - margin, max(xs) + margin, min(ys) - margin, max(ys) + margin
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _post_at_escort_target(tasks: dict[str, Any], escort: TaskSpec) -> str | None:
    """An unassigned hold_post task at the escort's destination POI, if any."""
    target = getattr(escort.target, "poi_id", None)
    for tid in sorted(tasks):
        t = tasks[tid]
        spec = _spec(t)
        if (
            spec.kind.value == "hold_post"
            and getattr(spec.target, "poi_id", None) == target
            and t["status"] == "pending"
            and not t.get("robot_id")
        ):
            return tid
    return None


def _manifest(entry: dict[str, Any]) -> RobotManifest:
    return RobotManifest.model_validate(entry["manifest"])


def _spec(entry: dict[str, Any]) -> TaskSpec:
    return TaskSpec.model_validate(entry["spec"])


def _posture_rank(cfg: A6GConfig, posture: str) -> int:
    order = [p.value for p in cfg.allocator.posture_priority]  # highest first
    return len(order) - order.index(posture)


def _target_label(spec: TaskSpec) -> str:
    t = spec.target
    if isinstance(t, SectorTarget):
        return t.sector_id
    if isinstance(t, PoiTarget):
        return t.poi_id
    return f"({t.pose.x:.0f}, {t.pose.y:.0f})"


def fmt_latency(seconds: float) -> str:
    return f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.2f} s"


class _Run:
    """Per-node helper: decisions and operator notes accumulate in state; audit writes them."""

    def __init__(self, deps: CoordinatorDeps, state: CoordState) -> None:
        self.deps = deps
        self.state = state
        self.decisions = list(state.get("pending_decisions") or [])
        self.notes = list(state.get("notes") or [])
        self.messages: list[AnyMessage] = []

    @property
    def mission_id(self) -> str:
        return self.state["mission_id"]

    def decide(
        self,
        kind: DecisionKind,
        output: dict[str, Any],
        *,
        policy: dict[str, Any] | None = None,
        justification: str = "",
        inputs_ref: str = "",
    ) -> None:
        decision = make_decision(
            self.deps.actor,
            kind,
            mission_id=self.mission_id,
            inputs_ref=inputs_ref,
            output=output,
            policy=policy,
            justification=justification,
            clock=self.deps.clock,
            ids=self.deps.ids,
        )
        self.decisions.append(decision.model_dump(mode="json"))

    def note(self, text: str) -> None:
        self.notes.append(text)

    # Each coordinator step is shown in the chat as a tool call + result, so the operator
    # watches the process (agent-chat-ui renders them; "Hide Tool Calls" collapses them).
    def announce(self, name: str, args: dict[str, Any]) -> dict[str, str]:
        """Show a call now; its result is added by a later node (slow steps)."""
        call_id = self.deps.ids("call")
        self.messages.append(
            AIMessage(
                "", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}]
            )
        )
        return {"id": call_id, "name": name}

    def result(self, call: dict[str, str], result: Any, latency_s: float | None = None) -> None:
        if latency_s is not None and isinstance(result, dict):
            result = {**result, "latency": fmt_latency(latency_s)}
        self.messages.append(
            ToolMessage(json.dumps(result, default=str), tool_call_id=call["id"], name=call["name"])
        )

    def call(
        self, name: str, args: dict[str, Any], result: Any, latency_s: float | None = None
    ) -> None:
        self.result(self.announce(name, args), result, latency_s)

    def finish(
        self, name: str, args: dict[str, Any], result: Any, latency_s: float | None = None
    ) -> None:
        """Complete the call announced by the previous node, or show a complete call."""
        pending = self.state.get("pending_call")
        if pending and pending.get("name") == name:
            self.result(pending, result, latency_s)
        else:
            self.call(name, args, result, latency_s)

    def thoughts(self, step: str, trace: LLMTrace) -> None:
        """The model's reasoning summary, kept in the chat (it streamed live while thinking).
        OpenAI leaves the summary out of about one call in four (gpt-5-nano): the step and
        its time are still shown, so every model call is visible."""
        if trace.used_fallback or trace.latency_s <= 0:
            return  # no model answer: nothing was thought
        thoughts = trace.thoughts or "_(The model gave no summary of its reasoning this time.)_"
        self.messages.append(
            AIMessage(
                content=[{"type": "reasoning", "reasoning": thoughts}],
                id=trace.message_id,
                response_metadata={"a6g_latency_s": round(trace.latency_s, 2), "a6g_step": step},
            )
        )

    def out(self, **extra: Any) -> dict[str, Any]:
        out = {"pending_decisions": self.decisions, "notes": self.notes, **extra}
        if self.messages:
            out["messages"] = self.messages
        return out


def _sdk_submit(assistant_id: str) -> Callable[[str, dict[str, Any]], Awaitable[Any]]:
    """Agent Server: queue a run on another thread (never joined: ``langgraph dev`` has a
    single worker, busy with the calling run)."""

    async def submit(mission_id: str, event: dict[str, Any]) -> Any:
        from langgraph_sdk import get_client

        return await get_client().runs.create(
            mission_id, assistant_id, input={"event": event}, multitask_strategy="enqueue"
        )

    return submit


def _sdk_peek() -> Callable[[str], Awaitable[dict[str, Any]]]:
    async def peek(mission_id: str) -> dict[str, Any]:
        from langgraph_sdk import get_client

        return (await get_client().threads.get_state(mission_id)).get("values") or {}

    return peek


class _Explanation(BaseModel):
    answer: str = Field(description="two or three plain sentences for the operator")


def _live_escalations(
    escalations: list[dict[str, Any]], tasks: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Drop 'unallocatable' escalations whose task is no longer waiting for a robot (and let
    it be escalated again should it come back)."""
    live = []
    for esc in escalations:
        task = tasks.get(esc.get("task_id") or "")
        if esc.get("kind") == "unallocatable" and (task is None or task["status"] != "pending"):
            if task is not None:
                task["escalated"] = False
            continue
        live.append(esc)
    return live


def _next_lap(tasks: dict[str, dict[str, Any]], task_id: str) -> str:
    """Queue the next lap of a patrol as a new task (robots never re-run a task id they
    finished): same route, posture and pin; a fresh attempt count."""
    done = tasks[task_id]
    base = done.get("lap_of") or task_id
    lap = int(done.get("lap", 1)) + 1
    new_id = f"{base}-lap{lap}"
    spec = {**done["spec"], "task_id": new_id, "depends_on": []}
    if spec["target"].get("type") == "sector":  # the whole sector, from the stalest end
        spec["target"] = {
            **spec["target"],
            "from_waypoint": 0,
            "reverse": None,
            # where this lap started (the robot reported it): patrolled longest ago now
            "prefer_reverse": done.get("reverse"),
        }
    tasks[new_id] = {
        "spec": spec,
        "status": "pending",
        "robot_id": None,
        "refused_by": [],
        "posture": done.get("posture", "routine"),
        # A bundled patrol stays with the robot that patrols (the others were told to stay
        # where they are); a named robot keeps its pin.
        "ordered_for": done.get("ordered_for"),  # the robot an order named
        "pinned_to": done.get("pinned_to")
        or done.get("lap_pin")  # after a remainder: the robot the operator named
        or (done.get("robot_id") if done.get("bundle") else None),
        "bundle": None,
        "excluded": list(done.get("excluded") or []),
        "attempts": 0,
        "repeat": True,
        "lap_of": base,
        "lap": lap,
    }
    return new_id


async def _other_active_missions(mission_id: str) -> list[str]:
    store = _store()
    if store is None:
        return []
    try:
        items = await store.asearch(("a6g", "context", "missions"), limit=100)
    except Exception as exc:  # the warning is best effort
        log.debug("mission listing unavailable: %s", exc)
        return []
    return sorted(
        i.value["mission_id"]
        for i in items
        if i.value.get("status") == "active" and i.value.get("mission_id") != mission_id
    )


async def _robot_states(
    deps: CoordinatorDeps, fleet: dict[str, dict[str, Any]]
) -> tuple[dict[str, RobotState], list[str]]:
    """Fetch states of available robots in parallel; returns (states, unreachable ids)."""

    async def one(rid: str) -> tuple[str, RobotState | None]:
        try:
            link: RobotLink = deps.links(rid, fleet[rid])
            async with asyncio.timeout(deps.state_timeout_s):
                return rid, await link.get_state()
        except Exception as exc:
            log.warning("state of %s unavailable: %s", rid, exc)
            return rid, None

    ids = sorted(r for r, e in fleet.items() if e["status"] == ACTIVE_ROBOT)
    results = await asyncio.gather(*(one(r) for r in ids))
    states = {rid: s for rid, s in results if s is not None}
    return states, [rid for rid, s in results if s is None]


def idle_moves(
    cfg: A6GConfig, tasks: dict[str, dict[str, Any]], fleet: dict[str, dict[str, Any]]
) -> list[tuple[str, str, str]]:
    """Idle robots take queued work: an active robot holding nothing (it asked
    for work, or was never given any) gets the nearest task another robot holds but has not
    started, when that robot has more than one open task. Work given by name (pinned), an
    escort or anything non-preemptible stays; so does a task the idle robot refused or
    cannot do. Bundled work moves (and leaves its bundle).
    Mutates ``tasks`` and ``fleet``; returns the moves (task, from, to)."""
    keep = {k.value for k in cfg.allocator.non_preemptible_kinds}
    moves: list[tuple[str, str, str]] = []

    def load(rid: str) -> int:
        return sum(t.get("robot_id") == rid and t["status"] in OPEN for t in tasks.values())

    for rid in sorted(fleet):
        entry = fleet[rid]
        if entry.get("status") != ACTIVE_ROBOT:
            continue
        if load(rid):
            entry["idle"] = False
            continue
        caps = {c.value for c in _manifest(entry).capabilities}
        here = entry.get("idle_pose")
        movable = []
        for tid, t in tasks.items():
            holder = t.get("robot_id")
            spec = _spec(t)
            if (
                t["status"] != "assigned"
                or not holder
                or holder == rid
                # Out of contact, it may have started it (its "started" report waits for
                # coverage), and two robots would run one survey. It keeps its work.
                or fleet.get(holder, {}).get("status") != ACTIVE_ROBOT
                or load(holder) < 2
                or t.get("pinned_to")
                or t.get("plan_group")  # model_first: "one robot does A and B"
                or spec.kind.value in keep
                or spec.interaction_id
                or rid in (t.get("refused_by") or [])
                or rid in (t.get("excluded") or [])
                or not set(spec.required_capabilities) <= caps
            ):
                continue
            battery = entry.get("idle_battery_pct")
            if here and battery is not None:  # only what it can finish with its reserve
                at = VirtualState(Pose2D(x=here["x"], y=here["y"]), float(battery), 0.0)
                est = estimate(
                    at,
                    spec,
                    _manifest(entry),
                    cfg.campus,
                    cfg.allocator,
                    cfg.allocator.default_battery_floor_pct,
                )
                if not isinstance(est, Estimate):
                    continue
            start = task_path(spec, cfg.campus)[0]
            far = math.dist((start.x, start.y), (here["x"], here["y"])) if here else 0.0
            movable.append((far, tid, holder))
        if not movable:
            continue
        _, tid, holder = min(movable)
        # A bundle ("the others patrol") kept its share on one robot; an idle robot
        # taking queued work out of it is what the operator asked for.
        tasks[tid].update(robot_id=rid, status="assigned", bundle=None)
        entry["idle"] = False
        moves.append((tid, holder, rid))
    return moves


def battery_refusal(cfg: A6GConfig, detail: str, unknown_battery: bool = False) -> str:
    """A robot's battery refusal for the operator, with its figures (a bare "not enough
    battery" reads as false next to a 32 % battery in the live view)."""
    reserve = cfg.allocator.default_battery_floor_pct + cfg.allocator.battery_margin_pct
    if detail.startswith("recharging"):
        return "it is recharging"
    if unknown_battery:
        return (
            "not enough battery for that route (planned conservatively at "
            f"{cfg.allocator.unknown_battery_pct:.0f} %)"
        )
    low = re.search(r"end at ([\d.]+)%", detail)
    if low:
        return (
            f"that route would leave it at {float(low.group(1)):.0f} % and it keeps "
            f"{reserve:.0f} % in reserve"
        )
    return "not enough battery for that route"


def _release(
    tasks: dict[str, dict[str, Any]],
    robot_id: str,
    statuses=("assigned", "in_progress"),
    keep_pinned: bool = False,
) -> list[str]:
    """Back to the allocator. ``keep_pinned``: work that belongs to this robot waits for it
    (a robot out of contact comes back): tasks the operator gave it by name, and the tasks
    of a bundle it carries ("the other robot patrols the campus": all three sectors stay
    with that robot, they are not shared out while it is in a 5G shadow)."""
    released = []
    for tid, t in tasks.items():
        if t.get("robot_id") == robot_id and t["status"] in statuses:
            if keep_pinned and (t.get("pinned_to") == robot_id or t.get("bundle")):
                continue
            t.update(status="pending", robot_id=None)
            released.append(tid)
    return released


def _apply_award(
    tasks: dict[str, dict[str, Any]], award: dict[str, Any], dirty: list[str]
) -> tuple[str, str | None, str] | None:
    a = Award.model_validate(award)
    entry = tasks.get(a.task_id)
    if entry is None or entry["status"] not in OPEN:
        return None
    old = entry.get("robot_id")
    entry.update(
        robot_id=a.awarded_to,
        status="in_progress"
        if entry["status"] == "in_progress" and old == a.awarded_to
        else "assigned",
    )
    for rid in (old, a.awarded_to):
        if rid and rid not in dirty:
            dirty.append(rid)
    return a.task_id, old, a.awarded_to


# --------------------------------------------------------------------------- graph


def build_graph(deps: CoordinatorDeps) -> StateGraph:
    cfg = deps.cfg
    narr = Narrator(cfg.campus)

    # ------------------------------------------------------------------ ingest

    async def ingest(state: CoordState, config: RunnableConfig) -> dict[str, Any]:
        mission_id = config["configurable"]["thread_id"]
        state = {**state, "mission_id": mission_id}
        run = _Run(deps, state)
        fleet = copy.deepcopy(state.get("fleet") or initial_fleet(cfg))
        if not state.get("fleet"):  # a new mission: also the robots admitted before it
            for rid, added in sorted(deps.admitted.items()):
                manifest = added.get("manifest") or await asyncio.to_thread(
                    _load_manifest, cfg, rid
                )
                if rid not in fleet and manifest is not None:
                    m = RobotManifest.model_validate(manifest)
                    fleet[rid] = {
                        "robot_id": rid,
                        "platform": m.platform.value,
                        "status": ACTIVE_ROBOT,
                        "a2a_url": added.get("a2a_url"),
                        "manifest": m.model_dump(mode="json"),
                    }
        tasks = copy.deepcopy(state.get("tasks") or {})
        dirty: list[str] = []
        upd: dict[str, Any] = {
            "mission_id": mission_id,
            "dirty": dirty,
            "new_tasks": [],
            "replies": [],
            "rounds": 0,
            "reallocating": False,
            "posture_changed": False,
            "branch": "audit",
        }
        ev = state.get("event")
        messages = state.get("messages") or []
        if ev is None and len(messages) > (state.get("chat_seen") or 0):
            last = messages[-1]
            upd["chat_seen"] = len(messages)
            if isinstance(last, HumanMessage):
                ev = {
                    "type": "chat",
                    "text": last.text if hasattr(last, "text") else str(last.content),
                }
        if ev is None:
            return run.out(**upd, fleet=fleet, tasks=tasks)

        kind = ev["type"]
        if kind == "chat":
            speculative: tuple[Any, LLMTrace, list[str]] | None = None
            if cfg.fleet.defaults.coordinator.understanding == "model_first" and deps.model:
                # The plan is made while the request is understood, not after (latency); it
                # is discarded if the request is not an order.
                ctx = _context({**state, "tasks": tasks}, keyword_posture(ev["text"]), fleet)
                (intent, trace), speculative = await asyncio.gather(
                    interpret_chat(ev["text"], bool(state.get("mission")), deps.model),
                    agentic.plan(deps.model, ctx, ev["text"], visible=False),
                )
            else:
                intent, trace = await interpret_chat(
                    ev["text"], bool(state.get("mission")), deps.model
                )
            run.thoughts("understand_request", trace)
            run.call(
                "understand_request",
                {"message": ev["text"]},
                {
                    "intent": intent.intent,
                    "posture": intent.posture.value if intent.posture else None,
                    "method": "keyword rules" if trace.used_fallback else "LLM",
                },
                trace.latency_s,
            )
            if intent.intent == "status":
                text = narr.status({**state, "fleet": fleet, "tasks": tasks})
                store = _store()
                if not tasks and store is not None:
                    # This conversation has none, but another one may be driving the robots.
                    items = await store.asearch(("a6g", "context", "missions"), limit=100)
                    others = [
                        i.value
                        for i in items
                        if i.value.get("status") == "active"
                        and i.value.get("mission_id") != mission_id
                    ]
                    if others:
                        robots = sorted({r for o in others for r in o.get("robots") or []})
                        text = (
                            "No mission is running in this conversation, but "
                            + plural(len(others), "mission")
                            + " from another conversation "
                            + ("is" if len(others) == 1 else "are")
                            + " still active"
                            + (f" ({join(narr.robot(r, fleet) for r in robots)})" if robots else "")
                            + ". Give me a goal here and I'll take the robots over."
                        )
                run.note(text)
                return run.out(**upd, fleet=fleet, tasks=tasks, event=None)
            if intent.intent == "explain":
                asked = await _explain(run, {**state, "fleet": fleet, "tasks": tasks}, ev["text"])
                return run.out(**upd, **asked, fleet=fleet, tasks=tasks, event=None)
            splan = speculative[0] if speculative is not None else None
            if (
                intent.intent in ("unsupported", "other")
                and splan
                and (splan.tasks or splan.end or splan.reassign)
            ):
                # model_first: the planner found a valid plan for it; the plan decides
                intent = intent.model_copy(update={"intent": "goal"})
            if intent.intent == "stop" and speculative is not None and speculative[0]:
                # model_first: "stop the south plaza patrol" ends that patrol, not the
                # mission; the plan says what the order stops.
                intent = intent.model_copy(update={"intent": "goal"})
            if intent.intent == "stop":
                _stop(run, state, tasks, dirty, upd)
                return run.out(**upd, fleet=fleet, tasks=tasks)
            if intent.intent == "all_clear":
                order = order_after_all_clear(ev["text"])
                cleared = _all_clear(
                    run, state, tasks, dirty, upd, ev["text"], quiet=bool(order), resume=not order
                )
                if not order:  # a plain all clear
                    if not cleared:
                        return run.out(**upd, fleet=fleet, tasks=tasks, event=None)
                    return run.out(**upd, fleet=fleet, tasks=tasks)
                # "All clear, return to base": the emergency is over, then the order.
                ev = {
                    "type": "goal",
                    "text": order,
                    "posture": keyword_posture(order).value,
                    "issued_by": "chat",
                    "approved": True,
                }
            if intent.intent == "unsupported":
                why = intent.summary.strip().rstrip(".")
                run.note(
                    (
                        f"I can't do that: {why}. "
                        if why
                        else "That isn't something the robots can do. "
                    )
                    + f"The robots can {CAPABILITIES_TEXT}."
                )
                return run.out(**upd, fleet=fleet, tasks=tasks, event=None)
            if intent.intent == "other":
                run.note(
                    "I coordinate the campus robots. You can give me a goal (for example "
                    "*Patrol the campus* or *Escort the visitor to the bus stop*), tell me "
                    "about a situation (*there is a weather alert*), or ask how things are "
                    "going (*status*)."
                )
                return run.out(**upd, fleet=fleet, tasks=tasks, event=None)
            posture = intent.posture or Posture.ROUTINE
            ev = {
                "type": "posture" if intent.intent == "posture" else "goal",
                "text": ev["text"],
                "posture": posture.value,
                "issued_by": "chat",
                "status_after": intent.also_status,
            }
            rules_first = cfg.fleet.defaults.coordinator.understanding == "rules_first"
            reasons = needs_care(ev["text"]) if rules_first and deps.model else []
            if reasons and posture is not Posture.EMERGENCY:
                ev["careful"] = reasons
                earlier = [
                    m.text
                    for m in (state.get("messages") or [])[:-1]
                    if isinstance(m, HumanMessage) and m.text.strip()
                ]
                if earlier and is_correction(ev["text"]):
                    # "that's not what I asked": the order it is about (only then, or the
                    # check would judge a new order by the previous one)
                    ev["previous_order"] = earlier[-1]
                run.messages.append(
                    AIMessage(
                        "🤔 This order needs more thought ("
                        + "; ".join(reasons)
                        + "). I'm planning it carefully and checking the result before the "
                        "robots move."
                    )
                )
            if speculative is not None:
                splan, strace, serrors = speculative
                ev["agent_plan"] = {
                    "plan": agentic.dumps(splan) if splan else None,
                    "errors": serrors[:10],
                    "latency_s": round(strace.latency_s, 3),
                    "posture": keyword_posture(ev["text"]).value,
                }
            kind = ev["type"]

        if kind in ("goal", "posture"):
            posture = Posture(ev.get("posture", "emergency" if kind == "posture" else "routine"))
            text = ev.get("text") or ev.get("reason") or posture.value
            in_emergency = (upd.get("posture") or state.get("posture")) == Posture.EMERGENCY.value
            if (
                kind == "goal"
                and in_emergency
                and posture is not Posture.EMERGENCY
                and not ev.get("approved")
            ):
                # A new order during an emergency ends it only with the operator's approval
                # (an explicit "all clear" needs none).
                seq = (state.get("escalation_seq") or 0) + 1
                upd["escalation_seq"] = seq
                upd["escalations"] = [
                    *(state.get("escalations") or []),
                    {
                        "esc_id": f"esc-{seq}",
                        "kind": "end_emergency",
                        "text": text,
                        "posture": posture.value,
                    },
                ]
                run.note(
                    f"🚨 We're in **emergency** mode. *{text}* would end it: please approve "
                    "or reject below."
                )
                return run.out(**upd, fleet=fleet, tasks=tasks, event=None)
            mission = state.get("mission")
            if state.get("status") != "active":  # new or revived: who else commands the robots?
                await _take_over_fleet(run, mission_id, fleet)
            if mission is None:
                mission = MissionGoal(
                    mission_id=mission_id,
                    text=text,
                    posture=posture,
                    priority=POSTURE_PRIORITY[posture],
                    issued_by=ev.get("issued_by", "operator"),
                ).model_dump(mode="json")
            current = upd.get("posture") or state.get("posture") or posture.value
            new_posture = (
                posture.value
                if _posture_rank(cfg, posture.value) >= _posture_rank(cfg, current)
                else current
            )
            upd["pending_call"] = run.announce(
                "plan_mission", {"goal": text, "posture": posture.value}
            )
            upd.update(
                mission=mission,
                posture=new_posture,
                posture_changed=new_posture != state.get("posture"),
                # Emergency reaction time starts when the change is written to the twin.
                kpi11_t0=(
                    time.time()
                    if new_posture == Posture.EMERGENCY.value
                    and new_posture != state.get("posture")
                    else state.get("kpi11_t0")
                ),
                status="active",
                branch="decompose",
                event={**ev, "posture": posture.value, "text": text},
            )
            if ev.get("exposed_pois"):
                upd["event"]["exposed_pois"] = ev["exposed_pois"]
            return run.out(**upd, fleet=fleet, tasks=tasks)

        if kind == "anomaly":  # a robot behaved unexpectedly
            asked = await _explain(
                run, {**state, "fleet": fleet, "tasks": tasks}, ev.get("what") or "", "anomaly"
            )
            upd.update(asked)
            upd["branch"] = "audit"
            return run.out(**upd, fleet=fleet, tasks=tasks, event=None)

        if kind == "order_check":  # model_first, after publication
            changed = await _order_check(run, state, ev.get("text") or "", tasks, fleet, dirty)
            upd["branch"] = "allocate" if changed else "audit"
            if changed:
                upd["dirty"] = dirty
            return run.out(**upd, fleet=fleet, tasks=tasks, event=None)

        if kind == "supersede":
            _supersede(run, state, tasks, dirty, upd, ev.get("by", "?"), ev.get("released") or {})
            return run.out(**upd, fleet=fleet, tasks=tasks, event=None)

        if kind == "world_reset":
            # The simulator started afresh: the world and the robots were reset, so a
            # mission from before has nothing left to drive (it would resume old laps on
            # new robots).
            _supersede(
                run,
                state,
                tasks,
                dirty,
                upd,
                "simulator restart",
                {},
                note="⏹️ This mission was ended because the simulator was restarted",
            )
            return run.out(**upd, fleet=fleet, tasks=tasks, event=None)

        if kind == "report":
            report = Report.model_validate(ev["report"])
            seen = list(state.get("seen_reports") or [])
            if report.report_id in seen:
                return run.out(**upd, fleet=fleet, tasks=tasks, event=None)  # duplicate
            seen = [*seen, report.report_id][-SEEN_REPORTS_MAX:]
            upd["seen_reports"] = seen
            if state.get("status") == "superseded":
                # A robot that was out of coverage when this mission ended still works on
                # it: send it the empty envelope now that it can be reached.
                rid = report.robot_id
                # Only reports of work in progress: the release itself makes the robot
                # report back (decision, preempted, completed), which must not re-trigger it.
                working = report.kind in (
                    ReportKind.SKILL_RESULT,
                    ReportKind.OBSERVATION,
                ) or (
                    report.kind is ReportKind.TASK_STATUS
                    and report.payload.get("status") not in ("preempted", "cancelled")
                )
                if working and rid in (state.get("envelopes") or {}) and rid in fleet:
                    fleet[rid]["status"] = ACTIVE_ROBOT  # it just reached us
                    dirty.append(rid)
                    upd["branch"] = "publish"
                return run.out(**upd, fleet=fleet, tasks=tasks)
            branch = await _ingest_report(run, report, tasks, fleet, dirty, upd)
            if report.kind in (
                ReportKind.SKILL_RESULT,
                ReportKind.OBSERVATION,
                ReportKind.RECONCILE,
            ):
                p = report.payload
                about = (
                    narr.task_id(tasks, p["task_id"])
                    if "task_id" in p
                    else p.get("label") or "its envelope"
                )
                outcome = (p.get("result") or {}).get("outcome") or (
                    f"{p.get('confidence', 0):.0%} confident" if "label" in p else "reconciled"
                )
                run.call(
                    "robot_report",
                    {"robot": report.robot_id, "report": report.kind.value, "about": about},
                    {
                        "outcome": outcome,
                        "coordinator_next": {
                            "allocate": "re-allocate",
                            "publish": "update envelopes",
                            "audit": "log it",
                        }.get(branch, branch),
                    },
                )
            upd["branch"] = branch
            return run.out(**upd, fleet=fleet, tasks=tasks)

        if kind in ("robot_failed", "robot_lost"):
            rid = ev["robot_id"]
            new_status = "failed" if kind == "robot_failed" else "lost"
            if rid in fleet and fleet[rid]["status"] not in ("failed", new_status):
                fleet[rid]["status"] = new_status
                # Dead: everything goes. Silent (a coverage gap, most often): it keeps all its
                # work, queued laps too; handing them out would leave robots idle each time
                # they came out of a lost stretch. Its work goes to the others only
                # if it is presumed stuck (robot_lost_long) or goes to charge.
                released = (
                    _release(tasks, rid, ("assigned", "in_progress"))
                    if kind == "robot_failed"
                    else []
                )
                if kind == "robot_failed":
                    for tid in released:
                        if tasks[tid].get("pinned_to") == rid:
                            tasks[tid]["pinned_to"] = None
                what = (
                    "has stopped responding and is considered down"
                    if kind == "robot_failed"
                    else "is out of contact (probably in a 5G coverage gap); it keeps its work "
                    "and goes on with it"
                )
                moved = join(narr.task_id(tasks, t) for t in released)
                run.note(
                    f"⚠️ {narr.robot(rid, fleet)} {what}."
                    + (
                        f" I'm handing its pending work to other robots: {moved}."
                        if released
                        else ""
                    )
                )
                run.decide(
                    DecisionKind.FALLBACK,
                    {"robot_id": rid, "released": released},
                    policy={
                        "rule": "robot_failure",
                        "robot_timeout_s": cfg.fleet.defaults.coordinator.robot_timeout_s,
                    },
                    justification=f"{rid} {new_status}: {ev.get('reason', 'no heartbeat')}",
                    inputs_ref=f"robot:{rid}",
                )
                upd.update(branch="allocate", reallocating=True)
            return run.out(**upd, fleet=fleet, tasks=tasks)

        if kind == "robot_added":
            rid = ev["robot_id"]
            manifest = ev.get("manifest") or await asyncio.to_thread(_load_manifest, cfg, rid)
            if manifest is None:
                run.note(
                    f"A robot called {rid} came online, but I have no manifest for it, "
                    "so I'm not using it."
                )
                return run.out(**upd, fleet=fleet, tasks=tasks, event=None)
            m = RobotManifest.model_validate(manifest)
            known = fleet.get(rid)
            if known is not None and known.get("status") != "failed":
                # Already in the fleet: a restarted coordinator hearing it announce itself
                # again. A reconnection (resync its envelope), never a newcomer whose
                # arrival re-plans everyone's work.
                if known.get("status") != ACTIVE_ROBOT:
                    known["status"] = ACTIVE_ROBOT
                    run.note(f"{narr.robot(rid, fleet)} is back in contact.")
                dirty.append(rid)
                upd.update(branch="allocate", dirty=sorted(set(dirty)))
                return run.out(**upd, fleet=fleet, tasks=tasks, event=None)
            fleet[rid] = {
                "robot_id": rid,
                "platform": m.platform.value,
                "status": ACTIVE_ROBOT,
                "a2a_url": ev.get("a2a_url") or (known or {}).get("a2a_url"),
                "manifest": m.model_dump(mode="json"),
            }
            # Fleet change: re-plan everything not yet started, including the newcomer.
            # Non-preemptible work (escorts) and pinned tasks stay with their robot.
            keep = {k.value for k in cfg.allocator.non_preemptible_kinds}
            released = []
            for tid, t in sorted(tasks.items()):
                if (
                    t["status"] == "assigned"
                    and not t.get("pinned_to")
                    and _spec(t).kind.value not in keep
                    and cfg.fleet.defaults.baseline != "independent"  # B0: never re-planned
                ):
                    if t["robot_id"] not in dirty:
                        dirty.append(t["robot_id"])
                    t.update(status="pending", robot_id=None)
                    released.append(tid)
            run.note(
                f"👋 {narr.robot(rid, fleet)} joined the fleet from the staging area."
                + (
                    f" I'm re-planning {plural(len(released), 'task')} that hadn't started yet."
                    if released
                    else ""
                )
            )
            run.decide(
                DecisionKind.FALLBACK,
                {"robot_id": rid, "released": released},
                policy={"rule": "fleet_change"},
                justification=f"{rid} admitted from the staging area",
                inputs_ref=f"robot:{rid}",
            )
            upd.update(branch="allocate", reallocating=True, dirty=sorted(set(dirty)))
            return run.out(**upd, fleet=fleet, tasks=tasks)

        if kind == "robot_charged":
            # Seen charged in its state messages (the fleet monitor): back at work even if
            # its "charged" report never came (charged idle at the dock, a report lost out
            # of coverage, an envelope already closed). Then reallocate, idle
            # rebalancing included, at once.
            rid = ev["robot_id"]
            if rid not in fleet or fleet[rid]["status"] in ("failed", "lost"):
                return run.out(**upd, fleet=fleet, tasks=tasks)
            was_charging = fleet[rid]["status"] == "charging"
            fleet[rid]["status"] = ACTIVE_ROBOT
            fleet[rid]["idle_battery_pct"] = ev.get("battery_pct")
            fleet[rid]["seen_battery_pct"] = ev.get("battery_pct")
            for t in tasks.values():
                if (t.get("refused_for") or {}).get(rid) == "battery":
                    t["refused_by"] = [r for r in t.get("refused_by") or [] if r != rid]
                    del t["refused_for"][rid]
                    (t.get("refused_battery") or {}).pop(rid, None)
            resumed = []
            for tid, t in sorted(tasks.items()):
                if t["status"] == "paused" and t.get("resume_on") == rid:
                    t.update(status="pending", robot_id=None, pinned_to=rid, resume_on=None)
                    resumed.append(tid)
            if was_charging or resumed:
                run.note(
                    f"🔋 {narr.robot(rid, fleet)} is charged ({ev.get('battery_pct', '?'):.0f} %)"
                    " and back at work"
                    + (
                        f": it resumes {join(narr.task_id(tasks, t) for t in resumed)}."
                        if resumed
                        else "."
                    )
                )
            upd["branch"] = "allocate"
            return run.out(**upd, fleet=fleet, tasks=tasks)

        if kind == "robot_lost_long":
            # Presumed stuck (it keeps seeking coverage on its own) once silent longer than
            # its running task should take, plus the link-loss hold, plus lost_release_s: a
            # robot patrolling a lost stretch is working, not stuck (scenario 3 ran laps
            # twice with a fixed 120 s). Then all its work goes to the others, the running
            # task and the work given to it by name too; if it comes back, the duplicate
            # rule settles any overlap.
            rid = ev["robot_id"]
            running = [
                _expected_s(_spec(t), fleet.get(rid))
                for t in tasks.values()
                if t.get("robot_id") == rid and t["status"] == "in_progress"
            ]
            bound = (
                max(running, default=0.0)
                + cfg.fleet.defaults.agent.link_loss_hold_s
                + (cfg.fleet.defaults.coordinator.lost_release_s or 0.0)
            )
            if ev.get("silent_s", 0) < bound:
                return run.out(**upd, fleet=fleet, tasks=tasks)  # still plausibly working
            if rid in fleet and fleet[rid]["status"] == "lost":
                released = _release(tasks, rid, ("assigned", "in_progress"))
                for tid in released:
                    if tasks[tid].get("pinned_to") == rid:
                        tasks[tid]["pinned_to"] = None
                silent = ev.get("reason", "").removeprefix("no state for ")
                if released:
                    run.note(
                        f"⚠️ {narr.robot(rid, fleet)} has been out of contact for {silent} "
                        "and may be stuck (it keeps trying to reach coverage). I'm handing "
                        "its work to the other robots: "
                        + join(narr.task_id(tasks, t) for t in released)
                        + "."
                    )
                run.decide(
                    DecisionKind.FALLBACK,
                    {"robot_id": rid, "released": released},
                    policy={
                        "rule": "robot_lost_long",
                        "lost_release_s": cfg.fleet.defaults.coordinator.lost_release_s,
                        "silent_bound_s": round(bound, 1),
                    },
                    justification=f"{rid} out of contact: {ev.get('reason', '')}",
                    inputs_ref=f"robot:{rid}",
                )
                if released:
                    upd.update(branch="allocate", reallocating=True)
            return run.out(**upd, fleet=fleet, tasks=tasks)

        if kind == "robot_recovered":
            rid = ev["robot_id"]
            if rid in fleet and fleet[rid]["status"] not in (ACTIVE_ROBOT, "charging"):
                fleet[rid]["status"] = ACTIVE_ROBOT
                dirty.append(rid)  # resync its envelope with what it still owns
                run.note(f"{narr.robot(rid, fleet)} is back in contact.")
                upd["branch"] = "allocate"
            return run.out(**upd, fleet=fleet, tasks=tasks)

        if kind == "tick":
            upd["branch"] = (
                "allocate" if any(t["status"] == "pending" for t in tasks.values()) else "audit"
            )
        return run.out(**upd, fleet=fleet, tasks=tasks)

    def _how_given(tasks: dict[str, dict[str, Any]], decisions: list[Any]) -> list[dict[str, Any]]:
        """For each open task, the allocation that gave it (its patrol's, for a later lap)
        with every robot's cost or reason: the decision log the model sees is the last 40
        entries, cut short, and may miss it."""
        given: dict[str, dict[str, Any]] = {}
        for d in decisions:
            if d.actor != "coordinator" or d.kind not in (
                DecisionKind.ALLOCATE,
                DecisionKind.REALLOCATE,
            ):
                continue
            for a in d.output.get("assignments") or []:
                base = re.sub(r"-lap\d+$", "", a["task_id"])
                given[base] = {
                    "to": a["robot_id"],
                    "when": d.stamp.isoformat(timespec="seconds"),
                    "candidates": (d.output.get("candidates") or {}).get(a["task_id"]),
                }
        out = []
        for tid, t in sorted(tasks.items()):
            if t["status"] not in OPEN:
                continue
            base = re.sub(r"-lap\d+$", "", tid)
            entry = {"task": narr.task_id(tasks, tid), "now": t.get("robot_id")}
            if base in given:
                entry["given"] = given[base]
            if t.get("repeat"):
                entry["note"] = "a patrol stays with the robot patrolling it, lap after lap"
            out.append(entry)
        return out

    def _candidates(
        result: AssignResult,
        tasks: dict[str, dict[str, Any]],
        fleet: dict[str, Any],
        states: dict[str, Any],
        manifests: dict[str, RobotManifest],
        floor: float,
    ) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        doing: dict[str, list[str]] = {}
        for tid, t in tasks.items():
            if t["status"] in ("assigned", "in_progress") and t.get("robot_id"):
                doing.setdefault(t["robot_id"], []).append(narr.task_id(tasks, tid))
        for a in result.assignments:
            spec = _spec(tasks[a.task_id])
            row: dict[str, str] = {}
            for rid in sorted(fleet):
                busy = [w for w in doing.get(rid, []) if w != narr.task_id(tasks, a.task_id)]
                also = f"; also doing {', '.join(busy[:2])}" if busy else ""
                if rid not in states:
                    row[rid] = f"not available ({fleet[rid].get('status')})"
                    continue
                vs = virtual_state(states[rid], manifests[rid], cfg.allocator)
                est = estimate(vs, spec, manifests[rid], cfg.campus, cfg.allocator, floor)
                if isinstance(est, Estimate):
                    row[rid] = f"cost {est.cost:.0f}{also}"
                else:
                    row[rid] = f"cannot ({est.reason.value}: {est.detail}){also}"
            out[a.task_id] = row
        return out

    def _expected_s(spec: TaskSpec, entry: dict[str, Any] | None) -> float:
        """How long a running task may take: its route at the robot's top speed plus its
        dwell, doubled (turns, slow stretches). A survey's ``est_duration_s`` is a 60 s
        placeholder, too short for a long survey: a robot surveying out of coverage would
        be presumed stuck while it works, and two robots would run the survey."""
        pts = task_path(spec, cfg.campus)
        length = 0.0
        for a, b in itertools.pairwise(pts):
            d = route_distance(cfg.campus, a, b)
            length += d if d is not None else math.hypot(b.x - a.x, b.y - a.y)
        speed = _manifest(entry).max_speed_mps if entry else 1.0
        return 2.0 * (spec.est_duration_s + length / speed)

    def describe_move(tasks: dict[str, dict[str, Any]], move: tuple[str, str | None, str]) -> str:
        task_id, old, new = move
        return f"**{new}** took over '{narr.task_id(tasks, task_id)}' from **{old or 'nobody'}**"

    region = campus_region(cfg)
    non_swappable = {k.value for k in cfg.allocator.non_preemptible_kinds}
    # Robots deliberate before answering an envelope.
    envelope_timeout_s = (
        deps.envelope_timeout_s or cfg.fleet.defaults.coordinator.envelope_timeout_s
    )

    def make_envelope(
        mission_id: str,
        rid: str,
        rec: dict[str, Any],
        specs: list[TaskSpec],
        default_priority: int,
        pinned: list[str] | None = None,
        flows: dict[str, dict[str, Any]] | None = None,
    ) -> Envelope:
        now = deps.clock.now()
        return Envelope(
            flows=flows or {},
            envelope_id=rec["envelope_id"],
            robot_id=rid,
            mission_id=mission_id,
            tasks=specs,
            region=region,
            valid_from=now,
            valid_until=now + timedelta(seconds=cfg.fleet.defaults.coordinator.mission_horizon_s),
            priority=max((s.priority for s in specs), default=default_priority),
            budgets=EnvelopeBudgets(
                battery_floor_pct=cfg.allocator.default_battery_floor_pct,
                uplink_mbps=cfg.fleet.budget_for(rid).sustained_mbps,
            ),
            swappable=not any(s.kind.value in non_swappable for s in specs),
            version=rec["version"],
            pinned_task_ids=sorted(pinned or []),
        )

    async def _release_robots(
        other: str, old: dict[str, Any], fleet: dict[str, Any]
    ) -> dict[str, int]:
        """Send the superseded mission's robots an empty envelope now (robot -> version).

        Done from this run, not the old mission's: its queued run may only start after
        this one (a single Agent Server worker), and the robots must be free *before* this
        mission's allocation or they decline on the old commitment.
        """
        released: dict[str, int] = {}

        async def one(rid: str, rec: dict[str, Any]) -> None:
            entry = fleet.get(rid) or (old.get("fleet") or {}).get(rid)
            if entry is None:
                return
            rec = {**rec, "version": rec["version"] + 1}
            try:
                async with asyncio.timeout(envelope_timeout_s):
                    await deps.links(rid, entry).send_envelope(
                        make_envelope(other, rid, rec, [], 0)
                    )
                released[rid] = rec["version"]
            except Exception as exc:  # the old mission's own run retries it later
                log.warning("releasing %s from mission %s: %s", rid, other, exc)

        await asyncio.gather(*(one(r, e) for r, e in (old.get("envelopes") or {}).items()))
        return released

    async def _explain(
        run: _Run, state: CoordState, question: str, trigger: str = "question"
    ) -> dict[str, Any]:
        """Answer a why-question from the mission's own records (tasks and decisions); with
        a model, an analysis: does the swarm do what the operator's
        orders ask? A fix it proposes is validated by code and offered to the operator
        (``apply_fix``). ``trigger``: ``question`` (the operator asked) or ``anomaly`` (a
        robot behaved unexpectedly). Returns state updates (the escalation, if any)."""
        started = time.perf_counter()
        tasks = state.get("tasks") or {}
        facts = {
            "tasks": [
                {
                    "task_id": tid,
                    "what": narr.task_id(tasks, tid),
                    "status": t["status"],
                    "robot": t.get("robot_id"),
                    "pinned_to": t.get("pinned_to"),
                    "refused_by": t.get("refused_by") or [],
                }
                for tid, t in tasks.items()
            ],
            "fleet": {r: e.get("status") for r, e in (state.get("fleet") or {}).items()},
            "posture": state.get("posture"),
        }
        decisions = []
        reader = getattr(deps.decisions, "read", None)
        if reader is not None:
            with contextlib.suppress(Exception):
                decisions = await asyncio.to_thread(reader, run.mission_id)
        # The order inside each robot (current task, queue) is decided on the robot: ask it.
        queues: dict[str, Any] = {}

        async def queue_of(rid: str, entry: dict[str, Any]) -> None:
            try:
                link = deps.links(rid, entry)
                async with asyncio.timeout(deps.state_timeout_s):
                    queues[rid] = await link.get_queue()
            except Exception as exc:  # out of coverage, down, or an old robot agent
                queues[rid] = f"unavailable ({type(exc).__name__})"

        await asyncio.gather(*(queue_of(rid, e) for rid, e in (state.get("fleet") or {}).items()))
        facts["robot_queues"] = queues
        # Robots' justifications are model-written: data for the prompt, never instructions
        # They go in a delimited block, referenced by index.
        statements: list[str] = []
        rows = []
        for d in decisions[-40:]:
            row = {
                "kind": d.kind.value,
                "actor": d.actor,
                "output": json.dumps(d.output, default=str)[:400],
            }
            if d.actor.endswith(".agent"):
                statements.append(f"[{len(statements)}] {d.actor}: {d.justification}")
                row["why"] = f"robot statement [{len(statements) - 1}]"
            else:
                row["why"] = d.justification
            rows.append(row)
        facts["decisions"] = rows
        facts["how_current_work_was_given"] = _how_given(state.get("tasks") or {}, decisions)
        fallback_text = narr.status(state)
        if deps.model is not None:
            return await _diagnose(run, state, question, trigger, facts, statements, started)

        def fallback() -> _Explanation:
            return _Explanation(answer=fallback_text)

        answer, trace = await structured_call(
            deps.model,
            _Explanation,
            [
                SystemMessage(
                    "You explain a robot-swarm coordinator's decisions to its operator in two "
                    "or three plain sentences. Use ONLY the facts given (tasks, fleet, the "
                    "decision log: decompose = the plan, allocate = assignments with costs, "
                    "accept/refuse = robot replies, fallback = link/emergency/battery handling; "
                    "robot_queues = each robot's own current task and queue in execution order, "
                    "decided on the robot: priority first, patrol laps one lap at a time, "
                    "nearest first within a lap). If the "
                    "facts do not explain it, say so and say what they do show. Name robots "
                    "and places as in the facts; never invent causes."
                ),
                HumanMessage(
                    f"Question: {question}\nFacts: {json.dumps(facts, default=str)}\n"
                    + untrusted("robot statements", "\n".join(statements))
                ),
            ],
            fallback,
            visible=True,
        )
        run.thoughts("explain", trace)
        run.call(
            "explain_decisions",
            {"question": question},
            {
                "decisions_read": len(facts["decisions"]),
                "method": "template" if trace.used_fallback else "LLM",
            },
            time.perf_counter() - started,
        )
        run.note(answer.answer)
        return {}

    def _fix_lines(
        plan: agentic.AgentPlan,
        ctx: agentic.Context,
        tasks: dict[str, dict[str, Any]],
        fleet: dict[str, Any],
    ) -> list[str]:
        """A proposed fix in the operator's words, one change per line."""
        lines = []
        for tid in plan.end:
            holder = tasks[tid].get("robot_id") or tasks[tid].get("pinned_to")
            who = f" ({narr.robot(holder, fleet)})" if holder else ""
            lines.append(f"stop: {narr.task_id(tasks, tid)}{who}")
        for r in plan.reassign:
            to = narr.robot(r.robot_id, fleet) if r.robot_id else "another robot"
            lines.append(f"{narr.task_id(tasks, r.task_id)} → {to}")
        for nt in agentic.to_tasks(plan, ctx, set(tasks)):
            who = narr.robot(nt.pinned_to, fleet) if nt.pinned_to else "any robot"
            lines.append(f"new: {narr.task(nt.spec)} ({who})")
        return lines

    def _breaches(
        tasks: dict[str, dict[str, Any]], fleet: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Open work an order gave to a robot by name, done by another robot or by nobody
        (the model alone often misses a swap of two named patrols).
        Returns (breaches with the fix, ones with a known reason: the robot is not
        available or refused the work)."""
        breaches, explained = [], []
        for tid, t in sorted(tasks.items()):
            want = t.get("ordered_for")
            holder = t.get("robot_id")
            if t["status"] not in OPEN or not want or holder == want:
                continue
            if holder is None and t["status"] != "pending":
                continue
            what = narr.task_id(tasks, tid)
            now = narr.robot(holder, fleet) if holder else "nobody yet"
            line = f"{narr.robot(want, fleet)} was ordered to {what}; {now} does it"
            if fleet.get(want, {}).get("status") != ACTIVE_ROBOT:
                explained.append(f"{line} ({want} is {fleet.get(want, {}).get('status')})")
            elif want in (t.get("refused_by") or []):
                reason = (t.get("refused_for") or {}).get(want, "refused")
                explained.append(f"{line} ({want} refused it: {reason})")
            else:
                breaches.append({"task_id": tid, "robot_id": want, "line": line})
        return breaches, explained

    async def _diagnose(
        run: _Run,
        state: CoordState,
        question: str,
        trigger: str,
        facts: dict[str, Any],
        statements: list[str],
        started: float,
    ) -> dict[str, Any]:
        fleet = state.get("fleet") or {}
        tasks = state.get("tasks") or {}
        posture = Posture(state.get("posture") or "routine")
        ctx = _context(state, posture, fleet)
        # what the swarm should be doing: the operator's recent orders, oldest first
        orders = [
            m.text
            for m in state.get("messages") or []
            if isinstance(m, HumanMessage)
            and m.text.strip()
            and m.text.strip() != question
            and not is_why(m.text)  # questions are not orders
        ][-6:]
        # who does what NOW (the log is history: the model must not judge by it)
        now: dict[str, list[str]] = {}
        for tid, t in sorted(tasks.items()):
            if t["status"] in OPEN:
                who = t.get("robot_id") or t.get("pinned_to") or "nobody yet"
                now.setdefault(who, []).append(f"{narr.task_id(tasks, tid)} [{tid}]")
        breaches, explained = _breaches(tasks, fleet)
        facts = {
            "now_who_does_what": now,
            # computed by code from what each order named: reliable
            "orders_not_followed": breaches,
            "orders_not_followed_for_a_known_reason": explained,
            "operator_orders_oldest_first": orders,
            **facts,
        }
        messages: list[Any] = [
            SystemMessage(agentic.DIAGNOSE_PROMPT),
            HumanMessage(
                ("Operator question" if trigger == "question" else "What looks wrong")
                + f": {question}\nFacts: {json.dumps(facts, default=str)}\n"
                + untrusted("robot statements", "\n".join(statements))
            ),
        ]
        fallback_text = narr.status(state)
        result: agentic.Diagnosis | None = None
        errors: list[str] = []
        total = 0.0
        trace = None
        try:
            async with asyncio.timeout(cfg.fleet.defaults.coordinator.careful_deadline_s):
                for attempt in (1, 2):
                    result, trace = await structured_call(
                        deps.model,
                        agentic.Diagnosis,
                        messages,
                        lambda: agentic.Diagnosis(answer=fallback_text),
                        visible=attempt == 1,
                    )
                    total += trace.latency_s
                    if attempt == 1:
                        run.thoughts("analyse", trace)
                    if not (result.mismatch and result.changes()):
                        errors = []
                        break
                    plan = agentic.tidy(result.plan(), ctx)
                    errors = agentic.validate(plan, ctx)
                    if not errors:
                        break
                    messages += [
                        AIMessage(result.model_dump_json()),
                        HumanMessage(
                            "This fix was rejected:\n- "
                            + "\n- ".join(errors)
                            + "\nReturn the analysis with a corrected fix (or none)."
                        ),
                    ]
        except TimeoutError:
            errors = errors or [
                f"no analysis within {cfg.fleet.defaults.coordinator.careful_deadline_s:g} s"
            ]
        if result is None:
            result = agentic.Diagnosis(answer=fallback_text)
        run.call(
            "explain_decisions",
            {"question": question, "trigger": trigger},
            {
                "decisions_read": len(facts["decisions"]),
                "mismatch": result.mismatch,
                "fix": agentic.dumps(result.plan()) if result.changes() else None,
                "rejected": errors[:5],
                "method": "template" if trace is None or trace.used_fallback else "LLM",
            },
            time.perf_counter() - started,
        )
        lead = f"🧐 Something looked wrong: {question}\n\n" if trigger == "anomaly" else ""
        # Task ids are for the model; the operator reads names.
        answer = result.answer
        for tid in sorted(tasks, key=len, reverse=True):  # "Rou-survey_..." opens sentences
            name = narr.task_id(tasks, tid)
            answer = re.sub(re.escape(tid), lambda _m, n=name: n, answer, flags=re.I)
        # field names leak into the wording ("orders_not_followed is empty")
        answer = re.sub(r"\borders_not_followed\w*", "my records of your orders", answer)
        answer = re.sub(r"\s*\(?\bmismatch\s*=\s*\w+\)?", "", answer)
        result = result.model_copy(update={"answer": answer})
        if breaches:
            # The records say an order is not followed: code states it and proposes the fix
            # (the model's wording of it was unreliable: "Yes. ... a mismatch", reversed).
            fix = agentic.AgentPlan(
                reassign=[
                    agentic.Reassign(task_id=b["task_id"], robot_id=b["robot_id"]) for b in breaches
                ]
            )
            if not agentic.validate(fix, ctx):
                result = result.model_copy(
                    update={
                        "answer": "Not quite: " + "; ".join(b["line"] for b in breaches) + ".",
                        "mismatch": True,
                        "end": [],
                        "add": [],
                        "reassign": fix.reassign,
                    }
                )
                errors = []
        if not (result.mismatch and result.changes()):
            run.note(lead + result.answer)
            return {}
        if errors:
            run.note(
                lead
                + result.answer
                + "\n\nI couldn't find a fix that passes my checks ("
                + errors[0]
                + "); tell me what you want changed."
            )
            return {}
        plan = agentic.tidy(result.plan(), ctx)
        lines = _fix_lines(plan, ctx, tasks, fleet)
        run.note(
            lead
            + result.answer
            + "\n\nI can fix it: "
            + "; ".join(lines)
            + ". Please approve or reject below."
        )
        seq = (state.get("escalation_seq") or 0) + 1
        esc = {
            "esc_id": f"esc-{seq}",
            "kind": "apply_fix",
            "text": question,
            "trigger": trigger,
            "posture": posture.value,
            "fix": agentic.dumps(plan),
            "changes": lines,
            "answer": result.answer,
        }
        run.decide(
            DecisionKind.ESCALATE,
            {"escalation": esc},
            policy={"rule": "analysis fix: the operator approves"},
            justification=result.answer,
            inputs_ref=f"analyse:{trigger}",
        )
        return {
            "escalation_seq": seq,
            "escalations": [*(state.get("escalations") or []), esc],
        }

    async def _take_over_fleet(run: _Run, mission_id: str, fleet: dict[str, Any]) -> None:
        others = await _other_active_missions(mission_id)
        if not others:
            return
        if cfg.fleet.defaults.coordinator.missions == "shared":
            run.note(
                "⚠️ Other missions are still active on this fleet ("
                + join(others)
                + "). They will command the same robots; continue in that conversation, or "
                "give it the *all clear* and let it finish first."
            )
            return
        assistant = cfg.fleet.defaults.coordinator.assistant_id
        submit = deps.submit or _sdk_submit(assistant)
        peek = deps.peek or _sdk_peek()

        async def end(other: str) -> dict[str, int]:
            released: dict[str, int] = {}
            try:
                old = await peek(other)
                released = await _release_robots(other, old or {}, fleet)
            except Exception as exc:
                log.warning("reading mission %s: %s", other, exc)
            # Its own bookkeeping (cancel tasks, status) runs later on its thread: queued,
            # never awaited.
            await submit(other, {"type": "supersede", "by": mission_id, "released": released})
            return released

        started = time.perf_counter()
        released = dict(zip(others, await asyncio.gather(*(end(o) for o in others)), strict=True))
        run.decide(
            DecisionKind.ARBITRATE,
            {"superseded": others, "released": released},
            policy={"missions": "exclusive"},
            justification="a new conversation takes over the fleet",
        )
        run.call(
            "end_previous_missions",
            {"missions": others},
            {"robots_released": sorted({r for rel in released.values() for r in rel})},
            time.perf_counter() - started,
        )
        run.note(
            f"I've ended {plural(len(others), 'previous mission')} so this conversation is in "
            "charge of the robots."
        )

    def _supersede(
        run: _Run,
        state: CoordState,
        tasks: dict[str, Any],
        dirty: list[str],
        upd: dict[str, Any],
        by: str,
        released: dict[str, int],
        note: str = "⏹️ This mission was ended because a newer conversation took over the robots",
    ) -> None:
        """Another conversation took over: cancel open work and release every robot.

        ``released`` robots already got an empty envelope from the new mission's run.
        """
        cancelled = []
        for tid in sorted(tasks):
            t = tasks[tid]
            if t["status"] in OPEN:
                t.update(status="cancelled")
                cancelled.append(tid)
        envelopes = copy.deepcopy(state.get("envelopes") or {})
        for rid, version in released.items():
            if rid in envelopes:
                envelopes[rid]["version"] = max(envelopes[rid]["version"], version)
        upd["envelopes"] = envelopes
        for rid in sorted({*envelopes, *dirty}):
            if rid not in dirty and rid not in released:
                dirty.append(rid)  # an empty envelope ends this mission on the robot
        run.decide(
            DecisionKind.ARBITRATE,
            {"cancelled": cancelled, "released": sorted({*dirty, *released})},
            policy={"missions": "exclusive"},
            justification=f"superseded by mission {by}",
            inputs_ref=f"mission:{by}",
        )
        run.note(
            note
            + (f" ({plural(len(cancelled), 'open task')} cancelled)" if cancelled else "")
            + ". Give me a new goal here to take them back."
        )
        upd.update(status="superseded", branch="publish" if dirty else "audit")

    def _stop(
        run: _Run,
        state: CoordState,
        tasks: dict[str, Any],
        dirty: list[str],
        upd: dict[str, Any],
    ) -> None:
        """The operator stops the mission: cancel all open work; robots stay where they are."""
        cancelled = []
        for tid in sorted(tasks):
            t = tasks[tid]
            if t["status"] in OPEN:
                if t.get("robot_id") and t["robot_id"] not in dirty:
                    dirty.append(t["robot_id"])
                t.update(status="cancelled")
                cancelled.append(tid)
        run.decide(
            DecisionKind.ARBITRATE,
            {"cancelled": cancelled, "released": sorted(dirty)},
            policy={"rule": "operator stop"},
            justification="operator stopped the mission",
            inputs_ref="chat:stop",
        )
        if cancelled:
            names = join(dict.fromkeys(narr.task_id(tasks, t) for t in cancelled))
            run.note(
                f"⏹️ Stopped: I've cancelled {names}. The robots stay where they are; say "
                "*return to base* to bring them back."
            )
        else:
            run.note("There is nothing running to stop.")
        upd.update(branch="publish" if dirty else "audit")

    def _all_clear(
        run: _Run,
        state: CoordState,
        tasks: dict[str, Any],
        dirty: list[str],
        upd: dict[str, Any],
        text: str,
        quiet: bool = False,
        resume: bool = True,
    ) -> bool:
        """End the emergency: cancel its posts, resume paused work, lower the posture.

        ``resume=False`` (the emergency ends with a new order): paused work is not resumed;
        robots stay where they are until the operator orders a patrol. Low-battery returns
        to base (pinned, no dependency) are kept. Returns False when there was nothing to
        clear (the reply is already noted).
        """
        started = time.perf_counter()
        emergency = Posture.EMERGENCY.value
        cancelled, resumed, stopped = [], [], []
        for tid in sorted(tasks):
            t = tasks[tid]
            spec = _spec(t)
            if t["status"] in OPEN and t.get("posture") == emergency:
                if spec.kind is TaskKind.RETURN_HOME and not spec.depends_on:
                    continue  # battery safety, not emergency work
                if t.get("robot_id") and t["robot_id"] not in dirty:
                    dirty.append(t["robot_id"])
                t.update(status="cancelled")
                cancelled.append(tid)
            elif t["status"] == "preempted" and resume:
                t.update(status="pending", robot_id=None, refused_by=[], attempts=0)
                resumed.append(tid)
            elif t["status"] == "preempted":  # a new order replaces it ("patrol ..." restarts it)
                t.update(status="cancelled", robot_id=None)
                stopped.append(tid)
        before = state.get("posture") or "routine"
        if not cancelled and not resumed and not stopped and before != emergency:
            if not quiet:  # an order came with the all clear: it speaks for itself
                run.note("There is no emergency to clear; the current plan stays as it is.")
            return False
        after = max(
            (t.get("posture") or "routine" for t in tasks.values() if t["status"] in OPEN),
            key=lambda p: _posture_rank(cfg, p),
            default=Posture.ROUTINE.value,
        )
        run.decide(
            DecisionKind.ARBITRATE,
            {"posture": after, "cancelled": cancelled, "resumed": resumed, "stopped": stopped},
            policy={"rule": "operator all clear"},
            justification=f"operator: {text}",
            inputs_ref="chat:all_clear",
        )
        msg = f"✅ All clear: back to **{after}** mode."
        if cancelled:
            msg += f" I've released {join(narr.task_id(tasks, t) for t in cancelled)}."
        if resumed:
            msg += f" Resuming {join(narr.task_id(tasks, t) for t in resumed)}."
        if stopped:
            msg += (
                f" The paused work ({join(narr.task_id(tasks, t) for t in stopped)}) stays "
                "stopped and the other robots stay where they are; ask for a patrol to restart it."
            )
        run.note(msg)
        run.call(
            "arbitrate_postures",
            {"new_posture": after, "reason": "all clear"},
            {
                "cancelled": [narr.task_id(tasks, t) for t in cancelled],
                "resumed": [narr.task_id(tasks, t) for t in resumed],
                **({"kept_stopped": [narr.task_id(tasks, t) for t in stopped]} if stopped else {}),
            },
            time.perf_counter() - started,
        )
        upd.update(
            posture=after,
            status="active",
            branch="allocate" if resumed else "publish",
        )
        return True

    async def _claim(
        run: _Run,
        task_id: str,
        claimant: str,
        tasks: dict[str, dict[str, Any]],
        fleet: dict[str, dict[str, Any]],
        dirty: list[str],
    ) -> str | None:
        """A robot claims a task (a peer award made while I was unreachable, or a task it
        reports running) that I have since given to another active robot: keep whichever is
        expected to finish it sooner from where they are now, and drop it from the other.
        Adopting such a claim blindly would put two robots on one task and keep the slower
        one. Returns the robot that keeps it, or None (no task)."""
        entry = tasks.get(task_id)
        if entry is None or entry["status"] not in OPEN:
            return None
        holder = entry.get("robot_id")
        if not holder or holder == claimant or fleet.get(holder, {}).get("status") != ACTIVE_ROBOT:
            return claimant
        pair = {r: fleet[r] for r in (holder, claimant) if r in fleet}
        states, _ = await _robot_states(deps, pair)
        spec = _spec(entry)
        floor = cfg.allocator.default_battery_floor_pct

        def cost(rid: str) -> float:
            if rid not in states:
                return math.inf
            est = estimate(
                virtual_state(states[rid], _manifest(fleet[rid]), cfg.allocator),
                spec,
                _manifest(fleet[rid]),
                cfg.campus,
                cfg.allocator,
                floor,
            )
            return est.cost if isinstance(est, Estimate) else math.inf

        costs = {holder: cost(holder), claimant: cost(claimant)}
        keep = claimant if costs[claimant] <= costs[holder] else holder
        drop = holder if keep == claimant else claimant
        for r in (holder, claimant):
            if r not in dirty:
                dirty.append(r)
        run.decide(
            DecisionKind.REALLOCATE,
            {
                "task_id": task_id,
                "claimed_by": claimant,
                "held_by": holder,
                "kept_by": keep,
                "costs": {k: round(v, 1) if v != math.inf else None for k, v in costs.items()},
            },
            policy={"rule": "double assignment after coordinator loss: the sooner keeps it"},
            justification=f"{claimant} and {holder} both had {task_id}; {keep} finishes sooner",
            inputs_ref=f"task:{task_id}",
        )
        run.note(
            f"⚖️ {narr.robot(claimant, fleet)} and {narr.robot(holder, fleet)} were both on "
            f"{narr.task_id(tasks, task_id)}; {narr.robot(keep, fleet)} keeps it (it gets "
            f"there sooner) and {narr.robot(drop, fleet)} drops it."
        )
        return keep

    def _rebalance(
        run: _Run,
        tasks: dict[str, dict[str, Any]],
        fleet: dict[str, dict[str, Any]],
        dirty: list[str],
    ) -> list[tuple[str, str, str]]:
        """Idle robots take queued work (``idle_moves``); notes, decisions, dirty robots."""
        moves = idle_moves(cfg, tasks, fleet)
        for tid, holder, rid in moves:
            for r in (holder, rid):
                if r not in dirty:
                    dirty.append(r)
            run.note(
                f"🔄 {narr.robot(rid, fleet)} had nothing to do, so it takes over "
                f"{narr.task_id(tasks, tid)} from {narr.robot(holder, fleet)}, which had "
                "it queued."
            )
            run.decide(
                DecisionKind.REALLOCATE,
                {"task_id": tid, "from": holder, "to": rid},
                policy={"rule": "load_balance: an idle robot takes queued work"},
                justification=f"{rid} idle; {holder} held {tid} queued behind other work",
                inputs_ref=f"robot:{rid}",
            )
        return moves

    def _recharging(
        run: _Run,
        rid: str,
        p: dict[str, Any],
        tasks: dict[str, dict[str, Any]],
        fleet: dict[str, dict[str, Any]],
        dirty: list[str],
    ) -> str:
        """Low battery: the robot released its tasks and drives to the staging area. Important
        work goes to another robot; routine work waits for it."""
        if rid in fleet:
            fleet[rid]["status"] = "charging"
        released = set(p.get("released") or []) | {
            tid for tid, t in tasks.items() if t.get("robot_id") == rid and t["status"] in OPEN
        }
        handed, paused = [], []
        for tid in sorted(released):
            t = tasks.get(tid)
            if t is None or t["status"] not in OPEN:
                continue
            if _spec(t).priority >= cfg.allocator.recharge_reassign_min_priority:
                pinned = None if t.get("pinned_to") == rid else t.get("pinned_to")
                t.update(status="pending", robot_id=None, pinned_to=pinned)
                handed.append(tid)
            else:
                t.update(status="paused", robot_id=None, resume_on=rid)
                paused.append(tid)
        text = (
            f"🔋 {narr.robot(rid, fleet)} is low on battery ({p.get('battery_pct', '?')} %): it is "
            "going back to the staging area to recharge."
        )
        if handed:
            text += f" Another robot takes over {join(narr.task_id(tasks, t) for t in handed)}."
        if paused:
            text += (
                f" {join(narr.task_id(tasks, t) for t in paused).capitalize()} resumes when it is "
                "charged."
            )
        run.note(text)
        run.decide(
            DecisionKind.FALLBACK,
            {"robot_id": rid, "recharging": True, "handed_over": handed, "paused": paused},
            policy={"reassign_min_priority": cfg.allocator.recharge_reassign_min_priority},
            justification=f"{rid} battery {p.get('battery_pct')} %: recharge",
            inputs_ref=f"robot:{rid}",
        )
        return "allocate" if handed else "audit"

    async def _status_from_observation(run: _Run, obs: Observation) -> None:
        """A robot proposed a status: re-validate with the same closed vocabulary and
        distance (external validation), then write it through the knowledge server, whose
        status policy decides its effect."""
        status = status_from_label(obs.label)
        radius = cfg.knowledge.status_match_radius_m
        subject = match_subject(cfg.campus, obs.pose.x, obs.pose.y, radius)
        outcome: dict[str, Any] = {"subject": obs.subject, "proposed_by": obs.robot_id}
        if status is None or subject != obs.subject:
            outcome.update(accepted=False, reason="does not re-validate (vocabulary/distance)")
        elif deps.kb is None:
            outcome.update(accepted=False, reason="no knowledge server")
        else:
            report = StatusReport(
                status_id=deps.ids("st"),
                subject=subject,
                status=status,
                source=f"robot:{obs.robot_id}",
                evidence=obs.obs_id,
                confidence=obs.confidence,
                stamp=deps.clock.now(),
            )
            try:
                answer = await deps.kb.put_status(report, run.mission_id)
            except Exception as exc:
                answer = {"accepted": False, "reason": f"knowledge server unreachable: {exc}"}
            outcome.update(status=status, status_id=report.status_id, **answer)
        decision = make_decision(
            deps.actor,
            DecisionKind.STATUS_UPDATE,
            mission_id=run.mission_id,
            inputs_ref=f"obs:{obs.obs_id}",
            output=outcome,
            policy={"rule": "rule 27", "status_match_radius_m": radius},
            justification=(
                f"{obs.robot_id} reported {obs.label!r} at {obs.subject}: "
                + ("written" if outcome.get("accepted") else outcome.get("reason", "rejected"))
            ),
            clock=deps.clock,
            ids=deps.ids,
        ).model_copy(update={"validated_by": deps.actor})
        run.decisions.append(decision.model_dump(mode="json"))
        if outcome.get("accepted") and outcome.get("status"):
            run.note(
                f"🚧 {obs.robot_id} reports {obs.subject} {outcome['status'].replace('_', ' ')}; "
                "people routes avoid it from now on."
            )

    async def _human_request(
        run: _Run,
        report: Report,
        tasks: dict[str, dict[str, Any]],
        fleet: dict[str, dict[str, Any]],
    ) -> str:
        """A person asked a robot for guidance: an escort task carrying the
        ``interaction_id``, pinned to that robot. The request holds ids and poses only; the
        constraint stays in the InteractionStore (personal data is forgotten at the end)."""
        p = report.payload
        rid = report.robot_id
        iid = p.get("interaction_id")
        dest = p.get("destination") or {}
        known = {
            "poi": {x.poi_id for x in cfg.campus.pois},
            "sector": {x.sector_id for x in cfg.campus.sectors},
            "place": set(cfg.campus.places()),
        }
        if not iid or dest.get("id") not in known.get(dest.get("kind"), set()):
            run.note(f"🗣️ A person asked {narr.robot(rid, fleet)} for something I can't plan.")
            return "audit"
        if any(t["spec"].get("interaction_id") == iid for t in tasks.values()):
            return "audit"  # a re-sent request (idempotent)
        kind, ident = dest["kind"], dest["id"]
        if kind == "sector":
            target: Any = SectorTarget(sector_id=ident)
        elif kind == "poi":
            target = PoiTarget(poi_id=ident)
        else:
            target = place_target(cfg.campus, ident)
        spec = make_task(TaskKind.ESCORT, target, Posture.INTERACTIVE, suffix=ident)
        spec = spec.model_copy(
            update={
                "task_id": f"{spec.task_id}-{rid}-{report.report_id}"[:120],
                "interaction_id": iid,
            }
        )
        tasks[spec.task_id] = {
            "spec": spec.model_dump(mode="json"),
            "status": "pending",
            "robot_id": None,
            "refused_by": [],
            "posture": Posture.INTERACTIVE.value,
            "pinned_to": rid,
            "bundle": None,
            "attempts": 0,
            "repeat": False,
            "requested_by": f"person via {rid}",
            "interaction_holder": rid,
        }
        run.decide(
            DecisionKind.DECOMPOSE,
            {"interaction_id": iid, "task": spec.task_id, "pinned_to": rid, "destination": ident},
            policy={"rule": "human_request (SPEC A3.4.4): escort of the person speaking"},
            justification=f"a person asked {rid} for guidance",
            inputs_ref=f"report:{report.report_id}",
        )
        text = f"🗣️ A person asked {narr.robot(rid, fleet)} for guidance: it will {narr.task(spec)}."
        if cfg.show_constraints_to_operator and deps.interactions is not None:
            # Development only: off in the real profile.
            try:
                content = (await deps.interactions.get(iid))["content"]
            except Exception:
                content = {}
            said = content.get("declared_constraint")
            text += (
                f" *(development view)* Constraint: “{said}” → profile "
                f"`{content.get('profile') or 'default'}`."
                if said
                else " *(development view)* No constraint declared."
            )
        run.note(text)
        return "allocate"

    async def _interaction_end(
        run: _Run,
        report: Report,
        tasks: dict[str, dict[str, Any]],
        fleet: dict[str, dict[str, Any]],
        dirty: list[str],
    ) -> str:
        """The interaction ended: close the escort, delete the store entry, and keep only
        ids, timestamps and booleans (personal data is forgotten)."""
        p = report.payload
        rid = report.robot_id
        iid = p.get("interaction_id")
        outcome = p.get("outcome") or "failed"
        for t in tasks.values():
            if t["spec"].get("interaction_id") == iid and t["status"] in OPEN:
                if t.get("robot_id") and t["robot_id"] not in dirty:
                    dirty.append(t["robot_id"])
                t.update(status="done", outcome=outcome)
        returned = []
        for tid, t in sorted(tasks.items()):
            # The robot goes back to the survey it left: the remainder
            # nobody has started yet is its own again.
            if t.get("released_by") == rid and t["status"] in ("pending", "assigned"):
                if t.get("robot_id") and t["robot_id"] not in dirty:
                    dirty.append(t["robot_id"])
                t.update(status="pending", robot_id=None, pinned_to=rid, refused_by=[])
                t.pop("released_by")
                returned.append(tid)
        deleted = False
        if deps.interactions is not None:
            try:
                deleted = await deps.interactions.delete(iid)
            except Exception:
                deleted = False  # the TTL removes it
        metrics = {
            k: p.get(k)
            for k in (
                "request_to_confirmation_s",
                "request_to_departure_s",
                "p7_round_trip_s",
                "route_compliant",
                "handovers",
                "repeated_requests",
            )
        }
        run.decide(
            DecisionKind.FORGET,
            {
                "interaction_id": iid,
                "robot_id": rid,
                "outcome": outcome,
                "deleted": deleted,
                **metrics,
            },
            policy={"rule": "rule 28: the interaction ended; the store entry is deleted"},
            justification="interaction ended: content deleted, ids and metrics kept",
            inputs_ref=f"report:{report.report_id}",
        )
        compliant = metrics["route_compliant"]
        route = (
            ""
            if compliant is None
            else "; route compliant"
            if compliant
            else "; ⚠️ route NOT compliant"
        )
        words = {
            "arrived": "arrived",
            "concluded_at_shelter": "concluded at a shelter",
            "ended_by_person": "ended by the person",
            "failed": "could not be completed",
        }.get(outcome, outcome)
        run.note(
            f"🧭 {narr.robot(rid, fleet)} finished guiding a visitor: {words}{route}."
            + (
                f" It goes back to {join(narr.task_id(tasks, t) for t in returned)}."
                if returned
                else ""
            )
        )
        if returned:
            return "allocate"
        return "publish" if dirty else "audit"

    async def _handover(
        run: _Run,
        entry: dict[str, Any],
        holder: str,
        to: str,
        states: dict[str, Any],
        fleet: dict[str, dict[str, Any]],
    ) -> None:
        """Another robot completes the escort: the context passes through
        the InteractionStore; the person is not asked again."""
        iid = entry["spec"]["interaction_id"]
        content: dict[str, Any] = {}
        if deps.interactions is not None:
            try:
                current = (await deps.interactions.get(iid))["content"]
                content["handovers"] = int(current.get("handovers") or 0) + 1
                if holder in states:  # the person waits where the last robot left them
                    pose = states[holder].pose
                    content["meeting_pose"] = {"x": pose.x, "y": pose.y}
                await deps.interactions.update(iid, content=content, participants=[to])
            except Exception:
                content["store"] = "unavailable"
        run.decide(
            DecisionKind.REALLOCATE,
            {"interaction_id": iid, "task_id": entry["spec"]["task_id"], "from": holder, "to": to},
            policy={"rule": "escort hand-over through the InteractionStore (SPEC A3.4.6)"},
            justification="the escort continues with another robot; the person is not asked again",
        )
        run.note(
            f"🤝 {narr.robot(to, fleet)} continues guiding the visitor that "
            f"{narr.robot(holder, fleet)} was escorting."
        )

    def _remainder(
        run: _Run,
        tid: str,
        start: int,
        rid: str,
        tasks: dict[str, dict[str, Any]],
        fleet: dict[str, dict[str, Any]],
        dirty: list[str],
        reverse: bool = False,
    ) -> str:
        """A robot left a survey for a person's escort: the uncovered remainder of the
        sector becomes a new survey task for the other robots."""
        entry = tasks[tid]
        spec = _spec(entry)
        entry.update(status="released", robot_id=None)
        if rid not in dirty:
            dirty.append(rid)
        sector = cfg.campus.sector(spec.target.sector_id)  # type: ignore[union-attr]
        total = len(sector.survey_waypoints) or 1
        new_id = None
        if start < total:
            new_id = f"{tid}-rem{start}"[:120]
            target = SectorTarget(
                sector_id=sector.sector_id, from_waypoint=start, reverse=reverse
            )  # the rest of the route, in the direction the robot was running it
            share = (total - start) / total
            rem = spec.model_copy(
                update={
                    "task_id": new_id,
                    "target": target,
                    "depends_on": [],
                    "est_duration_s": round(spec.est_duration_s * share, 1),
                }
            )
            tasks[new_id] = {
                "spec": rem.model_dump(mode="json"),
                "status": "pending",
                "robot_id": None,
                "refused_by": [rid],  # the other robots: it is guiding a person
                "posture": entry.get("posture", "routine"),
                "pinned_to": None,
                "bundle": None,
                "attempts": 0,
                "repeat": bool(entry.get("repeat")),
                "lap_of": entry.get("lap_of") or tid,
                "lap": entry.get("lap", 1),
                "remainder_of": tid,
                "released_by": rid,
                "lap_pin": entry.get("pinned_to") or entry.get("lap_pin"),
            }
        elif entry.get("repeat"):
            _next_lap(tasks, tid)
        run.decide(
            DecisionKind.REALLOCATE,
            {"released": tid, "by": rid, "remainder": new_id, "from_waypoint": start},
            policy={"rule": "remainder survey to the fleet (SPEC A3.4.4)"},
            justification=f"{rid} left {tid} for a person's escort",
            inputs_ref=f"robot:{rid}",
        )
        run.note(
            f"🔀 {narr.robot(rid, fleet)} left {narr.task_id(tasks, tid)} to guide a visitor; "
            + (
                f"the rest of {sector.name or sector.sector_id} goes to another robot."
                if new_id
                else "the sector was already covered."
            )
        )
        return "allocate"

    async def _ingest_report(
        run: _Run,
        report: Report,
        tasks: dict[str, dict[str, Any]],
        fleet: dict[str, dict[str, Any]],
        dirty: list[str],
        upd: dict[str, Any],
    ) -> str:
        p = report.payload
        rid = report.robot_id
        if report.kind is ReportKind.SKILL_RESULT:
            entry = tasks.get(p.get("task_id"))
            if entry is None or entry["status"] not in OPEN:
                return "audit"
            outcome = p["result"]["outcome"]
            owner = entry.get("robot_id")
            if outcome == "succeeded":
                # Done is done, whoever did it; a re-assignee drops it from its envelope.
                entry.update(status="done", robot_id=rid)
                if owner and owner != rid:
                    dirty.append(owner)
                if entry.get("repeat"):
                    lap = _next_lap(tasks, p["task_id"])
                    if run.state.get("posture") == Posture.EMERGENCY.value:
                        # Reported late (out of contact when the alert came): the next lap
                        # waits for the all clear like the rest of the routine work.
                        tasks[lap]["status"] = "preempted"
                        if rid not in dirty:
                            dirty.append(rid)  # its envelope drops the lap it started
                        run.note(
                            f"🔁 {narr.robot(rid, fleet)} {narr.task_done(_spec(entry))} "
                            "(reported late); its next lap waits for the all clear."
                        )
                        return "publish"
                    # The robot started that lap itself: it stays with it, as
                    # running when it had nothing else, else queued (rebalancing may move it).
                    # (Its queued report may arrive before the "recovered" event: lost too.)
                    if fleet.get(rid, {}).get("status") in (ACTIVE_ROBOT, "lost") and tasks[
                        lap
                    ].get("pinned_to") in (None, rid):
                        busy = any(
                            t.get("robot_id") == rid and t["status"] in OPEN and tid != lap
                            for tid, t in tasks.items()
                        )
                        tasks[lap].update(
                            robot_id=rid, status="assigned" if busy else "in_progress"
                        )
                        if rid not in dirty:
                            dirty.append(rid)  # its envelope lists the lap it runs
                    run.note(
                        f"🔁 {narr.robot(rid, fleet)} {narr.task_done(_spec(entry))} "
                        f"(lap {tasks[lap]['lap'] - 1}); patrolling continues."
                    )
                    return "allocate"
                run.note(f"✅ {narr.robot(rid, fleet)} {narr.task_done(_spec(entry))}.")
                return "publish" if owner and owner != rid else "audit"
            if owner not in (rid, None):
                return "audit"  # a stale failure from a robot that no longer owns it
            entry["attempts"] = entry.get("attempts", 0) + 1
            entry.setdefault("refused_by", [])
            if rid not in entry["refused_by"]:
                entry["refused_by"].append(rid)
            if outcome == "cancelled":
                return "audit"  # preemption or hold: the robot or arbitration owns it
            if entry["attempts"] >= 3:
                entry["status"] = "failed"
                run.note(
                    f"❌ I gave up on '{narr.task_id(tasks, p['task_id'])}' after 3 failed "
                    f"attempts (last: {outcome.replace('_', ' ')})."
                )
                return "audit"
            entry.update(status="pending", robot_id=None)
            if rid not in dirty:
                dirty.append(rid)
            run.note(
                f"{narr.robot(rid, fleet)} couldn't {narr.task_id(tasks, p['task_id'])} "
                f"({outcome.replace('_', ' ')}); I'll give it to another robot."
            )
            upd["reallocating"] = True
            return "allocate"
        if report.kind is ReportKind.TASK_STATUS:
            entry = tasks.get(p.get("task_id"))
            if entry and p.get("status") == "started" and entry.get("robot_id") == rid:
                entry["status"] = "in_progress"
                if p.get("reverse") is not None:  # the direction the lap runs (next lap)
                    entry["reverse"] = p["reverse"]
            if (
                entry
                and p.get("status") == "released"
                and entry.get("robot_id") == rid
                and entry["status"] in OPEN
            ):
                return _remainder(
                    run,
                    p["task_id"],
                    int(p.get("remainder_from") or 0),
                    rid,
                    tasks,
                    fleet,
                    dirty,
                    reverse=bool(p.get("remainder_reverse")),
                )
            if (
                entry
                and p.get("outcome") == "skipped"
                and entry.get("robot_id") == rid
                and entry["status"] in OPEN
            ):
                # The robot's validated replan skipped it: someone else does it.
                entry.setdefault("refused_by", [])
                if rid not in entry["refused_by"]:
                    entry["refused_by"].append(rid)
                entry.update(status="pending", robot_id=None)
                if entry.get("pinned_to") == rid:
                    entry["pinned_to"] = None
                if rid not in dirty:
                    dirty.append(rid)
                run.note(
                    f"{narr.robot(rid, fleet)} skipped '{narr.task_id(tasks, p['task_id'])}' "
                    f"({p.get('reason') or 'its own decision'}); I'll give it to another robot."
                )
                return "allocate"
            return "audit"
        if report.kind is ReportKind.HUMAN_REQUEST:
            return await _human_request(run, report, tasks, fleet)
        if report.kind is ReportKind.INTERACTION_END:
            return await _interaction_end(run, report, tasks, fleet, dirty)
        if report.kind is ReportKind.ENVELOPE_STATUS and p.get("status") in ("idle", "completed"):
            # "completed": the robot's envelope has nothing left (after a lap whose next
            # lap went to another robot, say): it is as idle as one that asked.
            if rid in fleet and fleet[rid]["status"] == ACTIVE_ROBOT:
                fleet[rid]["idle"] = True
                if p.get("pose"):
                    fleet[rid]["idle_pose"] = p["pose"]
                if p.get("battery_pct") is not None:
                    fleet[rid]["idle_battery_pct"] = p["battery_pct"]
            return "publish" if _rebalance(run, tasks, fleet, dirty) else "audit"
        if report.kind is ReportKind.ENVELOPE_STATUS and p.get("status") == "recharging":
            return _recharging(run, rid, p, tasks, fleet, dirty)
        if report.kind is ReportKind.ENVELOPE_STATUS and p.get("status") == "charged":
            if rid in fleet and fleet[rid]["status"] == "charging":
                fleet[rid]["status"] = ACTIVE_ROBOT
            if rid in fleet:  # charged: it may well be idle now, with a full battery
                fleet[rid]["idle_battery_pct"] = p.get("battery_pct")
                fleet[rid]["idle_pose"] = cfg.campus.staging.model_dump()
            for t in tasks.values():
                # Its battery refusals end with the charge (a robot at 100 % is not barred
                # from a lap it declined low or charging).
                if (t.get("refused_for") or {}).get(rid) == "battery":
                    t["refused_by"] = [r for r in t.get("refused_by") or [] if r != rid]
                    del t["refused_for"][rid]
            resumed = []
            for tid, t in sorted(tasks.items()):
                if t["status"] == "paused" and t.get("resume_on") == rid:
                    t.update(status="pending", robot_id=None, pinned_to=rid, resume_on=None)
                    resumed.append(tid)
            run.note(
                f"🔋 {narr.robot(rid, fleet)} is charged ({p.get('battery_pct', '?')} %) and back "
                "at work"
                + (
                    f": it resumes {join(narr.task_id(tasks, t) for t in resumed)}."
                    if resumed
                    else "."
                )
            )
            return "allocate"
        if report.kind is ReportKind.OBSERVATION:
            obs = Observation.model_validate(p)
            if obs.subject:
                await _status_from_observation(run, obs)
            store = _store()
            if store is not None:
                await store.aput(
                    ("a6g", "ltm", "observations"), obs.obs_id, obs.model_dump(mode="json")
                )
            if deps.kb is not None:  # "what was seen near here", best effort
                with contextlib.suppress(Exception):
                    await deps.kb.add_observation(obs.model_dump(mode="json"), run.mission_id)
            run.note(
                f"👁️ {narr.robot(rid, fleet)} spotted **{obs.label}** "
                f"({obs.confidence:.0%} confident)."
                + (" I need you to confirm it." if obs.requires_confirmation else "")
            )
            if obs.requires_confirmation:
                seq = (run.state.get("escalation_seq") or 0) + 1
                upd["escalation_seq"] = seq
                upd["escalations"] = [
                    *(run.state.get("escalations") or []),
                    {
                        "esc_id": f"esc-{seq}",
                        "kind": "observation",
                        "observation": obs.model_dump(mode="json"),
                    },
                ]
            return "audit"
        if report.kind is ReportKind.DECISION and p.get("kind") == "award":
            award = (p.get("output") or {}).get("award")
            if award and (
                await _claim(run, award["task_id"], award["awarded_to"], tasks, fleet, dirty)
                != award["awarded_to"]
            ):
                return "publish"  # another robot keeps it; the awardee drops it
            if award:
                moved = _apply_award(tasks, award, dirty)
                if moved and moved[1] == moved[2]:
                    return "audit"  # it already had it ("took over ... from itself")
                if moved:
                    run.note(
                        "🔁 While I was unreachable, the robots reassigned work among "
                        f"themselves: {describe_move(tasks, moved)}."
                    )
                    return "publish"
            return "audit"
        if report.kind is ReportKind.RECONCILE:
            moved = []
            for award in p.get("awards_made", []):
                to = award["awarded_to"]
                if await _claim(run, award["task_id"], to, tasks, fleet, dirty) != to:
                    continue
                m = _apply_award(tasks, award, dirty)
                if m:
                    moved.append(describe_move(tasks, m))
            for tid, t in (p.get("tasks") or {}).items():
                entry = tasks.get(tid)
                if entry is None:
                    continue
                if t["status"] == "succeeded" and entry["status"] in OPEN:
                    entry.update(status="done", robot_id=entry.get("robot_id") or rid)
                elif t["status"] == "dispatched" and entry["status"] in OPEN:
                    if await _claim(run, tid, rid, tasks, fleet, dirty) == rid:
                        entry.update(status="in_progress", robot_id=rid)
                elif t["status"] == "pending" and entry["status"] == "pending":
                    entry.update(status="assigned", robot_id=rid)
            if rid in fleet and fleet[rid]["status"] != ACTIVE_ROBOT:
                fleet[rid]["status"] = ACTIVE_ROBOT
            if rid not in dirty:
                dirty.append(rid)
            run.note(
                f"🔄 {narr.robot(rid, fleet)} reported in and I've caught up with its progress."
                + (f" While I was away the robots reassigned: {'; '.join(moved)}." if moved else "")
            )
            run.decide(
                DecisionKind.FALLBACK,
                {"robot_id": rid, "moves": moved},
                policy={"rule": "reconcile_after_coordinator_loss"},
                justification="coordinator back: adopt robot-side progress and awards",
                inputs_ref=f"report:{report.report_id}",
            )
            return "publish"
        return "audit"

    def route_ingest(state: CoordState) -> str:
        return {
            "decompose": "decompose",
            "allocate": "allocate",
            "publish": "reconcile",
        }.get(state.get("branch", "audit"), "audit")

    # ---------------------------------------------------------- decompose / validate / arbitrate

    def _share_patrols(
        new: list[TaskSpec],
        pinned: dict[str, str],
        tasks: dict[str, dict[str, Any]],
        fleet: dict[str, dict[str, Any]],
        text: str,
        additive: bool,
    ) -> str | None:
        """ "go2_01 patrol (again)" with no sector named, while the others already patrol:
        the robot joins with its fair share (its own sector first), instead of taking every
        sector from the robots patrolling them. Unpins the rest of ``new`` in place."""
        robots = {pinned.get(t.task_id) for t in new}
        if (
            additive
            or len(new) < 2
            or len(robots) != 1
            or None in robots
            or any(t.kind is not TaskKind.SURVEY_SECTOR for t in new)
            or any(kind == "sector" for kind, _ in mentions(text, cfg.campus))
        ):
            return None
        robot = next(iter(robots))
        holder: dict[str, str | None] = {}  # sector -> robot patrolling it now
        lap_pin: dict[str, str | None] = {}
        for t in tasks.values():
            if t["status"] in OPEN and t["spec"]["kind"] == TaskKind.SURVEY_SECTOR.value:
                sector = t["spec"]["target"]["sector_id"]
                holder[sector] = t.get("robot_id") or t.get("pinned_to")
                lap_pin[sector] = t.get("lap_pin") or t.get("pinned_to")
        sectors = {t.task_id: t.target.sector_id for t in new}  # type: ignore[union-attr]
        others = {h for h in holder.values() if h and h != robot}
        if not others:
            return None  # nobody else patrols: the named robot patrols everything
        active = [r for r, e in fleet.items() if e.get("status") == ACTIVE_ROBOT] or [robot]
        share = max(1, -(-len(new) // len(active)))  # ceil
        load = Counter(holder.get(s) for s in sectors.values())
        take = [tid for tid, s in sectors.items() if holder.get(s) == robot]
        order = sorted(
            (tid for tid in sectors if tid not in take),
            key=lambda tid: (
                holder.get(sectors[tid]) is not None,  # unpatrolled sectors first
                lap_pin.get(sectors[tid]) != robot,  # then the robot's own sector
                -load[holder.get(sectors[tid])],  # then from the busiest robot
                tid,
            ),
        )
        for tid in order:
            if len(take) >= share:
                break
            h = holder.get(sectors[tid])
            if h is not None and load[h] <= 1 and len(take) > 0:
                continue  # never leave a patrolling robot without its only sector
            take.append(tid)
            load[h] -= 1
        for tid in sectors:
            if tid not in take:
                pinned.pop(tid, None)
        names = join(cfg.campus.sector(sectors[t]).name or sectors[t] for t in take)
        return f"🔁 {robot} rejoins the patrol with {names}; the other robots keep theirs."

    POINT_KINDS = {TaskKind.HOLD_POST.value, TaskKind.INSPECT_POI.value, TaskKind.GO_TO.value}

    def _point_first(
        run: _Run,
        tasks: dict[str, dict[str, Any]],
        given: list[str],
        dirty: list[str],
        fleet: dict[str, Any],
    ) -> list[str]:
        """A post, inspection or trip given to a robot by name outranks its routine work
        ("Send go2_02 to hold the bus stop" does not queue behind its patrol lap): the robot
        goes now and its other routine work, pinned to it earlier or not, goes to the
        rest of the fleet. Escorts and emergency work stay. Returns the released tasks."""
        # escorts; trips home; posts (a routine post is released by the order itself)
        stay = {k.value for k in cfg.allocator.non_preemptible_kinds} | {
            TaskKind.RETURN_HOME.value,
            TaskKind.HOLD_POST.value,
        }
        released: list[str] = []
        for tid in given:
            t = tasks[tid]
            robot = t.get("pinned_to")
            posture = Posture(t.get("posture") or "routine")
            if (
                not robot
                or t["spec"]["kind"] not in POINT_KINDS
                or posture is Posture.EMERGENCY
                or t["status"] not in (*OPEN, "new")
            ):
                continue
            level = max(_spec(t).priority, POSTURE_PRIORITY[posture] + 10)
            t["spec"]["priority"] = level
            t["point_first"] = True  # the allocator keeps lower work off the robot meanwhile
            mine = []
            for oid, o in sorted(tasks.items()):
                if (
                    oid not in given
                    and o["status"] in (*OPEN, "new")
                    and robot in (o.get("robot_id"), o.get("pinned_to"))
                    and o["spec"]["kind"] not in stay
                    and o.get("posture") != Posture.EMERGENCY.value
                    and _spec(o).priority < level
                ):
                    if o.get("robot_id") and o["robot_id"] not in dirty:
                        dirty.append(o["robot_id"])
                    o.update(pinned_to=None, bundle=None, plan_group=None, ordered_for=None)
                    if o["status"] != "new":
                        o.update(status="pending", robot_id=None)
                    mine.append(oid)
            if robot not in dirty:
                dirty.append(robot)
            if mine:
                released += mine
                run.note(
                    f"{narr.robot(robot, fleet)} will {narr.task_id(tasks, tid)} now; the "
                    "other robots take over its other work: "
                    f"{join(narr.task_id(tasks, o) for o in mine)}."
                )
        return released

    def _add_planned(
        planned: list[agentic.NewTask],
        tasks: dict[str, dict[str, Any]],
        posture: Posture,
        open_keys: set[tuple[str, str]],
        status: str = "new",
        order: str | None = None,
    ) -> tuple[list[str], list[str]]:
        """Model-first plan tasks into the twin: (added, already open)."""
        added, covered = [], []
        for nt in planned:
            key = (nt.spec.kind.value, _target_label(nt.spec))
            if key in open_keys and not nt.pinned_to and not (nt.bundle or "").startswith("copy:"):
                covered.append(nt.spec.task_id)  # already open: not asked twice
                continue
            tasks[nt.spec.task_id] = {
                "spec": nt.spec.model_dump(mode="json"),
                "status": status,
                "robot_id": None,
                "refused_by": [],
                "posture": posture.value,
                "pinned_to": nt.pinned_to,
                "bundle": nt.bundle,
                # kept after allocation (the bundle is consumed): the check may not split it
                "plan_group": nt.bundle if (nt.bundle or "").startswith("group:") else None,
                "excluded": nt.excluded,
                "attempts": 0,
                "repeat": nt.repeat,
                "order": order,  # the order that planned it (its robot is not protected)
                "named_by": nt.pinned_to,  # the robot the plan named for it
            }
            added.append(nt.spec.task_id)
        return added, covered

    def _context(state: CoordState, posture: Posture, fleet: dict[str, Any]) -> agentic.Context:
        tasks = state.get("tasks") or {}
        return agentic.Context(
            campus=cfg.campus,
            posture=posture,
            robots={
                rid: {
                    "platform": e.get("platform", ""),
                    "status": e.get("status", ""),
                    "capabilities": [c.value for c in _manifest(e).capabilities],
                }
                for rid, e in fleet.items()
            },
            open_tasks=[
                {
                    "task_id": tid,
                    "kind": t["spec"]["kind"],
                    "target_id": (_target_key(_spec(t).target, cfg.campus) or ("", ""))[1],
                    "robot_id": t.get("robot_id") or t.get("pinned_to"),
                    "status": t["status"],
                    "posture": t.get("posture"),
                }
                for tid, t in sorted(tasks.items())
                if t["status"] in OPEN
            ],
        )

    async def _plan_in_time(
        ctx: agentic.Context, text: str
    ) -> tuple[agentic.AgentPlan | None, LLMTrace, list[str]]:
        """The careful plan within ``careful_deadline_s`` (wall clock)."""
        deadline = cfg.fleet.defaults.coordinator.careful_deadline_s
        try:
            async with asyncio.timeout(deadline):
                return await agentic.plan(deps.model, ctx, text)
        except TimeoutError:
            trace = LLMTrace(latency_s=deadline)
            trace.used_fallback = True
            return None, trace, [f"no plan within {deadline:g} s"]

    def _apply_plan(
        run: _Run,
        tasks: dict[str, dict[str, Any]],
        plan: agentic.AgentPlan,
        ctx: agentic.Context,
        posture: Posture,
        dirty: list[str],
        fleet: dict[str, Any],
        text: str,
        status: str = "new",
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        """A validated plan into the twin (the careful path, model-first, an approved fix):
        ends, reassignments, new tasks; a robot given work by name leaves its routine post
        and its other routine work. Returns (ended, reassigned, added, already open)."""

        def mark(rid: str | None) -> None:
            if rid and rid not in dirty:
                dirty.append(rid)

        stopped, reassigned, added, covered = [], [], [], []
        for tid in plan.end:
            t = tasks[tid]
            mark(t.get("robot_id") or t.get("pinned_to"))
            t.update(status="cancelled", detail="ended by the operator")
            stopped.append(tid)
        for r in plan.reassign:
            t = tasks[r.task_id]
            if t["status"] not in OPEN:
                continue
            holder = t.get("robot_id")
            t.update(pinned_to=r.robot_id, bundle=None)
            if r.robot_id is not None:
                t["refused_by"] = [x for x in t.get("refused_by") or [] if x != r.robot_id]
            elif holder:  # back to the fleet: someone else
                t.setdefault("refused_by", []).append(holder)
            if holder != r.robot_id:
                mark(holder)
                t.update(status="pending", robot_id=None)
            reassigned.append(r.task_id)
            t["ordered_for"] = r.robot_id  # what the order said
        open_keys = {
            (t["spec"]["kind"], _target_label(_spec(t)))
            for t in tasks.values()
            if t["status"] in OPEN
        }
        a, c = _add_planned(
            agentic.to_tasks(plan, ctx, set(tasks)), tasks, posture, open_keys, status, text
        )
        added += a
        covered += c
        for tid in a:
            tasks[tid]["ordered_for"] = tasks[tid].get("named_by")
        # A robot given work by name leaves its routine post (held until released, the post
        # never ends: the new work would queue behind it for good).
        named = {tasks[t].get("pinned_to") for t in added} | {
            r.robot_id for r in plan.reassign if r.robot_id
        }
        for tid, t in sorted(tasks.items()):
            holder = t.get("robot_id") or t.get("pinned_to")
            if (
                tid not in added
                and holder in named
                and t["status"] in OPEN
                and t["spec"]["kind"] == TaskKind.HOLD_POST.value
                and t["spec"].get("until_cleared")
                and t.get("posture") != Posture.EMERGENCY.value
            ):
                mark(holder)
                t.update(status="cancelled", detail="released by a new order")
                run.note(
                    f"{narr.robot(holder, fleet)} stops "
                    f"{narr.task_id(tasks, tid).replace('stand guard', 'standing guard')} "
                    "for its new work."
                )
        _point_first(run, tasks, added, dirty, fleet)
        return stopped, reassigned, added, covered

    async def _model_first(
        run: _Run,
        state: CoordState,
        ev: dict[str, Any],
        posture: Posture,
        fleet: dict[str, Any],
        text: str,
        mode: str,
        started: float,
        check: str = "after",
        ask: bool = False,
    ) -> dict[str, Any] | None:
        """``understanding: model_first`` (and the rules-first careful path): the model's
        validated plan (agentic.py) becomes the mission's work; None: rejected twice or no
        answer (the rules plan instead). ``check``: ``after`` publication (its own run) or
        ``before`` it (the careful path: the robots move once it is checked)."""
        ctx = _context(state, posture, fleet)
        _protect(ctx, text, fleet)
        early = ev.get("agent_plan") or {}
        same_posture = early.get("posture") == posture.value or Posture.EMERGENCY.value not in (
            early.get("posture"),
            posture.value,
        )
        if early and same_posture and early.get("plan"):  # made while understanding
            plan = agentic.AgentPlan.model_validate(early["plan"])
            trace = LLMTrace(latency_s=0.0)
            errors = agentic.validate(plan, ctx)
            if errors:  # the state moved meanwhile: plan again
                plan, trace, errors = await _plan_in_time(ctx, text)
        elif early and same_posture:  # it was rejected twice already
            plan, trace, errors = None, LLMTrace(), list(early.get("errors") or [])
        else:
            plan, trace, errors = await _plan_in_time(ctx, text)
        run.thoughts("plan_mission", trace)
        if plan is None:
            run.careful_errors = errors  # type: ignore[attr-defined]
            if errors and errors[0].startswith("no plan within") and not ask:
                run.note(
                    f"⏱️ I couldn't think this order through within {errors[0][15:]}, so I "
                    "used my quick reading. If the result is not what you meant, say it more "
                    "simply or in two orders."
                )
            run.decide(
                DecisionKind.FALLBACK,
                {"errors": errors[:10]},
                policy={"rule": "fallback_rejected", "understanding": "model_first"},
                justification="the model's plan was rejected twice: rule-based planning",
                inputs_ref=f"goal:{posture.value}",
            )
            return None
        tasks = copy.deepcopy(state.get("tasks") or {})
        dirty = list(state.get("dirty") or [])

        stopped, reassigned, added, covered = _apply_plan(
            run, tasks, plan, ctx, posture, dirty, fleet, text
        )
        run.decide(
            DecisionKind.DECOMPOSE,
            {
                "goal": ev.get("text"),
                "tasks": added,
                "ended": stopped,
                "reassigned": reassigned,
                "skipped_existing": covered,
                "plan": agentic.dumps(plan),
            },
            policy={
                "posture": posture.value,
                "method": "llm",
                "understanding": "model_first",
                "decompose_mode": mode,
                "phase": ev.get("phase") or "single",
            },
            justification=plan.summary or "model-first plan",
            inputs_ref=f"goal:{posture.value}",
        )
        for tid in stopped:
            holder = tasks[tid].get("robot_id") or tasks[tid].get("pinned_to")
            run.note(
                f"⏹️ {narr.robot(holder, fleet) + ' stops' if holder else 'Stopped'}: "
                f"{narr.task_id(tasks, tid)}."
            )
        for tid in reassigned:
            to = tasks[tid].get("pinned_to")
            run.note(
                f"🔀 {narr.task_id(tasks, tid).capitalize()} goes to "
                + (narr.robot(to, fleet) if to else "another robot")
                + "."
            )
        if added:
            run.note(narr.plan(posture.value, [_spec(tasks[t]) for t in added]))
        if plan.reply.strip():
            run.note(plan.reply.strip())
        elif not (added or stopped or reassigned):
            run.note(
                narr.plan(posture.value, [], covered=[narr.task_id(tasks, t) for t in covered])
            )
        run.finish(
            "plan_mission",
            {"goal": ev.get("text"), "posture": posture.value},
            {
                "method": "LLM, model-first (structured output, validated)",
                "new_tasks": [narr.task(_spec(tasks[t])) for t in added],
                "ended": [narr.task_id(tasks, t) for t in stopped],
                "reassigned": [narr.task_id(tasks, t) for t in reassigned],
                **({"llm_latency": fmt_latency(trace.latency_s)} if trace.latency_s else {}),
            },
            time.perf_counter() - started,
        )
        if check == "before" and (added or reassigned):
            return run.out(
                tasks=tasks, new_tasks=added, dirty=dirty, pending_call=None, order_check=text
            )
        if cfg.fleet.defaults.coordinator.plan_check and added:
            # The check runs as its own run, queued behind this one: the robots get their
            # envelopes first, a correction follows (the check takes seconds).
            submit = deps.submit or _sdk_submit(cfg.fleet.defaults.coordinator.assistant_id)
            try:
                await submit(state["mission_id"], {"type": "order_check", "text": text})
            except Exception as exc:
                log.warning("queueing the assignment check failed: %s", exc)
        return run.out(tasks=tasks, new_tasks=added, dirty=dirty, pending_call=None)

    async def _ask_operator(
        run: _Run,
        state: CoordState,
        ev: dict[str, Any],
        posture: Posture,
        fleet: dict[str, Any],
        robots: dict[str, str],
        idle: list[str],
        low: list[str],
    ) -> dict[str, Any]:
        """The careful path could not decide (no plan within the deadline, or rejected
        twice): the operator decides, as for ending an emergency. They see the
        quick reading and approve it, rephrase the order, or drop it."""
        text = ev.get("text") or ""
        quick, pinned, _, _, _, _ = await decompose_goal(
            text, posture, cfg.campus, None, low, robots, idle, kb=deps.kb
        )
        why = (getattr(run, "careful_errors", None) or ["no valid plan"])[0]
        seq = (state.get("escalation_seq") or 0) + 1
        esc = {
            "esc_id": f"esc-{seq}",
            "kind": "unclear_order",
            "text": text,
            "posture": posture.value,
            "why": why,
            # the quick reading robot by robot, not only the tasks
            "quick": [
                f"{narr.robot(who, fleet) if who in fleet else 'any robot'}: {narr.task(t)}"
                for t in quick
                for who in [pinned.get(t.task_id)]  # a robot id, a bundle mark or None
            ],
        }
        run.note(
            f"🤷 I couldn't work out *{text}* ({why}). Please decide below: carry out my "
            "quick reading, rephrase the order, or drop it."
        )
        run.decide(
            DecisionKind.ESCALATE,
            {"escalation": esc},
            policy={"rule": "careful path undecided: the operator decides"},
            justification=why,
            inputs_ref=f"goal:{posture.value}",
        )
        return run.out(
            new_tasks=[],
            pending_call=None,
            escalation_seq=seq,
            escalations=[*(state.get("escalations") or []), esc],
        )

    async def decompose(state: CoordState) -> dict[str, Any]:
        run = _Run(deps, state)
        ev = state.get("event") or {}
        posture = Posture(ev.get("posture") or state.get("posture") or "routine")
        fleet = state.get("fleet") or {}
        low: list[str] = []
        if posture is Posture.EMERGENCY:
            states, _ = await _robot_states(deps, fleet)
            floor = cfg.allocator.default_battery_floor_pct + cfg.allocator.battery_margin_pct
            low = [
                r
                for r, s in states.items()
                if _manifest(fleet[r]).battery_reliable
                and s.battery_pct is not None
                and s.battery_pct < floor
            ]
        started = time.perf_counter()
        robots = {rid: e.get("platform", "") for rid, e in fleet.items()}
        busy = {
            t.get("robot_id") or t.get("pinned_to")
            for t in (state.get("tasks") or {}).values()
            if t["status"] in OPEN
        }
        idle = [r for r, e in fleet.items() if e.get("status") == ACTIVE_ROBOT and r not in busy]
        text = ev.get("text", posture.value)
        kb_log: list[str] = []
        # Fast path (emergency reaction time): on template_then_model, the rule-based plan goes
        # out now; the model's decomposition follows as its own run and can only add or
        # adjust tasks (the same validate, allocate and reconcile path).
        mode = cfg.fleet.defaults.coordinator.decompose_mode.get(posture.value, "model")
        fast = mode == "template_then_model" and ev.get("phase") != "model"
        planner = None if mode == "template" or fast else deps.model
        current = [
            {
                "task_id": tid,
                "kind": t["spec"]["kind"],
                "target_id": (_target_key(_spec(t).target, cfg.campus) or ("", ""))[1],
                "robot_id": t.get("robot_id") or t.get("pinned_to"),
                "status": t["status"],
            }
            for tid, t in sorted((state.get("tasks") or {}).items())
            if t["status"] in OPEN
        ]
        ended: list[str] = []
        if cfg.fleet.defaults.coordinator.understanding == "model_first" and planner is not None:
            out = await _model_first(run, state, ev, posture, fleet, text, mode, started)
            if out is not None:
                return out
            planner = None  # the model's plan was rejected twice: the rules decide
        careful_text = text
        if ev.get("previous_order"):
            careful_text = f"Previous order: {ev['previous_order']}\nNow: {text}"
        if ev.get("quick_only"):  # the operator approved the quick reading
            planner = None
        if ev.get("careful") and planner is not None:
            # The careful path (rules-first): an order with structure the rules
            # do not handle is planned by the model in the full format, validated, and its
            # assignment checked before the robots move.
            out = await _model_first(
                run, state, ev, posture, fleet, careful_text, mode, started, "before", True
            )
            if out is not None:
                return out
            return await _ask_operator(run, state, ev, posture, fleet, robots, idle, low)
        new, pinned, used_template, summary, trace, notes = await decompose_goal(
            text,
            posture,
            cfg.campus,
            planner,
            low,
            robots,
            idle,
            kb=deps.kb,
            kb_log=kb_log,
            current=current,
            ended=ended,
        )
        gaps = (
            plan_gaps(text, new, pinned, cfg.campus, robots, ended)
            if deps.model is not None
            and not fast
            and posture is not Posture.EMERGENCY
            and not ev.get("quick_only")
            else []
        )
        if (
            not new
            and not ended
            and deps.model is not None
            and not fast
            and posture is not Posture.EMERGENCY
            and cfg.fleet.defaults.coordinator.understanding == "rules_first"
            and not ev.get("careful")
            and not ev.get("quick_only")
        ):
            # Nothing came out of the quick reading: think it through rather than answer
            # that it cannot be done.
            gaps = ["my quick reading found nothing to plan"]
        if gaps:
            # The quick plan left something out: plan it carefully instead (the quick plan
            # is discarded; nothing of it was written yet).
            run.messages.append(
                AIMessage(
                    "🤔 My quick reading could not handle this order ("
                    + "; ".join(gaps)
                    + "). I'm planning it carefully and checking the result before the robots "
                    "move."
                )
            )
            run.decide(
                DecisionKind.FALLBACK,
                {"gaps": gaps},
                policy={"rule": "careful path: the quick plan left something out"},
                justification="; ".join(gaps),
                inputs_ref=f"goal:{posture.value}",
            )
            out = await _model_first(
                run, state, ev, posture, fleet, careful_text, mode, started, "before", True
            )
            if out is not None:
                return out
            return await _ask_operator(run, state, ev, posture, fleet, robots, idle, low)
        if fast and deps.model is not None:
            submit = deps.submit or _sdk_submit(cfg.fleet.defaults.coordinator.assistant_id)
            try:
                await submit(state["mission_id"], {**ev, "phase": "model"})
            except Exception as exc:  # the template plan stands on its own
                log.warning("queueing the model decomposition failed: %s", exc)
        if "fallback_no_kb" in kb_log:
            run.decide(
                DecisionKind.FALLBACK,
                {"places": "word overlap"},
                policy={"rule": "fallback_no_kb"},
                justification="knowledge server unreachable: places resolved by word overlap",
                inputs_ref="kb_resolve_place",
            )
        bundles = {t: p.split(":", 1)[1] for t, p in pinned.items() if p.startswith("bundle:")}
        pinned = {t: p for t, p in pinned.items() if t not in bundles}
        # "... patrol the campus too": the named robots join work that is already running.
        additive = bool(re.search(r"\b(too|also|as well|in addition|additionally)\b", text.lower()))
        plan_latency = time.perf_counter() - started
        run.thoughts("plan_mission", trace)
        tasks = copy.deepcopy(state.get("tasks") or {})
        added = []
        share_note = _share_patrols(new, pinned, tasks, fleet, text, additive)
        if share_note:
            run.note(share_note)
        covered, moved = [], []
        dirty = list(state.get("dirty") or [])
        stopped = []
        for tid in ended:  # the operator ends open work ("stop holding the east gate")
            t = tasks.get(tid)
            if t is None or t["status"] not in OPEN:
                continue
            if t.get("posture") == Posture.EMERGENCY.value:
                run.note(
                    f"{narr.task_id(tasks, tid).capitalize()} is part of the emergency: it "
                    "ends with the *all clear*."
                )
                continue
            holder = t.get("robot_id") or t.get("pinned_to")
            if holder and holder not in dirty:
                dirty.append(holder)
            t.update(status="cancelled", detail="ended by the operator")
            stopped.append(tid)
            run.note(
                f"⏹️ {narr.robot(holder, fleet) + ' stops' if holder else 'Stopped'}: "
                f"{narr.task_id(tasks, tid)}."
            )

        def release(tid: str) -> None:
            t = tasks[tid]
            if t.get("robot_id") and t["robot_id"] not in dirty:
                dirty.append(t["robot_id"])
            t.update(status="pending", robot_id=None)

        for spec in new:
            robot = pinned.get(spec.task_id)
            key = (spec.kind.value, _target_label(spec))
            if robot and additive:
                mine = any(
                    t["status"] in OPEN
                    and robot in (t.get("robot_id"), t.get("pinned_to"))
                    and (t["spec"]["kind"], _target_label(_spec(t))) == key
                    for t in tasks.values()
                )
                if mine:
                    covered.append(spec.task_id)
                    continue
                if spec.task_id in tasks and tasks[spec.task_id]["status"] in OPEN:
                    spec = spec.model_copy(update={"task_id": f"{spec.task_id}-{robot}"})
                    if spec.task_id in tasks and tasks[spec.task_id]["status"] in OPEN:
                        covered.append(spec.task_id)
                        continue
                pinned[spec.task_id] = robot
            elif spec.task_id in tasks and tasks[spec.task_id]["status"] in OPEN:
                t = tasks[spec.task_id]
                if robot and t.get("pinned_to") != robot:
                    t["pinned_to"] = robot  # the operator moves it to the named robot
                    t["refused_by"] = [r for r in t.get("refused_by") or [] if r != robot]
                    if t.get("robot_id") not in (None, robot):
                        release(spec.task_id)
                    moved.append(spec.task_id)
                else:
                    covered.append(spec.task_id)
                continue  # same work already planned or running: no duplicated tasks
            # open now (after the ends: work the order ended is planned again, e.g. "summit
            # takes over the south plaza patrol" ends it, then plans it for the summit)
            same = sorted(
                tid
                for tid, t in tasks.items()
                if t["status"] in OPEN and (t["spec"]["kind"], _target_label(_spec(t))) == key
            )
            if same and robot and not additive:
                # The same work runs under another id (a patrol on lap 3): the named robot
                # takes it over ("go2_02 patrol the North-west walk").
                if any(
                    robot in (tasks[t].get("robot_id"), tasks[t].get("pinned_to")) for t in same
                ):
                    covered.extend(same)
                else:
                    t = tasks[same[0]]
                    t.update(pinned_to=robot, bundle=None)
                    if t.get("robot_id"):
                        release(same[0])
                    pinned[same[0]] = robot
                    moved.append(same[0])
                continue
            if same and not (robot and additive):
                covered.extend(
                    tid
                    for tid, t in tasks.items()
                    if t["status"] in OPEN
                    and (t["spec"]["kind"], _target_label(_spec(t)))
                    == (spec.kind.value, _target_label(spec))
                )
                continue
            # New, or finished / paused / cancelled earlier: (re)plan it ("patrol again").
            tasks[spec.task_id] = {
                "spec": spec.model_dump(mode="json"),
                "status": "new",
                "robot_id": None,
                "refused_by": [],
                "posture": posture.value,
                "pinned_to": pinned.get(spec.task_id),
                "bundle": bundles.get(spec.task_id),
                "attempts": 0,
                # "Patrol ..." goes on lap after lap until stopped; a survey is one pass.
                "repeat": spec.kind is TaskKind.SURVEY_SECTOR and is_patrol(ev.get("text") or ""),
            }
            added.append(spec.task_id)
        # The whole fleet sent home: the patrols end (their laps would pull robots out again).
        homes = {
            tasks[t].get("pinned_to")
            for t in added
            if tasks[t]["spec"]["kind"] == TaskKind.RETURN_HOME.value
        }
        active = {r for r, e in fleet.items() if e.get("status") == ACTIVE_ROBOT}
        ended = []
        if active and active <= homes:
            for tid, t in sorted(tasks.items()):
                if t.get("repeat") and t["status"] in OPEN:
                    release(tid)
                    t["status"] = "cancelled"
                    ended.append(tid)
            if ended:
                run.note("⏹️ The patrols end here: every robot is going back to base.")
        # A robot given a direct order drops its other (unpinned, preemptible) work now;
        # the allocator hands that work to the rest of the fleet.
        keep = {k.value for k in cfg.allocator.non_preemptible_kinds}
        ordered = {pinned[t] for t in [*added, *moved] if t in pinned}
        freed = [
            tid
            for tid, t in sorted(tasks.items())
            if t.get("robot_id") in ordered
            and t["status"] in ("assigned", "in_progress")
            and not t.get("pinned_to")
            and _spec(t).kind.value not in keep
        ]
        # A robot sent back to base drops all its other work, named or not ("send the
        # summit to base"): the allocator hands it on.
        homing = {
            tasks[t].get("pinned_to")
            for t in added
            if tasks[t]["spec"]["kind"] == TaskKind.RETURN_HOME.value
            and not tasks[t]["spec"].get("depends_on")
        }
        for tid, t in sorted(tasks.items()):
            if (
                homing & {t.get("robot_id"), t.get("pinned_to")}
                and t["status"] in OPEN
                and tid not in added
                and t["spec"]["kind"] != TaskKind.RETURN_HOME.value
                and _spec(t).kind.value not in keep
                # a routine post held until released ends with the order (below): handed
                # on, "send all Go2 to base" would leave posts nobody could take
                and not (
                    t["spec"]["kind"] == TaskKind.HOLD_POST.value
                    and t["spec"].get("until_cleared")
                    and t.get("posture") != Posture.EMERGENCY.value
                )
            ):
                t.update(pinned_to=None, bundle=None, ordered_for=None)
                if t.get("robot_id") and tid not in freed:
                    freed.append(tid)
        # Step 2: the robot each order named, for the analysis to compare with who does it
        for tid in [*added, *moved]:
            if tid in pinned:
                tasks[tid]["ordered_for"] = pinned[tid]
        # a post, inspection or trip given by name first: it says who takes over what
        freed += [
            t for t in _point_first(run, tasks, [*added, *moved], dirty, fleet) if t not in freed
        ]
        for tid in freed:
            release(tid)
        # A robot guarding a post until released (a routine one: emergency posts end with
        # the all clear) is released from it by its next order.
        for tid, t in sorted(tasks.items()):
            if (
                tid not in added
                and t["status"] in OPEN
                and t["spec"]["kind"] == TaskKind.HOLD_POST.value
                and t["spec"].get("until_cleared")
                and t.get("posture") != Posture.EMERGENCY.value
                and ordered & {t.get("robot_id"), t.get("pinned_to")}
            ):
                holder = t.get("robot_id") or t.get("pinned_to")
                if holder and holder not in dirty:
                    dirty.append(holder)
                t.update(status="cancelled", detail="released by a new order")
                run.note(
                    f"{narr.robot(holder, fleet)} stops "
                    f"{narr.task_id(tasks, tid).replace('stand guard', 'standing guard')} "
                    "for its new order."
                )
        run.decide(
            DecisionKind.DECOMPOSE,
            {
                "goal": ev.get("text"),
                "tasks": added,
                "pinned": {t: pinned[t] for t in [*added, *moved] if t in pinned},
                "moved": moved,
                "freed": freed,
                "skipped_existing": [t.task_id for t in new if t.task_id not in added],
            },
            policy={
                "posture": posture.value,
                "method": "template" if used_template else "llm",
                "decompose_mode": mode,
                "phase": ev.get("phase") or ("template_first" if fast else "single"),
            },
            justification=summary,
            inputs_ref=f"goal:{posture.value}",
        )
        for line in notes:  # an unknown place replaced by the closest one, or left out
            run.note(line)
        if ev.get("phase") == "model" and not added and not moved:
            # The fast path's follow-up: the model planned the alert itself and found
            # nothing the template's plan, already published, does not cover.
            run.note("✔️ I've checked the emergency plan in detail: it already covers everything.")
        elif ev.get("phase") == "model" and added:
            run.note(
                "After checking the emergency plan in detail, I've added: "
                + join(narr.task(_spec(tasks[t])) for t in added)
                + "."
            )
        elif added or not (moved or stopped):
            run.note(
                narr.plan(
                    posture.value,
                    [_spec(tasks[t]) for t in added],
                    skipped=len(covered),
                    covered=[narr.task_id(tasks, t) for t in dict.fromkeys(covered)],
                )
            )
        if moved:
            run.note(
                "As you asked: "
                + join(
                    f"{narr.robot(tasks[t]['pinned_to'], fleet)} will {narr.task_id(tasks, t)}"
                    for t in moved
                )
                + "."
            )
        run.finish(
            "plan_mission",
            {"goal": ev.get("text"), "posture": posture.value},
            {
                "method": "deterministic template" if used_template else "LLM (structured output)",
                "new_tasks": [narr.task(_spec(tasks[t])) for t in added],
                "already_covered": len(new) - len(added),
                **({"llm_latency": fmt_latency(trace.latency_s)} if trace.latency_s else {}),
            },
            plan_latency,
        )
        return run.out(tasks=tasks, new_tasks=added, dirty=dirty, pending_call=None)

    async def validate(state: CoordState) -> dict[str, Any]:
        started = time.perf_counter()
        run = _Run(deps, state)
        tasks = copy.deepcopy(state.get("tasks") or {})
        fleet = state.get("fleet") or {}
        caps = set()
        for e in fleet.values():
            if e["status"] == ACTIVE_ROBOT:
                caps |= {c.value for c in _manifest(e).capabilities}
        sectors = {s.sector_id for s in cfg.campus.sectors}
        pois = {p.poi_id for p in cfg.campus.pois}
        dropped: dict[str, str] = {}
        for tid in state.get("new_tasks") or []:
            spec = _spec(tasks[tid])
            t = spec.target
            if isinstance(t, SectorTarget) and t.sector_id not in sectors:
                dropped[tid] = f"unknown sector {t.sector_id}"
            elif isinstance(t, PoiTarget) and t.poi_id not in pois:
                dropped[tid] = f"unknown POI {t.poi_id}"
            elif not set(spec.required_capabilities) <= caps:
                dropped[tid] = f"no robot has {sorted(set(spec.required_capabilities) - caps)}"
            elif any(d not in tasks for d in spec.depends_on):
                dropped[tid] = "depends on an unknown task"
        # Dependency cycle check over all live tasks.
        graph = {
            tid: [d for d in _spec(t).depends_on if d in tasks]
            for tid, t in tasks.items()
            if t["status"] not in ("dropped",) and tid not in dropped
        }
        visiting, done = set(), set()

        def cyclic(node: str) -> bool:
            if node in done:
                return False
            if node in visiting:
                return True
            visiting.add(node)
            bad = any(cyclic(d) for d in graph.get(node, []))
            visiting.discard(node)
            done.add(node)
            return bad

        for tid in state.get("new_tasks") or []:
            if tid not in dropped and cyclic(tid):
                dropped[tid] = "dependency cycle"
        for tid in state.get("new_tasks") or []:
            tasks[tid]["status"] = "dropped" if tid in dropped else "pending"
        if dropped:
            run.decide(
                DecisionKind.VALIDATE,
                {"dropped": dropped},
                policy={"rule": "schema+campus+capability+dag"},
                justification="invalid tasks dropped",
            )
            run.note(
                "I left out: "
                + join(f"{narr.task_id(tasks, k)} ({v})" for k, v in dropped.items())
                + "."
            )
        if state.get("new_tasks"):
            run.call(
                "validate_plan",
                {"tasks": [narr.task_id(tasks, t) for t in state["new_tasks"]]},
                {
                    "checks": "targets exist, a robot has the capability, no cycles",
                    "valid": len(state["new_tasks"]) - len(dropped),
                    "dropped": {narr.task_id(tasks, k): v for k, v in dropped.items()},
                },
                time.perf_counter() - started,
            )
        return run.out(tasks=tasks)

    async def _shelter_for(
        entry: dict[str, Any], robot: str, fleet: dict[str, dict[str, Any]]
    ) -> str | None:
        """Retarget a person's escort to the nearest shelter from where the robot is, on
        the person's profile (read from the store); the store notes the conclusion."""
        if deps.kb is None or robot not in fleet:
            return None
        iid = entry["spec"]["interaction_id"]
        profile = "default"
        if deps.interactions is not None:
            with contextlib.suppress(Exception):
                profile = (await deps.interactions.get(iid))["content"].get("profile") or profile
        states, _ = await _robot_states(deps, {robot: fleet[robot]})
        if robot not in states:
            return None
        pose = states[robot].pose
        try:
            route = await deps.kb.nearest_shelter({"x": pose.x, "y": pose.y}, profile)
        except Exception:
            return None
        if not route.get("ok"):
            return None
        shelter = route["destination"]
        entry["spec"]["target"] = PoiTarget(poi_id=shelter).model_dump(mode="json")
        entry["concluded_at"] = shelter
        if deps.interactions is not None:
            with contextlib.suppress(Exception):
                await deps.interactions.update(iid, content={"conclude_at": shelter})
        return shelter

    async def arbitrate(state: CoordState) -> dict[str, Any]:
        started = time.perf_counter()
        run = _Run(deps, state)
        tasks = copy.deepcopy(state.get("tasks") or {})
        posture = state.get("posture") or "routine"
        rank = _posture_rank(cfg, posture)
        if posture not in {p.value for p in cfg.allocator.preempt_on}:
            return run.out()  # outranks via priority, does not preempt
        non_preemptible = {k.value for k in cfg.allocator.non_preemptible_kinds}
        dirty = list(state.get("dirty") or [])
        preempted, concluded = [], []
        for tid in sorted(tasks):
            t = tasks[tid]
            if t["status"] not in OPEN or _posture_rank(cfg, t.get("posture", posture)) >= rank:
                continue
            spec = _spec(t)
            if spec.kind.value in non_preemptible:
                # An open escort is concluded (handover/return_home), never dropped.
                robot = t.get("robot_id")
                if spec.interaction_id and robot and not t.get("concluded_at"):
                    # A person's escort concludes at the nearest shelter.
                    shelter = await _shelter_for(t, robot, state.get("fleet") or {})
                    if shelter:
                        if robot not in dirty:
                            dirty.append(robot)
                        concluded.append(f"{tid} -> {shelter} (shelter) on {robot}")
                    continue
                post = _post_at_escort_target(tasks, spec)
                if robot and t["status"] in ("assigned", "in_progress") and post:
                    # The escort ends at an exposed POI that now needs a post: the escort
                    # robot holds it (it is going there anyway) rather than a second robot
                    # converging on the same spot.
                    p = tasks[post]
                    p["pinned_to"] = robot
                    p["spec"]["depends_on"] = sorted({*p["spec"].get("depends_on", []), tid})
                    concluded.append(f"{tid} -> {post} on {robot}")
                elif robot and t["status"] in ("assigned", "in_progress"):
                    home = home_task(robot, Posture(posture), cfg.campus, after=[tid])
                    if home.task_id not in tasks:
                        tasks[home.task_id] = {
                            "spec": home.model_dump(mode="json"),
                            "status": "pending",
                            "robot_id": None,
                            "refused_by": [],
                            "posture": posture,
                            "pinned_to": robot,
                            "attempts": 0,
                        }
                        concluded.append(f"{tid} -> {home.task_id} on {robot}")
                continue
            if t.get("robot_id") and t["robot_id"] not in dirty:
                dirty.append(t["robot_id"])
            t.update(status="preempted", robot_id=None)
            preempted.append(tid)
        if preempted or concluded:
            template = f"{posture} preempts lower-priority work; escorts are concluded, not dropped"
            # The outcome is code; the model only words its rationale.
            # The fast path waits for no model: the template words it.
            fast = (
                cfg.fleet.defaults.coordinator.decompose_mode.get(posture) == "template_then_model"
                and (state.get("event") or {}).get("phase") != "model"
            )
            rationale, templated = await one_sentence(
                None if fast else deps.model,
                "You state, in one short sentence for the operator, why a robot-swarm "
                "coordinator paused work. The decision is already made; do not change it.",
                f"Posture now {posture}. Paused: {', '.join(preempted) or 'none'}. Concluded "
                f"(escorts finish first): {', '.join(concluded) or 'none'}.",
                template,
            )
            run.decide(
                DecisionKind.ARBITRATE,
                {"posture": posture, "preempted": preempted, "concluded": concluded},
                policy={
                    "posture_priority": [p.value for p in cfg.allocator.posture_priority],
                    "non_preemptible": sorted(non_preemptible),
                    "rationale": "template" if templated else "model",
                },
                justification=rationale,
            )
            if state.get("posture_changed", True):
                text = f"🚨 Switching to **{posture}** mode."
                if preempted:
                    text += f" I've paused {plural(len(preempted), 'lower-priority task')}."
            else:
                text = f"🚨 We're still in **{posture}** mode."
                if preempted:
                    text += (
                        f" I've queued {plural(len(preempted), 'lower-priority task')}; "
                        "say *all clear* when the situation is over and I'll resume "
                        "them."
                    )
            sheltered = [c for c in concluded if "(shelter)" in c]
            if sheltered:
                text += " " + " ".join(
                    f"The visitor escort {narr.task_id(tasks, c.split(' -> ')[0])} now ends at "
                    f"the nearest shelter ({c.split(' -> ')[1].split(' ')[0]})."
                    for c in sheltered
                )
                concluded = [c for c in concluded if c not in sheltered]
            if concluded:
                escorts = [c.split(" -> ")[0] for c in concluded]
                holds = any("hold_post" in c.split(" -> ")[1] for c in concluded)
                text += (
                    " The visitor escort continues — "
                    + join(narr.task_id(tasks, e) for e in escorts)
                    + " — and that robot "
                    + ("then holds the post there." if holds else "returns to base afterwards.")
                )
            run.note(text)
        if preempted or concluded:
            run.call(
                "arbitrate_postures",
                {"new_posture": posture, "policy": "emergency > interactive > routine"},
                {
                    "paused": [narr.task_id(tasks, t) for t in preempted],
                    "escort_kept_and_concluded": concluded,
                },
                time.perf_counter() - started,
            )
        return run.out(tasks=tasks, dirty=dirty)

    # ---------------------------------------------------------- allocate / publish / replies

    def _protect(ctx: agentic.Context, order: str, fleet: dict[str, Any]) -> None:
        """The robots the order names (a correction: and those of the order it corrects)."""
        platforms = {r: e.get("platform", "") for r, e in fleet.items()}
        named: set[str] | None = set()
        for part in re.split(r"^Previous order: |\nNow: ", order):
            if part.strip():
                got = order_robots(part, platforms)
                named = None if got is None or named is None else named | got
        ctx.order_robots = named or None
        ctx.takeover = bool(TAKEOVER.search(order))
        ctx.named_places = {ident for _, ident in mentions(order, cfg.campus)}

    async def _order_check(
        run: _Run,
        state: CoordState,
        order: str,
        tasks: dict[str, dict[str, Any]],
        fleet: dict[str, Any],
        dirty: list[str],
    ) -> bool:
        """model_first, after publication: the model compares who-does-what with the order;
        it may move open work and add what the plan missed (validated like any plan).
        Code keeps what the plan tied together (groups, order). True: something changed."""
        if deps.model is None:
            return False
        posture = Posture(state.get("posture") or "routine")
        ctx = _context({**state, "tasks": tasks}, posture, fleet)
        _protect(ctx, order, fleet)
        ctx.fresh = {tid for tid, t in tasks.items() if t.get("order") == order}
        started = time.perf_counter()
        deadline = cfg.fleet.defaults.coordinator.careful_deadline_s
        try:
            async with asyncio.timeout(deadline):
                result, trace = await agentic.check(deps.model, order, ctx)
        except TimeoutError:
            run.note(f"⏱️ I couldn't finish checking the assignment within {deadline:.0f} s.")
            return False
        run.thoughts("check_assignment", trace)
        errors = agentic.check_errors(result, ctx)
        for m in result.reassign:
            t = tasks.get(m.task_id) or {}
            tied = t.get("spec", {}).get("depends_on") or any(
                m.task_id in (o["spec"].get("depends_on") or [])
                or (
                    t.get("plan_group")
                    and o.get("plan_group") == t.get("plan_group")
                    and o is not t
                )
                for o in tasks.values()
                if o["status"] in OPEN
            )
            if tied:
                errors.append(f"{m.task_id} is grouped or ordered with other work")
            if t.get("named_by") and m.robot_id != t["named_by"]:
                # this order gave it to that robot by name
                errors.append(f"{m.task_id}: the order gives it to {t['named_by']} by name")
        extra = agentic.AgentPlan(tasks=result.add)
        if result.add:
            errors += [f"add: {e}" for e in agentic.validate(extra, ctx)]
            live = {(t["kind"], t["target_id"]) for t in ctx.open_tasks}
            errors += [  # it adds what is missing only (it added a copy per robot)
                f"add: {t.kind.value} {t.target_id} is already open"
                for t in result.add
                if (t.kind.value, t.target_id) in live
            ]
        changes = not result.ok and not errors and bool(result.reassign or result.add)
        run.call(
            "check_assignment",
            {"order": order},
            {
                "matches_the_order": result.ok,
                **({"issue": result.issue} if result.issue else {}),
                **({"rejected": errors} if errors else {}),
                "reassign": [
                    f"{m.task_id} -> {m.robot_id or 'any robot'}" for m in result.reassign
                ],
                "add": [f"{t.kind.value} {t.target_id}" for t in result.add],
            },
            time.perf_counter() - started,
        )
        if not changes:
            return False
        for m in result.reassign:
            t = tasks[m.task_id]
            holder = t.get("robot_id")
            t.update(pinned_to=m.robot_id, bundle=None, status="pending", robot_id=None)
            if m.robot_id is None and holder:
                t.setdefault("refused_by", []).append(holder)
            for r in (holder, m.robot_id):
                if r and r not in dirty:
                    dirty.append(r)
        open_keys = {
            (t["spec"]["kind"], _target_label(_spec(t)))
            for t in tasks.values()
            if t["status"] in OPEN
        }
        added, _ = _add_planned(
            agentic.to_tasks(extra, ctx, set(tasks)), tasks, posture, open_keys, "pending", order
        )
        run.decide(
            DecisionKind.REALLOCATE,
            {
                "order": order,
                "issue": result.issue,
                "moves": [m.model_dump() for m in result.reassign],
                "added": added,
            },
            policy={"rule": "plan_check (model proposes, allocator decides)"},
            justification=result.issue or "assignment adjusted to the order",
            inputs_ref="order_check",
        )
        text = f"🧐 Checking the assignment against your order: {result.issue}"
        if added:
            text += " Adding " + join(narr.task_id(tasks, t) for t in added) + "."
        run.note(text)
        return True

    async def check_order(state: CoordState) -> dict[str, Any]:
        """The careful path: the model checks who-does-what against the order before the
        envelopes go out; a correction is validated and allocated again (once)."""
        order = state.get("order_check")
        if not order:
            return {"recheck": False}
        run = _Run(deps, state)
        tasks = copy.deepcopy(state.get("tasks") or {})
        dirty = list(state.get("dirty") or [])
        changed = await _order_check(run, state, order, tasks, state.get("fleet") or {}, dirty)
        if not changed:
            run.note("✔️ I've checked the assignment against your order: it matches.")
        return run.out(tasks=tasks, dirty=dirty, order_check=None, recheck=changed)

    def route_check(state: CoordState) -> str:
        return "allocate" if state.get("recheck") else "reconcile"

    async def allocate(state: CoordState) -> dict[str, Any]:
        run = _Run(deps, state)
        tasks = copy.deepcopy(state.get("tasks") or {})
        fleet = copy.deepcopy(state.get("fleet") or {})
        dirty = list(state.get("dirty") or [])
        # Safety net: a robot I hold as charging whose battery is back (its "charged" report
        # was lost) is back at work; otherwise it stays out of every allocation.
        charging = {r: e for r, e in fleet.items() if e.get("status") == "charging"}
        if charging:
            probe = {r: {**e, "status": ACTIVE_ROBOT} for r, e in charging.items()}
            back, _ = await _robot_states(deps, probe)
            for rid, st in back.items():
                if (
                    st.battery_pct is not None
                    and st.battery_pct >= cfg.allocator.recharge_resume_pct
                ):
                    fleet[rid]["status"] = ACTIVE_ROBOT
        # A robot I marked lost because a request to it failed (not by the telemetry plane,
        # whose "recovered" event brings it back) is asked again: otherwise it stays "lost"
        # for good while it stands idle at staging, publishing its state.
        unanswered = {
            r: e for r, e in fleet.items() if e.get("status") == "lost" and e.get("lost_by")
        }
        if unanswered:
            probe = {r: {**e, "status": ACTIVE_ROBOT} for r, e in unanswered.items()}
            back, _ = await _robot_states(deps, probe)
            for rid, st in sorted(back.items()):
                if st.link_quality.value != "lost":
                    fleet[rid]["status"] = ACTIVE_ROBOT
                    fleet[rid].pop("lost_by", None)
                    if rid not in dirty:
                        dirty.append(rid)  # resync its envelope with what it still owns
                    run.note(f"{narr.robot(rid, fleet)} is back in contact.")
        started = time.perf_counter()
        states, unreachable = await _robot_states(deps, fleet)
        query_latency = time.perf_counter() - started
        for rid, st in states.items():
            if st.battery_pct is None:
                continue
            fleet[rid]["seen_battery_pct"] = round(st.battery_pct, 1)
            # What idle rebalancing estimates from: the live state, not the battery of the
            # robot's last idle report (it may have charged since).
            fleet[rid]["idle_battery_pct"] = round(st.battery_pct, 1)
            fleet[rid]["idle_pose"] = {"x": st.pose.x, "y": st.pose.y}
            # A battery refusal lapses once the robot has charged since: its battery is seen
            # higher than when it refused. A robot charged idle at the dock never reports
            # "charged" and would stay barred from the laps it had declined.
            for t in tasks.values():
                if (t.get("refused_for") or {}).get(rid) != "battery":
                    continue
                then = (t.get("refused_battery") or {}).get(rid)
                if (then is not None and st.battery_pct >= then + 5) or (
                    st.battery_pct >= cfg.allocator.recharge_resume_pct
                ):
                    t["refused_by"] = [r for r in t.get("refused_by") or [] if r != rid]
                    del t["refused_for"][rid]
                    (t.get("refused_battery") or {}).pop(rid, None)
        run.call(
            "query_robot_states",
            {"robots": sorted(r for r, e in fleet.items() if e["status"] == ACTIVE_ROBOT)},
            {
                **{
                    rid: {
                        "position": [round(st.pose.x, 1), round(st.pose.y, 1)],
                        "battery_pct": st.battery_pct if st.battery_pct is not None else "unknown",
                        "link": st.link_quality.value,
                    }
                    for rid, st in sorted(states.items())
                },
                **{rid: "unreachable" for rid in unreachable},
            },
            query_latency,
        )
        for rid in unreachable:
            fleet[rid]["status"] = "lost"
            fleet[rid]["lost_by"] = "query"  # asked again at the next allocation
            # Its work waits for it: out of reach is most often a coverage gap.
        pending = [tid for tid, t in tasks.items() if t["status"] == "pending"]
        if cfg.fleet.defaults.baseline == "independent":
            # B0: each task is allocated once; work released by a failure or a
            # refusal is not given to anyone else (only back to its own robot).
            for tid in pending:
                t = tasks[tid]
                first = t.get("allocated_to")
                if first and t.get("pinned_to") != first:
                    t.update(status="failed", detail="B0: allocated once, not reallocated")
            pending = [tid for tid in pending if tasks[tid]["status"] == "pending"]
        if not pending:
            return run.out(
                tasks=tasks, fleet=fleet, dirty=dirty, rounds=(state.get("rounds") or 0) + 1
            )

        manifests = {rid: _manifest(fleet[rid]) for rid in states}
        floor = cfg.allocator.default_battery_floor_pct
        excluded = {(tid, r) for tid in pending for r in tasks[tid].get("refused_by", [])}
        # Robots the order keeps out of a task ("everyone except go2_01", model_first)
        excluded |= {(tid, r) for tid in pending for r in tasks[tid].get("excluded") or []}
        pinned = {tid: tasks[tid]["pinned_to"] for tid in pending if tasks[tid].get("pinned_to")}
        # A robot on its way back to base takes no other work until it is given some by name.
        homing = {
            e.get("robot_id") or e.get("pinned_to")
            for e in tasks.values()
            if e["spec"]["kind"] == TaskKind.RETURN_HOME.value
            and e["status"] in OPEN
            and not e["spec"].get("depends_on")
        }
        excluded |= {
            (tid, r)
            for tid in pending
            for r in homing
            if r
            and tasks[tid]["spec"]["kind"] != TaskKind.RETURN_HOME.value
            and pinned.get(tid) != r
        }
        # A robot holding a post until released takes no other work of its priority or lower
        # (it would queue behind the post forever) unless the work is given to it by name;
        # higher-priority work (an emergency) preempts the post as any other. Likewise while
        # it does a post, inspection or trip given to it by name (its other work goes to the
        # rest of the fleet).
        guarding: dict[str, int] = {}
        for e in tasks.values():
            if (
                (
                    (
                        e["spec"]["kind"] == TaskKind.HOLD_POST.value
                        and e["spec"].get("until_cleared")
                    )
                    or e.get("point_first")
                )
                and e["status"] in ("assigned", "in_progress")
                and e.get("robot_id")
            ):
                guarding[e["robot_id"]] = max(guarding.get(e["robot_id"], 0), _spec(e).priority)
            elif e.get("point_first") and e["status"] == "pending" and e.get("pinned_to"):
                # placed in this same round: its robot is kept free all the same
                rid = e["pinned_to"]
                guarding[rid] = max(guarding.get(rid, 0), _spec(e).priority)
        excluded |= {
            (tid, r)
            for tid in pending
            for r, level in guarding.items()
            if pinned.get(tid) != r and _spec(tasks[tid]).priority <= level
        }
        started = time.perf_counter()
        # Priority tiers, highest first. Robots interrupt lower-priority work for a
        # higher-priority task, so a tier is costed only behind commitments of at least its
        # priority (an escort is not planned after a patrol lap).
        result = AssignResult()
        # Bundles ("and other to patrol the campus": these tasks go to ONE robot) are placed
        # after the single tasks, each on a robot that got nothing else from this plan.
        bundled: dict[str, list[str]] = {}
        for tid in pending:
            if tasks[tid].get("bundle") and not tasks[tid].get("pinned_to"):
                bundled.setdefault(tasks[tid]["bundle"], []).append(tid)
        pending = [t for t in pending if not any(t in b for b in bundled.values())]
        tiers = sorted({_spec(tasks[tid]).priority for tid in pending}, reverse=True)
        for tier in tiers:
            committed = [
                t
                for t, e in tasks.items()
                if e.get("robot_id") in states
                and e["status"] in ("assigned", "in_progress")
                and _spec(e).priority >= tier
            ]
            vstates = {}
            for rid, rs in states.items():
                vs = virtual_state(rs, manifests[rid], cfg.allocator)
                for tid in sorted(t for t in committed if tasks[t]["robot_id"] == rid):
                    _, vs = estimate_next(
                        vs, _spec(tasks[tid]), manifests[rid], cfg.campus, cfg.allocator, floor
                    )
                vstates[rid] = vs
            holding = {
                e["robot_id"]
                for e in tasks.values()
                if e["spec"]["kind"] == TaskKind.HOLD_POST.value
                and e["status"] in ("assigned", "in_progress")
                and e.get("robot_id")
            }
            visiting = {
                (e["robot_id"], key)
                for e in tasks.values()
                if e["status"] in ("assigned", "in_progress") and e.get("robot_id")
                for key in [visit_key(_spec(e))]
                if key
            }
            tier_ids = sorted(t for t in pending if _spec(tasks[t]).priority == tier)
            part = assign(
                [_spec(tasks[t]) for t in tier_ids],
                vstates,
                manifests,
                cfg.campus,
                cfg.allocator,
                battery_floor_pct=floor,
                excluded=excluded,
                pinned={t: r for t, r in pinned.items() if t in tier_ids},
                holding=holding,
                visiting=visiting,
            )
            for a in part.assignments:  # the next tier is costed behind these
                tasks[a.task_id].update(status="assigned", robot_id=a.robot_id)
                tasks[a.task_id].setdefault("allocated_to", a.robot_id)  # B0 reads it
            result.assignments += part.assignments
            result.unallocated.update(part.unallocated)
            result.details.update(part.details)
            result.rounds = max(result.rounds, part.rounds)
        taken = {a.robot_id for a in result.assignments}
        for label, tids in sorted(bundled.items()):
            specs = [_spec(tasks[t]) for t in sorted(tids)]
            best: tuple[float, str, list[Estimate]] | None = None
            occupied = {
                e.get("robot_id")
                for e in tasks.values()
                if e["status"] in ("assigned", "in_progress") and e.get("robot_id")
            }
            # Prefer robots with no work at all, then robots without work from this plan.
            for pool in (set(states) - occupied - taken, set(states) - taken, set(states)):
                for rid in sorted(pool):
                    if any((t, rid) in excluded for t in tids):
                        continue
                    vs = virtual_state(states[rid], manifests[rid], cfg.allocator)
                    ests: list[Estimate] = []
                    for spec in specs:
                        e, vs = estimate_next(
                            vs, spec, manifests[rid], cfg.campus, cfg.allocator, floor
                        )
                        if not isinstance(e, Estimate):
                            break
                        ests.append(e)
                    if len(ests) == len(specs):
                        total = sum(e.cost for e in ests)
                        if best is None or total < best[0]:
                            best = (total, rid, ests)
                if best is not None:
                    break
            if best is None:
                for t in tids:
                    result.unallocated[t] = RefusalReason.CONFLICT
                    result.details[t] = f"no single robot can take bundle {label}"
                continue
            _, rid, ests = best
            taken.add(rid)
            for spec, e in zip(specs, ests, strict=True):
                result.assignments.append(Assignment(spec.task_id, rid, e.cost, result.rounds + 1))
        for a in result.assignments:
            t = tasks[a.task_id]
            t.update(status="assigned", robot_id=a.robot_id)
            t.setdefault("allocated_to", a.robot_id)  # B0 reads it
            if a.robot_id not in dirty:
                dirty.append(a.robot_id)
            holder = t.get("interaction_holder")
            if t["spec"].get("interaction_id") and holder and holder != a.robot_id:
                await _handover(run, t, holder, a.robot_id, states, fleet)
            if t["spec"].get("interaction_id"):
                t["interaction_holder"] = a.robot_id
        # Robots that may come back (out of coverage, unreachable): a task that only lacks a
        # free robot waits for them instead of asking the operator.
        away = sorted(
            set(unreachable)
            | {r for r, e in fleet.items() if e["status"] not in (ACTIVE_ROBOT, "failed")}
        )
        escalations = _live_escalations(state.get("escalations") or [], tasks)
        seq = state.get("escalation_seq") or 0
        for tid, reason in result.unallocated.items():
            spec = _spec(tasks[tid])
            waits = reason is RefusalReason.CONFLICT and bool(away)
            if (
                spec.priority >= cfg.fleet.defaults.coordinator.escalation_priority
                and not tasks[tid].get("escalated")
                and not waits
            ):
                tasks[tid]["escalated"] = True
                seq += 1
                escalations.append(
                    {
                        "esc_id": f"esc-{seq}",
                        "kind": "unallocatable",
                        "task_id": tid,
                        "reason": reason.value,
                        "detail": result.details.get(tid, ""),
                    }
                )
        kind = DecisionKind.REALLOCATE if state.get("reallocating") else DecisionKind.ALLOCATE
        run.decide(
            kind,
            {
                "assignments": [
                    {"task_id": a.task_id, "robot_id": a.robot_id, "cost": a.cost, "round": a.round}
                    for a in result.assignments
                ],
                "unallocated": {k: v.value for k, v in result.unallocated.items()},
                "details": result.details,
                "robots": sorted(states),
                # why this robot and not another: every robot's cost (estimated seconds, lower
                # wins) or why it could not take it, and what it was doing (so the analysis
                # can explain why one robot was chosen over another)
                "candidates": _candidates(result, tasks, fleet, states, manifests, floor),
            },
            policy={
                "rule": "hungarian (scipy.linear_sum_assignment), ties -> lowest robot_id",
                "cost_weights": cfg.allocator.weights.model_dump(),
                "battery_floor_pct": floor,
                "excluded": sorted(f"{t}@{r}" for t, r in excluded),
            },
            justification=(
                f"{len(result.assignments)} assigned, {len(result.unallocated)} unallocated "
                f"in {result.rounds} round(s)"
            ),
        )
        run.call(
            "allocate_tasks",
            {
                "tasks": [narr.task_id(tasks, t) for t in sorted(pending)],
                "method": "Hungarian assignment on travel time, battery, commitment and priority",
            },
            {
                "assignments": [
                    {
                        "robot": a.robot_id,
                        "task": narr.task_id(tasks, a.task_id),
                        "cost": round(a.cost, 1),
                    }
                    for a in result.assignments
                ],
                "unassigned": {
                    narr.task_id(tasks, t): v.value for t, v in result.unallocated.items()
                },
            },
            time.perf_counter() - started,
        )
        if result.assignments:
            by_robot: dict[str, list[str]] = {}
            for a in result.assignments:
                by_robot.setdefault(a.robot_id, []).append(a.task_id)
            run.note(
                narr.allocation(by_robot, tasks, fleet, replanned=bool(state.get("reallocating")))
            )
        if result.unallocated:
            run.note(
                narr.unallocated(
                    {t: v.value for t, v in result.unallocated.items()},
                    tasks,
                    details=result.details,
                    away=[narr.robot(r, fleet) for r in away],
                    fleet=fleet,
                )
            )
        _rebalance(run, tasks, fleet, dirty)  # idle robots take queued work
        sending = sorted(r for r in dirty if fleet.get(r, {}).get("status") == ACTIVE_ROBOT)
        pending_call = None
        if sending:
            pending_call = run.announce(
                "send_envelopes",
                {
                    rid: [
                        narr.task_id(tasks, t)
                        for t in sorted(tasks)
                        if tasks[t].get("robot_id") == rid
                        and tasks[t]["status"] in ("assigned", "in_progress")
                    ]
                    or ["(nothing: stand by)"]
                    for rid in sending
                },
            )
        return run.out(
            pending_call=pending_call,
            tasks=tasks,
            fleet=fleet,
            dirty=dirty,
            escalations=escalations,
            escalation_seq=seq,
            rounds=(state.get("rounds") or 0) + 1,
        )

    def _flow_request(flow: dict[str, Any], tasks: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """A flow request with what the network needs to admit it: the task's
        priority and where it will stream (the POI or post; a sector's centroid)."""
        t = tasks.get(flow["task_id"]) or {}
        spec = _spec(t) if t else None
        out = {**flow, "priority": spec.priority if spec else 0, "risk": 0}
        target = spec.target if spec else None
        xy = None
        if isinstance(target, PoiTarget):
            poi = cfg.campus.poi(target.poi_id) if hasattr(cfg.campus, "poi") else None
            if poi is None:
                poi = next((p for p in cfg.campus.pois if p.poi_id == target.poi_id), None)
            if poi is not None:
                xy = (poi.pose.x, poi.pose.y)
                out["risk"] = 1 if getattr(poi, "exposed", False) else 0
            if target.viewpoint is not None:  # relocated for coverage: it streams from there
                xy = (target.viewpoint.x, target.viewpoint.y)
        elif isinstance(target, SectorTarget):
            sec = next((s for s in cfg.campus.sectors if s.sector_id == target.sector_id), None)
            if sec is not None and sec.polygon:
                xy = (
                    sum(p[0] for p in sec.polygon) / len(sec.polygon),
                    sum(p[1] for p in sec.polygon) / len(sec.polygon),
                )
        elif isinstance(target, PoseTarget):
            xy = (target.pose.x, target.pose.y)
        if xy is not None:
            out["x"], out["y"] = xy
        return out

    def _viewpoint(poi_id: str, flow_class: str) -> dict[str, Any] | None:
        """``relocate_for_coverage``: a footpath spot within ``relocate_radius_m`` of
        the POI, better than the POI's own level, where the flow's rate fits the robot's
        uplink (what admission checks for coverage; "at least medium" alone does not carry
        4 Mbps video), with no building between it and the POI; the best level, then the
        shortest distance."""
        from a6g_common.coverage import Footprints
        from a6g_common.netconfig import LEVELS

        cmap = getattr(deps.network, "coverage", None)
        poi = next((p for p in cfg.campus.pois if p.poi_id == poi_id), None)
        if cmap is None or poi is None:
            return None
        px, py = poi.pose.x, poi.pose.y
        here = cmap.nearest(px, py)
        rank = LEVELS.index(here["level"]) if here else len(LEVELS)
        radius = cfg.network.reconcile.relocate_radius_m * (cfg.campus.scale or 1.0)
        walls = Footprints(cfg.campus.obstacles, [1e9] * len(cfg.campus.obstacles))
        req = cfg.network.capacity.flow_requirements.get(flow_class)
        need = req.ul_mbps if req else 0.0
        best = None
        for s in cmap.path_samples:
            d = math.hypot(s["x"] - px, s["y"] - py)
            r = LEVELS.index(s["level"])
            sinr = s["sinr"] + cfg.network.sinr_shift_db
            if d > radius or r >= rank or cfg.network.uplink_rate.rate_mbps(sinr) < need:
                continue
            if walls.blocked((s["x"], s["y"]), (px, py), h_a=0.5, h_b=0.5):
                continue
            key = (r, d)
            if best is None or key < best[0]:
                best = (key, s, d)
        if best is None:
            return None
        _, s, d = best
        yaw = math.atan2(py - s["y"], px - s["x"])  # facing the POI
        return {
            "pose": {"x": round(s["x"], 3), "y": round(s["y"], 3), "yaw": round(yaw, 6)},
            "level": s["level"],
            "from_level": here["level"] if here else None,
            "distance_m": round(d, 1),
            "sinr_db": round(s["sinr"] + cfg.network.sinr_shift_db, 2),
            "from_sinr_db": round(here["sinr"] + cfg.network.sinr_shift_db, 2) if here else None,
        }

    async def reconcile(state: CoordState) -> dict[str, Any]:
        """The flows the envelopes imply, requested from the network
        branch, and the invariants I1 (no live task without a flow) and I2 (no flow without
        a live task) checked before publication."""
        run = _Run(deps, state)
        tasks = copy.deepcopy(state.get("tasks") or {})
        flows = copy.deepcopy(state.get("flows") or {})
        now = deps.clock.now()
        requested: list[str] = []
        released: list[str] = []
        switched: list[str] = []
        relocated: list[dict[str, Any]] = []
        answers_all: list[dict[str, Any]] = []  # every answer, in order (a post's video
        # and telemetry flows share a flow id: a dict would keep only the last)
        problems: dict[str, list] = {"i1": [], "i2": []}
        rounds = 0
        net_cfg = cfg.network
        # The degradation loop (code only); off in the reconcile_off baseline
        admission = getattr(deps.network, "mode", "always_grant") == "capacity" and (
            net_cfg.reconcile.mode != "off"
        )
        # admission_only baseline: the same admission, one request pass, and a refused flow
        # is not adapted (no switch to telemetry, no further rounds, no relocation)
        degrade = admission and net_cfg.reconcile.mode != "admission_only"
        if net_cfg.post_sensing_mode:  # posts stream video unless degraded
            for t in tasks.values():
                if t["spec"]["kind"] == TaskKind.HOLD_POST.value and not t["spec"].get(
                    "sensing_mode"
                ):
                    t["spec"]["sensing_mode"] = net_cfg.post_sensing_mode
        # The relocation variant's move to a viewpoint is a round of its own: it does not use
        # up the degradation loop's rounds (otherwise, at a low capacity, the telemetry asked
        # in the last round is never settled, and every post ends without a stream).
        extra = 1 if degrade and net_cfg.reconcile.mode == "relocate" else 0
        for _ in range(cfg.fleet.defaults.coordinator.reconcile_max_iter + extra):
            rounds += 1
            asks, gone = flow_rules.derive(tasks, flows, now)
            if gone:  # releases first: their capacity goes to this round's requests
                await deps.network.release_flows(state["mission_id"], gone)
                released += gone
            if asks:
                answers = await deps.network.request_flows(
                    state["mission_id"], [_flow_request(a, tasks) for a in asks]
                )
                for fid, answer in answers.items():
                    flows[fid].update(answer)
                    answers_all.append(
                        {
                            "flow_id": fid,
                            "flow_class": flows[fid]["flow_class"],
                            "state": answer["state"],
                            "reason": answer.get("reason", ""),
                        }
                    )
                requested += [a["flow_id"] for a in asks]
            if degrade and net_cfg.reconcile.mode == "relocate":
                # Relocation variant: a video post rejected for coverage first moves to a
                # nearby viewpoint with better coverage; the flow is asked again from there.
                moved = []
                for f in flows.values():
                    t = tasks.get(f["task_id"])
                    if (
                        t is None
                        or f["flow_class"] != "video_control"
                        or f["state"] != "rejected"
                        or not str(f.get("reason", "")).startswith("coverage")
                        or t["spec"]["kind"] != TaskKind.HOLD_POST.value
                        or t["spec"]["target"].get("type") != "poi"
                        or t["spec"]["target"].get("viewpoint")
                    ):
                        continue
                    spot = _viewpoint(t["spec"]["target"]["poi_id"], f["flow_class"])
                    if spot is None:
                        continue
                    t["spec"]["target"]["viewpoint"] = spot["pose"]
                    f.update(state="released", reason=f"relocated ({f['reason']})")
                    relocated.append({"task_id": f["task_id"], **spot})
                    moved.append(f["flow_id"])
                    run.decide(
                        DecisionKind.RECONCILE,
                        {"task_id": f["task_id"], **spot},
                        policy={
                            "rule": "relocate_for_coverage",
                            "relocate_radius_m": net_cfg.reconcile.relocate_radius_m,
                        },
                        justification=f"{t['spec']['target']['poi_id']}: video rejected for "
                        f"coverage; held from {spot['distance_m']} m away ({spot['level']}), "
                        "in sight of the POI",
                        inputs_ref=f"flow:{f['flow_id']}",
                    )
                if moved:
                    await deps.network.release_flows(state["mission_id"], moved)
                    continue
            if degrade:
                # A rejected or degraded video flow: the post switches to telemetry sensing,
                # lowest priority (risk) first; the new telemetry_plus flow is requested on
                # the next pass.
                bad = sorted(
                    (
                        f
                        for f in flows.values()
                        if f["flow_class"] == "video_control"
                        and f["state"] in ("rejected", "degraded")
                        and f["task_id"] in tasks
                    ),
                    key=lambda f: (_spec(tasks[f["task_id"]]).priority, f["flow_id"]),
                )
                # A telemetry flow refused for capacity while a video post holds a grant:
                # that video post (the lowest priority) gives way, and the refused flows
                # are asked again (sensing at every post before video at some).
                starved = [
                    f
                    for f in flows.values()
                    if f["flow_class"] != "video_control"
                    and f["state"] == "rejected"
                    and str(f.get("reason", "")).startswith("capacity")
                    and f["task_id"] in tasks
                ]
                if starved and not bad:
                    granted_video = sorted(
                        (
                            f
                            for f in flows.values()
                            if f["flow_class"] == "video_control"
                            and f["state"] in ("granted", "degraded")
                            and f["task_id"] in tasks
                        ),
                        key=lambda f: (_spec(tasks[f["task_id"]]).priority, f["flow_id"]),
                    )
                    bad = granted_video[:1]
                if starved and bad:
                    for f in starved:
                        f.update(state="released", reason=f"asked again ({f['reason']})")
                if bad:
                    for f in bad:
                        t = tasks[f["task_id"]]
                        t["spec"]["sensing_mode"] = "telemetry"
                        t["degraded_from"] = "video"
                        f.update(state="released", reason=f"switched to telemetry ({f['reason']})")
                        switched.append(f["task_id"])
                    await deps.network.release_flows(
                        state["mission_id"], [f["flow_id"] for f in bad]
                    )
                    continue
            if admission and not degrade:
                break
            problems = flow_rules.check(tasks, flows)
            if not problems["i1"] and not problems["i2"]:
                break
        problems = flow_rules.check(tasks, flows)  # after the last pass, whatever it did
        escalated = []
        if admission and problems["i1"]:
            # The last step: a post the network cannot serve at all is held on state
            # telemetry only (no sensing stream, so no flow: I1 holds) and escalated.
            floor = net_cfg.reconcile.recharge_reassign_min_priority
            for tid in problems["i1"]:
                t = tasks[tid]
                if t["spec"]["kind"] == TaskKind.HOLD_POST.value:
                    t["spec"]["sensing_mode"] = "state_only"
                    t["degraded_from"] = t.get("degraded_from") or "telemetry"
                elif t["spec"]["kind"] == TaskKind.ESCORT.value:
                    t["spec"]["sensing_mode"] = "state_only"  # escorted without the stream
                    t["degraded_from"] = t.get("degraded_from") or "conversational"
                if _spec(t).priority >= floor:
                    escalated.append(tid)
            for f in flows.values():
                if f["task_id"] in problems["i1"] and f["state"] == "rejected":
                    f.update(state="released", reason=f"post held without sensing ({f['reason']})")
            problems = flow_rules.check(tasks, flows)
        if requested or released or problems["i1"] or problems["i2"]:
            report = None
            if getattr(deps.network, "mode", "") == "capacity":
                report = await deps.network.get_achievable(state["mission_id"])
            run.decide(
                DecisionKind.RECONCILE,
                {
                    "requested": requested,
                    "released": released,
                    "answers": answers_all,
                    "switched_to_telemetry": switched,
                    "relocated": relocated,
                    "escalated_without_flow": escalated,
                    "achievable": report,
                    "tasks_without_flow": problems["i1"],
                    "flows_without_task": problems["i2"],
                    "iterations": rounds,
                },
                policy={
                    "rule": "D-02: flows reconciled before publication (SPEC A3.7)",
                    "network": getattr(deps.network, "mode", "always_grant"),
                    "reconcile": net_cfg.reconcile.mode,
                },
                justification="robot and network branches consistent"
                if not problems["i1"] and not problems["i2"]
                else "inconsistency left after the fix-up passes",
            )
        if switched:
            run.note(
                "📶 The network cannot carry video at "
                + join(narr.task_id(tasks, t) for t in dict.fromkeys(switched))
                + ": "
                + ("that post switches" if len(set(switched)) == 1 else "those posts switch")
                + " to telemetry sensing."
            )
        if escalated:
            run.note(
                "⚠️ The network cannot carry any stream for "
                + join(narr.task_id(tasks, t) for t in escalated)
                + ": the robot goes on without it (state only). Please decide (move it, "
                "or accept it like this)."
            )
        if problems["i1"]:
            run.note(
                f"⚠️ {plural(len(problems['i1']), 'task')} without a network flow: "
                + join(narr.task_id(tasks, t) for t in problems["i1"])
                + "."
            )
        return run.out(tasks=tasks, flows=flows)

    async def publish_envelopes(state: CoordState) -> dict[str, Any]:
        run = _Run(deps, state)
        tasks = copy.deepcopy(state.get("tasks") or {})
        fleet = copy.deepcopy(state.get("fleet") or {})
        envelopes = copy.deepcopy(state.get("envelopes") or {})
        replies: list[dict[str, Any]] = []
        started = time.perf_counter()
        round_trip: dict[str, float] = {}
        batch = []
        for rid in sorted(set(state.get("dirty") or [])):
            entry = fleet.get(rid)
            if entry is None or entry["status"] != ACTIVE_ROBOT:
                continue
            mine = sorted(
                t
                for t, e in tasks.items()
                if e.get("robot_id") == rid and e["status"] in ("assigned", "in_progress")
            )
            # A continuous patrol's lap says so: the robot starts the next lap itself.
            specs = [
                _spec(tasks[t]).model_copy(update={"repeat": bool(tasks[t].get("repeat"))})
                for t in mine
            ]
            rec = envelopes.get(rid) or {
                "envelope_id": f"env-{state['mission_id']}-{rid}"[:120],
                "version": 0,
            }
            rec = {**rec, "version": rec["version"] + 1}
            envelope = make_envelope(
                state["mission_id"],
                rid,
                rec,
                specs,
                POSTURE_PRIORITY[Posture(state.get("posture") or "routine")],
                pinned=[t for t in mine if tasks[t].get("pinned_to") == rid],
                flows={
                    f["task_id"]: {
                        "class": f["flow_class"],
                        "state": f["state"],
                        "ul_mbps": (f.get("granted") or {}).get("ul_mbps"),
                    }
                    for f in (state.get("flows") or {}).values()
                    if f.get("robot_id") == rid
                    and f["task_id"] in mine
                    and f["state"] != "released"
                },
            )
            batch.append((rid, entry, rec, envelope, mine))

        async def send(rid: str, entry: dict[str, Any], envelope: Any) -> Any:
            sent_at = time.perf_counter()
            try:
                async with asyncio.timeout(envelope_timeout_s):
                    reply = await deps.links(rid, entry).send_envelope(envelope)
                round_trip[rid] = time.perf_counter() - sent_at
                return reply
            except Exception as exc:
                return exc

        # Every robot gets its envelope at once (a robot's model deliberates before it
        # answers: one at a time, the last would wait for all the others).
        sent_at_wall = time.time()  # reconciled envelopes handed to the transport
        results = await asyncio.gather(*(send(rid, e, env) for rid, e, _, env, _ in batch))
        for (rid, _entry, rec, _envelope, mine), reply in zip(batch, results, strict=True):
            if isinstance(reply, BaseException):
                log.warning("envelope to %s failed: %s", rid, reply)
                fleet[rid]["status"] = "lost"
                fleet[rid]["lost_by"] = "envelope"  # asked again at the next allocation
                # Its work waits for it: out of reach is most often a coverage
                # gap; it gets this envelope when it is back in contact.
                replies.append({"robot_id": rid, "error": str(reply), "released": []})
                run.note(f"I can't reach {narr.robot(rid, fleet)} right now; it keeps its work.")
                continue
            rec["ack"] = reply.decision
            rec["task_ids"] = mine
            envelopes[rid] = rec
            replies.append(reply.model_dump(mode="json"))
        upd: dict[str, Any] = {}
        t0 = state.get("kpi11_t0")
        if t0 is not None:  # reaction time: posture change -> reconciled envelopes published
            run.decide(
                DecisionKind.RECONCILE,
                {
                    "kpi": "UC2-KPI-11",
                    # The KPI: until the reconciled envelopes are published (sent) ...
                    "posture_change_to_publication_s": round(sent_at_wall - t0, 3),
                    # ... and, for reference, until every robot has answered (its model
                    # deliberates first).
                    "posture_change_to_acknowledged_s": round(time.time() - t0, 3),
                    "robots": sorted(rid for rid, *_ in batch),
                    "decompose": (state.get("event") or {}).get("phase") or "first",
                },
                policy={"decompose_mode": cfg.fleet.defaults.coordinator.decompose_mode},
                justification="envelopes for the new posture published",
            )
            upd["kpi11_t0"] = None
        outcome: dict[str, str] = {}
        for reply in replies:
            if "error" in reply:
                outcome[reply["robot_id"]] = "unreachable"
            elif reply["refusals"]:
                outcome[reply["robot_id"]] = f"{reply['decision']}: refused " + join(
                    f"{narr.task_id(tasks, t)} ({r})" for t, r in reply["refusals"].items()
                )
            else:
                outcome[reply["robot_id"]] = (
                    f"accepted ({plural(len(reply['accepted_task_ids']), 'task')})"
                )
            if reply["robot_id"] in round_trip:
                outcome[reply["robot_id"]] += f" in {fmt_latency(round_trip[reply['robot_id']])}"
        if outcome:
            run.finish(
                "send_envelopes",
                {r: "envelope" for r in outcome},
                outcome,
                time.perf_counter() - started,
            )
        return run.out(
            **upd,
            tasks=tasks,
            fleet=fleet,
            envelopes=envelopes,
            replies=replies,
            dirty=[],
            pending_call=None,
        )

    async def handle_reply(state: CoordState) -> dict[str, Any]:
        run = _Run(deps, state)
        tasks = copy.deepcopy(state.get("tasks") or {})
        fleet = copy.deepcopy(state.get("fleet") or {})
        dirty: list[str] = []
        retry = False
        for reply in state.get("replies") or []:
            if "error" in reply:
                retry = retry or bool(reply.get("released"))
                continue
            rid = reply["robot_id"]
            for tid, reason in (reply.get("refusals") or {}).items():
                entry = tasks.get(tid)
                if entry is None or entry.get("robot_id") != rid:
                    continue
                entry.update(status="pending", robot_id=None)
                detail = (reply.get("refusal_details") or {}).get(tid, "")
                if not (reason == "battery" and detail.startswith("recharging")):
                    # A refusal while charging says nothing about the task: no lasting bar.
                    entry.setdefault("refused_by", []).append(rid)
                    entry.setdefault("refused_for", {})[rid] = reason  # battery: until charged
                    seen = (state.get("fleet") or {}).get(rid, {}).get("seen_battery_pct")
                    if reason == "battery" and seen is not None:
                        entry.setdefault("refused_battery", {})[rid] = seen
                if entry["spec"].get("interaction_id") and entry.get("pinned_to") == rid:
                    # A person is waiting: their escort goes on with another robot.
                    entry["pinned_to"] = None
                if rid not in dirty:
                    dirty.append(rid)
                entry_fleet = (state.get("fleet") or {}).get(rid)
                unknown_battery = (
                    entry_fleet is not None and not _manifest(entry_fleet).battery_reliable
                )
                # It is on its way to charge (the report may not have reached me yet): not a
                # battery shortfall for this route; offer it nothing until it is charged.
                recharging = reason == "battery" and detail.startswith("recharging")
                if recharging and fleet.get(rid, {}).get("status") == ACTIVE_ROBOT:
                    fleet[rid]["status"] = "charging"
                battery = battery_refusal(cfg, detail, unknown_battery)
                why = {
                    "battery": battery,
                    "capability": "it can't do that",
                    "region": "it's outside its area",
                    "conflict": "it's committed elsewhere",
                    "link": "it has no connection",
                }.get(reason, reason)
                said = str(reply.get("justification") or "").strip()
                own_words = said and not said.startswith(("Refusing", "Proposing", "Accepting"))
                run.note(
                    f"{narr.robot(rid, state.get('fleet'))} declined to "
                    f"{narr.task_id(tasks, tid)} ({why}"
                    + (f"; it says: “{said[:200]}”" if own_words else "")
                    + "); asking another robot."
                )
                retry = True
        anomaly_upd = await _anomalies(run, state, tasks, fleet)
        rounds = state.get("rounds") or 0
        pending = any(t["status"] == "pending" for t in tasks.values())
        branch = (
            "allocate"
            if retry and pending and rounds < cfg.fleet.defaults.coordinator.max_allocation_rounds
            else "audit"
        )
        if branch == "audit" and dirty:
            branch = "publish"  # re-issue envelopes without the refused tasks
        return run.out(
            **anomaly_upd,
            tasks=tasks,
            fleet=fleet,
            dirty=dirty,
            branch=branch,
            reallocating=True if retry else state.get("reallocating"),
        )

    async def _anomalies(
        run: _Run, state: CoordState, tasks: dict[str, dict[str, Any]], fleet: dict[str, Any]
    ) -> dict[str, Any]:
        """Robots behaving unexpectedly are analysed by the model (an
        ``anomaly`` event, its own run after this one). Known causes are not anomalies: a
        robot out of coverage or on its way to charge."""
        coord = cfg.fleet.defaults.coordinator
        streak = dict(state.get("refusal_streak") or {})
        found: list[tuple[str, str]] = []
        for reply in state.get("replies") or []:
            if "error" in reply:
                continue
            rid = reply["robot_id"]
            details = reply.get("refusal_details") or {}
            meaningful = {
                tid: reason
                for tid, reason in (reply.get("refusals") or {}).items()
                if reason != "link" and not str(details.get(tid, "")).startswith("recharging")
            }
            if meaningful and not reply.get("accepted_task_ids"):
                streak[rid] = streak.get(rid, 0) + 1
                if streak[rid] >= 2:
                    reasons = join(sorted(set(meaningful.values())))
                    found.append(
                        (
                            f"robot:{rid}",
                            f"{narr.robot(rid, fleet)} has refused all the work it was given "
                            f"twice in a row ({reasons}).",
                        )
                    )
            elif reply.get("accepted_task_ids"):
                streak[rid] = 0
        active = {r for r, e in fleet.items() if e.get("status") == ACTIVE_ROBOT}
        for tid, t in sorted(tasks.items()):
            refused = set(t.get("refused_by") or [])
            known = set((t.get("refused_for") or {}).values()) <= {"battery"}  # narrated
            if t["status"] == "pending" and len(active) >= 2 and active <= refused and not known:
                found.append((f"task:{tid}", f"every robot refused to {narr.task_id(tasks, tid)}."))
        upd: dict[str, Any] = {"refusal_streak": streak}
        if not found or deps.model is None or not coord.anomaly_check:
            return upd
        now = deps.clock.now()
        seen = dict(state.get("anomalies") or {})
        submit = deps.submit or _sdk_submit(coord.assistant_id)
        for key, what in found:
            last = seen.get(key)
            if last and (now - datetime.fromisoformat(last)).total_seconds() < (
                coord.anomaly_cooldown_s
            ):
                continue
            seen[key] = now.isoformat()
            try:
                await submit(state["mission_id"], {"type": "anomaly", "key": key, "what": what})
            except Exception as exc:
                log.warning("queueing the anomaly analysis failed: %s", exc)
        upd["anomalies"] = seen
        return upd

    def route_reply(state: CoordState) -> str:
        return {"allocate": "allocate", "publish": "reconcile"}.get(state.get("branch"), "audit")

    # ------------------------------------------------------------------ audit / escalate

    async def audit(state: CoordState) -> dict[str, Any]:
        records = [Decision.model_validate(raw) for raw in state.get("pending_decisions") or []]
        if records:  # file/DB I/O off the event loop (the Agent Server is async)
            await asyncio.to_thread(lambda: [deps.decisions.record(d) for d in records])
        tasks = state.get("tasks") or {}
        status = state.get("status") or "idle"
        notes = list(state.get("notes") or [])
        if (state.get("event") or {}).get("status_after"):
            notes.append(narr.status(state))
        # Routine work of a robot that is recharging, and work an emergency preempted (it
        # resumes with the all clear): the mission is not over while either waits.
        waiting = ("paused", "preempted")
        if (
            status == "active"
            and tasks
            and not any(t["status"] in OPEN or t["status"] in waiting for t in tasks.values())
        ):
            status = "completed"
            done = sum(t["status"] == "done" for t in tasks.values())
            notes.append(f"🏁 Mission complete: {plural(done, 'task')} done out of {len(tasks)}.")
        store = _store()
        if store is not None:
            robots = sorted(
                {t["robot_id"] for t in tasks.values() if t.get("robot_id") and t["status"] in OPEN}
            )
            await store.aput(
                ("a6g", "context", "missions"),
                state["mission_id"],
                {
                    "mission_id": state["mission_id"],
                    "status": status,
                    "posture": state.get("posture"),
                    "robots": robots,
                },
            )
        # Never ask the operator about a task that found a robot in the meantime.
        escalations = _live_escalations(state.get("escalations") or [], tasks)
        out: dict[str, Any] = {
            "pending_decisions": [],
            "notes": [],
            "event": None,
            "status": status,
            "escalations": escalations,
            "branch": "escalate" if escalations else "end",
        }
        if notes:
            text = "\n\n".join(notes)
            if cfg.fleet.defaults.coordinator.narration == "llm":
                text = await polish(deps.model, text)
            ev = state.get("event") or {}
            meta = {}
            if ev and ev.get("type") != "chat" and ev.get("issued_by") != "chat":
                # Not an answer to the operator's message: something the swarm reported on
                # its own (agent-chat-ui labels it "Swarm update" and shows it as it comes).
                meta = {"a6g_update": True, "a6g_at": deps.clock.now().isoformat()}
            out["messages"] = [AIMessage(text, response_metadata=meta)]
        return out

    def route_audit(state: CoordState) -> str:
        return "escalate" if state.get("escalations") else END

    async def escalate(state: CoordState) -> dict[str, Any]:
        """Pause for the operator (agent-chat-ui HITL format; ``a6g resume`` sends the same)."""
        esc = state["escalations"][0]
        if esc["kind"] == "observation":
            obs = esc["observation"]
            request = {
                "action_requests": [
                    {
                        "name": "confirm_observation",
                        "args": {
                            "obs_id": obs["obs_id"],
                            "robot_id": obs["robot_id"],
                            "label": obs["label"],
                            "confidence": obs["confidence"],
                            "pose": obs["pose"],
                            "image_uri": (obs.get("evidence") or {}).get("image_uri"),
                        },
                        "description": (
                            f"**{obs['robot_id']}** spotted **{obs['label']}** "
                            f"({obs['confidence']:.0%} confident). Approve to confirm it and "
                            "send a robot to guard the spot, reject to dismiss it, or edit to "
                            "correct the label."
                        ),
                    }
                ],
                "review_configs": [
                    {
                        "action_name": "confirm_observation",
                        "allowed_decisions": ["approve", "edit", "reject"],
                    }
                ],
            }
        elif esc["kind"] == "unclear_order":
            quick = "".join(f"\n- {q}" for q in esc.get("quick") or []) or " nothing"
            request = {
                "action_requests": [
                    {
                        "name": "unclear_order",
                        # The chat UI shows the args as the form (the description sits in
                        # another tab): the quick reading to approve, and an empty field for
                        # the order in other words (not the order repeated).
                        "args": {
                            "quick_reading_to_approve": "; ".join(
                                q.replace("**", "") for q in esc.get("quick") or []
                            )
                            or "nothing",
                            "rephrase_order": "",
                        },
                        "description": (
                            f"🤷 I couldn't work out *{esc['text']}* ({esc['why']}).\n\n"
                            f"**Approve**: carry out my quick reading:{quick}\n\n"
                            "**Edit**: write the order in other words (I'll plan it again).\n\n"
                            "**Reject**: drop it; nothing changes."
                        ),
                    }
                ],
                "review_configs": [
                    {
                        "action_name": "unclear_order",
                        "allowed_decisions": ["approve", "edit", "reject"],
                    }
                ],
            }
        elif esc["kind"] == "apply_fix":
            request = {
                "action_requests": [
                    {
                        "name": "apply_fix",
                        # shown as the form (the description sits in another tab)
                        "args": {"proposed_changes": "; ".join(esc["changes"])},
                        "description": (
                            f"{esc['answer']}\n\n**Approve** to apply:"
                            + "".join(f"\n- {line}" for line in esc["changes"])
                            + "\n\n**Reject** to leave everything as it is."
                        ),
                    }
                ],
                "review_configs": [
                    {"action_name": "apply_fix", "allowed_decisions": ["approve", "reject"]}
                ],
            }
        elif esc["kind"] == "end_emergency":
            request = {
                "action_requests": [
                    {
                        "name": "end_emergency",
                        "args": {"order": esc["text"]},
                        "description": (
                            "🚨 The campus is in **emergency** mode. Approve to end the "
                            "emergency (posts released; paused work stays stopped) and carry out "
                            f"*{esc['text']}*; reject to stay in emergency mode (the order is "
                            "dropped)."
                        ),
                    }
                ],
                "review_configs": [
                    {"action_name": "end_emergency", "allowed_decisions": ["approve", "reject"]}
                ],
            }
        else:
            request = {
                "action_requests": [
                    {
                        "name": "unallocatable_task",
                        "args": {
                            "task_id": esc["task_id"],
                            "reason": esc["reason"],
                            "detail": esc.get("detail", ""),
                        },
                        "description": (
                            f"High-priority task **{esc['task_id']}** cannot be allocated "
                            f"({esc['reason']}: {esc.get('detail', '')}). Approve to keep "
                            "retrying, reject to drop it, or edit its priority."
                        ),
                    }
                ],
                "review_configs": [
                    {
                        "action_name": "unallocatable_task",
                        "allowed_decisions": ["approve", "edit", "reject"],
                    }
                ],
            }
        answer = interrupt(request)

        run = _Run(deps, state)
        decision = _first_decision(answer)
        tasks = copy.deepcopy(state.get("tasks") or {})
        branch = "audit"
        extra: dict[str, Any] = {}
        if esc["kind"] == "observation":
            obs = dict(esc["observation"])
            if decision["type"] == "edit":
                obs.update((decision.get("edited_action") or {}).get("args") or {})
            if decision["type"] in ("approve", "edit"):
                pose = obs["pose"]
                guard = make_task(
                    TaskKind.HOLD_POST,
                    PoseTarget(pose={"x": pose["x"], "y": pose["y"]}),
                    Posture(state.get("posture") or "routine"),
                    priority=95,
                    suffix=obs["obs_id"],
                )
                if guard.task_id not in tasks:
                    tasks[guard.task_id] = {
                        "spec": guard.model_dump(mode="json"),
                        "status": "pending",
                        "robot_id": None,
                        "refused_by": [],
                        "posture": state.get("posture") or "routine",
                        "pinned_to": None,
                        "attempts": 0,
                    }
                store = _store()
                if store is not None:
                    await store.aput(
                        ("a6g", "ltm", "observations"), obs["obs_id"], {**obs, "confirmed": True}
                    )
                run.note(
                    f"Thanks — confirmed **{obs['label']}**. "
                    f"I'm sending a robot to {narr.task(guard)}."
                )
                branch = "allocate"
            else:
                run.note(
                    f"Understood, I'll ignore **{obs['label']}**"
                    + (f" ({decision['message']})" if decision.get("message") else "")
                    + "."
                )
        elif esc["kind"] == "unclear_order":
            if decision["type"] == "reject":
                run.note(f"OK, I've dropped *{esc['text']}*; nothing changed.")
            else:
                text = esc["text"]
                if decision["type"] == "edit":
                    args = (decision.get("edited_action") or {}).get("args") or {}
                    rephrased = str(args.get("rephrase_order") or args.get("order") or "").strip()
                    if rephrased and rephrased != text:
                        text = rephrased
                    else:  # only the quick reading field touched: that is an approval
                        decision = {**decision, "type": "approve"}
                event = {
                    "type": "goal",
                    "text": text,
                    "posture": esc["posture"],
                    "issued_by": "chat",
                    "approved": True,
                }
                if decision["type"] == "approve":
                    event["quick_only"] = True
                elif needs_care(text):
                    event["careful"] = needs_care(text)
                extra = {
                    "event": event,
                    "pending_call": run.announce(
                        "plan_mission", {"goal": text, "posture": esc["posture"]}
                    ),
                }
                branch = "decompose"
        elif esc["kind"] == "apply_fix":
            if decision["type"] == "reject":
                run.note("OK, I leave everything as it is.")
            else:
                fleet = state.get("fleet") or {}
                posture = Posture(state.get("posture") or "routine")
                ctx = _context({**state, "tasks": tasks}, posture, fleet)
                plan = agentic.AgentPlan.model_validate(esc["fix"])
                stale = agentic.validate(plan, ctx)
                if stale:  # the swarm moved on while the operator decided
                    run.note(
                        "Things changed while you decided, so the fix no longer applies ("
                        + stale[0]
                        + "). Ask me again if it still looks wrong."
                    )
                else:
                    dirty = list(state.get("dirty") or [])
                    # straight to allocation: new work is pending (no plan validation)
                    _apply_plan(
                        run, tasks, plan, ctx, posture, dirty, fleet, esc["text"], "pending"
                    )
                    run.note("✅ Fixed: " + "; ".join(esc["changes"]) + ".")
                    extra = {"dirty": dirty}
                    branch = "allocate"
        elif esc["kind"] == "end_emergency":
            if decision["type"] == "reject":
                run.note(f"OK, we stay in **emergency** mode; I won't start *{esc['text']}*.")
            else:  # approve: all clear, then the order (planned in this run)
                dirty = list(state.get("dirty") or [])
                cleared: dict[str, Any] = {}
                # Only the approved order runs: paused patrols are not resumed with it.
                _all_clear(
                    run, state, tasks, dirty, cleared, f"approved: {esc['text']}", resume=False
                )
                after = cleared.get("posture") or Posture.ROUTINE.value
                goal = Posture(esc["posture"])
                extra = {
                    "dirty": dirty,
                    "status": "active",
                    "posture": goal.value
                    if _posture_rank(cfg, goal.value) >= _posture_rank(cfg, after)
                    else after,
                    "posture_changed": True,
                    "event": {
                        "type": "goal",
                        "text": esc["text"],
                        "posture": goal.value,
                        "issued_by": "chat",
                        "approved": True,
                    },
                    "pending_call": run.announce(
                        "plan_mission", {"goal": esc["text"], "posture": goal.value}
                    ),
                }
                branch = "decompose"
        else:
            tid = esc["task_id"]
            if decision["type"] == "reject":
                tasks[tid]["status"] = "dropped"
                run.note(f"OK, I've dropped '{narr.task_id(tasks, tid)}'.")
            else:
                if decision["type"] == "edit":
                    args = (decision.get("edited_action") or {}).get("args") or {}
                    if "priority" in args:
                        spec = _spec(tasks[tid]).model_copy(
                            update={"priority": int(args["priority"])}
                        )
                        tasks[tid]["spec"] = spec.model_dump(mode="json")
                tasks[tid]["escalated"] = False
                run.note(f"OK, I'll keep trying to find a robot to {narr.task_id(tasks, tid)}.")
                branch = "allocate"
        run.call(
            "operator_decision",
            {"question": request["action_requests"][0]["description"]},
            {
                "decision": decision["type"],
                **({"message": decision["message"]} if decision.get("message") else {}),
            },
        )
        run.decide(
            DecisionKind.ESCALATE,
            {"escalation": esc, "operator_decision": decision},
            policy={"rule": "human_in_the_loop"},
            justification=f"operator {decision['type']}",
            inputs_ref=esc["esc_id"],
        )
        return run.out(
            **extra, tasks=tasks, escalations=state["escalations"][1:], branch=branch, rounds=0
        )

    def route_escalate(state: CoordState) -> str:
        return {"allocate": "allocate", "decompose": "decompose"}.get(
            state.get("branch") or "", "audit"
        )

    g = StateGraph(CoordState)
    for name, fn in [
        ("ingest", ingest),
        ("decompose", decompose),
        ("validate", validate),
        ("arbitrate", arbitrate),
        ("allocate", allocate),
        ("check_order", check_order),
        ("reconcile", reconcile),
        ("publish_envelopes", publish_envelopes),
        ("handle_reply", handle_reply),
        ("audit", audit),
        ("escalate", escalate),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "ingest")
    g.add_conditional_edges("ingest", route_ingest, ["decompose", "allocate", "reconcile", "audit"])
    g.add_edge("decompose", "validate")
    g.add_edge("validate", "arbitrate")
    g.add_edge("arbitrate", "allocate")
    g.add_edge("allocate", "check_order")
    g.add_conditional_edges("check_order", route_check, ["allocate", "reconcile"])
    g.add_edge("reconcile", "publish_envelopes")
    g.add_edge("publish_envelopes", "handle_reply")
    g.add_conditional_edges("handle_reply", route_reply, ["allocate", "reconcile", "audit"])
    g.add_conditional_edges("audit", route_audit, ["escalate", END])
    g.add_conditional_edges("escalate", route_escalate, ["allocate", "decompose", "audit"])
    return g


def _first_decision(answer: Any) -> dict[str, Any]:
    """Accept agent-chat-ui's ``{"decisions": [...]}``, a bare decision dict, or a string."""
    if isinstance(answer, dict) and answer.get("decisions"):
        return dict(answer["decisions"][0])
    if isinstance(answer, dict) and "type" in answer:
        return dict(answer)
    if isinstance(answer, str) and answer in ("approve", "reject"):
        return {"type": answer}
    return {"type": "reject", "message": f"unrecognised answer {answer!r}"}


def _store():
    try:
        return get_store()
    except Exception:
        return None


def _load_manifest(cfg: A6GConfig, robot_id: str) -> dict[str, Any] | None:
    path = cfg.config_dir / "robots" / f"{robot_id}.yaml"
    if not path.exists():
        return None
    return load_manifest(path).model_dump(mode="json")
