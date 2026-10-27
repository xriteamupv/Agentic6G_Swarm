"""Work given to robots by name (2026-10-22 user reports): robots the order does not mention
keep their work; a post, inspection or trip given by name runs at once and the robot's other
routine work goes to the rest of the fleet; a battery shortfall of named work names the
robot."""

from pathlib import Path

from a6g_common.config import load_config
from a6g_common.schemas import Posture
from a6g_coordinator import agentic
from a6g_coordinator.decompose import TAKEOVER, mentions, order_robots
from a6g_coordinator.narrate import Narrator
from a6g_devstack import SimStack

ROBOTS = {"go2_01": "go2", "go2_02": "go2", "summit_01": "summit-xl"}


def _cfg():
    return load_config("config", env={}, fleet_file="sim/fleet.yaml")


def test_the_robots_an_order_names() -> None:
    swap = "Swap areas: go2_01 patrols the south plaza and go2_02 the north-west walk"
    assert order_robots(swap, ROBOTS) == {"go2_01", "go2_02"}
    assert order_robots("send the summit home and the two Go2 to patrol", ROBOTS) == set(ROBOTS)
    assert order_robots("Stop holding the bus stop and send that robot to patrol", ROBOTS) is None
    assert order_robots("Patrol the south plaza", ROBOTS) is None  # places only: no rule
    assert TAKEOVER.search("go2_01 takes over the east corridor patrol")


def _ctx(order: str) -> agentic.Context:
    cfg = _cfg()
    ctx = agentic.Context(
        campus=cfg.campus,
        posture=Posture.ROUTINE,
        robots={
            r: {"platform": p, "status": "available", "capabilities": ["survey"]}
            for r, p in ROBOTS.items()
        },
        open_tasks=[
            {
                "task_id": "south",
                "kind": "survey_sector",
                "target_id": "sec_south",
                "robot_id": "summit_01",
                "status": "in_progress",
                "posture": "routine",
            },
            {
                "task_id": "north",
                "kind": "survey_sector",
                "target_id": "sec_north",
                "robot_id": "go2_02",
                "status": "in_progress",
                "posture": "routine",
            },
            {
                "task_id": "east",
                "kind": "survey_sector",
                "target_id": "sec_east",
                "robot_id": "go2_01",
                "status": "in_progress",
                "posture": "routine",
            },
        ],
    )
    ctx.order_robots = order_robots(order, ROBOTS)
    ctx.takeover = bool(TAKEOVER.search(order))
    ctx.named_places = {ident for _, ident in mentions(order, ctx.campus)}
    return ctx


def test_a_plan_may_not_take_work_from_a_robot_the_order_does_not_mention() -> None:
    ctx = _ctx("Swap areas: go2_01 patrols the north-west walk and go2_02 the east corridor")
    taking = agentic.AgentPlan(reassign=[agentic.Reassign(task_id="south", robot_id="go2_01")])
    assert any("summit_01" in e for e in agentic.validate(taking, ctx))
    ending = agentic.AgentPlan(end=["south"], reply="ok")
    assert any("summit_01" in e for e in agentic.validate(ending, ctx))
    # the named robots' own work moves freely
    swap = agentic.AgentPlan(
        reassign=[
            agentic.Reassign(task_id="north", robot_id="go2_01"),
            agentic.Reassign(task_id="east", robot_id="go2_02"),
        ]
    )
    assert agentic.validate(swap, ctx) == []


def test_a_place_the_order_gives_to_a_named_robot_is_taken_from_its_holder() -> None:
    """User report (2026-10-22): the careful plan for the swap moved the summit's south plaza
    to go2_01, as the order says, and code rejected it twice: the operator was asked."""
    ctx = _ctx("Swap areas: go2_01 patrols the south plaza and go2_02 the north-west walk")
    plan = agentic.AgentPlan(reassign=[agentic.Reassign(task_id="south", robot_id="go2_01")])
    assert agentic.validate(plan, ctx) == []


def test_take_over_wording_may_take_work_from_its_holder() -> None:
    ctx = _ctx("go2_01 takes over the south plaza patrol")
    plan = agentic.AgentPlan(reassign=[agentic.Reassign(task_id="south", robot_id="go2_01")])
    assert agentic.validate(plan, ctx) == []


def test_the_check_may_not_move_work_of_robots_the_order_does_not_mention() -> None:
    ctx = _ctx("go2_01 patrols the north-west walk")
    result = agentic.PlanCheck(
        ok=False, reassign=[agentic.Reassign(task_id="south", robot_id="go2_01")]
    )
    assert agentic.check_errors(result, ctx)
    ctx.fresh = {"south"}  # planned by this order: placed by the allocator, it may move
    assert not agentic.check_errors(result, ctx)


async def test_a_post_given_by_name_runs_now_and_the_rest_goes_to_others(tmp_path: Path) -> None:
    stack = SimStack(_cfg(), tmp_path / "s", model=None)
    await stack.start()
    try:
        stack.world.pause()
        state = await stack.coordinator.chat("m1", "Patrol the campus")
        before = {t: e.get("robot_id") for t, e in state["tasks"].items()}
        assert "go2_02" in before.values()
        state = await stack.coordinator.chat("m1", "Send go2_02 to hold the bus stop")
    finally:
        await stack.stop()
    tasks = state["tasks"]
    post = next(e for e in tasks.values() if e["spec"]["kind"] == "hold_post")
    assert post["robot_id"] == "go2_02" and post["spec"]["priority"] > 40
    patrols = [e for e in tasks.values() if e["spec"]["kind"] == "survey_sector"]
    assert all(e.get("robot_id") != "go2_02" and e.get("pinned_to") != "go2_02" for e in patrols)
    assert "the other robots take over" in " ".join(m.text for m in state["messages"])


def test_a_battery_shortfall_of_named_work_names_the_robot() -> None:
    cfg = _cfg()
    narr = Narrator(cfg.campus)
    tasks = {
        "t": {
            "spec": {
                "task_id": "t",
                "kind": "survey_sector",
                "target": {"type": "sector", "sector_id": "sec_south"},
                "priority": 40,
                "est_duration_s": 60,
            },
            "status": "pending",
            "pinned_to": "go2_02",
        }
    }
    text = narr.unallocated(
        {"t": "battery"},
        tasks,
        details={"t": "battery would end at 13.2% (< floor 15% + margin 5%)"},
        fleet={"go2_02": {"platform": "go2"}},
    )
    assert "go2_02" in text and "13 %" in text and "once it has recharged" in text


async def test_ending_work_and_giving_it_to_a_robot_by_name(tmp_path: Path) -> None:
    """User report (2026-10-22): "summit takes over the south plaza patrol" was planned as
    end the open patrol + a new one for the summit, and the coordinator crashed."""
    import json

    from a6g_common.models import fake_model

    stack = SimStack(_cfg(), tmp_path / "s", model=None)
    await stack.start()
    try:
        stack.world.pause()
        state = await stack.coordinator.chat("m1", "Patrol the campus")
        south = next(t for t in state["tasks"] if "sec_south" in t)
        other = next(r for r in ROBOTS if r != state["tasks"][south]["robot_id"])
        plan = json.dumps(
            {
                "tasks": [
                    {
                        "kind": "survey_sector",
                        "target_type": "sector",
                        "target_id": "sec_south",
                        "robot_id": other,
                    }
                ],
                "end": [south],
            }
        )
        goal = '{"intent": "goal", "posture": "routine"}'
        stack.coordinator.deps.model = fake_model([goal, plan, plan, '{"ok": true}'])
        state = await stack.coordinator.chat("m1", f"{other} takes over the south plaza patrol")
    finally:
        await stack.stop()
    live = [
        e
        for e in state["tasks"].values()
        if "sec_south" in json.dumps(e["spec"]["target"])
        and e["status"] in ("pending", "assigned", "in_progress")
    ]
    assert [e["robot_id"] for e in live] == [other]


def test_only_a_correction_carries_the_previous_order() -> None:
    from a6g_coordinator.decompose import is_correction

    assert is_correction("That's not what I asked")
    assert not is_correction("send summit to a random place and the others to patrol that area")


async def test_the_check_may_not_move_work_the_order_gave_by_name(tmp_path: Path) -> None:
    """User report (2026-10-22): "send summit to a random place and the others to patrol
    that area"; the check moved the summit's trip to the 8K door to go2_02."""
    import json

    from a6g_common.models import fake_model

    plan = json.dumps(
        {
            "tasks": [
                {
                    "kind": "go_to",
                    "target_type": "poi",
                    "target_id": "poi_kiosk",
                    "robot_ids": ["summit_01"],
                },
                {
                    "kind": "survey_sector",
                    "target_type": "sector",
                    "target_id": "sec_north",
                    "robots": 2,
                    "exclude_robot_ids": ["summit_01"],
                },
            ]
        }
    )
    goal = '{"intent": "goal", "posture": "routine"}'
    stack = SimStack(_cfg(), tmp_path / "s", model=None)
    await stack.start()
    try:
        stack.world.pause()
        await stack.coordinator.chat("m1", "Patrol the campus")
        check = lambda tid: json.dumps(  # noqa: E731
            {"ok": False, "issue": "x", "reassign": [{"task_id": tid, "robot_id": "go2_02"}]}
        )
        order = "send summit to a random place and the others to patrol that area"
        stack.coordinator.deps.model = fake_model(
            [goal, plan, check("rou-go_to-poi_kiosk"), check("rou-go_to-poi_kiosk")]
        )
        state = await stack.coordinator.chat("m1", order)
    finally:
        await stack.stop()
    trip = next(e for e in state["tasks"].values() if e["spec"]["kind"] == "go_to")
    assert trip["robot_id"] == "summit_01" and trip["named_by"] == "summit_01"
    # and its patrol went to the others while it makes the trip
    others = [e for t, e in state["tasks"].items() if t != "rou-go_to-poi_kiosk"]
    assert all(e.get("robot_id") != "summit_01" for e in others if e["status"] != "cancelled")


def test_a_new_task_repeating_a_reassigned_one_is_dropped_by_code() -> None:
    """A slip seen with gpt-5-nano (2026-10-22): the swap planned sec_south again AND
    reassigned the open one; rejected, the retry took the plan past the 30 s deadline."""
    ctx = _ctx("Swap areas: go2_01 patrols the south plaza and go2_02 the north-west walk")
    plan = agentic.AgentPlan(
        tasks=[
            agentic.AgentTask(
                kind="survey_sector", target_type="sector", target_id="sec_south", continuous=True
            )
        ],
        reassign=[agentic.Reassign(task_id="south", robot_id="go2_01")],
    )
    tidy = agentic.tidy(plan, ctx)
    assert tidy.tasks == [] and agentic.validate(tidy, ctx) == []


async def test_all_go2_to_base_sends_only_the_go2_and_ends_their_posts(tmp_path) -> None:
    """User report (2026-10-27, 8 robots): "Send all the Go2 to hold the Building 9B door",
    then "Send all Go2 to base": the Summits were sent home too ("all Go2" was not read as
    the Go2 group), and the five posts were left for robots that could not take them."""
    from a6g_common.config import load_config
    from a6g_devstack import SimStack
    from a6g_devstack.stack import EXTRA_ROBOTS

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        for rid in EXTRA_ROBOTS:
            await stack.add_robot(robot_id=rid)
        stack.world.pause()
        await stack.mission("Patrol the campus")
        coord = stack.coordinator
        await coord.chat("m1", "Send all the Go2 to hold the Building 9B door")
        await coord.chat("m1", "Send all Go2 to base")
        await coord.drain()
        state = await coord.state("m1")
    finally:
        await stack.stop()
    open_tasks = [
        t for t in state["tasks"].values() if t["status"] in ("pending", "assigned", "in_progress")
    ]
    homes = {
        t.get("pinned_to") or t.get("robot_id")
        for t in state["tasks"].values()  # one may be home already
        if t["spec"]["kind"] == "return_home"
    }
    go2 = {r for r in state["fleet"] if r.startswith("go2")}
    assert homes == go2  # the Go2s, not the Summits
    assert not [t for t in open_tasks if t["spec"]["kind"] == "hold_post"]  # posts ended
    patrolling = {t.get("robot_id") for t in open_tasks if t["spec"]["kind"] == "survey_sector"}
    assert patrolling and patrolling <= {r for r in state["fleet"] if r.startswith("summit")}
