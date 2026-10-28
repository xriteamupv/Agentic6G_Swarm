"""An idle robot asks for work, and takes what another robot has queued.

When a robot finishes its patrol lap and its sector's next lap goes to a nearer robot that
already patrols another sector, the first robot does not stand idle while the second holds
two sectors: the idle robot tells the coordinator, which moves to it a task the busy robot
has not started."""

import math
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
    (a robot freed from a post does not stay idle beside a bundled patrol)."""
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
        await stack.mission("Inspect the bike rack, the bus stop and the building 8K door")

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
    """The rebalancing does not push laps to an idle robot at ~32 %, which would decline
    them ("not enough battery") again and again."""
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
    """Robots at 100 % are not barred from a North-west lap they declined while charging
    ("every available robot is busy or declined")."""
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
    """A robot that finished its lap (its envelope closed) and recharged, with no envelope
    thread, still asks for work and reports charging and charged, so the coordinator does
    not keep it as charging."""
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
            # Idle, it may already be back at the dock (idle robots return to
            # the staging area) and charge there without a formal recharge: "charged" is
            # what the coordinator needs.
            return (rid, "charged") in seen

        await wait_for(charged, 60, "charged reported")
        await coord.drain()
        fleet = (await coord.state("m1"))["fleet"]
    finally:
        await stack.stop()
    assert fleet[rid]["status"] == "available"


async def test_a_guard_post_is_held_until_the_robot_gets_another_order(tmp_path: Path, cfg) -> None:
    """A post ("Send go2_02 to hold the bike rack") does not end after 600 s ("finished
    standing guard") with a patrol lap queued behind it. A post is
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
        state = await stack.coordinator.chat("m1", "Send go2_02 to the bus stop")
    finally:
        await stack.stop()
    post = next(t for t in state["tasks"].values() if t["spec"]["kind"] == "hold_post")
    assert post["status"] == "cancelled"
    assert any("stops standing guard" in m.text for m in state["messages"])


async def test_the_others_share_the_patrol_instead_of_each_doing_all_of_it(
    tmp_path: Path, cfg
) -> None:
    """ "Send one robot to guard the bus stop, the others to patrol" does not give each of
    the other two robots the whole campus (both on the same sectors at the same time), and
    the guard does not become a plain trip to the gate."""
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        state = await stack.coordinator.chat(
            "m1", "Send one robot to guard the bus stop, the others to patrol"
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
    """ "Stop holding the bus stop and send that robot to patrol also" is not answered
    "already under way": a plan can remove work as well as add it."""
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        await stack.coordinator.chat(
            "m1", "Send one robot to guard the bus stop, the others to patrol"
        )
        state = await stack.coordinator.state("m1")
        guard = next(
            t["robot_id"] for t in state["tasks"].values() if t["spec"]["kind"] == "hold_post"
        )
        state = await stack.coordinator.chat(
            "m1", "Stop holding the bus stop and send that robot to patrol also"
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


async def test_a_robot_charged_idle_at_the_dock_says_so(tmp_path: Path, cfg) -> None:
    """Robots low after their laps may charge idle in the staging area (the dock) without
    a formal recharge; the coordinator must not keep their low battery and battery refusals
    and leave them idle at 100 % while another robot holds three laps. A robot that was low
    and is charged again reports it."""
    alloc = cfg.allocator.model_copy(update={"recharge_idle_grace_s": 3600.0})
    cfg = cfg.model_copy(update={"allocator": alloc})  # no formal recharge in this test
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
        rid = next(t["robot_id"] for t in state["tasks"].values())  # in the mission

        async def done() -> bool:
            tasks = (await coord.state("m1"))["tasks"]
            return all(t["status"] == "done" for t in tasks.values())

        await wait_for(done, 60, "the inspection done")
        robot = stack.world.robot(rid)  # back at the dock, idle and low
        robot.x, robot.y = stack.cfg.campus.staging.x, stack.cfg.campus.staging.y
        stack.world.charge_pct_per_s = 0.1  # slow enough that the robot sees it low first
        robot.battery_pct = 30.0

        def charged() -> bool:
            return (rid, "charged") in seen

        await wait_for(charged, 120, "charged reported")
        await coord.drain()
        fleet = (await coord.state("m1"))["fleet"]
    finally:
        await stack.stop()
    assert (rid, "recharging") not in seen  # it never formally recharged
    assert fleet[rid]["idle_battery_pct"] >= cfg.allocator.recharge_resume_pct


async def test_allocation_brings_back_a_robot_and_lifts_a_lapsed_battery_refusal(
    tmp_path: Path, cfg
) -> None:
    """A robot does not stay "lost" for good after one failed request while it publishes
    its state, and a robot charged at the dock is not barred for good from a lap it declined
    for battery. The next allocation asks a robot lost that way
    again, and a battery refusal lapses once the robot's battery is seen higher."""
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        coord = stack.coordinator
        await coord.drain()
        state = await coord.state("m1")
        tasks, fleet = state["tasks"], state["fleet"]
        tid = sorted(tasks)[0]
        tasks[tid].update(
            status="pending",
            robot_id=None,
            refused_by=["go2_01"],
            refused_for={"go2_01": "battery"},
            refused_battery={"go2_01": 30.0},  # it is at 100 % now
        )
        fleet["summit_01"].update(status="lost", lost_by="query")
        await coord.graph.aupdate_state(coord._config("m1"), {"tasks": tasks, "fleet": fleet})
        await coord.broadcast({"type": "tick"})
        await coord.drain()
        state = await coord.state("m1")
    finally:
        await stack.stop()
    assert state["fleet"]["summit_01"]["status"] == "available"
    assert "lost_by" not in state["fleet"]["summit_01"]
    assert "go2_01" not in (state["tasks"][tid].get("refused_by") or [])
    assert state["fleet"]["go2_01"]["seen_battery_pct"] == pytest.approx(100.0, abs=1)


async def test_an_idle_robot_asks_again_once_charged(tmp_path: Path, cfg) -> None:
    """An idle robot that asks for work at 27 % gets none (no lap fits its reserve) and its
    asks back off; charged at the dock, it must not stay idle while another robot holds
    three laps. It asks again as soon as its battery has risen."""
    alloc = cfg.allocator.model_copy(update={"recharge_idle_grace_s": 3600.0})
    cfg = cfg.model_copy(update={"allocator": alloc})
    stack = SimStack(cfg, tmp_path / "s")
    asks: list[tuple[str, float]] = []
    await stack.start()
    try:
        coord = stack.coordinator
        handle = coord.handle_report

        async def spy(report):
            if report.kind.value == "envelope_status" and report.payload.get("status") == "idle":
                asks.append((report.robot_id, report.payload.get("battery_pct") or 0.0))
            return await handle(report)

        coord.handle_report = spy
        await stack.mission("Inspect the bike rack")
        rid = next(t["robot_id"] for t in (await coord.state("m1"))["tasks"].values())

        async def done() -> bool:
            tasks = (await coord.state("m1"))["tasks"]
            return all(t["status"] == "done" for t in tasks.values())

        await wait_for(done, 60, "the inspection done")
        robot = stack.world.robot(rid)
        robot.x, robot.y = stack.cfg.campus.staging.x, stack.cfg.campus.staging.y
        stack.world.charge_pct_per_s = 0.05
        robot.battery_pct = 30.0
        agent = stack.robots[rid].agent
        import time as _time

        # its last ask was just now, at 30 %, long backed off after unanswered asks
        agent._work_asked = (stack.world.clock.now(), _time.time())
        agent._asks, agent._ask_battery = 5, 30.0

        def asked_charged() -> bool:
            return any(r == rid and 35.0 <= pct < 50.0 for r, pct in asks)

        await wait_for(asked_charged, 120, "an ask after charging")
    finally:
        await stack.stop()


async def test_robots_added_before_the_mission_ask_for_and_get_work(tmp_path: Path, cfg) -> None:
    """With 5 robots, go2_03 and go2_04, added at the staging area, get laps instead of
    go2_02 holding three. Robots added before a mission join it, and an
    idle robot that was never given work still asks for it."""
    stack = SimStack(cfg, tmp_path / "s")
    asks: set[str] = set()
    await stack.start()
    try:
        for rid in ("go2_03", "go2_04"):
            await stack.add_robot(robot_id=rid)
        coord = stack.coordinator
        handle = coord.handle_report

        async def spy(report):
            if report.kind.value == "envelope_status" and report.payload.get("status") == "idle":
                asks.add(report.robot_id)
            return await handle(report)

        coord.handle_report = spy
        await coord.drain()
        await stack.mission("Patrol the campus")
        worked: set[str] = set()

        async def everyone_worked() -> bool:
            tasks = (await coord.state("m1"))["tasks"]
            worked.update(t["robot_id"] for t in tasks.values() if t.get("robot_id"))
            return {"go2_03", "go2_04"} <= worked

        await wait_for(everyone_worked, 240, "the added robots given laps")
        fleet = (await coord.state("m1"))["fleet"]
    finally:
        await stack.stop()
    assert {"go2_03", "go2_04"} <= set(fleet)


async def test_an_idle_robot_goes_back_to_the_staging_area(tmp_path: Path, cfg) -> None:
    """A robot idle for 30 s away from the staging area drives
    back and waits there for work."""
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        await stack.mission("Inspect the bike rack")
        coord = stack.coordinator
        rid = next(t["robot_id"] for t in (await coord.state("m1"))["tasks"].values())

        async def done() -> bool:
            tasks = (await coord.state("m1"))["tasks"]
            return all(t["status"] == "done" for t in tasks.values())

        await wait_for(done, 60, "the inspection done")
        robot, staging = stack.world.robot(rid), stack.cfg.campus.staging
        assert math.dist((robot.x, robot.y), (staging.x, staging.y)) > 20  # at the bike rack

        def home() -> bool:
            return math.dist((robot.x, robot.y), (staging.x, staging.y)) < 3

        await wait_for(home, 120, "back at the staging area")
    finally:
        await stack.stop()


async def test_a_robot_out_of_contact_keeps_its_queued_work(tmp_path: Path, cfg) -> None:
    """A robot out of contact for 10 s keeps its queued laps, so it does not come out of
    the lost stretch with nothing to do (only a robot presumed stuck, failed or charging
    hands them on)."""
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        coord = stack.coordinator
        await coord.drain()
        state = await coord.state("m1")
        tasks, fleet = state["tasks"], state["fleet"]
        holder = sorted(t["robot_id"] for t in tasks.values() if t.get("robot_id"))[0]
        queued = next(
            tid for tid, t in sorted(tasks.items()) if t.get("robot_id") != holder
        )  # give the holder a second, queued task
        tasks[queued].update(robot_id=holder, status="assigned")
        await coord.graph.aupdate_state(coord._config("m1"), {"tasks": tasks, "fleet": fleet})
        await coord.broadcast({"type": "robot_lost", "robot_id": holder, "reason": "test"})
        await coord.drain()
        state = await coord.state("m1")
    finally:
        await stack.stop()
    assert state["fleet"][holder]["status"] == "lost"
    assert state["tasks"][queued]["robot_id"] == holder


async def test_a_weather_alert_stops_the_laps_robots_started_themselves(
    tmp_path: Path, cfg
) -> None:
    """After a weather alert, a robot given no post that is on a lap it started itself
    while out of contact (which the coordinator does not know to pause) stops patrolling:
    back in contact, the emergency envelope drops that
    lap, and a robot left without work by the emergency goes to the staging area at once."""
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        await stack.mission("Patrol the campus")
        coord = stack.coordinator
        found: dict = {}

        async def own_lap() -> bool:  # a robot on a lap it started itself
            for rid, proc in stack.robots.items():
                agent = proc.agent
                for thread in list(agent._threads):
                    values = (await agent.graph.aget_state(agent._config(thread))).values
                    if any(
                        t.get("local") and t["status"] == "dispatched"
                        for t in (values.get("tasks") or {}).values()
                    ):
                        found["rid"] = rid
                        return True
            return False

        await wait_for(own_lap, 240, "a robot on a lap it started itself")
        rid = found["rid"]
        stack.link_loss(rid, 60)  # out of contact when the alert comes
        await stack.weather_alert()
        await coord.drain()
        state = await coord.state("m1")
        posts = {
            t["robot_id"]
            for t in state["tasks"].values()
            if t["status"] in ("assigned", "in_progress") and t["spec"]["kind"] == "hold_post"
        }
        agent = stack.robots[rid].agent

        async def stopped_patrolling() -> bool:
            task = (await agent.activity())["task"] or ""
            return "survey_sector" not in task

        await wait_for(stopped_patrolling, 180, "its own lap dropped once back in contact")
        if rid not in posts:
            staging = stack.cfg.campus.staging

            def home() -> bool:
                r = stack.world.robot(rid)
                return math.dist((r.x, r.y), (staging.x, staging.y)) < 3

            await wait_for(home, 240, "a robot without a post at the staging area")
        state = await coord.state("m1")
    finally:
        await stack.stop()
    running = [t for t in state["tasks"].values() if t["status"] in ("assigned", "in_progress")]
    assert all(t["spec"]["kind"] != "survey_sector" for t in running)


async def test_a_summit_added_at_the_staging_area_sees_its_battery(tmp_path: Path, cfg) -> None:
    """A Summit added with --robots sees its battery and charges: its manifest says the
    Summit's estimator is unreliable and only the fleet file tells the simulator otherwise,
    so a robot added outside it must not patrol without a battery reading until it is flat."""
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        await stack.add_robot(robot_id="summit_02")
        state = stack.world.state("summit_02")
        assert state.battery_pct is not None
        stack.world.robot("summit_02").battery_pct = 22.0  # below floor + margin

        def charging() -> bool:
            return stack.robots["summit_02"].agent._recharging

        await wait_for(charging, 60, "summit_02 going to charge")
    finally:
        await stack.stop()
