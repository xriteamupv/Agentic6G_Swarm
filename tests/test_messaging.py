"""SPEC Part A2, stage C0: messaging layer, bindings, routing and per-message metrics."""

import ast
import asyncio
import json
from pathlib import Path

import pytest
from a6g_common.config import load_config
from a6g_common.messages import AgentMessage, MessageHeader, Pattern, Performative, SchemaRef
from a6g_devstack import SimStack
from a6g_observability.comms import load_records, mission_records, summarize
from a6g_transport.messaging import (
    TOPIC_HEARTBEAT,
    configure_messaging,
    memory_bus,
)
from conftest import wait_for

ROOT = Path(__file__).resolve().parents[1]
TRANSPORT_LIBS = {"a2a", "paho", "aiomqtt", "amqtt"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out


def test_no_agent_imports_a_transport_library() -> None:
    """Rule 22: only a6g_transport.bindings imports a2a / paho / aiomqtt / amqtt, and agents
    reach the bindings only through a6g_transport.messaging."""
    offenders = []
    for path in sorted((ROOT / "packages").glob("*/src/**/*.py")):
        rel = path.relative_to(ROOT).as_posix()
        in_bindings = "/a6g_transport/bindings/" in rel
        in_transport = "/src/a6g_transport/" in rel
        for module in _imports(path):
            top = module.split(".")[0]
            if top in TRANSPORT_LIBS and not in_bindings:
                offenders.append(f"{rel}: {module}")
            if module.startswith("a6g_transport.bindings") and not in_transport:
                offenders.append(f"{rel}: {module} (use a6g_transport.messaging)")
    assert not offenders, "\n".join(offenders)


def test_agent_message_round_trips_exactly() -> None:
    payload = {"deadline_ms": 1500, "cost": 12.5, "robot_id": None, "ids": [1, 2]}
    am = AgentMessage(
        header=MessageHeader(
            sender="go2_01",
            receivers=["coordinator"],
            pattern=Pattern.P2_REPORT,
            performative=Performative.INFORM,
            schema=SchemaRef(name="report"),
        ),
        payload=payload,
        nl={"summary": "patrol lap done"},
    )
    back = AgentMessage.from_json(am.to_json())
    assert back.payload == payload and isinstance(back.payload["deadline_ms"], int)
    assert back.header.pattern is Pattern.P2_REPORT and back.nl.summary == "patrol lap done"
    assert "robot_id" in json.loads(am.to_json())["payload"]  # payload None kept


def test_comms_yaml_hybrid_preset_reproduces_todays_routing() -> None:
    comms = load_config("config", env={}, fleet_file="sim/fleet.yaml").comms
    assert comms is not None and comms.preset == "hybrid"
    bindings = {p: comms.route(p).binding.value for p in comms.patterns}
    assert bindings == {
        "P1": "a2a_jsonrpc",
        "P2": "a2a_jsonrpc",
        "P3": "a2a_jsonrpc",
        "P4": "mqtt",
        "P5": "mqtt",
        "P6": "a2a_jsonrpc",
        "P7": "a2a_jsonrpc",
        "P8": "mqtt",
        "P9": "a2a_jsonrpc",
        "P10": "mcp",  # knowledge server (SPEC A3.12)
        "P11": "mqtt",  # health reports
    }
    assert comms.patterns["P8"].priority_class == max(
        p.priority_class for p in comms.patterns.values()
    )


async def test_publications_travel_wrapped_measured_and_arrive_unwrapped(tmp_path) -> None:
    configure_messaging(None, log_path=tmp_path / "comms.jsonl")
    bus = memory_bus("coordinator")
    sub = bus.as_agent("go2_01").subscribe(TOPIC_HEARTBEAT)
    first = asyncio.create_task(anext(sub))  # the subscription registers on its first read
    await asyncio.sleep(0)
    await bus.publish(TOPIC_HEARTBEAT, '{"seq": 7}', qos=1, retain=False)
    msg = await asyncio.wait_for(first, 5)
    await sub.aclose()
    assert msg.json() == {"seq": 7}  # subscribers see the payload as published
    assert msg.message.header.pattern is Pattern.P5_LIVENESS
    assert msg.message.header.sender == "coordinator"
    raw = bus.inner.history[-1].payload  # on the wire: the canonical message
    assert json.loads(raw)["header"]["pattern"] == "P5"
    sent, received = load_records([tmp_path / "comms.jsonl"])
    assert (sent["direction"], received["direction"]) == ("send", "recv")
    assert sent["wire_bytes"] > len(raw) > sent["payload_bytes"]  # MQTT framing + envelope
    assert received["agent"] == "go2_01" and received["e2e_ms"] is not None


async def test_comms_report_lists_every_message_of_a_scenario_run(tmp_path) -> None:
    """C0 acceptance: ``a6g comms report`` lists every message of a P3 scenario run (scenario
    1, routine survey to completion) with byte counts per pattern."""
    from a6g_cli.comms_report import render

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "stack")
    await stack.start()
    try:
        await stack.mission("Survey the campus")

        async def completed() -> bool:
            return (await stack.coordinator.state("m1")).get("status") == "completed"

        await wait_for(completed, timeout=90)
    finally:
        await stack.stop()

    log = stack.workdir / "comms.jsonl"
    everything = load_records([log])
    records = mission_records(everything, "m1")
    rows = {r["pattern"]: r for r in summarize(records)}
    assert {"P1", "P2", "P4", "P5"} <= set(rows)  # envelopes, reports, state, liveness
    for row in rows.values():
        assert row["messages"] > 0 and row["wire_bytes"] >= row["payload_bytes"] > 0
    # Every report a robot delivered is a P2/P6 message (none lost to the instrumentation).
    reports = [r for r in everything if r["direction"] == "send" and r["schema"] == "report"]
    assert len(reports) == len(stack.delivered) > 0
    # Every request has its reply, measured by the requester.
    sends = {
        r["msg_id"]
        for r in everything
        if r["direction"] == "send" and r["binding"] == "a2a_jsonrpc"
    }
    replies = {r["in_reply_to"] for r in everything if r["direction"] == "reply"}
    assert sends == replies
    envelopes = [r for r in records if r["schema"] == "envelope" and r["direction"] == "send"]
    assert {r["receivers"][0] for r in envelopes} == {"go2_01", "go2_02", "summit_01"}

    text, listed = render("m1", [log])
    assert "P1" in text and "envelope" in text and len(listed) == len(records)
    assert sum(1 for line in text.splitlines() if " send " in line or " reply " in line) == sum(
        1 for r in records if r["direction"] != "recv"
    )


@pytest.fixture(autouse=True)
def _memory_only_metrics():
    """Each test starts from the default routing and an in-memory sink."""
    configure_messaging(None, log_path=None)
    yield
    configure_messaging(None, log_path=None)
