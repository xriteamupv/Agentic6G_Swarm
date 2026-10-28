"""Flows, the two-branch check and the emergency fast path."""

import time
from pathlib import Path

from a6g_common.config import load_config
from a6g_common.schemas import DecisionKind, PoiTarget, SectorTarget, TaskKind, TaskSpec
from a6g_coordinator import flows as F
from a6g_devstack import SimStack
from conftest import wait_for
from langchain_core.language_models import FakeListChatModel

PATROL_PLAN = (
    '{"tasks": [{"kind": "survey_sector", "target_type": "sector", "target_id": "sec_north"},'
    ' {"kind": "survey_sector", "target_type": "sector", "target_id": "sec_south"},'
    ' {"kind": "survey_sector", "target_type": "sector", "target_id": "sec_east"}]}'
)
ALERT_PLAN = '{"tasks": [{"kind": "hold_post", "target_type": "poi", "target_id": "poi_bus_stop"}]}'


def _cfg():
    return load_config("config", env={}, fleet_file="sim/fleet.yaml")


def _task(tid: str, kind: TaskKind, target, status: str, robot: str | None, **kw) -> dict:
    spec = TaskSpec(task_id=tid, kind=kind, target=target, est_duration_s=60, **kw)
    return {"spec": spec.model_dump(mode="json"), "status": status, "robot_id": robot}


def test_flow_classes_follow_the_spec_table() -> None:
    survey = TaskSpec(
        task_id="s",
        kind=TaskKind.SURVEY_SECTOR,
        target=SectorTarget(sector_id="x"),
        est_duration_s=1,
    )
    post = survey.model_copy(
        update={
            "kind": TaskKind.HOLD_POST,
            "target": PoiTarget(poi_id="p"),
            "sensing_mode": "video",
        }
    )
    assert F.flow_class(survey) == "survey_uplink"
    assert F.flow_class(post) == "video_control"
    assert F.flow_class(post.model_copy(update={"sensing_mode": "telemetry"})) == "telemetry_plus"
    assert F.flow_class(survey.model_copy(update={"kind": TaskKind.ESCORT})) == "conversational"
    assert F.flow_class(survey.model_copy(update={"kind": TaskKind.INSPECT_POI})) == "telemetry"


def test_derive_and_the_invariants() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 10, 18, tzinfo=UTC)
    tasks = {
        "s": _task("s", TaskKind.SURVEY_SECTOR, SectorTarget(sector_id="a"), "assigned", "go2_01"),
        "i": _task("i", TaskKind.INSPECT_POI, PoiTarget(poi_id="p"), "assigned", "go2_02"),
        "w": _task("w", TaskKind.SURVEY_SECTOR, SectorTarget(sector_id="b"), "pending", None),
    }
    flows: dict = {}
    asks, gone = F.derive(tasks, flows, now)
    assert [a["task_id"] for a in asks] == ["s"] and gone == []  # telemetry and unassigned: none
    assert tasks["s"]["spec"]["flow_class"] == "survey_uplink"
    assert F.check(tasks, flows) == {"i1": [], "i2": []}  # requested counts for I1
    tasks["s"]["status"] = "done"  # the task ends: its flow is released
    asks, gone = F.derive(tasks, flows, now)
    assert asks == [] and gone == ["flow-s-go2_01"]
    assert F.check(tasks, flows) == {"i1": [], "i2": []}
    tasks["w"].update(status="assigned", robot_id="go2_03")  # I1 until derived
    assert F.check(tasks, flows)["i1"] == ["w"]


def _reconciles(stack: SimStack) -> list:
    return [d for d in stack.log.read() if d.kind is DecisionKind.RECONCILE]


async def test_every_publication_is_reconciled_and_a_failure_moves_its_flows(
    tmp_path: Path,
) -> None:
    stack = SimStack(_cfg(), tmp_path / "s")
    await stack.start()
    try:
        await stack.mission("Patrol the campus")
        await stack.coordinator.drain()
        state = await stack.coordinator.state("m1")
        granted = {f["task_id"] for f in state["flows"].values() if f["state"] == "granted"}
        surveys = {t for t, e in state["tasks"].items() if e["spec"]["kind"] == "survey_sector"}
        assert surveys and surveys <= granted  # a granted survey_uplink per patrol
        victim = next(e["robot_id"] for t, e in state["tasks"].items() if t in surveys)
        await stack.fail_robot(victim)

        def moved() -> bool:
            return any(
                d.output.get("released") and d.output.get("requested") for d in _reconciles(stack)
            )

        await wait_for(moved, 60, "the failed robot's flows move with its tasks")
        await stack.weather_alert()
        await stack.coordinator.drain()
    finally:
        await stack.stop()
    for d in _reconciles(stack):  # zero inconsistencies after every publication
        if "kpi" not in d.output:
            assert d.output["tasks_without_flow"] == [] and d.output["flows_without_task"] == []
    swap = next(
        d for d in _reconciles(stack) if d.output.get("released") and d.output.get("requested")
    )
    assert all(victim in fid for fid in swap.output["released"])  # in the same invocation


async def test_the_fast_path_publishes_the_template_before_a_slow_model(tmp_path: Path) -> None:
    """Emergency reaction time: a coordinator model taking 4 s per call does not delay the emergency
    envelopes; its own plan follows through the normal path."""
    slow = FakeListChatModel(responses=[PATROL_PLAN, ALERT_PLAN], sleep=4.0)
    robots = FakeListChatModel(responses=['{"action": "accept_all"}'])  # robots answer at once
    stack = SimStack(_cfg(), tmp_path / "s", model=slow, robot_model=robots)
    await stack.start()
    try:
        await stack.mission("Patrol the campus")
        await stack.coordinator.drain()
        t0 = time.time()
        await stack.weather_alert()

        def kpi() -> list:
            return [d for d in _reconciles(stack) if d.output.get("kpi") == "UC2-KPI-11"]

        await wait_for(kpi, 30, "emergency envelopes published")
        published = time.time() - t0

        def followed() -> bool:
            return any(
                d.kind is DecisionKind.DECOMPOSE and d.policy.get("phase") == "model"
                for d in stack.log.read()
            )

        await wait_for(followed, 60, "the model's plan follows")
        await stack.coordinator.drain()
        texts = [m.text for m in (await stack.coordinator.state("m1"))["messages"]]
    finally:
        await stack.stop()
    value = kpi()[0].output["posture_change_to_publication_s"]
    assert value < 3.0 and published < 4.0, (value, published)  # before the model answered
    phases = [d.policy.get("phase") for d in stack.log.read() if d.kind is DecisionKind.DECOMPOSE]
    assert phases[-2:] == ["template_first", "model"]
    # The model's plan (one post) is within the template's: confirmed, not "nothing new".
    assert any("already covers everything" in t for t in texts)
    assert not any("Nothing new to add" in t for t in texts)
