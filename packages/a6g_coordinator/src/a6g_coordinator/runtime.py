"""In-process coordinator host (tests, simulation stack, headless deployments).

The production host for the chat UI is the LangGraph Agent Server (``langgraph.json``),
which persists threads in Postgres and queues runs. This host reproduces that contract
in-process: one compiled graph, a checkpointer and a store, ``thread_id = mission_id``,
runs serialised per mission by a worker fed from a queue (so a robot's report never
waits for the coordinator run it triggers).
"""

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from a6g_common.models import MODEL_CALLER, set_call_recorder
from a6g_common.schemas import Posture, Report, ReportKind
from a6g_memory import StoreSharedMemory
from a6g_observability.checkpoint_meter import CHECKPOINT_EVENT, event_kind
from a6g_transport.messaging import TelemetryBus
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore
from langgraph.types import Command

from a6g_coordinator.graph import CoordinatorDeps, build_graph
from a6g_coordinator.telemetry import CoordinatorTelemetry

log = logging.getLogger(__name__)


def _record_model_call(caller: str, function: str, trace: Any) -> None:
    """Rule 23 / SPEC A4 N2: tokens and latency of the coordinator's model calls, next to
    the robots' in the message log (pattern ``model``)."""
    from a6g_observability.comms import comms_log

    comms_log().record(
        {
            "ts": time.time(),
            "direction": "model",
            "agent": caller,
            "msg_id": None,
            "mission_id": None,
            "sender": caller,
            "receivers": [],
            "pattern": "model",
            "performative": function,
            "schema": function,
            "binding": "coordinator_llm",
            "attempt": 1,
            "inference_ms": round(trace.latency_s * 1000.0, 1),
            "input_tokens": trace.input_tokens or 0,
            "output_tokens": trace.output_tokens or 0,
            "outcome": "fallback" if trace.used_fallback else "ok",
        }
    )


set_call_recorder(_record_model_call)


class Coordinator:
    def __init__(
        self,
        deps: CoordinatorDeps,
        checkpointer: BaseCheckpointSaver,
        store: BaseStore,
        *,
        bus: TelemetryBus | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        gate: Callable[[], contextlib.AbstractAsyncContextManager[Any]] | None = None,
    ) -> None:
        self.deps = deps
        self.cfg = deps.cfg
        if deps.submit is None:
            deps.submit = self._post  # superseded missions run on their own queue
        if deps.peek is None:
            deps.peek = self.state
        self.graph = build_graph(deps).compile(checkpointer=checkpointer, store=store)
        self.store = store
        self.memory = StoreSharedMemory(store)
        self.bus = bus
        self._sleep = sleep
        self._gate = gate or contextlib.nullcontext
        self.telemetry = (
            CoordinatorTelemetry(self.cfg, bus, deps.clock, self.broadcast, sleep=sleep)
            if bus is not None
            else None
        )
        self._queues: dict[str, asyncio.Queue[tuple[Any, asyncio.Future[Any]]]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._busy: dict[str, int] = defaultdict(int)
        #: wall seconds of each graph run (SPEC A4 N1: processing time per invocation)
        self.run_wall_s: list[float] = []
        self.running = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.running = True
        if self.telemetry is not None:
            self.telemetry.start()

    async def stop(self, graceful: bool = True) -> None:
        """``graceful=False`` emulates a crash: queued events are lost (robots keep their
        reports queued until the coordinator is back)."""
        self.running = False
        if self.telemetry is not None:
            await self.telemetry.stop(graceful=graceful)
        tasks = list(self._workers.values())
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._workers.clear()
        self._queues.clear()

    async def __aenter__(self) -> "Coordinator":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------ runs

    def _config(self, mission_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": mission_id}}

    async def _run(self, mission_id: str, payload: Any) -> dict[str, Any]:
        async with self._gate():
            started = time.perf_counter()
            token = MODEL_CALLER.set("coordinator")
            event = CHECKPOINT_EVENT.set(event_kind(payload))
            try:
                return await self.graph.ainvoke(payload, self._config(mission_id))
            finally:
                CHECKPOINT_EVENT.reset(event)
                MODEL_CALLER.reset(token)
                self.run_wall_s.append(time.perf_counter() - started)

    async def _worker(self, mission_id: str) -> None:
        queue = self._queues[mission_id]
        while True:
            payload, done = await queue.get()
            self._busy[mission_id] += 1
            try:
                result = await self._run(mission_id, payload)
                if not done.done():
                    done.set_result(result)
            except Exception as exc:
                log.exception("coordinator run failed for %s", mission_id)
                if not done.done():
                    done.set_exception(exc)
            finally:
                self._busy[mission_id] -= 1
                queue.task_done()

    def enqueue(self, mission_id: str, payload: Any) -> asyncio.Future[Any]:
        """Queue a run on the mission thread (multitask strategy: enqueue)."""
        if not self.running:
            raise RuntimeError("coordinator is not running")
        if mission_id not in self._queues:
            self._queues[mission_id] = asyncio.Queue()
            self._workers[mission_id] = asyncio.create_task(
                self._worker(mission_id), name=f"coord-{mission_id}"
            )
        done: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._queues[mission_id].put_nowait((payload, done))
        return done

    async def _post(self, mission_id: str, event: dict[str, Any]) -> None:
        """Queue an event without waiting for its run."""
        self.enqueue(mission_id, {"event": event})

    async def submit(self, mission_id: str, event: dict[str, Any]) -> dict[str, Any]:
        return await self.enqueue(mission_id, {"event": event})

    async def goal(
        self, mission_id: str, text: str, posture: Posture | str = "routine"
    ) -> dict[str, Any]:
        return await self.submit(
            mission_id,
            {
                "type": "goal",
                "text": text,
                "posture": Posture(posture).value,
                "issued_by": "operator",
            },
        )

    async def chat(self, mission_id: str, text: str) -> dict[str, Any]:
        return await self.enqueue(mission_id, {"messages": [HumanMessage(text)]})

    async def resume(self, mission_id: str, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        return await self.enqueue(mission_id, Command(resume={"decisions": decisions}))

    async def handle_report(self, report: Report) -> bool:
        """Robot uplink entry point: queued and acknowledged immediately."""
        if not self.running:
            return False
        if report.mission_id is None and report.kind in (
            ReportKind.HUMAN_REQUEST,
            ReportKind.INTERACTION_END,
        ):
            # A person stopped an idle robot: the request joins the running mission.
            active = await self.active_missions()
            report = report.model_copy(
                update={"mission_id": active[-1] if active else f"people-{report.robot_id}"}
            )
        if report.mission_id is None and report.kind is ReportKind.DECISION:
            return True  # a robot's own decision outside any mission: logged at the robot
        if report.mission_id is None and report.kind is ReportKind.ENVELOPE_STATUS:
            # A robot with no mission yet (admitted at the staging area) asks for work, or
            # says it is charged: every active mission hears it (2026-10-27).
            for mission_id in await self.active_missions():
                self.enqueue(
                    mission_id,
                    {"event": {"type": "report", "report": report.model_dump(mode="json")}},
                )
            return True
        if report.mission_id is None:
            return False
        self.enqueue(
            report.mission_id,
            {"event": {"type": "report", "report": report.model_dump(mode="json")}},
        )
        return True

    async def drain(self, timeout: float = 30.0) -> None:
        """Wait until every queued run has finished (tests)."""
        async with asyncio.timeout(timeout):
            while any(q.qsize() for q in self._queues.values()) or any(self._busy.values()):
                await asyncio.sleep(0.005)

    async def state(self, mission_id: str) -> dict[str, Any]:
        return (await self.graph.aget_state(self._config(mission_id))).values

    async def interrupts(self, mission_id: str) -> list[Any]:
        snapshot = await self.graph.aget_state(self._config(mission_id))
        return [i.value for i in snapshot.interrupts]

    async def active_missions(self) -> list[str]:
        missions = await self.memory.list_context("missions")
        return sorted(m["mission_id"] for m in missions if m.get("status") == "active")

    async def broadcast(self, event: dict[str, Any]) -> None:
        if event.get("type") == "robot_added":  # remembered for missions started later
            self.deps.admitted[event["robot_id"]] = event
        for mission_id in await self.active_missions():
            self.enqueue(mission_id, {"event": event})
