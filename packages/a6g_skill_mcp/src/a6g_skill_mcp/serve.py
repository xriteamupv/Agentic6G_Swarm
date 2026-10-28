"""``a6g-skill-mcp``: serve one robot's skills over MCP streamable HTTP.

    uv run a6g-skill-mcp --robot go2_01                       # 2D simulator backend
    uv run a6g-skill-mcp --robot summit_01 --backend ros --profile gazebo

``--backend ros`` talks to that robot's ``ros_adapter`` (Go2Backend / SummitBackend from
the manifest's platform); socket, ``map_to_campus`` and timeout scale come from the profile.
"""

import argparse
import asyncio
import contextlib
import logging
from urllib.parse import urlsplit

import uvicorn
from a6g_common.clock import ManualClock, utc_now
from a6g_common.config import ProfileName, adapter_socket, load_config
from a6g_observability.decision_log import DecisionLog
from a6g_sim.runner import SimRunner
from a6g_sim.world import SimWorld

from a6g_skill_mcp.adapter_client import AdapterClient
from a6g_skill_mcp.ros_backend import BACKENDS
from a6g_skill_mcp.server import build_mcp_server
from a6g_skill_mcp.service import SkillService
from a6g_skill_mcp.sim_backend import SimBackend


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="a6g-skill-mcp", description=__doc__)
    p.add_argument("--robot", required=True, help="robot_id from fleet.yaml")
    p.add_argument("--backend", choices=["sim", "ros"], default="sim")
    p.add_argument("--profile", default=None, help="gazebo | real (required with --backend ros)")
    p.add_argument("--socket", default=None, help="override the adapter socket")
    p.add_argument("--config-dir", default=None)
    p.add_argument("--host", default=None, help="bind host (default: from mcp_url)")
    p.add_argument("--port", type=int, default=None, help="bind port (default: from mcp_url)")
    p.add_argument("--image-dir", default="var/images")
    p.add_argument("--realtime-factor", type=float, default=1.0, help="sim speed-up")
    return p.parse_args(argv)


def build_service(cfg, robot_id: str, args: argparse.Namespace):
    """(service, runner context) for the chosen backend."""
    decisions = DecisionLog(cfg.fleet.edge.decision_log_path)
    floor = cfg.allocator.default_battery_floor_pct
    if args.backend == "sim":
        world = SimWorld(
            cfg.campus, ManualClock(utc_now()), charge_below_pct=cfg.allocator.recharge_idle_pct
        )
        world.add_robot(cfg.manifests[robot_id])
        backend = SimBackend(world, robot_id, args.image_dir)
        runner = SimRunner(world, realtime_factor=args.realtime_factor, idle_when_quiet=False)
        return SkillService(backend, cfg.campus, decisions, battery_floor_pct=floor), runner
    if cfg.profile is None:
        raise SystemExit("--backend ros needs --profile gazebo|real")
    return ros_service(cfg, robot_id, args.socket, args.image_dir, decisions), (
        contextlib.nullcontext()
    )


def ros_service(cfg, robot_id: str, socket: str | None, image_dir, decisions=None):
    """SkillService over the robot's ros_adapter (also used by the Gazebo tests)."""
    profile = cfg.profile
    manifest = cfg.manifests[robot_id]
    scale = profile.timeout_scale
    if scale != 1.0:
        manifest = manifest.model_copy(
            update={"skill_timeouts": {k: v * scale for k, v in manifest.skill_timeouts.items()}}
        )
    robot = profile.robots.get(robot_id)
    backend = BACKENDS[manifest.platform](
        manifest,
        AdapterClient(socket or adapter_socket(robot_id, profile)),
        map_to_campus=robot.map_to_campus if robot else None,
        image_dir=image_dir,
        real_robot=profile.profile is ProfileName.REAL,
    )
    return SkillService(
        backend,
        cfg.campus,
        decisions,
        battery_floor_pct=cfg.allocator.default_battery_floor_pct,
        time_scale=scale,
    )


async def _serve(args: argparse.Namespace) -> None:
    cfg = load_config(args.config_dir, profile=args.profile)
    fleet_robot = cfg.fleet.robot(args.robot)
    url = urlsplit(fleet_robot.mcp_url)
    host = args.host or url.hostname or "127.0.0.1"
    port = args.port or url.port or 8200
    service, runner = build_service(cfg, args.robot, args)
    app = build_mcp_server(service).streamable_http_app(
        streamable_http_path=url.path or "/mcp", host=host
    )
    async with runner:
        await uvicorn.Server(uvicorn.Config(app, host=host, port=port)).serve()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_serve(_parse(argv)))


if __name__ == "__main__":  # pragma: no cover
    main()
