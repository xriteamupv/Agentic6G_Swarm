"""Rules-first coordinator, careful path (2026-10-22): expected orders keep the fast path;
an order with structure the rules do not handle, or whose quick plan leaves something out,
is planned by the model in the full plan format, validated, and checked before the robots
move."""

import json
from pathlib import Path

import pytest
from a6g_common.config import load_config
from a6g_common.models import fake_model
from a6g_coordinator.decompose import needs_care
from a6g_devstack import SimStack

GOAL = '{"intent": "goal", "posture": "routine"}'
OK = '{"ok": true}'


def _cfg():
    return load_config("config", env={}, fleet_file="sim/fleet.yaml")


@pytest.mark.parametrize(
    "text",
    [
        "Patrol the campus",
        "Inspect the bike rack",
        "go2_01 return to base",
        "Weather alert",
        "All clear",
        "Escort the visitor to the bus stop",
        "Send go2_02 to hold the bike rack",
        "Patrulla el campus",
        "Guard the 9B door",
        "The summit inspects the bus stop",
    ],
)
def test_expected_orders_keep_the_fast_path(text: str) -> None:
    assert needs_care(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "Two robots patrol the east corridor",
        "Everyone except go2_01 patrol the campus",
        "Inspect the kiosk, then patrol the north-west walk",
        "Swap areas: go2_01 patrols the south plaza and go2_02 the north-west walk",
        "Check every point of interest once",
        "Send two robots to patrol the campus and the rest to guard east gate",
        "That's not what I asked",
        "Patrulla la plaza sur con dos robots",
    ],
)
def test_unexpected_orders_take_the_careful_path(text: str) -> None:
    assert needs_care(text)


async def _chat(tmp_path: Path, responses: list[str], text: str) -> tuple[dict, list]:
    stack = SimStack(_cfg(), tmp_path / "s", model=fake_model(responses))
    await stack.start()
    try:
        stack.world.pause()
        state = await stack.coordinator.chat("m1", text)
    finally:
        await stack.stop()
    return state, stack.log.read()


def _texts(state: dict) -> str:
    return " ".join(m.text for m in state["messages"])


async def test_an_unexpected_order_is_planned_carefully_and_checked(tmp_path: Path) -> None:
    plan = json.dumps(
        {
            "tasks": [
                {
                    "kind": "survey_sector",
                    "target_type": "sector",
                    "target_id": "sec_east",
                    "robots": 2,
                    "continuous": True,
                }
            ]
        }
    )
    state, log = await _chat(tmp_path, [GOAL, plan, OK], "Two robots patrol the east corridor")
    east = [
        t for t in state["tasks"].values() if t["spec"]["target"].get("sector_id") == "sec_east"
    ]
    assert len(east) == 2 and len({t["robot_id"] for t in east}) == 2
    text = _texts(state)
    assert "needs more thought" in text and "it matches" in text
    decomposed = [d for d in log if d.kind.value == "decompose"][-1]
    assert decomposed.policy["understanding"] == "model_first"  # the careful planner


async def test_an_expected_order_is_not_checked(tmp_path: Path) -> None:
    plan = (
        '{"tasks": [{"kind": "inspect_poi", "target_type": "poi", "target_id": "poi_bike_rack"}]}'
    )
    state, _ = await _chat(tmp_path, [GOAL, plan], "Inspect the bike rack")
    text = _texts(state)
    assert "needs more thought" not in text and "could not handle" not in text
    assert "it matches" not in text
    assert [t["spec"]["kind"] for t in state["tasks"].values()] == ["inspect_poi"]


async def test_a_quick_plan_with_a_gap_is_planned_again_carefully(tmp_path: Path) -> None:
    quick = (
        '{"tasks": [{"kind": "survey_sector", "target_type": "sector", "target_id": "sec_north"}]}'
    )
    careful = json.dumps(
        {
            "tasks": [
                {
                    "kind": "survey_sector",
                    "target_type": "sector",
                    "target_id": "sec_north",
                    "continuous": True,
                },
                {"kind": "inspect_poi", "target_type": "poi", "target_id": "poi_bike_rack"},
            ]
        }
    )
    state, _ = await _chat(
        tmp_path,
        [GOAL, quick, careful, OK],
        "Patrol the north-west walk and inspect the bike rack",
    )
    kinds = sorted(t["spec"]["kind"] for t in state["tasks"].values())
    assert kinds == ["inspect_poi", "survey_sector"]
    assert "could not handle this order" in _texts(state)


async def test_an_order_the_quick_reading_cannot_plan_is_thought_through(tmp_path: Path) -> None:
    """User report (2026-10-22): "You should force go2_02 to patrol because it has a lot of
    tasks queued" was answered "I don't know where 'because it has ...' is"."""
    quick = '{"tasks": []}'
    careful = json.dumps(
        {
            "reply": "There is no cafeteria on the campus map; the closest places are the info "
            "kiosk and the bus stop. Which one?"
        }
    )
    state, _ = await _chat(tmp_path, [GOAL, quick, careful], "Send a robot to the cafeteria")
    text = _texts(state)
    assert "could not handle this order" in text and "Which one?" in text


def test_a_reason_is_not_read_as_a_place() -> None:
    from a6g_coordinator.decompose import _unknown_object

    assert not _unknown_object(
        "You should force Go2_02 to patrol because it has a lot of tasks queued"
    )


async def test_work_given_by_name_releases_the_robots_post(tmp_path: Path) -> None:
    """User report (2026-10-22): go2_02 held the bus stop and its patrols queued behind
    the post for good."""
    from a6g_common.models import fake_model as fm

    stack = SimStack(
        _cfg(),
        tmp_path / "s",
        model=fm(
            [
                GOAL,
                json.dumps(
                    {
                        "tasks": [
                            {
                                "kind": "hold_post",
                                "target_type": "poi",
                                "target_id": "poi_bus_stop",
                                "robot_id": "go2_02",
                            }
                        ]
                    }
                ),
            ]
        ),
    )
    await stack.start()
    try:
        stack.world.pause()
        await stack.coordinator.chat("m1", "Send go2_02 to hold the bus stop")
        careful = json.dumps(
            {
                "tasks": [
                    {
                        "kind": "survey_sector",
                        "target_type": "sector",
                        "target_id": "sec_north",
                        "robots": 2,
                        "robot_ids": ["go2_02", "go2_01"],
                    }
                ]
            }
        )
        stack.coordinator.deps.model = fm([GOAL, careful, OK])
        state = await stack.coordinator.chat("m1", "go2_02 and go2_01 both patrol the north")
    finally:
        await stack.stop()
    post = next(t for t in state["tasks"].values() if t["spec"]["kind"] == "hold_post")
    assert post["status"] == "cancelled"
    assert "stops standing guard" in _texts(state)


def _with_deadline(seconds: float):
    cfg = _cfg()
    d = cfg.fleet.defaults
    coord = d.coordinator.model_copy(update={"careful_deadline_s": seconds})
    defaults = d.model_copy(update={"coordinator": coord})
    return cfg.model_copy(update={"fleet": cfg.fleet.model_copy(update={"defaults": defaults})})


@pytest.mark.parametrize("decision", ["approve", "edit", "reject"])
async def test_an_order_the_careful_path_cannot_decide_goes_to_the_operator(
    tmp_path: Path, decision: str
) -> None:
    """Past the deadline the operator decides (as for ending an emergency): approve the
    quick reading, rephrase the order, or drop it."""
    from langchain_core.language_models import FakeListChatModel

    stack = SimStack(_with_deadline(0.5), tmp_path / "s", model=fake_model([GOAL]))
    await stack.start()
    try:
        stack.world.pause()
        stack.coordinator.deps.model = FakeListChatModel(responses=[GOAL, "{}"], sleep=2.0)
        state = await stack.coordinator.chat("m1", "Two robots patrol the east corridor")
        asks = await stack.coordinator.interrupts("m1")
        assert asks and asks[0]["action_requests"][0]["name"] == "unclear_order"
        described = asks[0]["action_requests"][0]["description"]
        assert "quick reading" in described and "any robot: patrol" in described
        assert "couldn't work out" in _texts(state)
        assert not state["tasks"]  # nothing moves before the operator decides
        stack.coordinator.deps.model = None  # what follows needs no model
        answer = {"type": decision}
        if decision == "edit":
            answer["edited_action"] = {
                "name": "unclear_order",
                "args": {"rephrase_order": "Patrol the south plaza"},
            }
        state = await stack.coordinator.resume("m1", [answer])
        await stack.coordinator.drain()
        state = await stack.coordinator.state("m1")
    finally:
        await stack.stop()
    targets = {t["spec"]["target"].get("sector_id") for t in state["tasks"].values()}
    if decision == "approve":
        assert "sec_east" in targets  # the quick reading
    elif decision == "edit":
        assert targets == {"sec_south"}
    else:
        assert not state["tasks"] and "dropped" in _texts(state)
