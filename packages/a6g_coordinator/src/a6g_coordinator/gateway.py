"""HTTP app mounted into the LangGraph Agent Server (``langgraph.json`` ``http.app``).

* ``/a6g`` is the coordinator's A2A endpoint (agent card + JSON-RPC): robot reports
  become runs on the mission thread (``thread_id = mission_id``, multitask ``enqueue``),
  queued by the server so a report never waits for the run it triggers. Operator events
  (``a6g sim inject weather_alert``) are broadcast to the active missions.
* The lifespan runs the telemetry plane over MQTT: coordinator heartbeat (last-will
  ``offline``) and the fleet monitor (robot failures, coverage losses, arrivals).
* ``LatestCheckpointOnly`` keeps every run on the head of its mission thread. Chat
  clients (agent-chat-ui's ``useStream``) pin each message to the last checkpoint *they*
  saw, which forks the thread and silently drops the robot reports enqueued meanwhile
  (skill results, recoveries). A mission thread has many writers and one truth, so the
  pin is removed unless the request opts in with ``x-a6g-allow-fork: 1``.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from typing import Any

from a6g_common.clock import SystemClock
from a6g_common.config import load_config
from a6g_common.schemas import Report, ReportKind
from a6g_transport.messaging import (
    TOPIC_HEARTBEAT,
    A6GMessage,
    MessageType,
    agent_routes,
    build_agent_card,
    close_handlers,
    configure_messaging,
    mqtt_bus,
)
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a6g_coordinator.telemetry import CoordinatorTelemetry

log = logging.getLogger(__name__)


class _NoDocstringNoise(logging.Filter):
    """langgraph-api parses route docstrings as YAML for its OpenAPI page and logs a full
    traceback for every plain-prose one (its own and the A2A SDK's). Harmless: drop them."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "Unable to parse docstring" not in str(record.msg)


logging.getLogger("langgraph_api.utils").addFilter(_NoDocstringNoise())

PREFIX = "/a6g"
CFG = load_config(os.environ.get("A6G_CONFIG_DIR", "config"))
ASSISTANT = CFG.fleet.defaults.coordinator.assistant_id


def _client():
    from langgraph_sdk import get_client

    return get_client()


async def submit(mission_id: str, event: dict[str, Any]) -> None:
    await _client().runs.create(
        mission_id,
        ASSISTANT,
        input={"event": event},
        multitask_strategy="enqueue",
        if_not_exists="create",
    )


async def active_missions() -> list[str]:
    result = await _client().store.search_items(["a6g", "context", "missions"], limit=100)
    return sorted(
        item["value"]["mission_id"]
        for item in result.get("items", [])
        if item["value"].get("status") == "active"
    )


async def end_missions_before(event: dict[str, Any], before: str) -> list[str]:
    """The simulator restarted (``world_reset``): end the active missions whose conversation
    started before it, never one started since (the operator may already have begun one)."""
    from datetime import datetime

    cutoff = datetime.fromisoformat(before)
    ended = []
    for mission_id in await active_missions():
        try:
            thread = await _client().threads.get(mission_id)
            created = datetime.fromisoformat(str(thread["created_at"]).replace("Z", "+00:00"))
        except Exception:
            continue
        if created < cutoff:
            await submit(mission_id, event)
            ended.append(mission_id)
    return ended


async def broadcast(event: dict[str, Any]) -> None:
    if event.get("type") == "robot_added":  # remembered for missions started later
        from a6g_coordinator.graph import ADMITTED  # the graph's registry (one object)

        ADMITTED[event["robot_id"]] = event
    for mission_id in await active_missions():
        await submit(mission_id, event)


async def handle(msg: A6GMessage) -> A6GMessage:
    if msg.type is MessageType.REPORT:
        report = Report.model_validate(msg.payload)
        event = {"type": "report", "report": report.model_dump(mode="json")}
        if report.mission_id:
            await submit(report.mission_id, event)
        elif report.kind is ReportKind.ENVELOPE_STATUS:
            # A robot with no mission yet (admitted at the staging area) asks for work, or
            # says it is charged: every active mission hears it.
            for mission_id in await active_missions():
                await submit(mission_id, event)
        return A6GMessage(MessageType.REPORT_ACK, {"report_id": report.report_id})
    if msg.type is MessageType.OPERATOR:
        event = msg.payload["event"]
        if event.get("type") == "world_reset":
            ended = await end_missions_before(event, event["before"])
            return A6GMessage(MessageType.OPERATOR_ACK, {"ok": True, "ended": ended})
        if msg.payload.get("mission_id"):
            await submit(msg.payload["mission_id"], event)
        else:
            await broadcast(event)
        return A6GMessage(MessageType.OPERATOR_ACK, {"ok": True})
    return A6GMessage(MessageType.ERROR, {"error": f"unsupported message {msg.type.value}"})


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "assistant_id": ASSISTANT, "fleet": CFG.fleet.fleet_id})


RUN_PATH = re.compile(r"^/threads/[^/]+/runs(/stream|/wait)?/?$")
FORK_HEADER = b"x-a6g-allow-fork"


def unpin(body: dict[str, Any]) -> bool:
    """Drop checkpoint pins from a run-create payload; True if anything was removed."""
    removed = body.pop("checkpoint", None) is not None
    removed |= body.pop("checkpoint_id", None) is not None
    configurable = (body.get("config") or {}).get("configurable") or {}
    for key in ("checkpoint_id", "checkpoint_ns", "checkpoint_map"):
        removed |= configurable.pop(key, None) is not None
    return removed


class LatestCheckpointOnly:
    """ASGI middleware: run-create requests always start from the thread's latest state."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not RUN_PATH.match(scope["path"])
            or dict(scope["headers"]).get(FORK_HEADER) == b"1"
        ):
            await self.app(scope, receive, send)
            return
        chunks, more = [], True
        while more:
            message = await receive()
            if message["type"] != "http.request":  # client went away
                await self.app(scope, _replay([message], receive), send)
                return
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        raw = b"".join(chunks)
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        if isinstance(body, dict) and unpin(body):
            log.info("run on %s unpinned from a stale checkpoint", scope["path"])
            raw = json.dumps(body).encode()
            scope = dict(scope)
            scope["headers"] = [
                (k, str(len(raw)).encode() if k == b"content-length" else v)
                for k, v in scope["headers"]
            ]
        await self.app(scope, _replay([{"type": "http.request", "body": raw}], receive), send)


def _replay(messages: list[dict], upstream: Any) -> Any:
    """Serve the buffered messages, then the real channel (streams watch for disconnect)."""
    queue = list(messages)

    async def receive() -> dict:
        if queue:
            return queue.pop(0)
        return await upstream()

    return receive


A2A_HANDLERS: list[Any] = []


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    try:
        async with _telemetry(app):
            yield
    finally:
        await close_handlers(A2A_HANDLERS)


@contextlib.asynccontextmanager
async def _telemetry(app: Starlette) -> AsyncIterator[None]:
    configure_messaging(CFG.comms)  # routing + per-message metrics
    stop = asyncio.Event()
    task = asyncio.create_task(_telemetry_loop(stop), name="coord-telemetry")
    try:
        yield
    finally:
        stop.set()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, 5.0)


async def _telemetry_loop(stop: asyncio.Event, retry_s: float = 2.0) -> None:
    """The heartbeat and robot-liveness plane, kept up for the coordinator's whole life.

    The broker may start after the coordinator, or go away and come back (the 2D stack's
    embedded broker restarts with terminal 1): connect when it is there, and on a failed
    heartbeat reconnect, so the robots keep hearing heartbeats after a simulator restart
    and never report the coordinator unreachable."""
    mqtt = CFG.fleet.edge.mqtt
    warned = False
    while not stop.is_set():
        bus = mqtt_bus(
            mqtt.host,
            mqtt.port,
            agent_id="coordinator",
            client_id="a6g-coordinator",
            will=(TOPIC_HEARTBEAT, b"offline", 1, True),
        )
        try:
            await bus.__aenter__()
        except Exception as exc:
            if not warned:
                log.warning(
                    "MQTT broker unavailable at %s:%s (%s): coordinator heartbeat and robot "
                    "liveness monitoring wait for it (a6g-sim-stack --embedded-broker, "
                    "mosquitto); retrying every %g s.",
                    mqtt.host,
                    mqtt.port,
                    exc,
                    retry_s,
                )
                warned = True
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), retry_s)
            continue
        warned = False
        telemetry = CoordinatorTelemetry(CFG, bus, SystemClock(), broadcast)
        telemetry.start()
        log.info("coordinator telemetry plane up on mqtt://%s:%s", mqtt.host, mqtt.port)
        waits = [asyncio.create_task(stop.wait()), asyncio.create_task(telemetry.failed.wait())]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()
            await telemetry.stop(graceful=stop.is_set())
            with contextlib.suppress(Exception):
                await bus.__aexit__(None, None, None)
        if not stop.is_set():
            log.warning("MQTT connection lost: reconnecting the coordinator telemetry plane")


CARD = build_agent_card(
    name="a6g-coordinator",
    description="Agentic6G UC2 Swarm Coordination Agent (robot reports, operator events).",
    base_url=CFG.fleet.edge.coordinator_a2a_url,
    skills=[
        ("report", "Robot report", "Skill results, task status, observations, reconcile."),
        ("operator", "Operator event", "Posture changes such as weather alerts."),
    ],
)

app = Starlette(
    routes=[
        *agent_routes(CARD, handle, prefix=PREFIX, agent_id="coordinator", handlers=A2A_HANDLERS),
        Route(f"{PREFIX}/health", health),
    ],
    lifespan=lifespan,
    middleware=[Middleware(LatestCheckpointOnly)],
)
