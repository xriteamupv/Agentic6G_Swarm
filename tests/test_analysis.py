"""Step 2 (2026-10-22): analysis on demand ("analyse why X happened instead of Y") and when a
robot behaves unexpectedly. The model explains from the mission's records; a fix it proposes
is validated by code and applied only when the operator approves it."""

import json
from pathlib import Path

import pytest
from a6g_common.config import load_config
from a6g_common.models import fake_model
from a6g_common.schemas import EnvelopeReply
from a6g_coordinator.decompose import is_why
from a6g_devstack import SimStack

EXPLAIN = '{"intent": "explain"}'


def _cfg():
    return load_config("config", env={}, fleet_file="sim/fleet.yaml")


@pytest.mark.parametrize(
    "text",
    [
        "Analyse why go2_02 is patrolling the east corridor instead of the north-west walk",
        "Check whether the robots are doing what I asked",
        "Is that what I asked?",
        "Analiza por qué el summit no patrulla",
    ],
)
def test_analysis_questions_are_not_orders(text: str) -> None:
    assert is_why(text)


async def _patrolling(tmp_path: Path) -> SimStack:
    stack = SimStack(_cfg(), tmp_path / "s", model=None)
    await stack.start()
    stack.world.pause()
    await stack.coordinator.chat("m1", "Patrol the campus")
    return stack


def _holder(state: dict, sector: str) -> str | None:
    return next(
        t.get("robot_id")
        for t in state["tasks"].values()
        if t["spec"]["target"].get("sector_id") == sector
        and t["status"] in ("pending", "assigned", "in_progress")
    )


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_an_analysis_offers_a_fix_for_the_operator(tmp_path: Path, decision: str) -> None:
    stack = await _patrolling(tmp_path)
    try:
        state = await stack.coordinator.state("m1")
        holder = _holder(state, "sec_east")
        other = next(r for r in ("go2_01", "go2_02", "summit_01") if r != holder)
        diagnosis = json.dumps(
            {
                "answer": f"{holder} got the East corridor because it was closest.",
                "mismatch": True,
                "reassign": [{"task_id": "rou-survey_sector-sec_east", "robot_id": other}],
            }
        )
        stack.coordinator.deps.model = fake_model([EXPLAIN, diagnosis])
        state = await stack.coordinator.chat(
            "m1", f"Analyse why {holder} patrols the east corridor instead of {other}"
        )
        texts = " ".join(m.text for m in state["messages"])
        assert "because it was closest" in texts and "I can fix it" in texts
        assert _holder(state, "sec_east") == holder  # nothing moves before the operator says
        asks = await stack.coordinator.interrupts("m1")
        assert asks[0]["action_requests"][0]["name"] == "apply_fix"
        assert other in asks[0]["action_requests"][0]["args"]["proposed_changes"]
        stack.coordinator.deps.model = None
        await stack.coordinator.resume("m1", [{"type": decision}])
        await stack.coordinator.drain()
        state = await stack.coordinator.state("m1")
    finally:
        await stack.stop()
    assert _holder(state, "sec_east") == (other if decision == "approve" else holder)


async def test_an_analysis_that_finds_nothing_wrong_only_explains(tmp_path: Path) -> None:
    stack = await _patrolling(tmp_path)
    try:
        diagnosis = '{"answer": "Every sector has its patrol, as you ordered.", "mismatch": false}'
        stack.coordinator.deps.model = fake_model([EXPLAIN, diagnosis])
        state = await stack.coordinator.chat("m1", "Check whether the robots do what I asked")
        asks = await stack.coordinator.interrupts("m1")
    finally:
        await stack.stop()
    assert state["messages"][-1].text == "Every sector has its patrol, as you ordered."
    assert not asks


async def test_a_robot_refusing_all_its_work_is_analysed(tmp_path: Path) -> None:
    """go2_01 refuses every envelope (twice in a row): the model is asked what is wrong."""
    stack = SimStack(_cfg(), tmp_path / "s", model=None)
    await stack.start()
    try:
        stack.world.pause()
        links = stack.coordinator.deps.links

        class Refusing:
            def __init__(self, inner):
                self.inner = inner

            async def send_envelope(self, envelope):
                return EnvelopeReply(
                    envelope_id=envelope.envelope_id,
                    version=envelope.version,
                    robot_id="go2_01",
                    mission_id=envelope.mission_id,
                    decision="refuse",
                    stamp=envelope.valid_from,
                    refusals={t.task_id: "conflict" for t in envelope.tasks},
                    refusal_details={t.task_id: "busy" for t in envelope.tasks},
                )

            def __getattr__(self, name):
                return getattr(self.inner, name)

        stack.coordinator.deps.links = lambda rid, e: (
            Refusing(links(rid, e)) if rid == "go2_01" else links(rid, e)
        )
        diagnosis = '{"answer": "go2_01 says it is busy with work from another mission."}'
        stack.coordinator.deps.model = fake_model(
            ['{"intent": "goal", "posture": "routine"}', diagnosis, diagnosis]
        )
        await stack.coordinator.chat("m1", "Patrol the campus")
        await stack.coordinator.chat("m1", "go2_01 patrols the east corridor")
        await stack.coordinator.drain()
        state = await stack.coordinator.state("m1")
    finally:
        await stack.stop()
    texts = " ".join(m.text for m in state["messages"])
    assert "Something looked wrong" in texts and "another mission" in texts
    assert "robot:go2_01" in (state.get("anomalies") or {})


async def test_code_finds_an_order_not_followed_when_the_model_misses_it(tmp_path: Path) -> None:
    """gpt-5-nano missed a swap of two named patrols 5 times out of 6 (2026-10-22): code
    compares who does it with the robot each order named, and proposes the fix."""
    import copy

    stack = await _patrolling(tmp_path)
    try:
        state = await stack.coordinator.chat(
            "m1", "go2_02 patrols the north-west walk and go2_01 the east corridor"
        )
        tasks = copy.deepcopy(state["tasks"])
        n, e = "rou-survey_sector-sec_north", "rou-survey_sector-sec_east"
        tasks[n]["robot_id"], tasks[e]["robot_id"] = "go2_01", "go2_02"
        await stack.coordinator.graph.aupdate_state(
            {"configurable": {"thread_id": "m1"}}, {"tasks": tasks}
        )
        wrong = '{"answer": "Yes, it matches your orders.", "mismatch": false}'
        stack.coordinator.deps.model = fake_model([EXPLAIN, wrong])
        state = await stack.coordinator.chat("m1", "Is that what I asked?")
        asks = await stack.coordinator.interrupts("m1")
        stack.coordinator.deps.model = None
        await stack.coordinator.resume("m1", [{"type": "approve"}])
        await stack.coordinator.drain()
        after = await stack.coordinator.state("m1")
    finally:
        await stack.stop()
    text = state["messages"][-1].text
    assert text.startswith("Not quite:") and "Yes, it matches" not in text
    assert asks[0]["action_requests"][0]["name"] == "apply_fix"
    assert _holder(after, "sec_north") == "go2_02" and _holder(after, "sec_east") == "go2_01"
