"""``a6g-sim-stack``: the simulated fleet as a service, for the coordinator on the LangGraph
Agent Server (``langgraph dev``) and agent-chat-ui.

* robot agents (+ in-process skill servers on the simulator) with their A2A servers on the
  ports of ``config/sim/fleet.yaml`` (8101..); reports go to the coordinator's A2A gateway;
* the telemetry plane over MQTT (``--embedded-broker`` starts an in-process broker when no
  mosquitto is available);
* a control API (default :8300) used by ``a6g sim inject ...`` and ``a6g sim add_robot``.

Simulated time runs ``--speed`` times faster than wall time (default 10).
"""

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from a6g_common.config import load_config, load_manifest
from a6g_common.schemas import LinkQuality
from a6g_kb.serve import make_embedder
from a6g_robot_agent.server import build_robot_app
from a6g_robot_agent.uplink import A2AUplink
from a6g_transport.messaging import (
    A6GMessage,
    MessageType,
    MessagingClient,
    configure_messaging,
    mqtt_bus,
    start_embedded_broker,
)
from langsmith import tracing_context
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a6g_devstack.stack import EXTRA_ROBOTS, SimStack
from a6g_devstack.view import robot_activity, status_overlay, view_routes

log = logging.getLogger("a6g-sim-stack")


def serve_kb(stack: Any, cfg: Any, servers: dict, tasks: list, server_cls: Any) -> None:
    """The stack's knowledge server over HTTP at ``knowledge.url`` (SPEC A3.3.3), for a
    coordinator in ``langgraph dev`` and the ``a6g kb`` CLI."""
    from a6g_kb import build_kb_server

    url = urlsplit(cfg.knowledge.url)
    host = url.hostname or "127.0.0.1"
    app = build_kb_server(stack.kb_service).streamable_http_app(
        streamable_http_path=url.path or "/mcp", host=host
    )
    server = server_cls(uvicorn.Config(app, host=host, port=url.port or 8400, log_level="warning"))
    servers["kb"] = server
    tasks.append(asyncio.create_task(server.serve(), name="kb"))


def serve_interactions(stack: Any, cfg: Any, servers: dict, tasks: list, server_cls: Any) -> None:
    """The stack's InteractionStore over HTTP at ``interactions.url`` (SPEC A3.5), for a
    coordinator in ``langgraph dev``. Bodies are never logged."""
    from a6g_interactions import build_interactions_app

    url = urlsplit(cfg.interactions.url)
    host = url.hostname or "127.0.0.1"
    app = build_interactions_app(stack.interactions)
    server = server_cls(
        uvicorn.Config(app, host=host, port=url.port or 8410, log_level="warning", access_log=False)
    )
    servers["interactions"] = server
    tasks.append(asyncio.create_task(server.serve(), name="interactions"))


class _QuietServer(uvicorn.Server):
    """Secondary servers: the control server owns Ctrl+C handling."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


class _AgentProxy:
    """Always resolves to the robot's current agent process (it can crash and restart)."""

    def __init__(self, stack: SimStack, robot_id: str) -> None:
        self._stack, self._robot_id = stack, robot_id
        first = stack.robots[robot_id].agent
        self.deps, self.robot_id = first.deps, robot_id  # type: ignore[union-attr]

    def __getattr__(self, name: str) -> Any:
        agent = self._stack.robots[self._robot_id].agent
        if agent is None:
            raise ConnectionError(f"{self._robot_id} is down")
        if self._stack.world.link_quality(self._robot_id) is LinkQuality.LOST:
            raise ConnectionError(f"{self._robot_id} is out of 5G coverage")  # emulated shadow
        return getattr(agent, name)


def install_shutdown(server: uvicorn.Server, force: Callable[[], None] | None = None) -> None:
    """The stack, not uvicorn, owns Ctrl-C / SIGTERM.

    uvicorn re-raises a captured signal once its server stops, which turns into a
    KeyboardInterrupt that cancels the stack's own cleanup halfway (robot servers, agents,
    MQTT broker), leaving the process hanging. Here the first signal stops the control server
    and the cleanup then runs to the end; a second signal forces an exit (``force`` first).
    """
    loop = asyncio.get_running_loop()
    first: float | None = None

    def on_signal() -> None:
        nonlocal first
        now = loop.time()
        if first is None:
            first = now
            log.warning("shutting down (press Ctrl-C again to force)")
            server.should_exit = True
            return
        if now - first < 1.5:
            return  # the same Ctrl-C forwarded by `uv run` (it reaches the whole process group)
        log.warning("forced exit")
        if force is not None:
            force()
        os._exit(130)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, on_signal)


def _reachable(host: str, port: int) -> bool:
    with contextlib.suppress(OSError), socket.create_connection((host, port), timeout=0.5):
        return True
    return False


def _robot_model(cfg: Any) -> Any:
    from a6g_common.config import ModelRole
    from a6g_common.models import chat_model

    return chat_model(ModelRole.ONBOARD_VLM, cfg.models)  # each robot's cognitive core


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="a6g-sim-stack",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config-dir", default="config")
    p.add_argument("--fleet-file", default="sim/fleet.yaml")
    p.add_argument("--speed", type=float, default=10.0, help="simulated seconds per wall second")
    p.add_argument("--workdir", default="var/sim_stack")
    p.add_argument("--control-port", type=int, default=8300)
    p.add_argument(
        "--embedded-broker",
        choices=["auto", "yes", "no"],
        default="auto",
        help="start an in-process MQTT broker (auto: only if none is reachable)",
    )
    p.add_argument(
        "--robots",
        type=int,
        default=None,
        help="fleet size, up to 8 (default: the 3 robots of --fleet-file). Above 3, "
        "simulation-only robots join from the staging area in this order: "
        + ", ".join(EXTRA_ROBOTS),
    )
    p.add_argument("--no-arm", action="store_true", help="leave robots disarmed")
    p.add_argument(
        "--recharge-advisor",
        choices=["off", "rule", "model"],
        default=None,
        help="early, staggered recharges: rule, or the robot's model (default: config)",
    )
    p.add_argument(
        "--no-robot-llm",
        action="store_true",
        help="robots decide with code only (default: each robot's model, role onboard_vlm, "
        "proposes and code validates; SPEC A2.3)",
    )
    p.add_argument(
        "--model-time",
        choices=["full", "freeze"],
        default="full",
        help="while a model answers, the world runs on at full speed (default: the wait "
        "counts as latency x --speed in simulated time) or freezes (old behaviour)",
    )
    p.add_argument(
        "--langsmith",
        action="store_true",
        help="trace the in-process agents to LangSmith (default off: every robot event is a "
        "run, which floods the trace quota; the coordinator in langgraph dev is unaffected)",
    )
    return p.parse_args(argv)


async def _amain(args: argparse.Namespace) -> None:
    cfg = load_config(args.config_dir, fleet_file=args.fleet_file)
    if args.robots is not None and not (
        len(cfg.fleet.robots) <= args.robots <= len(cfg.fleet.robots) + len(EXTRA_ROBOTS)
    ):
        raise SystemExit(
            f"--robots: {len(cfg.fleet.robots)} to {len(cfg.fleet.robots) + len(EXTRA_ROBOTS)}"
        )
    if args.recharge_advisor is not None:
        cfg = with_agent(cfg, recharge_advisor=args.recharge_advisor)
    mqtt = cfg.fleet.edge.mqtt
    # Robots judge coordinator heartbeats (1 Hz wall) on the simulated clock: scale the timeout.
    defaults = cfg.fleet.defaults
    defaults.coordinator_timeout_s = max(
        defaults.coordinator_timeout_s, 5 * defaults.coordinator.heartbeat_period_s * args.speed
    )

    broker = None
    if args.embedded_broker == "yes" or (
        args.embedded_broker == "auto" and not _reachable(mqtt.host, mqtt.port)
    ):
        broker = await start_embedded_broker(mqtt.host, mqtt.port)
        log.warning("embedded MQTT broker on %s:%s", mqtt.host, mqtt.port)

    configure_messaging(cfg.comms)
    url = cfg.fleet.edge.coordinator_a2a_url
    coordinator = MessagingClient(url, sender="devstack", receiver="coordinator", timeout_s=5.0)
    a2a_urls = {r.robot_id: r.a2a_url for r in cfg.fleet.robots}
    async with mqtt_bus(
        mqtt.host, mqtt.port, agent_id="devstack", client_id="a6g-sim-stack"
    ) as bus:
        stack = SimStack(
            cfg,
            Path(args.workdir),
            bus=bus,
            realtime_factor=args.speed,
            auto_arm=not args.no_arm,
            uplink_factory=lambda rid: A2AUplink(
                MessagingClient(url, sender=rid, receiver="coordinator", timeout_s=5.0)
            ),
            a2a_urls=a2a_urls,
            model=None if args.no_robot_llm else _robot_model(cfg),
            model_time=args.model_time,
            kb_embedder=make_embedder(cfg),
        )
        started_at = datetime.now(UTC).isoformat()
        await stack.start(coordinator=False)
        servers: dict[str, uvicorn.Server] = {}
        tasks: list[asyncio.Task[Any]] = []
        tasks.append(asyncio.create_task(world_reset(coordinator, started_at), name="world-reset"))

        def serve_robot(robot_id: str) -> None:
            url = urlsplit(stack.a2a_urls[robot_id])
            app = build_robot_app(_AgentProxy(stack, robot_id), stack.a2a_urls[robot_id])  # type: ignore[arg-type]
            server = _QuietServer(
                uvicorn.Config(app, host=url.hostname, port=url.port, log_level="warning")
            )
            servers[robot_id] = server
            tasks.append(asyncio.create_task(server.serve(), name=f"a2a-{robot_id}"))

        async def hot_add(manifest: Any) -> None:
            port = 8101 + len(stack.a2a_urls)
            stack.a2a_urls[manifest.robot_id] = f"http://127.0.0.1:{port}"
            await stack.start_robot(manifest.robot_id, manifest)
            serve_robot(manifest.robot_id)

        for robot_id in stack.robots:
            serve_robot(robot_id)
        for rid in EXTRA_ROBOTS[: max(0, (args.robots or 0) - len(stack.robots))]:
            await hot_add(load_manifest(cfg.config_dir / "robots" / f"{rid}.yaml"))
        serve_kb(stack, cfg, servers, tasks, _QuietServer)
        serve_interactions(stack, cfg, servers, tasks, _QuietServer)
        stack.echo_speech = True  # the robots' speech output: this console

        async def body(request: Request) -> dict[str, Any]:
            with contextlib.suppress(ValueError):
                return await request.json()
            return {}

        async def robot_fail(request: Request) -> JSONResponse:
            rid = (await body(request))["robot_id"]
            await stack.fail_robot(rid)
            return JSONResponse({"ok": True, "robot_id": rid, "state": "failed"})

        async def link_loss(request: Request) -> JSONResponse:
            b = await body(request)
            stack.link_loss(b["robot_id"], float(b["seconds"]))
            return JSONResponse({"ok": True, **b})

        async def anomaly(request: Request) -> JSONResponse:
            b = await body(request)
            stack.anomaly(b["poi_id"], b["label"])
            return JSONResponse({"ok": True, **b})

        async def weather_alert(request: Request) -> JSONResponse:
            exposed = [p.poi_id for p in cfg.campus.pois if p.exposed]
            event = {
                "type": "posture",
                "posture": "emergency",
                "text": "weather alert: heavy rain",
                "reason": "weather_alert",
                "exposed_pois": exposed,
            }
            await coordinator.send(A6GMessage(MessageType.OPERATOR, {"event": event}))
            return JSONResponse({"ok": True, "exposed_pois": exposed})

        async def coordinator_kill(request: Request) -> JSONResponse:
            return JSONResponse(
                {
                    "ok": False,
                    "hint": "the coordinator runs in `langgraph dev`: stop it with Ctrl+C "
                    "and start it again; the broker announces the last-will and robots fall back "
                    "to peer-to-peer contract-net meanwhile",
                },
                status_code=409,
            )

        async def add_robot(request: Request) -> JSONResponse:
            manifest = load_manifest(Path((await body(request))["manifest"]))
            await hot_add(manifest)
            return JSONResponse(
                {
                    "ok": True,
                    "robot_id": manifest.robot_id,
                    "a2a_url": stack.a2a_urls[manifest.robot_id],
                }
            )

        async def say(request: Request) -> JSONResponse:
            data = await body(request)
            rid = str(data.get("robot_id"))
            proc = stack.robots.get(rid)
            if proc is None or proc.agent is None:
                return JSONResponse({"error": f"unknown or down robot {rid}"}, status_code=404)
            says = await proc.agent.say(
                str(data["text"]), lang=data.get("lang"), end=bool(data.get("end"))
            )
            return JSONResponse({"robot_id": rid, "says": says})

        async def status(request: Request) -> JSONResponse:
            world = stack.world
            return JSONResponse(
                {
                    "sim_time": world.clock.now().isoformat(),
                    "robots": {
                        rid: {
                            "x": round(r.x, 1),
                            "y": round(r.y, 1),
                            "battery_pct": round(r.battery_pct, 1),
                            "moving": r.job is not None,
                            "failed": r.failed,
                            "link": world.link_quality(rid).value,
                            "rsrp_dbm": world.radio(rid)[0],
                            "sinr_db": world.radio(rid)[1],
                            "agent": stack.robots[rid].alive,
                        }
                        for rid, r in world.robots.items()
                    },
                }
            )

        async def view_state() -> dict[str, Any]:
            world = stack.world
            robots = {}
            for rid, r in world.robots.items():
                proc = stack.robots.get(rid)
                robots[rid] = {
                    "x": r.x,
                    "y": r.y,
                    "yaw": r.yaw,
                    "battery_pct": r.battery_pct,
                    "moving": r.job is not None,
                    "failed": r.failed,
                    "link": world.link_quality(rid).value,
                    "link_level": world.link_level(rid)[0],
                    "rsrp_dbm": world.radio(rid)[0],
                    "sinr_db": world.radio(rid)[1],
                    "activity": await robot_activity(proc.agent if proc else None),
                }
            return {
                "sim_time_label": world.clock.now().strftime("%H:%M:%S"),
                "robots": robots,
                "statuses": status_overlay(stack.kb_service),
            }

        control = Starlette(
            routes=[
                *view_routes(cfg, view_state, "a6g · 2D simulator"),
                Route("/sim/inject/robot_fail", robot_fail, methods=["POST"]),
                Route("/sim/inject/link_loss", link_loss, methods=["POST"]),
                Route("/sim/inject/anomaly", anomaly, methods=["POST"]),
                Route("/sim/inject/weather_alert", weather_alert, methods=["POST"]),
                Route("/sim/inject/coordinator_kill", coordinator_kill, methods=["POST"]),
                Route("/sim/add_robot", add_robot, methods=["POST"]),
                Route("/sim/say", say, methods=["POST"]),
                Route("/sim/status", status),
            ]
        )
        log.warning(
            "sim stack up: %s robots, speed x%s, coordinator %s, control http://127.0.0.1:%s "
            "(live view: /view)",
            len(stack.robots),
            args.speed,
            cfg.fleet.edge.coordinator_a2a_url,
            args.control_port,
        )
        control_server = _QuietServer(
            uvicorn.Config(control, host="127.0.0.1", port=args.control_port, log_level="warning")
        )
        install_shutdown(control_server)
        try:
            await control_server.serve()
        finally:
            for server in servers.values():
                server.should_exit = True
            await asyncio.gather(*tasks, return_exceptions=True)
            await stack.stop()
    if broker is not None:
        await broker.shutdown()
    log.warning("sim stack stopped")


def with_agent(cfg: Any, **agent: Any) -> Any:
    """``cfg`` with the robots' agent settings changed (``fleet.defaults.agent``)."""
    d = cfg.fleet.defaults
    defaults = d.model_copy(update={"agent": d.agent.model_copy(update=agent)})
    return cfg.model_copy(update={"fleet": cfg.fleet.model_copy(update={"defaults": defaults})})


async def world_reset(coordinator: Any, before: str, every_s: float = 5.0) -> None:
    """A fresh simulator ends the missions from before it (their robots and world are gone;
    one resumed old laps on the new robots, 2026-10-21). Retried until the coordinator
    answers: ``langgraph dev`` may start after this stack. Missions begun after ``before``
    are kept."""
    event = {"type": "world_reset", "before": before}
    for _ in range(int(30 * 60 / every_s)):
        try:
            reply = await coordinator.send(A6GMessage(MessageType.OPERATOR, {"event": event}))
            ended = (reply.payload or {}).get("ended") or []
            if ended:
                log.warning("simulator restarted: ended %d mission(s) from before", len(ended))
            return
        except Exception:
            await asyncio.sleep(every_s)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("a6g_skill_mcp.service").setLevel(logging.ERROR)
    args = _parse(argv)
    # LangSmith tracing of the robots (and in-process coordinator) only on request.
    with contextlib.nullcontext() if args.langsmith else tracing_context(enabled=False):
        asyncio.run(_amain(args))


if __name__ == "__main__":  # pragma: no cover
    main()
