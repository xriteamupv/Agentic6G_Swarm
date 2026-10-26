"""Robot agent runtime (SPEC 8.1, 9.3, 9.4).

Each incoming event (envelope, CFP, award, observation, link change, operator command)
triggers one invocation of the compiled graph with ``thread_id = f"{robot_id}:{envelope_id}"``
on a *local* SQLite checkpointer, so the robot survives a process restart without the
edge. Invocations are serialised. Background tasks:

* the skill worker runs one dispatched MCP skill at a time and feeds its result back;
* the monitor polls ``get_state`` (default 1 Hz), publishes ``RobotState`` on
  ``a6g/fleet/<robot_id>/state``, turns link changes into events, flushes queued reports,
  tracks coordinator reachability and, while the coordinator is lost, redistributes the
  swappable tasks of dead peers by contract-net;
* bus listeners: kill-switch, coordinator heartbeat, peer state/status/envelopes.

Coordinator loss (SPEC 9.4): no heartbeat for ``coordinator_timeout_s``, an ``offline``
last-will, or ``a2a_failures_for_loss`` consecutive failed uplink calls. On return, each
envelope thread sends a ``reconcile`` report (current envelope + awards).
"""

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from a6g_allocator.cost import best_bid
from a6g_common.schemas import (
    Award,
    Bid,
    CallForProposals,
    DecisionKind,
    Envelope,
    EnvelopeReply,
    LinkQuality,
    RefusalReason,
    Report,
    ReportKind,
    RobotState,
    SkillName,
    SkillOutcome,
    TaskSpec,
    TrafficClass,
)
from a6g_interactions import language as lang_
from a6g_observability.checkpoint_meter import CHECKPOINT_EVENT
from a6g_observability.decision_log import make_decision
from a6g_transport.budget import Admission
from a6g_transport.messaging import (
    TOPIC_ENVELOPE,
    TOPIC_HEARTBEAT,
    TOPIC_KILLSWITCH,
    TOPIC_STATE,
    TOPIC_STATUS,
    BusMessage,
    TelemetryBus,
)
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from a6g_robot_agent.graph import ACTIVE_STATUSES, AgentDeps, build_graph
from a6g_robot_agent.peers import PeerDirectory
from a6g_robot_agent.skills import SkillClient

log = logging.getLogger(__name__)

OPEN_TASK_STATUSES = ("pending", "dispatched")

#: Accepted statuses from the knowledge server (SPEC A3.4.5: escorts replan at once).
TOPIC_KB_STATUS = "a6g/kb/status"

Gate = Callable[[], contextlib.AbstractAsyncContextManager[Any]]


def status_payload(state: str, a2a_url: str | None = None) -> str:
    """Retained ``a6g/fleet/<id>/status`` payload; ``offline`` is also the MQTT last-will."""
    return json.dumps({"state": state, "a2a_url": a2a_url})


def parse_status(payload: bytes | None) -> dict[str, Any]:
    text = (payload or b"").decode(errors="replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = {"state": text}
    return data if isinstance(data, dict) else {"state": str(data)}


class CountingUplink:
    """Wraps an uplink and counts consecutive delivery failures (coordinator loss)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.consecutive_failures = 0

    async def send(self, report: Any) -> bool:
        ok = await self.inner.send(report)
        self.consecutive_failures = 0 if ok else self.consecutive_failures + 1
        return ok


class RobotAgent:
    def __init__(
        self,
        deps: AgentDeps,
        skills: SkillClient,
        checkpointer: BaseCheckpointSaver,
        *,
        bus: TelemetryBus | None = None,
        peers: PeerDirectory | None = None,
        poll_interval_s: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        gate: Gate | None = None,
        coordinator_timeout_s: float = 10.0,
        a2a_failures_for_loss: int = 2,
        cfp_deadline_ms: int = 1500,
        a2a_url: str | None = None,
    ) -> None:
        self.deps = deps
        self.robot_id = deps.robot_id
        self.skills = skills
        self.checkpointer = checkpointer
        self.bus = bus
        self.peers = peers
        self.poll_interval_s = poll_interval_s or deps.agent_cfg.state_poll_interval_s
        self._sleep = sleep
        self._gate: Gate = gate or contextlib.nullcontext
        self.coordinator_timeout_s = coordinator_timeout_s
        self.a2a_failures_for_loss = a2a_failures_for_loss
        self.cfp_deadline_ms = cfp_deadline_ms
        self.a2a_url = a2a_url
        if not isinstance(deps.uplink, CountingUplink):
            deps.uplink = CountingUplink(deps.uplink)
        self.uplink: CountingUplink = deps.uplink  # type: ignore[assignment]
        self.graph = build_graph(deps).compile(checkpointer=checkpointer)
        self._lock = asyncio.Lock()
        self._threads: list[str] = []  # active envelope threads, in acceptance order
        self._queue: deque[tuple[str, dict[str, Any]]] = deque()
        self._skill: asyncio.Task[None] | None = None
        self._skill_task_id: str | None = None
        self._background: list[asyncio.Task[None]] = []
        self._followups: set[asyncio.Task[Any]] = set()
        self._cfp_thread: dict[str, str] = {}
        self._replan_holds: dict[str, datetime] = {}  # thread -> end of a replan hold
        self._outbox_len: dict[str, int] = {}
        self._hold_since: dict[str, datetime] = {}  # hold_then_recover timers per thread
        self._recharging = False  # low battery: at / driving to the staging area
        self._recharge_threads: list[str] = []
        self._recharge_phase: str | None = None  # "going" to the staging area, "charging"
        self._idle_since: float | None = None  # wall clock: low and waiting for orders
        self._work_asked: tuple[Any, float] | None = None  # last ask: (sim, wall) time
        self._asks = 0  # asks this idle spell (the wait between them grows)
        #: Recharge advisor (agent.recharge_advisor): peers' battery and charging, from
        #: their state and ``a6g/fleet/<id>/charge`` messages; the 5 % band last advised.
        self._peer_battery: dict[str, float] = {}
        self._peer_charging: set[str] = set()
        self._advised_band: int | None = None
        self._band_seen: int | None = None
        #: The mission of the last envelope with work (None once released): idle, recharge
        #: and charged reports go to it even when no envelope thread is open.
        self._mission: str | None = None
        self._direct: list[Report] = []  # such reports not delivered yet (no coverage)
        self._published_envelope: str | None = None
        self.link_quality = LinkQuality.OK
        self.killswitch_engaged = False
        # Coordinator and peer liveness (soft state rebuilt from the bus).
        self._started_at: datetime = deps.clock.now()
        self._coord_last: datetime | None = None
        self._coord_offline = False
        self.coordinator_reachable = True
        self._peer_seen: dict[str, datetime] = {}
        self._peer_offline: set[str] = set()
        self._peer_envelopes: dict[str, dict[str, Any]] = {}
        self._redistributed: set[str] = set()
        #: a dead peer's task nobody could take: offered again from this time (SPEC A4 N1)
        self._retry_at: dict[str, datetime] = {}
        #: mission -> the robot's latest envelope thread, active or finished: an idle robot
        #: bids for and takes a dead peer's work on it (SPEC A4 N1, 2026-10-26)
        self._mission_thread: dict[str, str] = {}
        #: thread -> the envelope's flow grants per task (SPEC A4.7, I3), from the last run
        self._grants: dict[str, dict[str, Any]] = {}
        # Speech output (SPEC A3.4): what the robot said while answering one ``say`` call;
        # everything also goes to ``on_speech`` (the sim console, a Gazebo label). Never
        # logged (rule 28).
        self._spoken: list[str] | None = None
        self.on_speech: Callable[[str, str], None] | None = None
        deps.speak = self._speak

    # ------------------------------------------------------------------ lifecycle

    def thread_id(self, envelope_id: str) -> str:
        return f"{self.robot_id}:{envelope_id}"

    def _config(self, thread: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread}}

    def _spawn(self, coro: Awaitable[None], name: str) -> None:
        self._background.append(asyncio.create_task(coro, name=f"{self.robot_id}-{name}"))  # type: ignore[arg-type]

    async def start(self) -> None:
        await self._restore()
        self._spawn(self._monitor(), "monitor")
        if self.bus is not None:
            await self.bus.publish(
                TOPIC_STATUS.format(robot_id=self.robot_id),
                status_payload("online", self.a2a_url),
                qos=1,
                retain=True,
            )
            self._spawn(self._listen(TOPIC_KILLSWITCH, self.on_killswitch), "killswitch")
            self._spawn(self._listen(TOPIC_HEARTBEAT, self._on_heartbeat), "heartbeat")
            self._spawn(self._listen("a6g/fleet/+/+", self._on_fleet_message), "peers")
            self._spawn(self._listen(TOPIC_KB_STATUS, self._on_kb_status), "kb-status")

    async def stop(self, graceful: bool = True) -> None:
        """Stop the process. ``graceful=False`` emulates a crash (the broker's last-will
        then announces ``offline``)."""
        tasks = [*self._background, *self._followups]
        if self._skill is not None:
            tasks.append(self._skill)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._background.clear()
        self._skill = None
        if graceful and self.bus is not None:
            with contextlib.suppress(Exception):
                await self.bus.publish(
                    TOPIC_STATUS.format(robot_id=self.robot_id),
                    status_payload("offline", self.a2a_url),
                    qos=1,
                    retain=True,
                )

    async def __aenter__(self) -> "RobotAgent":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    #: Re-adopt active envelopes on start (a robot process restarted mid-mission). A dev
    #: stack starting afresh sets False: the world is reset and the missions are gone, so
    #: the old threads are deleted (and the SQLite file compacted: it reached 580 MB over
    #: many sessions). The decision log keeps the history.
    resume: bool = True

    async def _restore(self) -> None:
        """Re-adopt active envelopes from the local checkpointer after a restart."""
        # Collect ids first: alist() holds the saver's lock while iterating, so calling
        # aget_state() inside the loop would deadlock.
        threads: list[str] = []
        async for checkpoint in self.checkpointer.alist(None):
            thread = checkpoint.config["configurable"]["thread_id"]
            if thread.startswith(f"{self.robot_id}:") and thread not in threads:
                threads.append(thread)
        if not self.resume:
            await self._forget(threads)
            return
        for thread in threads:
            snapshot = await self.graph.aget_state(self._config(thread))
            if snapshot.values.get("status") not in ACTIVE_STATUSES:
                continue
            if self._expired(snapshot.values.get("envelope")):
                log.warning("%s dropping expired envelope %s", self.robot_id, thread)
                continue
            self._threads.append(thread)
        if self._threads:
            # The skill server may still be executing the dead process's last call.
            await self.skills.call(SkillName.CANCEL)
            await self._check_link()
        for thread in sorted(self._threads):
            log.info("%s resuming %s after restart", self.robot_id, thread)
            await self.invoke(thread, {"type": "resume"})

    async def _forget(self, threads: list[str]) -> None:
        """Fresh start: drop every old envelope thread of this robot and compact the file."""
        for thread in threads:
            await self.checkpointer.adelete_thread(thread)
        if threads:
            log.warning(
                "%s fresh start: dropped %d old envelope threads", self.robot_id, len(threads)
            )
        conn = getattr(self.checkpointer, "conn", None)  # SQLite: give the space back
        if threads and conn is not None:
            with contextlib.suppress(Exception):
                await conn.commit()
                await conn.execute("VACUUM")

    def _expired(self, envelope: dict[str, Any] | None) -> bool:
        until = (envelope or {}).get("valid_until")
        if not until:
            return False
        return datetime.fromisoformat(str(until).replace("Z", "+00:00")) < self.deps.clock.now()

    # ------------------------------------------------------------------ invocation

    async def invoke(self, thread: str, event: dict[str, Any]) -> dict[str, Any]:
        async with self._lock, self._gate():  # never hold time still while queued
            token = CHECKPOINT_EVENT.set(str(event.get("type") or "other"))
            try:
                state = await self.graph.ainvoke(
                    {"event": event, "robot_id": self.robot_id}, self._config(thread)
                )
            finally:
                CHECKPOINT_EVENT.reset(token)
        self._track(thread, state)
        if state.get("cancel_current") and self._skill is not None:
            await self.skills.call(SkillName.CANCEL)  # preempted by a new envelope version
        if state.get("dispatch"):
            self._queue.append((thread, state["dispatch"]))
        for followup in state.get("followups") or []:
            task = asyncio.create_task(self.invoke(thread, followup))
            self._followups.add(task)
            task.add_done_callback(self._followups.discard)
        for action in state.get("actions") or []:
            if action.get("type") == "request_help":  # a validated replan (SPEC A2.3.1)
                self._spawn(self._redistribute_own(state, action["task_id"]), "request_help")
            elif action.get("type") == "forget_interaction":
                self._spawn(self._forget_interaction(action["interaction_id"]), "forget")
        self._pump()
        await self._publish_envelope(state)
        return state

    async def state(self, envelope_id: str) -> dict[str, Any]:
        return (await self.graph.aget_state(self._config(self.thread_id(envelope_id)))).values

    def _track(self, thread: str, state: dict[str, Any]) -> None:
        if state.get("envelope") is not None:
            self._grants[thread] = (state.get("envelope") or {}).get("flows") or {}
        mission = (state.get("envelope") or {}).get("mission_id")
        if mission and not thread.startswith(f"{self.robot_id}:interaction:"):
            self._mission_thread[mission] = thread
        active = state.get("status") in ACTIVE_STATUSES
        if active and thread not in self._threads:
            self._threads.append(thread)
        elif not active and thread in self._threads:
            self._threads.remove(thread)
        self._outbox_len[thread] = len(state.get("outbox") or [])
        if state.get("recovery_stage") == "holding" and state.get("link_lost_since"):
            self._hold_since[thread] = datetime.fromisoformat(state["link_lost_since"])
        else:
            self._hold_since.pop(thread, None)
        if state.get("hold_reason") == "replan" and state.get("hold_until"):
            self._replan_holds[thread] = datetime.fromisoformat(state["hold_until"])
        else:
            self._replan_holds.pop(thread, None)

    def _pump(self) -> None:
        if (self._skill is None or self._skill.done()) and self._queue:
            thread, dispatch = self._queue.popleft()
            self._skill_task_id = dispatch["task_id"]
            self._skill = asyncio.create_task(self._run_skill(thread, dispatch))

    def _open_stream(self, thread: str, task_id: str) -> None:
        """SPEC A4.7, I3 (rule 29): a class above telemetry opens only when its flow is
        granted (or degraded), at the granted rate; otherwise the robot stays on telemetry.
        The opening goes to the message log (``direction: budget``). Synchronous: the
        grants are cached from the last run (an await here shifted the robot's timing)."""
        flow = (self._grants.get(thread) or {}).get(task_id)
        if not flow:
            return
        ok = flow.get("state") in ("granted", "degraded")
        from a6g_observability.comms import comms_log

        comms_log().record(
            {
                "ts": time.time(),
                "direction": "budget",
                "agent": self.robot_id,
                "task_id": task_id,
                "flow_class": flow.get("class"),
                "grant_state": flow.get("state"),
                "opened": flow.get("class") if ok else "telemetry",
                "ul_mbps": (flow.get("ul_mbps") or 0.0) if ok else 0.0,
                "pattern": "budget",
            }
        )

    async def _run_skill(self, thread: str, dispatch: dict[str, Any]) -> None:
        self._open_stream(thread, dispatch["task_id"])
        result = await self.skills.call(
            dispatch["tool"], dispatch["args"], dispatch.get("mission_id")
        )
        event = {
            "type": "skill_result",
            "task_id": dispatch["task_id"],
            "result": result.model_dump(mode="json"),
            # Stopped by the kill-switch: re-run after resume, not a failure.
            "interrupted_by_hold": self.killswitch_engaged or self._recharging,
        }
        with contextlib.suppress(Exception):  # where the leg ended (escort replans)
            event["robot_state"] = (await self.skills.get_state()).model_dump(mode="json")
        self._skill = None
        self._skill_task_id = None
        await self._check_link()  # decide on fresh link state, not the last poll
        state = await self.invoke(thread, event)
        task = (state.get("tasks") or {}).get(dispatch["task_id"])
        if task and task["status"] == "failed" and not self.coordinator_reachable:
            await self._redistribute_own(state, dispatch["task_id"])
        if self._skill is None and not self._queue:
            # Let other active envelopes use the robot.
            for other in [t for t in self._threads if t != thread]:
                await self.invoke(other, {"type": "tick"})
                if self._skill is not None or self._queue:
                    break

    async def idle(self, timeout: float = 30.0) -> None:
        """Wait until no skill is running or queued (tests, shutdown)."""
        async with asyncio.timeout(timeout):
            while (
                (self._skill is not None and not self._skill.done())
                or self._queue
                or self._followups
            ):
                await asyncio.sleep(0.005)

    # ------------------------------------------------------------------ inbound messages

    async def handle_envelope(self, envelope: Envelope) -> EnvelopeReply:
        robot_state = await self.skills.get_state()
        thread = self.thread_id(envelope.envelope_id)
        commitments = []
        for other in self._threads:
            if other == thread:
                continue
            values = (await self.graph.aget_state(self._config(other))).values
            env = values.get("envelope") or {}
            if env.get("mission_id") == envelope.mission_id:
                continue  # same mission: the coordinator arbitrates
            if self._expired(env):
                continue  # past valid_until: no longer a commitment
            commitments.append(
                {"envelope_id": env.get("envelope_id"), "priority": env.get("priority", 0)}
            )
        state = await self.invoke(
            thread,
            {
                "type": "envelope",
                "envelope": envelope.model_dump(mode="json"),
                "robot_state": robot_state.model_dump(mode="json"),
                "commitments": commitments,
                "recharging": self._recharging,
            },
        )
        reply = EnvelopeReply.model_validate(state["reply"])
        # Handled (its thread exists): an empty envelope releases the robot from the
        # mission; one it accepted work from makes it the mission it reports to when idle.
        if not envelope.tasks:
            self._mission = None
        elif reply.accepted_task_ids:
            self._mission = envelope.mission_id
        return reply

    def _bid_thread(self, mission_id: str) -> str | None:
        """The envelope thread a contract-net round runs on: an active one, else the
        robot's finished envelope of that mission (an idle robot, the best placed to take a
        dead peer's work, refused every call before 2026-10-26)."""
        if self._threads:
            return self._threads[0]
        return self._mission_thread.get(mission_id)

    async def handle_cfp(self, cfp: CallForProposals, nl: dict[str, Any] | None = None) -> Bid:
        robot_state = await self.skills.get_state()
        thread = self._bid_thread(cfp.mission_id)
        if thread is None:
            return Bid(
                cfp_id=cfp.cfp_id,
                bidder=self.robot_id,
                refusal_reason=RefusalReason.REGION.value,
                stamp=self.deps.clock.now(),
            )
        self._cfp_thread[cfp.cfp_id] = thread
        state = await self.invoke(
            thread,
            {
                "type": "cfp",
                "cfp": cfp.model_dump(mode="json"),
                "robot_state": robot_state.model_dump(mode="json"),
                "nl": nl,
            },
        )
        return Bid.model_validate(state["reply"])

    async def handle_award(self, award: Award) -> dict[str, Any]:
        thread = self._cfp_thread.get(award.cfp_id) or (self._threads[0] if self._threads else None)
        if thread is None:
            return {"award_id": award.award_id, "accepted": False}
        state = await self.invoke(thread, {"type": "award", "award": award.model_dump(mode="json")})
        return state["reply"]

    async def handle_operator(self, command: str, reason: str = "operator") -> None:
        for thread in list(self._threads):
            await self.invoke(thread, {"type": "operator", "command": command, "reason": reason})

    async def task_queue(self) -> dict[str, Any]:
        """What this robot is doing and in which order it will do the rest (per envelope):
        the coordinator only sees assignments, the order is decided here."""
        envelopes = []
        for thread in list(self._threads):
            values = (await self.graph.aget_state(self._config(thread))).values
            tasks = values.get("tasks") or {}
            envelopes.append(
                {
                    "mission_id": values.get("mission_id"),
                    "status": values.get("status"),
                    "current_task": values.get("current_task_id"),
                    "queue_in_order": [
                        t
                        for t in values.get("sequence") or []
                        if (tasks.get(t) or {}).get("status") == "pending"
                    ],
                    "tasks": {
                        t: {"status": e.get("status"), "detail": e.get("detail")}
                        for t, e in tasks.items()
                    },
                    "holding": values.get("holding"),
                    "hold_reason": values.get("hold_reason"),
                }
            )
        return {
            "robot_id": self.robot_id,
            "recharging": self._recharging,
            "ordering": "priority first; patrols lap by lap; nearest first within a lap",
            "envelopes": envelopes,
        }

    async def get_robot_state(self) -> RobotState:
        return await self.skills.get_state()

    async def activity(self) -> dict[str, Any]:
        """What the robot is doing, for live views: current task, hold and link-recovery state."""
        out: dict[str, Any] = {
            "task": None,
            "holding": False,
            "recovery": None,
            "queued": 0,
            "queue": [],  # pending tasks, in the order this robot will run them
        }
        for thread in reversed(self._threads):  # most recently accepted envelope first
            values = (await self.graph.aget_state(self._config(thread))).values
            tasks = values.get("tasks") or {}
            out["queue"] += [
                t
                for t in values.get("sequence") or []
                if (tasks.get(t) or {}).get("status") == "pending" and t not in out["queue"]
            ]
            task = values.get("current_task_id")
            if out["task"] is not None:
                continue  # an older envelope: only its queue
            out.update(
                holding=bool(values.get("holding")),
                recovery=values.get("recovery_stage"),
                queued=len(values.get("outbox") or []),
            )
            if task:
                out["task"] = task
                esc = ((values.get("tasks") or {}).get(task) or {}).get("escort")
                if esc and esc.get("legs"):
                    # A person's route (geometry and leg only: no interaction content).
                    def pts(legs: list[list[dict[str, float]]]) -> list[list[float]]:
                        return [[w["x"], w["y"]] for leg in legs for w in leg]

                    leg, start = esc["leg"], esc["person_from"]
                    out["escort"] = {
                        "approach": pts(esc["legs"][leg:start]) if leg < start else [],
                        "route": pts(esc["legs"][max(leg, start) :]),
                    }
        out["coordinator"] = self.coordinator_reachable
        out["recharge"] = self._recharge_phase if self._recharging else None
        return out

    # ------------------------------------------------------------------ monitor loop

    async def _monitor(self) -> None:
        topic = TOPIC_STATE.format(robot_id=self.robot_id)
        while True:
            try:
                async with self._gate():
                    state = await self.skills.get_state()
                    if state.link_quality is not LinkQuality.LOST and self.bus is not None:
                        payload = state.model_dump_json()
                        if (
                            self.deps.budget.admit(TrafficClass.CONTROL_MSGS, len(payload))
                            is Admission.SEND
                        ):
                            await self.bus.publish(topic, payload, qos=0)
                await self._on_link(state.link_quality)
                await self._check_battery(state)
                await self._advise_recharge(state)
                await self._ask_for_work(state)
                await self._check_link_recovery(state)
                await self._end_replan_holds()
                await self._check_interactions()
                await self._check_coordinator()
                await self._flush_if_needed()
                if not self.coordinator_reachable:
                    await self._redistribute_dead_peers()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s monitor iteration failed", self.robot_id)
            await self._sleep(self.poll_interval_s)

    async def _idle(self) -> bool:
        """No pending or running task in any envelope, and not holding position."""
        for thread in list(self._threads):
            values = (await self.graph.aget_state(self._config(thread))).values
            if values.get("holding") or values.get("current_task_id"):
                return False
            # "dispatched" is a running task (robots have no "in_progress": a robot on its
            # last lap counted as idle).
            if any(
                (t or {}).get("status") in ("pending", "dispatched")
                for t in (values.get("tasks") or {}).values()
            ):
                return False
        return True

    async def _check_battery(self, robot_state: RobotState) -> None:
        """Low battery (floor + margin), or idle and below ``recharge_idle_pct`` (too low to
        be given a patrol lap): release the tasks, drive to the staging area and charge;
        back at work at ``recharge_resume_pct``."""
        pct = robot_state.battery_pct
        if pct is None:
            return
        alloc = self.deps.allocator
        low = pct <= alloc.default_battery_floor_pct + alloc.battery_margin_pct
        idle = not self._recharging and pct < alloc.recharge_idle_pct and await self._idle()
        if not idle:
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = time.time()
        waited = idle and time.time() - (self._idle_since or 0) >= alloc.recharge_idle_grace_s
        if not self._recharging and (low or waited):
            await self._start_recharge(pct, "" if low else " and idle")
        elif self._recharging and pct >= alloc.recharge_resume_pct:
            await self._end_recharge(pct)

    async def _start_recharge(self, pct: float, why: str) -> None:
        """Release the tasks, drive to the staging area and charge (low battery, idle and
        low, or an early, staggered recharge)."""
        self._recharging = True
        self._recharge_phase = "going"
        self._idle_since = None
        self._recharge_threads = list(self._threads)
        # Head for the charger at once, never behind a deliberating model (A2.3.1).
        self._spawn(self._go_charge(), "recharge")
        log.warning("%s battery %.0f %%%s: recharging at the staging area", self.robot_id, pct, why)
        await self._publish_charge(True)
        for thread in self._recharge_threads:
            await self.invoke(thread, {"type": "battery_low", "battery_pct": round(pct, 1)})
        if not self._recharge_threads:  # no envelope: say it anyway
            await self._report(
                {"status": "recharging", "battery_pct": round(pct, 1), "released": []}
            )

    async def _end_recharge(self, pct: float) -> None:
        self._recharging = False
        self._recharge_phase = None
        self._advised_band = None
        await self._publish_charge(False)
        log.warning("%s battery %.0f %%: charged, back at work", self.robot_id, pct)
        for thread in self._recharge_threads:
            await self.invoke(thread, {"type": "battery_charged", "battery_pct": round(pct, 1)})
        if not self._recharge_threads:  # no envelope: say it anyway
            await self._report({"status": "charged", "battery_pct": round(pct, 1)})
        self._recharge_threads = []

    async def _publish_charge(self, charging: bool) -> None:
        """Peers stagger their recharges by it (``a6g/fleet/<id>/charge``, retained)."""
        if self.bus is None:
            return
        with contextlib.suppress(Exception):
            await self.bus.publish(
                f"a6g/fleet/{self.robot_id}/charge",
                json.dumps({"charging": charging}),
                qos=1,
                retain=True,
            )

    async def _advise_recharge(self, robot_state: RobotState) -> None:
        """Early, staggered recharges (``agent.recharge_advisor``, 2026-10-22): below
        ``early_recharge_pct``, once per 5 % band, decide whether to charge now. ``rule``:
        charge when no peer is charging. ``model``: the robot's model decides; code allows
        charging now only when no peer is charging (or the battery is near the reserve),
        and the rule decides when the model fails (rule 20)."""
        cfg = self.deps.agent_cfg
        pct = robot_state.battery_pct
        if cfg.recharge_advisor == "off" or pct is None or self._recharging:
            return
        if pct >= cfg.early_recharge_pct or not self._threads:
            self._advised_band = None
            return
        band = int(pct // 5)
        if self._advised_band is not None and band >= self._advised_band:
            return
        if self._band_seen != band:  # decide on the next tick: the peers' fresh states
            self._band_seen = band  # (they crossed with us) have arrived by then
            return
        self._advised_band = band
        alloc = self.deps.allocator
        near_reserve = pct <= alloc.default_battery_floor_pct + alloc.battery_margin_pct + 10
        charging = sorted(p for p in self._peer_charging if self._peer_alive(p))
        # One at a time, the lowest first (ties: lowest id), so two robots crossing the
        # threshold in the same tick do not both go.
        others = {
            r: b for r, b in self._peer_battery.items() if self._peer_alive(r) and r not in charging
        }
        lowest = all((pct, self.robot_id) < (b, r) for r, b in others.items())
        allowed = near_reserve or (not charging and lowest)
        if allowed:
            reason = "no peer is charging and my battery is the lowest"
        elif charging:
            reason = f"{', '.join(charging)} charging"
        else:
            reason = "a peer has less battery: it charges first"
        decision, how = allowed, "rule"
        if cfg.recharge_advisor == "model" and self.deps.model is not None:
            from a6g_robot_agent.cognition import RechargeAdvice, propose

            peers = ", ".join(
                f"{r} {b:.0f} %{' (charging)' if r in charging else ''}"
                for r, b in sorted(self._peer_battery.items())
                if self._peer_alive(r)
            )
            out = await propose(
                self.deps.model,
                "recharge",
                RechargeAdvice,
                f"Your battery: {pct:.0f} %. Reserve (you charge anyway): "
                f"{alloc.default_battery_floor_pct + alloc.battery_margin_pct:.0f} %. "
                f"Peers: {peers or 'unknown'}. Charging now: {', '.join(charging) or 'none'}.",
                lambda a: (
                    None
                    if not a.charge_now or allowed
                    else f"not allowed now: {reason} (one robot charges early at a time, "
                    "the lowest first)"
                ),
                timeout_s=cfg.model_timeouts.replanning,
                agent_id=self.robot_id,
                mission_id=self._mission,
            )
            if out.accepted:
                decision, how, reason = out.value.charge_now, "model", out.value.reason
            else:
                how = out.policy
        self.deps.decisions.record(
            make_decision(
                self.deps.actor,
                DecisionKind.FALLBACK,
                mission_id=self._mission,
                output={"step": "early_recharge", "charge_now": decision, "battery_pct": pct},
                policy={"rule": "staggered recharge", "advisor": how, "peers_charging": charging},
                justification=reason,
                clock=self.deps.clock,
            )
        )
        if decision:
            await self._start_recharge(pct, " (early, staggered)")

    async def _report(self, payload: dict[str, Any]) -> None:
        """An envelope status sent by the runtime itself, to the mission it works for: a
        robot whose envelope is done has no thread to report through (it then never asked
        for work, nor said it was charging or charged). Kept until delivered."""
        if self._mission is None:
            return
        self._direct.append(
            Report(
                report_id=self.deps.ids("rep"),
                robot_id=self.robot_id,
                mission_id=self._mission,
                kind=ReportKind.ENVELOPE_STATUS,
                traffic_class=TrafficClass.CONTROL_MSGS,
                payload=payload,
                stamp=self.deps.clock.now(),
            )
        )
        await self._flush_direct()

    async def _flush_direct(self) -> None:
        while self._direct and self.coordinator_reachable:
            with contextlib.suppress(Exception):
                if not await self.uplink.send(self._direct[0]):
                    return
                self._direct.pop(0)
                continue
            return

    async def _ask_for_work(self, robot_state: RobotState) -> None:
        """Idle in a mission (nothing pending or running, not holding, not charging): ask the
        coordinator for work, and again every ``work_request_interval_s`` simulated (and at
        least ``work_request_min_wall_s`` real) seconds while it lasts. The coordinator
        moves to it what another robot has queued but not started."""
        await self._flush_direct()
        running = self._skill is not None or bool(self._queue)  # cheap checks first
        if running or self._recharging or self.killswitch_engaged or self._mission is None:
            self._work_asked, self._asks = None, 0
            return
        if not self.coordinator_reachable:
            return
        now, wall = self.deps.clock.now(), time.time()
        if self._work_asked is not None:
            sim_then, wall_then = self._work_asked
            cfg = self.deps.agent_cfg
            # Each unanswered ask doubles the wait (up to 16x): a mission with nothing left
            # to do is not asked every few seconds forever.
            backoff = 2 ** min(self._asks - 1, 4)
            if (now - sim_then).total_seconds() < cfg.work_request_interval_s * backoff or (
                wall - wall_then < cfg.work_request_min_wall_s * backoff
            ):
                return
        if not await self._idle():
            return
        self._work_asked, self._asks = (now, wall), self._asks + 1
        await self._report(
            {
                "status": "idle",
                "pose": {"x": robot_state.pose.x, "y": robot_state.pose.y},
                "battery_pct": robot_state.battery_pct,
            }
        )

    async def _go_charge(self) -> None:
        with contextlib.suppress(Exception):
            await self.skills.call(SkillName.CANCEL)
        try:
            result = await self.skills.call(SkillName.RETURN_HOME)
        except Exception:
            log.exception("%s could not drive to the staging area", self.robot_id)
            return
        if self._recharging and result.outcome is SkillOutcome.SUCCEEDED:
            self._recharge_phase = "charging"  # at the staging area

    async def _end_replan_holds(self) -> None:
        now = self.deps.clock.now()
        for thread, until in list(self._replan_holds.items()):
            if now >= until:
                self._replan_holds.pop(thread, None)
                await self.invoke(thread, {"type": "tick"})

    # ------------------------------------------------------------------ people (SPEC A3.4)

    def _speak(self, text: str) -> None:
        if self._spoken is not None:
            self._spoken.append(text)
        if self.on_speech is not None:
            with contextlib.suppress(Exception):
                self.on_speech(self.robot_id, text)

    def _ithread(self, interaction_id: str) -> str:
        return f"{self.robot_id}:interaction:{interaction_id}"

    def _current_interaction(self) -> str | None:
        """The interaction this robot is in (one person at a time), if any."""
        live = [(i, c) for i, c in self.deps.local.items() if c.get("stage") not in (None, "ended")]
        return max(live, key=lambda ic: ic[1].get("created") or "")[0] if live else None

    async def say(self, text: str, lang: str | None = None, end: bool = False) -> str:
        """A person speaks to the robot (text, as transcribed): what the robot answers.

        The text stays in this process: in the robot's in-memory copy while it is read,
        never in an event, the checkpoint or a log (rule 28)."""
        self._spoken = []
        try:
            iid = self._current_interaction()
            local = self.deps.local.get(iid) if iid else None
            if local is None:
                if not end:
                    await self._new_interaction(text, lang)
            elif local["stage"] == "confirming":
                if end or lang_.is_no(text):
                    await self._resume(iid, confirmed=False)
                elif lang_.is_yes(text):
                    await self._resume(iid, confirmed=True)
                else:
                    local["content"]["repeated_requests"] = (
                        int(local["content"].get("repeated_requests") or 0) + 1
                    )
                    lng = local["content"].get("lang")
                    self._speak(lang_.say("ask_again", lng) + " " + local.get("confirm_text", ""))
            elif local["stage"] == "arrived":
                await self.end_interaction(iid, local.get("outcome") or "arrived")
                self._speak(lang_.say("goodbye", local["content"].get("lang")))
            elif end or lang_.is_end(text):
                await self._person_ends(iid)
            else:
                place = local["content"].get("place_said") or "?"
                self._speak(lang_.say("on_our_way", local["content"].get("lang"), place=place))
            return " ".join(self._spoken)
        finally:
            self._spoken = None

    async def _new_interaction(self, text: str, lang: str | None) -> None:
        iid = self.deps.ids("int")
        robot_state = await self.skills.get_state()
        now = self.deps.clock.now().isoformat()
        self.deps.local[iid] = {
            "interaction_id": iid,
            "participants": [self.robot_id],
            "created": now,
            "stage": "new",
            "utterance": text,
            "pose": robot_state.pose.model_dump(mode="json"),
            "link": robot_state.link_quality.value,
            "content": {
                "lang": lang_.lang_code(lang) if lang else None,
                "t_request": now,
                "mission_id": await self._mission_hint(),
            },
        }
        await self.invoke(
            self._ithread(iid),
            {
                "type": "utterance",
                "interaction_id": iid,
                "mission_id": self.deps.local[iid]["content"]["mission_id"],
            },
        )
        local = self.deps.local.get(iid)
        if local is not None:
            local["content"]["meeting_pose"] = None
            local.pop("utterance", None)

    async def _mission_hint(self) -> str | None:
        for thread in reversed(self._threads):
            values = (await self.graph.aget_state(self._config(thread))).values
            if values.get("mission_id"):
                return values["mission_id"]
        return None

    async def _resume(self, iid: str, confirmed: bool) -> None:
        """Resume the interaction's wait for confirmation (the interrupt)."""
        local = self.deps.local.get(iid)
        if local is not None:
            local["link"] = (await self.skills.get_state()).link_quality.value
            local["coordinator"] = self.coordinator_reachable
        thread = self._ithread(iid)
        async with self._lock, self._gate():  # never hold time still while queued
            state = await self.graph.ainvoke(
                Command(resume={"interaction_id": iid, "confirmed": confirmed}),
                self._config(thread),
            )
        self._track(thread, state)
        await self._settle_thread(thread, state)

    async def _settle_thread(self, thread: str, state: dict[str, Any]) -> None:
        """An ended interaction thread is deleted once its reports are out."""
        conv = state.get("conversation") or {}
        if conv.get("stage") == "ended" and not state.get("outbox"):
            await self._delete_thread(thread)

    async def _delete_thread(self, thread: str) -> None:
        self._outbox_len.pop(thread, None)
        with contextlib.suppress(Exception):
            await self.checkpointer.adelete_thread(thread)

    async def end_interaction(self, iid: str, outcome: str) -> None:
        """Arrival confirmed, ended by the person, at a shelter, or failed (SPEC A3.4.7)."""
        thread = self._ithread(iid)
        local = self.deps.local.get(iid) or {}
        mission = (local.get("content") or {}).get("mission_id") or await self._mission_hint()
        state = await self.invoke(
            thread,
            {
                "type": "interaction_end",
                "interaction_id": iid,
                "outcome": outcome,
                "mission_id": mission,
            },
        )
        await self._settle_thread(thread, state)

    async def _forget_interaction(self, iid: str) -> None:
        self.deps.local.pop(iid, None)
        await self._delete_thread(self._ithread(iid))

    async def _person_ends(self, iid: str) -> None:
        """The person ends the escort (an ``end`` keyword or ``--end``)."""
        local = self.deps.local[iid]
        lng = local["content"].get("lang")
        for thread in list(self._threads):
            await self.invoke(thread, {"type": "person_end", "interaction_id": iid})
        await self.end_interaction(iid, "ended_by_person")
        self._speak(lang_.say("ended", lng))

    async def _check_interactions(self) -> None:
        """Deadlines (wall clock: people answer in real time): an unconfirmed request
        lapses; an arrival no one answers ends."""
        now = time.time()
        for iid, local in list(self.deps.local.items()):
            stage = local.get("stage")
            deadline = local.get({"confirming": "confirm_deadline"}.get(stage, "arrival_deadline"))
            if stage not in ("confirming", "arrived") or not deadline:
                continue
            if now < deadline:
                continue
            if stage == "confirming":
                local["timed_out"] = True
                await self._resume(iid, confirmed=False)
            else:
                await self.end_interaction(iid, local.get("outcome") or "arrived")

    async def _on_kb_status(self, msg: BusMessage) -> None:
        """A restrictive status reached the knowledge server: escorting threads check
        whether it touches their remaining route."""
        try:
            status = json.loads(msg.payload)
        except ValueError:
            return
        for thread in list(self._threads):
            values = (await self.graph.aget_state(self._config(thread))).values
            current = values.get("current_task_id")
            if current and ((values.get("tasks") or {}).get(current) or {}).get("escort"):
                await self.invoke(thread, {"type": "route_status", **status})

    async def _check_link_recovery(self, robot_state: RobotState) -> None:
        """hold_then_recover: once the hold expires with the link still lost, seek coverage."""
        if robot_state.link_quality is not LinkQuality.LOST:
            return
        hold = timedelta(seconds=self.deps.agent_cfg.link_loss_hold_s)
        for thread, since in list(self._hold_since.items()):
            if self.deps.clock.now() - since >= hold:
                await self.invoke(
                    thread,
                    {"type": "link_recover", "robot_state": robot_state.model_dump(mode="json")},
                )

    async def _check_link(self) -> None:
        await self._on_link((await self.skills.get_state()).link_quality)

    async def _on_link(self, quality: LinkQuality) -> None:
        if quality == self.link_quality:
            return
        log.info("%s link %s -> %s", self.robot_id, self.link_quality, quality)
        self.link_quality = quality
        for thread in list(self._threads):
            await self.invoke(thread, {"type": "link_change", "link_quality": quality.value})
        if quality is not LinkQuality.LOST:
            await self._flush_interactions()

    async def _flush_interactions(self) -> None:
        """Back in coverage: a person's queued request (or interaction end) goes up."""
        waiting = {
            self._ithread(i) for i, c in self.deps.local.items() if c.get("stage") == "confirming"
        }
        for thread, pending in list(self._outbox_len.items()):
            if pending and ":interaction:" in thread and thread not in waiting:
                state = await self.invoke(thread, {"type": "flush"})
                await self._settle_thread(thread, state)

    async def _flush_if_needed(self) -> None:
        if self.link_quality is LinkQuality.LOST or not self.coordinator_reachable:
            return
        waiting = {
            self._ithread(i) for i, c in self.deps.local.items() if c.get("stage") == "confirming"
        }
        for thread, pending in list(self._outbox_len.items()):
            if pending and thread not in waiting:  # never disturb a wait for the person
                state = await self.invoke(thread, {"type": "flush"})
                if ":interaction:" in thread:
                    await self._settle_thread(thread, state)

    # ------------------------------------------------------------------ coordinator loss

    async def _on_heartbeat(self, msg: BusMessage) -> None:
        text = (msg.payload or b"").decode(errors="replace").strip()
        if text == "offline":
            self._coord_offline = True
        else:
            self._coord_offline = False
            self._coord_last = self.deps.clock.now()
            self.uplink.consecutive_failures = 0  # it is alive; retries will tell

    def _coordinator_alive(self) -> bool:
        if self._coord_offline:
            return False
        if self.uplink.consecutive_failures >= self.a2a_failures_for_loss:
            return False
        last = self._coord_last or self._started_at
        if self.bus is None:
            return True  # no heartbeat channel: rely on uplink failures only
        return self.deps.clock.now() - last <= timedelta(seconds=self.coordinator_timeout_s)

    async def _check_coordinator(self) -> None:
        alive = self._coordinator_alive()
        if alive == self.coordinator_reachable:
            return
        self.coordinator_reachable = alive
        log.warning("%s coordinator %s", self.robot_id, "back" if alive else "LOST")
        for thread in list(self._threads):
            await self.invoke(thread, {"type": "coordinator", "reachable": alive})
            if alive:
                await self.invoke(thread, {"type": "reconcile"})

    # ------------------------------------------------------------------ peers & contract-net

    async def _on_fleet_message(self, msg: BusMessage) -> None:
        parts = msg.topic.split("/")
        if len(parts) != 4:
            return
        robot_id, kind = parts[2], parts[3]
        if robot_id == self.robot_id:
            return
        if kind == "state":
            self._peer_seen[robot_id] = self.deps.clock.now()
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                pct = json.loads(msg.payload).get("battery_pct")
                if pct is not None:
                    self._peer_battery[robot_id] = float(pct)
        elif kind == "charge":
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                if json.loads(msg.payload).get("charging"):
                    self._peer_charging.add(robot_id)
                else:
                    self._peer_charging.discard(robot_id)
        elif kind == "status":
            if parse_status(msg.payload)["state"] == "offline":
                self._peer_offline.add(robot_id)
            else:
                self._peer_offline.discard(robot_id)
                self._peer_seen[robot_id] = self.deps.clock.now()
        elif kind == "envelope":
            with contextlib.suppress(ValueError):
                self._peer_envelopes[robot_id] = json.loads(msg.payload)

    def _peer_alive(self, robot_id: str) -> bool:
        if robot_id in self._peer_offline:
            return False
        seen = self._peer_seen.get(robot_id)
        if seen is None:
            return False
        timeout = timedelta(seconds=self.deps.agent_cfg.peer_timeout_s)
        return self.deps.clock.now() - seen <= timeout

    def _known_peers(self) -> list[str]:
        ids = set(self.peers.peer_ids()) if self.peers else set()
        return sorted(ids | set(self._peer_seen) | set(self._peer_envelopes))

    async def _redistribute_dead_peers(self) -> None:
        """SPEC 9.2/9.3: with the coordinator lost, the lowest alive robot_id redistributes
        the open swappable tasks of dead peers among the survivors (never new goals). An
        idle robot takes part too; a task nobody could take is offered again later."""
        if self.peers is None or self.link_quality is LinkQuality.LOST:
            return  # out of coverage: it can reach nobody, and takes no new work (A1.12)
        alive = sorted([self.robot_id, *[p for p in self._known_peers() if self._peer_alive(p)]])
        if alive[0] != self.robot_id:
            return  # someone else is the initiator
        for dead in [p for p in self._known_peers() if not self._peer_alive(p)]:
            env = self._peer_envelopes.get(dead)
            if not env or not env.get("swappable"):
                continue
            for task_id, entry in sorted(env.get("tasks", {}).items()):
                if entry["status"] not in OPEN_TASK_STATUSES or task_id in self._redistributed:
                    continue
                retry = self._retry_at.get(task_id)
                if retry is not None and self.deps.clock.now() < retry:
                    continue
                self._redistributed.add(task_id)
                taken = await self._contract_net(
                    TaskSpec.model_validate(entry["spec"]),
                    mission_id=env["mission_id"],
                    envelope_id=env["envelope_id"],
                    from_robot=dead,
                    bidders=alive,
                )
                if not taken:  # nobody could take it now (busy, out of coverage): later
                    self._redistributed.discard(task_id)
                    self._retry_at[task_id] = self.deps.clock.now() + timedelta(
                        seconds=self.deps.agent_cfg.peer_timeout_s * 3
                    )

    async def _redistribute_own(self, state: dict[str, Any], task_id: str) -> None:
        env = state.get("envelope") or {}
        if self.peers is None or not env.get("swappable") or task_id in self._redistributed:
            return
        self._redistributed.add(task_id)
        bidders = [p for p in self._known_peers() if self._peer_alive(p)]
        task = TaskSpec.model_validate(state["tasks"][task_id]["spec"])
        await self._contract_net(
            task,
            mission_id=env["mission_id"],
            envelope_id=env["envelope_id"],
            from_robot=self.robot_id,
            bidders=bidders,
        )

    async def _contract_net(
        self,
        task: TaskSpec,
        *,
        mission_id: str,
        envelope_id: str,
        from_robot: str,
        bidders: list[str],
    ) -> bool:
        """True: the task found a robot (its award was accepted)."""
        if not bidders or not self.deps.agent_cfg.contract_net:  # B0-B2: no peer fallback
            return False
        cfp = CallForProposals(
            cfp_id=self.deps.ids("cfp"),
            mission_id=mission_id,
            envelope_id=envelope_id,
            initiator=self.robot_id,
            task=task,
            deadline_ms=self.cfp_deadline_ms,
            issued_at=self.deps.clock.now(),
        )

        # nl.summary for the peers' models (A2.3.1 negotiation): the task's context.
        summary = {
            "summary": f"{from_robot} cannot finish {task.kind.value} {task.task_id}; "
            f"{self.robot_id} asks who can take it (priority {task.priority})."
        }

        async def ask(rid: str) -> Bid | None:
            try:
                async with asyncio.timeout(cfp.deadline_ms / 1000):
                    if rid == self.robot_id:
                        return await self.handle_cfp(cfp, nl=summary)
                    link = self.peers.link(rid)  # type: ignore[union-attr]
                    try:
                        return await link.cfp(cfp, nl=summary)
                    except TypeError:  # a peer link without NL support
                        return await link.cfp(cfp)
            except Exception as exc:  # no reply before the deadline counts as refusal
                log.info("%s: no bid from %s (%s)", self.robot_id, rid, exc)
                return None

        bids = [b for b in await asyncio.gather(*(ask(r) for r in sorted(bidders))) if b]
        winner = best_bid([(b.bidder, b.cost) for b in bids if b.cost is not None])
        if winner is None:
            log.warning("%s: nobody can take %s", self.robot_id, task.task_id)
            return False
        award = Award(
            award_id=f"aw_{cfp.cfp_id}",
            cfp_id=cfp.cfp_id,
            mission_id=mission_id,
            envelope_id=envelope_id,
            task_id=task.task_id,
            awarded_by=self.robot_id,
            awarded_to=winner[0],
            cost=winner[1],
            stamp=self.deps.clock.now(),
        )
        if winner[0] == self.robot_id:
            ack = await self.handle_award(award)
        else:
            try:
                ack = await self.peers.link(winner[0]).award(award)  # type: ignore[union-attr]
            except Exception as exc:
                log.warning("%s: award to %s failed: %s", self.robot_id, winner[0], exc)
                return False
        if not ack.get("accepted"):
            return False
        my_thread = self._bid_thread(mission_id)
        if my_thread is not None:
            await self.invoke(
                my_thread,
                {
                    "type": "award_made",
                    "award": award.model_dump(mode="json"),
                    "from_robot": from_robot,
                    "bids": [b.model_dump(mode="json") for b in bids],
                },
            )
        return True

    async def _publish_envelope(self, state: dict[str, Any]) -> None:
        """Share the accepted envelope (retained) so peers can redistribute it if we die."""
        env = state.get("envelope")
        if self.bus is None or not env:
            return
        summary = {
            "robot_id": self.robot_id,
            "mission_id": env["mission_id"],
            "envelope_id": env["envelope_id"],
            "version": env["version"],
            "swappable": env["swappable"],
            "tasks": {
                tid: {"status": t["status"], "spec": t["spec"]}
                for tid, t in (state.get("tasks") or {}).items()
            },
        }
        payload = json.dumps(summary, sort_keys=True)
        if payload == self._published_envelope:
            return
        if self.deps.budget.admit(TrafficClass.CONTROL_MSGS, len(payload)) is Admission.SEND:
            await self.bus.publish(
                TOPIC_ENVELOPE.format(robot_id=self.robot_id), payload, qos=1, retain=True
            )
            self._published_envelope = payload

    # ------------------------------------------------------------------ bus listeners

    async def _listen(self, topic: str, handler: Callable[[BusMessage], Awaitable[None]]) -> None:
        assert self.bus is not None
        async for msg in self.bus.subscribe(topic):
            try:
                await handler(msg)
            except Exception:
                log.exception("%s handler for %s failed", self.robot_id, topic)

    async def on_killswitch(self, msg: BusMessage) -> None:
        try:
            data = json.loads(msg.payload or b"{}")
        except ValueError:
            data = {"state": "on"}  # unparsable kill-switch: fail safe
        target = data.get("robot_id")
        if target not in (None, self.robot_id):
            return
        if data.get("state", "on") == "on":
            # Stop at once: never behind a model deliberating in the graph (SPEC A2.3.1).
            # The engaged flag makes the cancelled task re-queue rather than fail.
            self.killswitch_engaged = True
            await self.skills.call(SkillName.STOP)
            log.warning("%s KILL-SWITCH: stopped and holding", self.robot_id)
            await self.handle_operator("hold", reason="killswitch")
        else:
            self.killswitch_engaged = False
            log.warning("%s kill-switch released (operator must resume)", self.robot_id)
