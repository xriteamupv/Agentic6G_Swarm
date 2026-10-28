"""``a6g-coordinator``: the coordinator without the LangGraph Agent Server.

Runs the same compiled graph (in-process host, one run at a time per mission) with a
Postgres checkpointer and store behind the A2A gateway, plus the MQTT telemetry plane:

    uv run a6g-coordinator --checkpointer postgres --dsn $A6G_PG_DSN

Robot reports and operator events use the same A2A messages as the Agent Server gateway.
Operator commands (the ``a6g goal/resume/status --plain`` CLI) are A2A ``operator``
messages with a ``command``. agent-chat-ui needs the Agent Server path (``langgraph dev``).
"""

import argparse
import asyncio
import contextlib
import logging
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from a6g_common.clock import SystemClock
from a6g_common.config import load_config
from a6g_common.ids import new_id
from a6g_common.schemas import Report
from a6g_interactions import InteractionClient
from a6g_kb import KnowledgeClient
from a6g_observability.decision_log import DecisionLog
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
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a6g_coordinator.graph import CoordinatorDeps
from a6g_coordinator.links import A2ALinkFactory
from a6g_coordinator.persistence import open_persistence
from a6g_coordinator.runtime import Coordinator
from a6g_coordinator.server import _model

log = logging.getLogger("a6g-coordinator")

PREFIX = "/a6g"


def _last_ai(values: dict[str, Any]) -> str:
    for message in reversed(values.get("messages") or []):
        if (
            getattr(message, "type", "") == "ai"
            and isinstance(message.content, str)
            and message.content
        ):
            return message.content
    return ""


def build_plain_app(coordinator: Coordinator, base_url: str) -> Starlette:
    async def handle(msg: A6GMessage) -> A6GMessage:
        if msg.type is MessageType.REPORT:
            report = Report.model_validate(msg.payload)
            await coordinator.handle_report(report)
            return A6GMessage(MessageType.REPORT_ACK, {"report_id": report.report_id})
        if msg.type is not MessageType.OPERATOR:
            return A6GMessage(MessageType.ERROR, {"error": f"unsupported message {msg.type.value}"})
        p = msg.payload
        command = p.get("command")
        if command is None:  # event, as sent by the sim stacks (weather_alert ...)
            if p.get("mission_id"):
                coordinator.enqueue(p["mission_id"], {"event": p["event"]})
            else:
                await coordinator.broadcast(p["event"])
            return A6GMessage(MessageType.OPERATOR_ACK, {"ok": True})
        mission = p.get("mission_id") or str(uuid.uuid4())
        if command == "goal":
            values = await coordinator.goal(mission, p["text"], p.get("posture", "routine"))
        elif command == "chat":
            values = await coordinator.chat(mission, p["text"])
        elif command == "resume":
            values = await coordinator.resume(mission, p["decisions"])
        elif command == "status":
            values = await coordinator.state(mission)
            interrupts = await coordinator.interrupts(mission)
            return A6GMessage(
                MessageType.OPERATOR_ACK,
                {
                    "mission_id": mission,
                    "tasks": {
                        t: {"status": e["status"], "robot_id": e.get("robot_id")}
                        for t, e in (values.get("tasks") or {}).items()
                    },
                    "pending": [i["action_requests"][0]["description"] for i in interrupts],
                },
            )
        else:
            return A6GMessage(MessageType.ERROR, {"error": f"unknown command {command!r}"})
        return A6GMessage(
            MessageType.OPERATOR_ACK, {"mission_id": mission, "reply": _last_ai(values)}
        )

    card = build_agent_card(
        name="a6g-coordinator",
        description="Agentic6G UC2 Swarm Coordination Agent (plain host, no Agent Server).",
        base_url=base_url,
        skills=[
            ("report", "Robot report", "Skill results, task status, observations, reconcile."),
            ("operator", "Operator", "Events and commands: goal, chat, resume, status."),
        ],
    )

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "host": "plain", "running": coordinator.running})

    handlers: list[Any] = []

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        await close_handlers(handlers)

    return Starlette(
        routes=[
            *agent_routes(card, handle, prefix=PREFIX, agent_id="coordinator", handlers=handlers),
            Route(f"{PREFIX}/health", health),
        ],
        lifespan=lifespan,
    )


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="a6g-coordinator",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--checkpointer", choices=["postgres", "sqlite", "memory"], default="postgres")
    p.add_argument("--dsn", default=None, help="Postgres DSN (default: $A6G_PG_DSN)")
    p.add_argument("--sqlite-path", default="var/coordinator")
    p.add_argument("--config-dir", default=None)
    p.add_argument("--profile", default=None)
    p.add_argument("--no-mqtt", action="store_true")
    return p.parse_args(argv)


async def _serve(args: argparse.Namespace) -> None:
    cfg = load_config(args.config_dir, profile=args.profile)
    configure_messaging(cfg.comms)
    url = urlsplit(cfg.fleet.edge.coordinator_a2a_url)
    base = f"{url.scheme}://{url.netloc}{PREFIX}"
    dsn = args.dsn or os.environ.get(cfg.fleet.edge.postgres_dsn_env)
    deps = CoordinatorDeps(
        cfg=cfg,
        links=A2ALinkFactory(cfg.fleet.defaults.coordinator.envelope_timeout_s),
        decisions=DecisionLog(cfg.fleet.edge.decision_log_path),
        clock=SystemClock(),
        ids=new_id,
        model=_model(cfg),
        kb=KnowledgeClient(
            cfg.knowledge.url, agent_id="coordinator", timeout_s=cfg.knowledge.timeout_s
        ),
        interactions=InteractionClient(
            cfg.interactions.url, agent_id="coordinator", timeout_s=cfg.interactions.timeout_s
        ),
    )
    async with contextlib.AsyncExitStack() as stack:
        checkpointer, store = await stack.enter_async_context(
            open_persistence(args.checkpointer, dsn=dsn, path=args.sqlite_path)
        )
        bus = None
        if not args.no_mqtt:
            mqtt = cfg.fleet.edge.mqtt
            bus = mqtt_bus(
                mqtt.host,
                mqtt.port,
                agent_id="coordinator",
                client_id="a6g-coordinator",
                will=(TOPIC_HEARTBEAT, b"offline", 1, True),
            )
            try:
                await stack.enter_async_context(bus)
            except Exception as exc:
                log.warning("MQTT unavailable (%s): no heartbeat / liveness monitoring", exc)
                bus = None
        coordinator = await stack.enter_async_context(
            Coordinator(deps, checkpointer, store, bus=bus)
        )
        log.warning("coordinator up (%s persistence) on %s", args.checkpointer, base)
        app = build_plain_app(coordinator, base)
        await uvicorn.Server(
            uvicorn.Config(app, host=url.hostname, port=url.port or 80, log_level="warning")
        ).serve()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(_serve(_parse(argv)))


if __name__ == "__main__":  # pragma: no cover
    main()
