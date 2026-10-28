"""Robots added at the staging area join missions started later, also on the Agent Server:
``langgraph dev`` loads the graph file as a module of its own, so the gateway
and the graph must share the registry through a module both import normally."""

import importlib.util
from pathlib import Path

from a6g_coordinator import graph as coordinator_graph
from a6g_coordinator.fleet_monitor import FleetMonitor
from a6g_transport.messaging import BusMessage


def test_the_graph_loaded_from_its_file_shares_the_gateways_registry() -> None:
    path = Path("packages/a6g_coordinator/src/a6g_coordinator/server.py")
    spec = importlib.util.spec_from_file_location("packages__a6g_server_copy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # as the Agent Server does
    assert module.DEPS.admitted is coordinator_graph.ADMITTED


async def test_the_gateway_records_an_added_robot_where_the_graph_reads_it(monkeypatch) -> None:
    from a6g_coordinator import gateway

    async def none() -> list[str]:
        return []

    monkeypatch.setattr(gateway, "active_missions", none)
    monkeypatch.setitem(coordinator_graph.ADMITTED, "placeholder", {})  # restored after
    await gateway.broadcast({"type": "robot_added", "robot_id": "go2_09", "a2a_url": "x"})
    try:
        assert coordinator_graph.ADMITTED["go2_09"]["a2a_url"] == "x"
    finally:
        coordinator_graph.ADMITTED.pop("go2_09", None)


def test_the_monitor_sees_a_robot_charged_in_its_state_messages() -> None:
    from datetime import UTC, datetime, timedelta

    t0 = datetime(2026, 1, 1, 8, tzinfo=UTC)
    m = FleetMonitor({"r1"}, timeout_s=10, charged_pct=90.0)

    def state(pct: float, s: int) -> list[dict]:
        msg = BusMessage(topic="a6g/fleet/r1/state", payload=f'{{"battery_pct": {pct}}}'.encode())
        return m.observe(msg, t0 + timedelta(seconds=s))

    assert state(30.0, 0) == [] and state(89.9, 1) == []
    assert state(90.2, 2) == [{"type": "robot_charged", "robot_id": "r1", "battery_pct": 90.2}]
    assert state(95.0, 3) == []  # once per crossing
    assert state(60.0, 4) == [] and state(91.0, 5)[0]["type"] == "robot_charged"
