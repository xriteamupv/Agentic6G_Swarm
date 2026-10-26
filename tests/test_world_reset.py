"""A fresh simulator ends the missions from before it (user report, 2026-10-21): after a
restart of the simulator, the coordinator still held a mission from an earlier
conversation and sent its envelopes to the new robots (the Summit resumed "lap 60"), while
"status" in the new conversation said no mission was running."""

from pathlib import Path

import pytest
from a6g_common.config import load_config
from a6g_devstack import SimStack


def _cfg():
    return load_config("config", env={}, fleet_file="sim/fleet.yaml")


async def test_a_world_reset_ends_the_mission_and_releases_the_robots(tmp_path: Path) -> None:
    stack = SimStack(_cfg(), tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        await stack.coordinator.drain()
        await stack.coordinator.broadcast(
            {"type": "world_reset", "before": "2100-01-01T00:00:00+00:00"}
        )
        await stack.coordinator.drain()
        state = await stack.coordinator.state("m1")
    finally:
        await stack.stop()
    assert state["status"] == "superseded"
    assert all(t["status"] == "cancelled" for t in state["tasks"].values())
    assert "simulator was restarted" in state["messages"][-1].content


async def test_status_in_an_empty_conversation_names_the_other_mission(tmp_path: Path) -> None:
    stack = SimStack(_cfg(), tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus", mission_id="m1")
        await stack.coordinator.drain()
        state = await stack.coordinator.chat("m2", "status")
    finally:
        await stack.stop()
    reply = state["messages"][-1].content
    if stack.store is None:
        pytest.skip("this stack has no shared store")
    assert "another conversation" in reply and "take the robots over" in reply


async def test_the_gateway_only_ends_missions_begun_before_the_restart(monkeypatch) -> None:
    from a6g_coordinator import gateway

    submitted: list[str] = []

    class Threads:
        async def get(self, mission_id: str) -> dict:
            created = {"old": "2026-10-21T10:00:00Z", "new": "2026-10-21T12:00:00Z"}
            return {"created_at": created[mission_id]}

    class Client:
        threads = Threads()

    async def active() -> list[str]:
        return ["new", "old"]

    async def submit(mission_id: str, event: dict) -> None:
        submitted.append(mission_id)

    monkeypatch.setattr(gateway, "_client", lambda: Client())
    monkeypatch.setattr(gateway, "active_missions", active)
    monkeypatch.setattr(gateway, "submit", submit)
    event = {"type": "world_reset", "before": "2026-10-21T11:00:00+00:00"}
    assert await gateway.end_missions_before(event, event["before"]) == ["old"]
    assert submitted == ["old"]  # the one the operator began after the restart stays


async def test_status_without_a_mission_is_a_question_not_an_order() -> None:
    from a6g_coordinator.decompose import interpret_chat

    assert (await interpret_chat("status", False, None))[0].intent == "status"
    assert (await interpret_chat("What is going on?", False, None))[0].intent == "status"
    order = await interpret_chat("Inspect the kiosk and report back to me", False, None)
    assert order[0].intent != "status"
