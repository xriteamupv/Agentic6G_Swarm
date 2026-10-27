"""Baselines B0-B2 (SPEC A3.8): what each one switches off, against P."""

from pathlib import Path

import pytest
from a6g_common.config import FleetDefaults, load_config
from a6g_devstack import SimStack
from a6g_devstack.bench_baselines import schedule
from conftest import wait_for

GOAL = "Inspect the bike rack and the building 8K door"


def _cfg():
    return load_config("config", env={}, fleet_file="sim/fleet.yaml")


def test_every_baseline_switches_contract_net_off() -> None:
    base = _cfg().fleet.defaults.model_dump()
    assert FleetDefaults.model_validate(base).agent.contract_net  # P
    for b in ("independent", "central_memory", "central_twin"):
        assert not FleetDefaults.model_validate({**base, "baseline": b}).agent.contract_net


@pytest.mark.parametrize(("baseline", "reallocated"), [("none", True), ("independent", False)])
async def test_b0_never_reallocates(tmp_path: Path, baseline: str, reallocated: bool) -> None:
    stack = SimStack(_cfg(), tmp_path / "s", baseline=baseline)
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission(GOAL)
        await stack.coordinator.drain()
        tasks = (await stack.coordinator.state("m1"))["tasks"]
        victim_task, victim = next(
            (tid, t["robot_id"]) for tid, t in sorted(tasks.items()) if t.get("robot_id")
        )
        stack.world.resume()
        await stack.fail_robot(victim)

        async def settled() -> bool:
            t = (await stack.coordinator.state("m1"))["tasks"][victim_task]
            return t["status"] == "failed" or (t.get("robot_id") not in (None, victim))

        await wait_for(settled, 60, "the failed robot's task handled")
        task = (await stack.coordinator.state("m1"))["tasks"][victim_task]
    finally:
        await stack.stop()
    if reallocated:
        assert task["robot_id"] != victim and task["status"] != "failed"
    else:
        assert task["status"] == "failed" and "B0" in task["detail"]


@pytest.mark.parametrize(("baseline", "kept"), [("central_twin", True), ("central_memory", False)])
async def test_b1_forgets_the_mission_on_a_restart(
    tmp_path: Path, baseline: str, kept: bool
) -> None:
    stack = SimStack(_cfg(), tmp_path / "s", baseline=baseline)
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission(GOAL)
        await stack.coordinator.drain()
        await stack.kill_coordinator()
        await stack.start_coordinator()
        state = await stack.coordinator.state("m1")
    finally:
        await stack.stop()
    assert bool(state.get("tasks")) is kept


def test_the_fault_schedule_depends_only_on_the_seed() -> None:
    s = schedule(2)
    assert s == schedule(2) and s["robot_fail_s"] > s["coordinator_kill_s"]
    assert s["coordinator_restart_s"] == s["coordinator_kill_s"] + 60


async def test_a_robot_added_before_a_mission_is_part_of_it(tmp_path: Path) -> None:
    """Found by the baseline bench: a robot admitted while no mission ran was ignored by
    every later mission until the coordinator restarted (and was then admitted again as a
    newcomer, re-planning everyone's work)."""
    stack = SimStack(_cfg(), tmp_path / "s")
    await stack.start()
    try:
        await stack.add_robot(robot_id="go2_03")

        async def admitted() -> bool:
            return "go2_03" in stack.coordinator.deps.admitted

        await wait_for(admitted, 30, "go2_03 admitted")
        stack.world.pause()
        await stack.mission(GOAL)
        await stack.coordinator.drain()
        fleet = (await stack.coordinator.state("m1"))["fleet"]
        await stack.kill_coordinator()  # a restart: the known robot is no newcomer
        await stack.start_coordinator()
        stack.world.resume()
        await stack.coordinator.drain()
    finally:
        await stack.stop()
    assert fleet["go2_03"]["status"] == "available"
    changes = [d for d in stack.log.read() if d.policy.get("rule") == "fleet_change"]
    assert not any(d.output.get("released") for d in changes)


@pytest.mark.parametrize(("claimant_far", "kept"), [(True, "holder"), (False, "claimant")])
async def test_a_late_peer_award_does_not_put_two_robots_on_one_task(
    tmp_path: Path, claimant_far: bool, kept: str
) -> None:
    """Found by the baseline bench: an award made by the robots while the coordinator was
    down arrived after the restarted coordinator had given the task to another robot, and
    was adopted blindly: two robots on one task, the slower one kept it. Now the robot
    expected to finish sooner keeps it."""
    from datetime import UTC, datetime

    from a6g_common.schemas import Award, Report, ReportKind, TrafficClass

    stack = SimStack(_cfg(), tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Inspect the building 8K door")
        await stack.coordinator.drain()
        tid, entry = next(iter((await stack.coordinator.state("m1"))["tasks"].items()))
        holder = entry["robot_id"]
        claimant = next(r for r in sorted(stack.robots) if r != holder)
        kiosk = stack.cfg.campus.poi_slot("poi_kiosk", 0)
        far = stack.world.robot(claimant)
        far.x, far.y = (kiosk.x + 150.0, kiosk.y) if claimant_far else (kiosk.x, kiosk.y)
        award = Award(
            award_id="aw_late",
            cfp_id="cfp_late",
            mission_id="m1",
            envelope_id="env-x",
            task_id=tid,
            awarded_by=claimant,
            awarded_to=claimant,
            cost=1.0,
            stamp=datetime.now(UTC),
        )
        report = Report(
            report_id="rep_late",
            robot_id=claimant,
            mission_id="m1",
            kind=ReportKind.DECISION,
            traffic_class=TrafficClass.CONTROL_MSGS,
            payload={"kind": "award", "output": {"award": award.model_dump(mode="json")}},
            stamp=datetime.now(UTC),
        )
        await stack.coordinator.handle_report(report)
        await stack.coordinator.drain()
        final = (await stack.coordinator.state("m1"))["tasks"][tid]["robot_id"]
    finally:
        await stack.stop()
    assert final == (holder if kept == "holder" else claimant)


def test_a_restarted_coordinator_hears_a_hot_added_robot_die() -> None:
    """Found by the baseline bench: after a coordinator restart the fleet monitor only knew
    the configured robots, so the last-will of a hot-added robot that died meanwhile was
    ignored and its task stayed with it for good."""
    from datetime import UTC, datetime

    from a6g_coordinator.fleet_monitor import FleetMonitor
    from a6g_transport.messaging import BusMessage

    monitor = FleetMonitor({"go2_01"}, 10.0)  # a fresh monitor: go2_03 unknown
    msg = BusMessage(topic="a6g/fleet/go2_03/status", payload=b"offline", retain=True)
    events = monitor.observe(msg, datetime.now(UTC))
    assert events == [
        {"type": "robot_failed", "robot_id": "go2_03", "reason": "offline (last-will)"}
    ]
    assert monitor.observe(msg, datetime.now(UTC)) == []  # once
