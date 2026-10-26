"""An idle robot asks for work, and takes what another robot has queued (2026-10-21).

User report: a robot finished its patrol lap, its sector's next lap went to a nearer robot
that already patrolled another sector, and the first robot stood idle while the second
held two sectors. Now the idle robot tells the coordinator, which moves to it a task the
busy robot has not started."""

from pathlib import Path

import pytest
from a6g_common.config import load_config
from a6g_common.schemas import PoiTarget, SectorTarget, TaskKind, TaskSpec
from a6g_coordinator.graph import idle_moves, initial_fleet
from a6g_devstack import SimStack
from conftest import wait_for


@pytest.fixture(scope="module")
def cfg():
    return load_config("config", env={}, fleet_file="sim/fleet.yaml")


def _task(tid: str, kind: TaskKind, target, robot: str | None, status: str, **extra) -> dict:
    spec = TaskSpec(task_id=tid, kind=kind, target=target, est_duration_s=60)
    caps = {TaskKind.SURVEY_SECTOR: ["survey"], TaskKind.INSPECT_POI: ["inspect"]}[kind]
    spec = spec.model_copy(update={"required_capabilities": caps})
    return {"spec": spec.model_dump(mode="json"), "status": status, "robot_id": robot, **extra}


def _state(cfg):
    tasks = {
        "lap_east": _task(
            "lap_east",
            TaskKind.SURVEY_SECTOR,
            SectorTarget(sector_id="sec_east"),
            "go2_02",
            "in_progress",
        ),
        "lap_south": _task(
            "lap_south",
            TaskKind.SURVEY_SECTOR,
            SectorTarget(sector_id="sec_south"),
            "go2_02",
            "assigned",
        ),
        "lap_north": _task(
            "lap_north",
            TaskKind.SURVEY_SECTOR,
            SectorTarget(sector_id="sec_north"),
            "summit_01",
            "in_progress",
        ),
    }
    fleet = initial_fleet(cfg)
    fleet["go2_01"].update(idle=True, idle_pose={"x": 20.0, "y": -40.0})
    return tasks, fleet


def test_the_idle_robot_takes_the_lap_the_busy_one_has_queued(cfg) -> None:
    tasks, fleet = _state(cfg)
    assert idle_moves(cfg, tasks, fleet) == [("lap_south", "go2_02", "go2_01")]
    assert tasks["lap_south"]["robot_id"] == "go2_01"
    assert tasks["lap_east"]["robot_id"] == "go2_02"  # running work never moves
    assert not fleet["go2_01"]["idle"]
    assert idle_moves(cfg, tasks, fleet) == []  # nothing left to balance


@pytest.mark.parametrize(
    "extra",
    [
        {"pinned_to": "go2_02"},  # the operator gave it by name
        {"refused_by": ["go2_01"]},  # the idle robot refused it
    ],
)
def test_work_that_belongs_to_its_robot_stays(cfg, extra) -> None:
    tasks, fleet = _state(cfg)
    tasks["lap_south"].update(extra)
    assert idle_moves(cfg, tasks, fleet) == []


def test_queued_bundled_work_moves_to_an_idle_robot(cfg) -> None:
    """A bundle keeps a share on one robot; an idle robot still takes queued work from it
    (2026-10-22: the robot freed from a post stayed idle beside a bundled patrol)."""
    tasks, fleet = _state(cfg)
    tasks["lap_south"]["bundle"] = "g1"
    assert idle_moves(cfg, tasks, fleet) == [("lap_south", "go2_02", "go2_01")]
    assert tasks["lap_south"]["bundle"] is None


def test_a_robot_never_given_work_is_idle_too(cfg) -> None:
    tasks, fleet = _state(cfg)
    fleet["go2_01"].pop("idle")  # it never reported: it had no envelope to report from
    assert idle_moves(cfg, tasks, fleet) == [("lap_south", "go2_02", "go2_01")]


def test_only_queued_work_of_a_robot_with_more_than_one_task_moves(cfg) -> None:
    tasks, fleet = _state(cfg)
    del tasks["lap_east"]  # go2_02 holds only the south lap: it is not overloaded
    assert idle_moves(cfg, tasks, fleet) == []


def test_an_idle_robot_only_gets_what_it_can_do(cfg) -> None:
    tasks, fleet = _state(cfg)
    tasks["lap_south"] = _task(
        "lap_south", TaskKind.INSPECT_POI, PoiTarget(poi_id="poi_kiosk"), "go2_02", "assigned"
    )
    tasks["lap_south"]["spec"]["required_capabilities"] = ["payload"]  # nobody has it
    assert idle_moves(cfg, tasks, fleet) == []


async def test_robots_that_finish_their_work_tell_the_coordinator(tmp_path: Path, cfg) -> None:
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        await stack.mission("Inspect the bike rack, the bus stop and the info kiosk")

        async def idle_known() -> bool:
            fleet = (await stack.coordinator.state("m1"))["fleet"]
            return all(e.get("idle") for e in fleet.values())

        await wait_for(idle_known, 120, "every robot reported idle after its inspection")
        fleet = (await stack.coordinator.state("m1"))["fleet"]
    finally:
        await stack.stop()
    assert all(e.get("idle_pose") for e in fleet.values())


def test_the_next_lap_carries_the_end_patrolled_longest_ago(cfg) -> None:
    from a6g_coordinator.graph import _next_lap

    tasks = {
        "lap": _task(
            "lap", TaskKind.SURVEY_SECTOR, SectorTarget(sector_id="sec_south"), "go2_01", "done"
        )
    }
    tasks["lap"]["reverse"] = False  # the robot reported it ran first -> last
    new = _next_lap(tasks, "lap")
    target = tasks[new]["spec"]["target"]
    assert target["prefer_reverse"] is False and target["reverse"] is None


def test_an_idle_robot_too_low_for_the_lap_is_not_given_it(cfg) -> None:
    """User report (2026-10-21): the rebalancing pushed laps to an idle go2_01 at ~32 %,
    which declined them ("not enough battery") again and again."""
    tasks, fleet = _state(cfg)
    fleet["go2_01"]["idle_battery_pct"] = 30.0  # a lap would leave it under the reserve
    assert idle_moves(cfg, tasks, fleet) == []
    fleet["go2_01"]["idle_battery_pct"] = 95.0
    assert idle_moves(cfg, tasks, fleet) == [("lap_south", "go2_02", "go2_01")]


def test_a_battery_refusal_says_by_how_much(cfg) -> None:
    from a6g_coordinator.graph import battery_refusal

    detail = "battery would end at 13.3% (< floor 20.0% + margin 5.0%)"
    assert battery_refusal(cfg, detail) == (
        "that route would leave it at 13 % and it keeps 25 % in reserve"
    )
    assert battery_refusal(cfg, "recharging at the staging area") == "it is recharging"


async def test_the_robot_reply_carries_the_refusal_details(agent_env) -> None:
    from conftest import make_envelope, survey

    agent_env.robot.battery_pct = 30.0
    reply = await agent_env.agent.handle_envelope(make_envelope([survey("sec_east")]))
    assert reply.refusals == {"t_survey_sec_east": "battery"}
    assert reply.refusal_details["t_survey_sec_east"].startswith("battery would end at")


async def test_charging_clears_battery_refusals_but_not_the_others(tmp_path: Path, cfg) -> None:
    """User report (2026-10-21): go2_02 and the Summit at 100 % stayed barred from the
    North-west lap they had declined while charging ("every available robot is busy or
    declined")."""
    from datetime import UTC, datetime

    from a6g_common.schemas import Report, ReportKind, TrafficClass

    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the south plaza")
        await stack.coordinator.drain()
        coord = stack.coordinator
        tasks = (await coord.state("m1"))["tasks"]
        tid = next(iter(tasks))
        tasks[tid].update(
            refused_by=["go2_02", "summit_01"],
            refused_for={"go2_02": "battery", "summit_01": "capability"},
        )
        await coord.graph.aupdate_state(coord._config("m1"), {"tasks": tasks})
        charged = Report(
            report_id="rep_charged",
            robot_id="go2_02",
            mission_id="m1",
            kind=ReportKind.ENVELOPE_STATUS,
            traffic_class=TrafficClass.CONTROL_MSGS,
            payload={"status": "charged", "battery_pct": 90.0},
            stamp=datetime.now(UTC),
        )
        await coord.handle_report(charged)
        await coord.drain()
        task = (await coord.state("m1"))["tasks"][tid]
    finally:
        await stack.stop()
    assert task["refused_by"] == ["summit_01"]  # the battery bar is gone, the other stays


async def test_a_robot_with_no_envelope_left_keeps_asking_and_reports_its_charge(
    tmp_path: Path, cfg
) -> None:
    """User report (2026-10-21): go2_02 finished its lap (its envelope closed), recharged
    and stood idle for minutes: with no envelope thread it neither asked for work nor said
    it was charging or charged, so the coordinator kept it as charging."""
    agent = cfg.fleet.defaults.agent.model_copy(
        update={"work_request_interval_s": 20.0, "work_request_min_wall_s": 0.0}
    )
    defaults = cfg.fleet.defaults.model_copy(update={"agent": agent})
    cfg = cfg.model_copy(update={"fleet": cfg.fleet.model_copy(update={"defaults": defaults})})
    stack = SimStack(cfg, tmp_path / "s")
    seen: list[tuple[str, str]] = []
    await stack.start()
    try:
        coord = stack.coordinator
        handle = coord.handle_report

        async def spy(report):
            if report.kind.value == "envelope_status":
                seen.append((report.robot_id, report.payload.get("status")))
            return await handle(report)

        coord.handle_report = spy
        await stack.mission("Inspect the bike rack")
        state = await coord.state("m1")
        rid = next(t["robot_id"] for t in state["tasks"].values())

        def asked_twice() -> bool:
            return sum(1 for r, s in seen if r == rid and s == "idle") >= 2

        await wait_for(asked_twice, 60, "repeated requests for work")
        stack.world.charge_pct_per_s = 20.0
        stack.world.robot(rid).battery_pct = 30.0  # idle and low: it goes to charge

        def charged() -> bool:
            return (rid, "recharging") in seen and (rid, "charged") in seen

        await wait_for(charged, 60, "recharging and charged reported")
        await coord.drain()
        fleet = (await coord.state("m1"))["fleet"]
    finally:
        await stack.stop()
    assert fleet[rid]["status"] == "available"


async def test_a_guard_post_is_held_until_the_robot_gets_another_order(tmp_path: Path, cfg) -> None:
    """User report (2026-10-21): "Send go2_02 to hold the bike rack" ended after 600 s
    ("finished standing guard"), and a patrol lap was queued behind the post. A post is now
    held until released, its robot takes no other work meanwhile, and its next order
    releases it."""
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        await stack.mission("Patrol the campus")
        await stack.coordinator.chat("m1", "Send go2_02 to hold the bike rack")
        t0 = stack.world.clock.now()

        async def long_after() -> bool:  # three hold periods of 600 s
            return (stack.world.clock.now() - t0).total_seconds() > 3 * 600

        await wait_for(long_after, 120, "three hold periods")
        await stack.coordinator.drain()
        tasks = (await stack.coordinator.state("m1"))["tasks"]
        post = next(t for t in tasks.values() if t["spec"]["kind"] == "hold_post")
        assert post["status"] == "in_progress" and post["spec"]["until_cleared"]
        others = [
            tid
            for tid, t in tasks.items()
            if t.get("robot_id") == "go2_02"
            and t["status"] in ("assigned", "in_progress")
            and t["spec"]["kind"] != "hold_post"
        ]
        assert others == []  # nothing queued behind the post
        state = await stack.coordinator.chat("m1", "Send go2_02 to the east gate")
    finally:
        await stack.stop()
    post = next(t for t in state["tasks"].values() if t["spec"]["kind"] == "hold_post")
    assert post["status"] == "cancelled"
    assert any("stops standing guard" in m.text for m in state["messages"])


async def test_the_others_share_the_patrol_instead_of_each_doing_all_of_it(
    tmp_path: Path, cfg
) -> None:
    """User report (2026-10-21): "Send one robot to guard the east gate, the others to
    patrol" gave each of the other two robots the whole campus (both on the same sectors at
    the same time), and the guard became a plain trip to the gate."""
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        state = await stack.coordinator.chat(
            "m1", "Send one robot to guard the east gate, the others to patrol"
        )
    finally:
        await stack.stop()
    tasks = state["tasks"].values()
    posts = [t for t in tasks if t["spec"]["kind"] == "hold_post"]
    laps = [t for t in tasks if t["spec"]["kind"] == "survey_sector"]
    assert len(posts) == 1 and posts[0]["spec"]["until_cleared"]
    sectors = sorted(t["spec"]["target"]["sector_id"] for t in laps)
    assert sectors == sorted(s.sector_id for s in cfg.campus.sectors)  # each sector once
    guard = posts[0]["robot_id"]
    patrollers = {t["robot_id"] for t in laps}
    assert guard not in patrollers and len(patrollers) == 2  # the others share them


async def test_stop_holding_ends_the_post_and_the_robot_joins_the_patrol(
    tmp_path: Path, cfg
) -> None:
    """User report (2026-10-22): "Stop holding the east gate and send that robot to patrol
    also" was answered "already under way": a plan could only add work."""
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        await stack.coordinator.chat(
            "m1", "Send one robot to guard the east gate, the others to patrol"
        )
        state = await stack.coordinator.state("m1")
        guard = next(
            t["robot_id"] for t in state["tasks"].values() if t["spec"]["kind"] == "hold_post"
        )
        state = await stack.coordinator.chat(
            "m1", "Stop holding the east gate and send that robot to patrol also"
        )

        async def guard_patrols() -> bool:
            tasks = (await stack.coordinator.state("m1"))["tasks"].values()
            return any(
                t["spec"]["kind"] == "survey_sector"
                and t.get("robot_id") == guard
                and t["status"] in ("assigned", "in_progress")
                for t in tasks
            )

        await wait_for(guard_patrols, 120, "the freed robot takes a sector")
    finally:
        await stack.stop()
    post = next(t for t in state["tasks"].values() if t["spec"]["kind"] == "hold_post")
    assert post["status"] == "cancelled"
    assert not any("already under way" in m.text for m in state["messages"][-3:])


def test_assignments_say_what_the_robot_is_already_doing(cfg) -> None:
    from a6g_coordinator.narrate import Narrator
    from conftest import survey

    narr = Narrator(cfg.campus)
    tasks = {
        "north": {
            "spec": survey("sec_north").model_dump(mode="json"),
            "status": "in_progress",
            "robot_id": "go2_02",
        },
        "south": {
            "spec": survey("sec_south").model_dump(mode="json"),
            "status": "assigned",
            "robot_id": "go2_02",
        },
    }
    text = narr.allocation({"go2_02": ["south"]}, tasks, {"go2_02": {"platform": "go2"}})
    assert text.endswith("will patrol the South plaza next (now: patrol the North-west walk).")
