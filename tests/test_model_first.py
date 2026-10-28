"""``coordinator.understanding: model_first`` and the robot recharge advisor.

The model plans every order in a richer plan format; code validates it, the
allocator honours it, the model checks the assignment against the order, and
the rules plan only when the model's plan is rejected twice. Fake models: deterministic."""

import json
from pathlib import Path

import pytest
from a6g_common.config import load_config
from a6g_common.models import fake_model
from a6g_common.schemas import DecisionKind, Posture
from a6g_coordinator import agentic
from a6g_devstack import SimStack
from a6g_devstack.serve import with_agent
from conftest import wait_for

GOAL = '{"intent": "goal", "posture": "routine"}'
OK = '{"ok": true}'


def _cfg(**coord):
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    d = cfg.fleet.defaults
    c = d.coordinator.model_copy(update={"understanding": "model_first", **coord})
    defaults = d.model_copy(update={"coordinator": c})
    return cfg.model_copy(update={"fleet": cfg.fleet.model_copy(update={"defaults": defaults})})


def _ctx(cfg, open_tasks=()):
    robots = {
        rid: {
            "platform": m.platform.value,
            "status": "available",
            "capabilities": [c.value for c in m.capabilities],
        }
        for rid, m in cfg.manifests.items()
        if rid in ("go2_01", "go2_02", "summit_01")
    }
    return agentic.Context(cfg.campus, Posture.ROUTINE, robots, list(open_tasks))


def _plan(**kw) -> str:
    return json.dumps({"tasks": [], "end": [], "reassign": [], **kw})


# --------------------------------------------------------------------------- the plan


def test_the_validator_rejects_what_cannot_be_done() -> None:
    cfg = _cfg()
    ctx = _ctx(
        cfg,
        [
            {
                "task_id": "eme-post",
                "kind": "hold_post",
                "target_id": "poi_bus_stop",
                "robot_id": "go2_01",
                "status": "in_progress",
                "posture": "emergency",
            }
        ],
    )
    bad = agentic.AgentPlan.model_validate(
        {
            "tasks": [
                {"kind": "survey_sector", "target_type": "sector", "target_id": "sec_gym"},
                {"kind": "inspect_poi", "target_type": "sector", "target_id": "sec_east"},
                {
                    "kind": "survey_sector",
                    "target_type": "sector",
                    "target_id": "sec_east",
                    "robot_ids": ["go2_09"],
                },
                {
                    "kind": "survey_sector",
                    "target_type": "sector",
                    "target_id": "sec_south",
                    "robots": 5,
                },
                {"kind": "go_to", "target_type": "poi", "target_id": "poi_kiosk", "after": [5]},
            ],
            "end": ["eme-post", "nope"],
        }
    )
    errors = " | ".join(agentic.validate(bad, ctx))
    for what in (
        "unknown sector",
        "inspect_poi takes",
        "unknown robot",
        "5 robots asked",
        "after=5",
        "emergency work",
        "'nope' is not an open task",
    ):
        assert what in errors, what
    empty = agentic.AgentPlan()
    assert agentic.validate(empty, ctx) == ["an empty plan needs a reply for the operator"]


def test_copies_groups_order_and_exclusions_become_allocator_constraints() -> None:
    cfg = _cfg()
    ctx = _ctx(cfg)
    plan = agentic.AgentPlan.model_validate(
        {
            "tasks": [
                {
                    "kind": "survey_sector",
                    "target_type": "sector",
                    "target_id": "sec_east",
                    "robots": 2,
                    "continuous": True,
                },
                {
                    "kind": "inspect_poi",
                    "target_type": "poi",
                    "target_id": "poi_kiosk",
                    "exclude_robot_ids": ["summit_01"],
                },
                {
                    "kind": "survey_sector",
                    "target_type": "sector",
                    "target_id": "sec_north",
                    "after": [1],
                },
                {
                    "kind": "go_to",
                    "target_type": "place",
                    "target_id": "lrp_staging",
                    "robot_ids": ["go2_02"],
                },
            ]
        }
    )
    assert agentic.validate(plan, ctx) == []
    out = agentic.to_tasks(plan, ctx, set())
    east = [t for t in out if t.spec.target.model_dump().get("sector_id") == "sec_east"]
    assert len(east) == 2 and len({t.bundle for t in east}) == 2  # two robots
    assert all(t.repeat for t in east)
    kiosk = next(t for t in out if t.spec.kind.value == "inspect_poi")
    north = next(t for t in out if t.spec.target.model_dump().get("sector_id") == "sec_north")
    assert kiosk.excluded == ["summit_01"]
    assert north.spec.depends_on == [kiosk.spec.task_id] and north.bundle == kiosk.bundle
    gate = next(t for t in out if t.spec.kind.value == "go_to")
    assert gate.pinned_to == "go2_02" and gate.bundle is None


# --------------------------------------------------------------------------- on the stack


async def _stack(tmp_path: Path, responses: list[str], cfg=None) -> SimStack:
    stack = SimStack(cfg or _cfg(), tmp_path / "s", model=fake_model(responses))
    await stack.start()
    stack.world.pause()
    return stack


def _set_model(stack: SimStack, responses: list[str]) -> None:
    stack.coordinator.deps.model = fake_model(responses)


GUARD_AND_PATROL = _plan(
    tasks=[
        {"kind": "hold_post", "target_type": "poi", "target_id": "poi_bus_stop"},
        {
            "kind": "survey_sector",
            "target_type": "sector",
            "target_id": "sec_north",
            "group": "west",
            "continuous": True,
        },
        {
            "kind": "survey_sector",
            "target_type": "sector",
            "target_id": "sec_south",
            "group": "west",
            "continuous": True,
        },
        {
            "kind": "survey_sector",
            "target_type": "sector",
            "target_id": "sec_east",
            "group": "east",
            "continuous": True,
        },
    ]
)


async def test_one_guards_and_the_others_share_the_patrol(tmp_path: Path) -> None:
    stack = await _stack(tmp_path, [GOAL, GUARD_AND_PATROL, OK])
    try:
        state = await stack.coordinator.chat(
            "m1", "Send one robot to guard the bus stop, the others to patrol"
        )
    finally:
        await stack.stop()
    tasks = state["tasks"].values()
    post = next(t for t in tasks if t["spec"]["kind"] == "hold_post")
    laps = [t for t in tasks if t["spec"]["kind"] == "survey_sector"]
    assert sorted(t["spec"]["target"]["sector_id"] for t in laps) == [
        "sec_east",
        "sec_north",
        "sec_south",
    ]
    by = {t["spec"]["target"]["sector_id"]: t["robot_id"] for t in laps}
    assert by["sec_north"] == by["sec_south"] != by["sec_east"]
    assert post["robot_id"] not in by.values()
    decomposed = [d for d in stack.log.read() if d.kind is DecisionKind.DECOMPOSE]
    assert decomposed[-1].policy["understanding"] == "model_first"


async def test_the_model_ends_a_post_and_gives_that_robot_a_sector(tmp_path: Path) -> None:
    """ "Stop holding the bus stop and send that robot to patrol also"."""
    stack = await _stack(tmp_path, [GOAL, GUARD_AND_PATROL, OK])
    try:
        await stack.coordinator.chat("m1", "Guard the gate, the others patrol")
        await stack.coordinator.drain()  # its assignment check runs after it
        state = await stack.coordinator.state("m1")
        post_id, post = next(
            (k, t) for k, t in state["tasks"].items() if t["spec"]["kind"] == "hold_post"
        )
        east = next(
            k
            for k, t in state["tasks"].items()
            if t["spec"]["target"].get("sector_id") == "sec_east"
        )
        guard = post["robot_id"]
        _set_model(
            stack,
            [GOAL, _plan(end=[post_id], reassign=[{"task_id": east, "robot_id": guard}]), OK],
        )
        state = await stack.coordinator.chat(
            "m1", "Stop holding the bus stop and send that robot to patrol also"
        )
    finally:
        await stack.stop()
    assert state["tasks"][post_id]["status"] == "cancelled"
    assert state["tasks"][east]["robot_id"] == guard
    assert not any("already under way" in m.text for m in state["messages"][-4:])


async def test_the_check_reassigns_what_does_not_match_the_order(tmp_path: Path) -> None:
    """A structured plan (two robots on the north-west walk); the check moves the south
    plaza to go2_01, the robot the order named, and the allocator places it."""
    plan = _plan(
        tasks=[
            {
                "kind": "survey_sector",
                "target_type": "sector",
                "target_id": "sec_north",
                "robots": 2,
            },
            {"kind": "survey_sector", "target_type": "sector", "target_id": "sec_south"},
        ]
    )
    check = json.dumps(
        {
            "ok": False,
            "issue": "go2_01 was to patrol the south plaza",
            "reassign": [{"task_id": "rou-survey_sector-sec_south", "robot_id": "go2_01"}],
        }
    )
    stack = await _stack(tmp_path, [GOAL, plan, check])
    try:
        await stack.coordinator.chat(
            "m1", "Two robots on the north-west walk, and go2_01 patrols the south plaza"
        )
        await stack.coordinator.drain()  # the check is its own run, after publication
        state = await stack.coordinator.state("m1")
    finally:
        await stack.stop()
    assert state["tasks"]["rou-survey_sector-sec_south"]["robot_id"] == "go2_01"
    checks = [d for d in stack.log.read() if d.policy.get("rule", "").startswith("plan_check")]
    assert checks and "go2_01" in json.dumps(checks[-1].output)


async def test_the_check_may_not_split_a_group(tmp_path: Path) -> None:
    plan = _plan(
        tasks=[
            {
                "kind": "survey_sector",
                "target_type": "sector",
                "target_id": "sec_north",
                "group": "a",
            },
            {
                "kind": "survey_sector",
                "target_type": "sector",
                "target_id": "sec_south",
                "group": "a",
            },
        ]
    )
    check = json.dumps(
        {
            "ok": False,
            "issue": "x",
            "reassign": [{"task_id": "rou-survey_sector-sec_south", "robot_id": "summit_01"}],
        }
    )
    stack = await _stack(tmp_path, [GOAL, plan, check])
    try:
        await stack.coordinator.chat("m1", "One robot patrols the north and the south")
        await stack.coordinator.drain()
        state = await stack.coordinator.state("m1")
    finally:
        await stack.stop()
    t = state["tasks"]
    assert (
        t["rou-survey_sector-sec_south"]["robot_id"] == t["rou-survey_sector-sec_north"]["robot_id"]
    )


async def test_the_check_adds_work_the_plan_missed_after_the_robots_start(
    tmp_path: Path,
) -> None:
    """ "Send two robots to patrol the campus and the rest to guard the bus stop" planned
    with two sectors (no East corridor): the check runs after the robots start, so it does
    not hold them, and adds the missing patrol."""
    plan = _plan(
        tasks=[
            {
                "kind": "survey_sector",
                "target_type": "sector",
                "target_id": "sec_north",
                "continuous": True,
            },
            {
                "kind": "survey_sector",
                "target_type": "sector",
                "target_id": "sec_south",
                "continuous": True,
            },
            {"kind": "hold_post", "target_type": "poi", "target_id": "poi_bus_stop"},
        ]
    )
    check = json.dumps(
        {
            "ok": False,
            "issue": "the campus has three sectors; the East corridor was left out",
            "add": [
                {
                    "kind": "survey_sector",
                    "target_type": "sector",
                    "target_id": "sec_east",
                    "continuous": True,
                }
            ],
        }
    )
    stack = await _stack(tmp_path, [GOAL, plan, check])
    try:
        first = await stack.coordinator.chat(
            "m1", "Send two robots to patrol the campus and the rest to guard the bus stop"
        )
        published = {t["spec"]["target"].get("sector_id") for t in first["tasks"].values()}
        await stack.coordinator.drain()
        state = await stack.coordinator.state("m1")
    finally:
        await stack.stop()
    assert "sec_east" not in published  # the first answer went out without waiting
    east = [
        t for t in state["tasks"].values() if t["spec"]["target"].get("sector_id") == "sec_east"
    ]
    assert len(east) == 1 and east[0]["robot_id"] and east[0]["repeat"]
    assert any("East corridor" in m.text for m in state["messages"])


async def test_a_plan_rejected_twice_falls_back_to_the_rules(tmp_path: Path) -> None:
    unknown = _plan(
        tasks=[{"kind": "survey_sector", "target_type": "sector", "target_id": "sec_gym"}]
    )
    stack = await _stack(tmp_path, [GOAL, unknown, unknown, "no", "no"])
    try:
        state = await stack.coordinator.chat("m1", "Patrol the campus")
    finally:
        await stack.stop()
    sectors = sorted(
        t["spec"]["target"]["sector_id"]
        for t in state["tasks"].values()
        if t["spec"]["kind"] == "survey_sector"
    )
    assert sectors == ["sec_east", "sec_north", "sec_south"]  # the rule-based plan
    fallback = [d for d in stack.log.read() if d.policy.get("rule") == "fallback_rejected"]
    assert fallback and "sec_gym" in json.dumps(fallback[0].output)


async def test_a_reply_instead_of_a_guess(tmp_path: Path) -> None:
    reply = _plan(reply="I don't know a cafeteria on the campus map. Which place do you mean?")
    stack = await _stack(tmp_path, [GOAL, reply])
    try:
        state = await stack.coordinator.chat("m1", "Send a robot to the cafeteria")
    finally:
        await stack.stop()
    assert not state["tasks"]
    assert "cafeteria" in state["messages"][-1].text


# --------------------------------------------------------------------------- robots


@pytest.mark.parametrize("advisor", ["rule"])
async def test_recharges_are_staggered(tmp_path: Path, advisor: str) -> None:
    """Two Go2s cross the early-recharge threshold together: only the lower charges early;
    the other waits while it charges."""
    cfg = with_agent(
        load_config("config", env={}, fleet_file="sim/fleet.yaml"),
        recharge_advisor=advisor,
        early_recharge_pct=50.0,
    )
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        await stack.mission("Patrol the campus")
        stack.world.robot("go2_01").battery_pct = 45.0
        stack.world.robot("go2_02").battery_pct = 47.0

        def advised() -> list:
            return [d for d in stack.log.read() if (d.output or {}).get("step") == "early_recharge"]

        await wait_for(lambda: len({d.actor for d in advised()}) >= 2, 60, "both advised")
        first = {d.actor.split(".")[0]: d.output["charge_now"] for d in advised()}
    finally:
        await stack.stop()
    assert first["go2_01"] is True and first["go2_02"] is False
