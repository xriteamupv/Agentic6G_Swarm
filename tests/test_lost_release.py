"""A robot out of contact for long is presumed stuck: the coordinator hands all its work,
the running task too, to the other robots (user request, 2026-10-27). The robot keeps
seeking coverage on its own."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from a6g_common.config import load_config
from a6g_common.schemas import DecisionKind
from a6g_coordinator.fleet_monitor import FleetMonitor
from a6g_devstack import SimStack
from a6g_transport.messaging import BusMessage
from conftest import wait_for

T0 = datetime(2026, 1, 1, 8, tzinfo=UTC)


def _state(rid: str) -> BusMessage:
    return BusMessage(topic=f"a6g/fleet/{rid}/state", payload=b"{}")


def test_the_monitor_repeats_a_long_loss_until_the_robot_is_back() -> None:
    m = FleetMonitor({"r1"}, timeout_s=10, release_s=120)
    m.observe(_state("r1"), T0)
    assert [e["type"] for e in m.check(T0 + timedelta(seconds=11))] == ["robot_lost"]
    assert m.check(T0 + timedelta(seconds=100)) == []
    late = m.check(T0 + timedelta(seconds=132))
    assert [e["type"] for e in late] == ["robot_lost_long"] and late[0]["silent_s"] == 132
    assert m.check(T0 + timedelta(seconds=200)) == []
    again = m.check(T0 + timedelta(seconds=253))  # every release period while silent
    assert [e["type"] for e in again] == ["robot_lost_long"] and again[0]["silent_s"] == 253
    assert [e["type"] for e in m.observe(_state("r1"), T0 + timedelta(seconds=501))] == [
        "robot_recovered"
    ]
    assert m.check(T0 + timedelta(seconds=505)) == []
    # a new loss starts a new count
    assert [e["type"] for e in m.check(T0 + timedelta(seconds=512))] == ["robot_lost"]
    assert [e["type"] for e in m.check(T0 + timedelta(seconds=633))] == ["robot_lost_long"]


def test_without_a_release_time_a_lost_robot_keeps_its_work() -> None:
    m = FleetMonitor({"r1"}, timeout_s=10, release_s=None)
    m.observe(_state("r1"), T0)
    m.check(T0 + timedelta(seconds=11))
    assert m.check(T0 + timedelta(hours=5)) == []


@pytest.mark.scenario
async def test_a_stuck_robots_patrol_goes_to_another_robot(tmp_path: Path) -> None:
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        await stack.mission(
            "go2_01 patrol the north-west walk, go2_02 patrol the south plaza and summit_01 "
            "patrol the east corridor"
        )

        async def running(rid: str) -> str | None:
            tasks = (await stack.coordinator.state("m1"))["tasks"]
            return next(
                (
                    tid
                    for tid, t in tasks.items()
                    if t["robot_id"] == rid and t["status"] == "in_progress"
                ),
                None,
            )

        async def summit_running() -> bool:
            return await running("summit_01") is not None

        await wait_for(summit_running, timeout=60)
        task = await running("summit_01")
        stack.link_loss("summit_01", 3600)  # stuck out of contact

        def released() -> bool:
            return any(
                d.kind is DecisionKind.FALLBACK
                and d.policy.get("rule") == "robot_lost_long"
                and task in d.output["released"]
                for d in stack.log.read("m1")
            )

        await wait_for(released, timeout=300)

        async def taken_over() -> bool:
            t = (await stack.coordinator.state("m1"))["tasks"][task]
            return t["robot_id"] not in (None, "summit_01")

        await wait_for(taken_over, timeout=60)
        lost_at = next(
            d.stamp
            for d in stack.log.read("m1")
            if d.policy.get("rule") == "robot_failure" and d.output.get("robot_id") == "summit_01"
        )
        release = next(
            d.stamp for d in stack.log.read("m1") if d.policy.get("rule") == "robot_lost_long"
        )
        # not before its running task could have ended, plus the hold and the margin
        rel = next(d for d in stack.log.read("m1") if d.policy.get("rule") == "robot_lost_long")
        assert (release - lost_at).total_seconds() >= rel.policy["silent_bound_s"] - 10
    finally:
        await stack.stop()
