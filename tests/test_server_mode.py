"""The coordinator as served by the LangGraph Agent Server (langgraph.json) for agent-chat-ui."""

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from a6g_common.schemas import Report, ReportKind, TrafficClass
from a6g_transport.messaging import A6GMessage, MessageType, MessagingClient

REPO = Path(__file__).resolve().parents[1]


def test_langgraph_json_points_at_graph_and_gateway() -> None:
    spec = json.loads((REPO / "langgraph.json").read_text())
    graph_path, graph_attr = spec["graphs"]["coordinator"].split(":")
    app_path, app_attr = spec["http"]["app"].split(":")
    assert (REPO / graph_path).exists() and graph_attr == "graph"
    assert (REPO / app_path).exists() and app_attr == "app"


def test_server_module_builds_the_graph_with_a_messages_key(monkeypatch) -> None:
    monkeypatch.setenv("A6G_COORDINATOR_LLM", "off")
    monkeypatch.setenv("A6G_FLEET_FILE", "sim/fleet.yaml")
    server = importlib.import_module("a6g_coordinator.server")
    graph = importlib.reload(server).graph
    assert "messages" in graph.get_input_jsonschema()["properties"]  # what agent-chat-ui needs
    nodes = set(graph.get_graph().nodes)
    assert {
        "ingest",
        "decompose",
        "validate",
        "arbitrate",
        "allocate",
        "publish_envelopes",
        "handle_reply",
        "escalate",
        "audit",
    } <= nodes


async def test_gateway_turns_robot_reports_into_queued_runs(monkeypatch) -> None:
    monkeypatch.setenv("A6G_FLEET_FILE", "sim/fleet.yaml")
    gateway = importlib.reload(importlib.import_module("a6g_coordinator.gateway"))
    created = []

    class Runs:
        async def create(self, thread_id, assistant_id, **kwargs):
            created.append((thread_id, assistant_id, kwargs))

    class Client:
        runs = Runs()

    monkeypatch.setattr(gateway, "_client", lambda: Client())
    report = Report(
        report_id="rep_1",
        robot_id="go2_01",
        mission_id="0b8f3e1e-2f3a-4a5b-9c1d-111122223333",
        kind=ReportKind.SKILL_RESULT,
        traffic_class=TrafficClass.CONTROL_MSGS,
        payload={"task_id": "t1"},
        stamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    base = "http://127.0.0.1:2024/a6g"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app), base_url="http://127.0.0.1:2024"
    ) as http:
        card = (await http.get("/a6g/.well-known/agent-card.json")).json()
        assert card["name"] == "a6g-coordinator"
        reply = await MessagingClient(base, httpx_client=http).send(
            A6GMessage(MessageType.REPORT, report.model_dump(mode="json"))
        )
        assert (await http.get("/a6g/health")).json()["assistant_id"] == "coordinator"
    assert reply.type is MessageType.REPORT_ACK
    ((thread, assistant, kwargs),) = created
    assert thread == report.mission_id and assistant == "coordinator"
    assert kwargs["multitask_strategy"] == "enqueue"
    assert kwargs["input"]["event"]["report"]["report_id"] == "rep_1"


def test_cli_mission_names_map_to_stable_uuid_threads() -> None:
    from a6g_cli.main import mission_thread

    assert mission_thread("demo-1") == mission_thread("demo-1")
    uuid = "0b8f3e1e-2f3a-4a5b-9c1d-111122223333"
    assert mission_thread(uuid) == uuid


async def test_plain_coordinator_gateway_serves_reports_and_operator_commands(tmp_path) -> None:
    """a6g-coordinator's A2A gateway (no Agent Server)."""
    from a6g_common.config import load_config
    from a6g_coordinator.plain import build_plain_app
    from a6g_devstack import SimStack

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        base = "http://coord.test/a6g"
        app = build_plain_app(stack.coordinator, base)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://coord.test"
        ) as http:
            client = MessagingClient(base, httpx_client=http, timeout_s=60)
            goal = await client.send(
                A6GMessage(
                    MessageType.OPERATOR,
                    {
                        "command": "goal",
                        "mission_id": "m_plain",
                        "text": "Patrol the campus",
                    },
                )
            )
            assert "Assignments:" in goal.payload["reply"]
            status = await client.send(
                A6GMessage(MessageType.OPERATOR, {"command": "status", "mission_id": "m_plain"})
            )
            assert len(status.payload["tasks"]) == 3 and status.payload["pending"] == []
            report = Report(
                report_id="rep_plain",
                robot_id="go2_01",
                mission_id="m_plain",
                kind=ReportKind.TASK_STATUS,
                traffic_class=TrafficClass.CONTROL_MSGS,
                payload={"task_id": "x", "status": "started"},
                stamp=stack.world.clock.now(),
            )
            ack = await client.send(A6GMessage(MessageType.REPORT, report.model_dump(mode="json")))
            assert ack.type is MessageType.REPORT_ACK
    finally:
        await stack.stop()
