"""``a6g-gz-stack``: the Gazebo deployment as a service (SPEC A1 11).

    uv run a6g-gz-stack [--fleet config/profiles/gazebo.yaml] [--headless]
                        [--robots summit_01,go2_01,go2_02]

Starts the Gazebo world, and per robot its spawn, bridge, shims, localization, Nav2,
``ros_adapter``, skill server (on the fleet's MCP URL) and robot agent (A2A on 81xx); the
embedded MQTT broker when none is running; and the same control API on :8300 as
``a6g-sim-stack``, so ``a6g sim inject ...`` works unchanged. The coordinator runs in
``langgraph dev`` and agent-chat-ui works as before.
"""

import argparse
import asyncio
import contextlib
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from a6g_common.config import load_config
from a6g_common.schemas import LinkQuality
from a6g_kb.serve import make_embedder
from a6g_observability.metrics import mission_metrics, stamp, write_csv
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

from a6g_devstack.gz_stack import GzStack
from a6g_devstack.serve import (
    _QuietServer,
    _reachable,
    install_shutdown,
    serve_interactions,
    serve_kb,
    world_reset,
)
from a6g_devstack.view import robot_activity, status_overlay, view_routes

log = logging.getLogger("a6g-gz-stack")


class _AgentProxy:
    """Resolves to the robot's current agent; unreachable when failed or out of coverage."""

    def __init__(self, stack: GzStack, robot_id: str) -> None:
        self._stack, self._robot_id = stack, robot_id
        first = stack.robots[robot_id].agent
        self.deps, self.robot_id = first.deps, robot_id  # type: ignore[union-attr]

    def __getattr__(self, name: str) -> Any:
        agent = self._stack.robots[self._robot_id].agent
        if agent is None:
            raise ConnectionError(f"{self._robot_id} is down")
        if self._stack.links.quality(self._robot_id) is LinkQuality.LOST:
            raise ConnectionError(f"{self._robot_id} is out of 5G coverage")
        return getattr(agent, name)


def _models(cfg: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Robot justifications and perception triage from models.yaml (SPEC P5), on request."""
    from a6g_common.config import ModelRole
    from a6g_common.models import chat_model
    from a6g_robot_agent.perception import vlm_triage

    out: dict[str, Any] = {}
    if not args.no_robot_llm:  # each robot's cognitive core (SPEC A2.3), cloud by default
        out["model"] = chat_model(ModelRole.ONBOARD_VLM, cfg.models)
    if args.triage == "vlm":
        out["triage"], out["edge"] = vlm_triage(cfg.models)
    return out


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="a6g-gz-stack",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--fleet", default="config/profiles/gazebo.yaml", help="Gazebo profile file")
    p.add_argument("--fleet-file", default="gazebo/fleet.yaml", help="relative to the config dir")
    p.add_argument("--config-dir", default="config")
    p.add_argument("--robots", default="", help="comma-separated (default: the whole fleet)")
    p.add_argument("--headless", action="store_true", help="no Gazebo GUI")
    p.add_argument("--workdir", default="var/gz_stack")
    p.add_argument("--control-port", type=int, default=8300)
    p.add_argument("--embedded-broker", choices=["auto", "yes", "no"], default="auto")
    p.add_argument("--no-arm", action="store_true", help="leave robots disarmed")
    p.add_argument(
        "--no-robot-llm",
        action="store_true",
        help="robots decide with code only (default: each robot's model, role onboard_vlm, "
        "proposes and code validates; SPEC A2.3)",
    )
    p.add_argument(
        "--triage",
        choices=["stub", "vlm"],
        default="stub",
        help="vlm: robots triage camera images with the on-board/edge VLMs of models.yaml "
        "(default stub: injected anomalies only)",
    )
    p.add_argument(
        "--langsmith",
        action="store_true",
        help="trace the in-process agents to LangSmith (default off: every robot event is a "
        "run, which floods the trace quota; the coordinator in langgraph dev is unaffected)",
    )
    return p.parse_args(argv)


async def _amain(args: argparse.Namespace) -> None:
    profile = Path(args.fleet).stem
    cfg = load_config(args.config_dir, fleet_file=args.fleet_file, profile=profile)
    mqtt = cfg.fleet.edge.mqtt
    broker = None
    if args.embedded_broker == "yes" or (
        args.embedded_broker == "auto" and not _reachable(mqtt.host, mqtt.port)
    ):
        broker = await start_embedded_broker(mqtt.host, mqtt.port)
        log.warning("embedded MQTT broker on %s:%s", mqtt.host, mqtt.port)

    configure_messaging(cfg.comms)
    url = cfg.fleet.edge.coordinator_a2a_url
    coordinator = MessagingClient(url, sender="devstack", receiver="coordinator", timeout_s=5.0)
    workdir = Path(args.workdir)
    async with mqtt_bus(mqtt.host, mqtt.port, agent_id="devstack", client_id="a6g-gz-stack") as bus:
        stack = GzStack(
            cfg,
            workdir,
            robot_ids=[r for r in args.robots.split(",") if r] or None,
            headless=True,
            gui=not args.headless,
            auto_arm=not args.no_arm,
            bus=bus,
            uplink_factory=lambda rid: A2AUplink(
                MessagingClient(url, sender=rid, receiver="coordinator", timeout_s=5.0)
            ),
            a2a_urls={r.robot_id: r.a2a_url for r in cfg.fleet.robots},
            operator_token=os.environ.get("A6G_OPERATOR_TOKEN"),
            **_models(cfg, args),
            kb_embedder=make_embedder(cfg),
        )
        servers: dict[str, uvicorn.Server] = {}
        tasks: list[asyncio.Task[Any]] = []
        try:
            started_at = datetime.now(UTC).isoformat()
            await stack.start(coordinator=False)
            tasks.append(
                asyncio.create_task(world_reset(coordinator, started_at), name="world-reset")
            )

            def serve_robot(robot_id: str) -> None:
                url = urlsplit(stack.a2a_urls[robot_id])
                app = build_robot_app(_AgentProxy(stack, robot_id), stack.a2a_urls[robot_id])  # type: ignore[arg-type]
                server = _QuietServer(
                    uvicorn.Config(app, host=url.hostname, port=url.port, log_level="warning")
                )
                servers[robot_id] = server
                tasks.append(asyncio.create_task(server.serve(), name=f"a2a-{robot_id}"))

            for robot_id in stack.robots:
                serve_robot(robot_id)
            serve_kb(stack, cfg, servers, tasks, _QuietServer)
            serve_interactions(stack, cfg, servers, tasks, _QuietServer)
            stack.echo_speech = True

            async def body(request: Request) -> dict[str, Any]:
                with contextlib.suppress(ValueError):
                    return await request.json()
                return {}

            async def say(request: Request) -> JSONResponse:
                data = await body(request)
                rid = str(data.get("robot_id"))
                robot = stack.robots.get(rid)
                if robot is None or robot.agent is None:
                    return JSONResponse({"error": f"unknown or down robot {rid}"}, status_code=404)
                answer = await robot.agent.say(
                    str(data["text"]), lang=data.get("lang"), end=bool(data.get("end"))
                )
                return JSONResponse({"robot_id": rid, "says": answer})

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
                await stack.anomaly(b["poi_id"], b["label"])
                return JSONResponse({"ok": True, **b})

            async def weather_alert(request: Request) -> JSONResponse:
                event = stack.weather_event()
                stack.injections.append({"kind": "weather_alert", "t": time.time()})
                await coordinator.send(A6GMessage(MessageType.OPERATOR, {"event": event}))
                return JSONResponse({"ok": True, "exposed_pois": event["exposed_pois"]})

            async def coordinator_kill(request: Request) -> JSONResponse:
                return JSONResponse(
                    {"ok": False, "hint": "the coordinator runs in `langgraph dev`: stop it there"},
                    status_code=409,
                )

            async def add_robot(request: Request) -> JSONResponse:
                b = await body(request)
                agent = await stack.add_robot(b.get("manifest"), b.get("robot_id"))
                rid = agent.robot_id
                port = 8101 + len(stack.a2a_urls)
                stack.a2a_urls[rid] = f"http://127.0.0.1:{port}"
                agent.a2a_url = stack.a2a_urls[rid]
                serve_robot(rid)
                return JSONResponse({"ok": True, "robot_id": rid, "a2a_url": stack.a2a_urls[rid]})

            async def status(request: Request) -> JSONResponse:
                return JSONResponse(await stack.status())

            async def metrics(request: Request) -> JSONResponse:
                mission = (await body(request)).get("mission_id") or "m1"
                rows = mission_metrics(
                    mission, stack.log.read(mission), stack.injections, stack.bytes_per_robot()
                )
                path = write_csv(workdir / "metrics" / f"{mission}_{stamp()}.csv", rows)
                return JSONResponse({"ok": True, "csv": str(path), "rows": rows})

            async def view_state() -> dict[str, Any]:
                snap = await stack.status()
                robots = {}
                for rid, r in snap.get("robots", {}).items():
                    proc = stack.robots.get(rid)
                    robots[rid] = {
                        **r,
                        "moving": abs(r.get("vx", 0.0)) > 0.02 or abs(r.get("wz", 0.0)) > 0.05,
                        "activity": await robot_activity(proc.agent if proc else None),
                    }
                t = float(snap.get("sim_time") or 0.0)
                label = f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}"
                return {
                    "sim_time_label": label,
                    "robots": robots,
                    "statuses": status_overlay(stack.kb_service),
                }

            control = Starlette(
                routes=[
                    *view_routes(cfg, view_state, "a6g · Gazebo"),
                    Route("/sim/inject/robot_fail", robot_fail, methods=["POST"]),
                    Route("/sim/inject/link_loss", link_loss, methods=["POST"]),
                    Route("/sim/inject/anomaly", anomaly, methods=["POST"]),
                    Route("/sim/inject/weather_alert", weather_alert, methods=["POST"]),
                    Route("/sim/inject/coordinator_kill", coordinator_kill, methods=["POST"]),
                    Route("/sim/add_robot", add_robot, methods=["POST"]),
                    Route("/sim/say", say, methods=["POST"]),
                    Route("/sim/status", status),
                    Route("/sim/metrics", metrics, methods=["POST"]),
                ]
            )
            log.warning(
                "gz stack up: %s, coordinator %s, control http://127.0.0.1:%s (live view: /view)",
                ", ".join(stack.robots), cfg.fleet.edge.coordinator_a2a_url, args.control_port,
            )  # fmt: skip
            control_server = _QuietServer(
                uvicorn.Config(
                    control, host="127.0.0.1", port=args.control_port, log_level="warning"
                )
            )
            install_shutdown(control_server, force=stack.kill_now)
            await control_server.serve()
        finally:
            for server in servers.values():
                server.should_exit = True
            await asyncio.gather(*tasks, return_exceptions=True)
            await stack.stop()
    if broker is not None:
        await broker.shutdown()
    log.warning("gz stack stopped")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("a6g_skill_mcp.service").setLevel(logging.ERROR)
    args = _parse(argv)
    # LangSmith tracing of the robots (and in-process coordinator) only on request.
    with contextlib.nullcontext() if args.langsmith else tracing_context(enabled=False):
        asyncio.run(_amain(args))


if __name__ == "__main__":  # pragma: no cover
    main()
