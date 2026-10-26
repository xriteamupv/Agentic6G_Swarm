"""``a6g-robot-agent``: run one robot agent (A2A server + MQTT + local SQLite checkpoints)."""

import argparse
import asyncio
import contextlib
import logging
from urllib.parse import urlsplit

import uvicorn
from a6g_common.config import ModelRole, load_config
from a6g_common.models import chat_model
from a6g_interactions import InteractionClient
from a6g_kb import KnowledgeClient
from a6g_observability.decision_log import DecisionLog
from a6g_transport.messaging import MessagingClient, configure_messaging, mqtt_bus
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from a6g_robot_agent.builder import build_robot_agent
from a6g_robot_agent.perception import vlm_triage
from a6g_robot_agent.server import build_robot_app
from a6g_robot_agent.skills import SkillClient
from a6g_robot_agent.uplink import A2AUplink


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="a6g-robot-agent", description=__doc__)
    p.add_argument("--robot", required=True)
    p.add_argument("--config-dir", default=None)
    p.add_argument("--state-db", default=None, help="default var/<robot>_agent.sqlite")
    p.add_argument("--no-mqtt", action="store_true", help="run without the MQTT broker")
    p.add_argument("--no-llm", action="store_true", help="template justifications only")
    p.add_argument(
        "--triage",
        choices=["vlm", "stub"],
        default="vlm",
        help="vlm: on-board + edge VLMs from models.yaml (default); stub: no models",
    )
    return p.parse_args(argv)


async def _serve(args: argparse.Namespace) -> None:
    cfg = load_config(args.config_dir)
    configure_messaging(cfg.comms)
    robot = cfg.fleet.robot(args.robot)
    url = urlsplit(robot.a2a_url)
    db = args.state_db or f"var/{args.robot}_agent.sqlite"
    model = None if args.no_llm else chat_model(ModelRole.COORDINATOR_LLM, cfg.models)
    triage, edge = vlm_triage(cfg.models) if args.triage == "vlm" else (None, None)
    async with contextlib.AsyncExitStack() as stack:
        bus = None
        if not args.no_mqtt:
            mqtt = cfg.fleet.edge.mqtt
            bus = await stack.enter_async_context(
                mqtt_bus(
                    mqtt.host, mqtt.port, agent_id=args.robot, client_id=f"a6g-agent-{args.robot}"
                )
            )
        checkpointer = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(db))
        skills = await stack.enter_async_context(SkillClient(robot.mcp_url))
        agent = build_robot_agent(
            cfg,
            args.robot,
            skills=skills,
            checkpointer=checkpointer,
            uplink=A2AUplink(
                MessagingClient(
                    cfg.fleet.edge.coordinator_a2a_url, sender=args.robot, receiver="coordinator"
                )
            ),
            decisions=DecisionLog(cfg.fleet.edge.decision_log_path),
            model=model,
            triage=triage,
            edge=edge,
            bus=bus,
            kb=KnowledgeClient(
                cfg.knowledge.url, agent_id=args.robot, timeout_s=cfg.knowledge.timeout_s
            ),
            interactions=InteractionClient(
                cfg.interactions.url, agent_id=args.robot, timeout_s=cfg.interactions.timeout_s
            ),
        )
        await stack.enter_async_context(agent)
        app = build_robot_app(agent, robot.a2a_url)
        server = uvicorn.Server(uvicorn.Config(app, host=url.hostname, port=url.port or 8100))
        await server.serve()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_serve(_parse(argv)))


if __name__ == "__main__":  # pragma: no cover
    main()
