"""SPEC Part A2, stage C1 acceptance with the real (cloud) models: ``pytest --model``.

Each robot's cognitive core runs on the ``onboard_vlm`` role of ``config/models.yaml``
(``fidelity: functional``: the cloud model stands in for the on-board one until stage C6).
The coordinator keeps its deterministic templates so the figures isolate the robots.
Reported per scenario: validator acceptance rate, fallback count, tokens, inference times.
"""

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from a6g_common.config import ModelRole, load_config
from a6g_common.models import chat_model
from a6g_devstack import SimStack
from a6g_observability.comms import load_records, model_summary
from conftest import wait_for

pytestmark = [pytest.mark.model, pytest.mark.scenario]
RESULTS = Path(__file__).resolve().parents[1] / "bench" / "results"


async def _run(tmp_path: Path, goal: str, posture: str, timeout_s: float) -> dict:
    cfg = load_config("config", fleet_file="sim/fleet.yaml")  # keys from the environment / .env
    stack = SimStack(cfg, tmp_path / "s", robot_model=chat_model(ModelRole.ONBOARD_VLM, cfg.models))
    await stack.start()
    try:
        await stack.mission(goal, posture=posture)

        async def completed() -> bool:
            return (await stack.coordinator.state("m1")).get("status") == "completed"

        await wait_for(completed, timeout_s, f"{goal!r} completes")
        state = await stack.coordinator.state("m1")
    finally:
        await stack.stop()
    policies = Counter(
        d.policy.get("model_policy")
        for d in stack.log.read()
        if d.actor.endswith(".agent") and d.policy.get("model_policy")
    )
    calls = model_summary(load_records([stack.workdir / "comms.jsonl"]))
    proposals = sum(policies.values()) - policies.get(None, 0)
    metrics = {
        "scenario": goal,
        "model": cfg.models.roles[ModelRole.ONBOARD_VLM].model,
        "fidelity": "functional",
        "decisions_by_policy": dict(policies),
        "validator_acceptance_rate": round(policies.get("model", 0) / proposals, 3)
        if proposals
        else None,
        "fallbacks": policies.get("fallback_no_model", 0) + policies.get("fallback_rejected", 0),
        "model_calls": calls,
        "tasks": {t: e["status"] for t, e in state["tasks"].items()},
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    name = goal.lower().replace(" ", "_")
    out = RESULTS / f"c1_cognition_{name}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics, indent=2))
    return metrics


async def test_routine_survey_completes_with_the_robots_models(tmp_path: Path) -> None:
    m = await _run(tmp_path, "Survey the campus", "routine", 240)
    assert set(m["tasks"].values()) == {"done"}
    deliberation = next(c for c in m["model_calls"] if c["function"] == "deliberation")
    assert deliberation["calls"] >= 3  # one per robot at least


async def test_weather_alert_completes_with_the_robots_models(tmp_path: Path) -> None:
    m = await _run(tmp_path, "Weather alert", "emergency", 300)
    assert set(m["tasks"].values()) == {"done"}
    assert any(c["function"] == "deliberation" for c in m["model_calls"])


GUIDANCE = [  # (utterance, destination, profile): SPEC A3.4 in the three languages
    ("¿Me llevas a la parada del bus? Voy en silla de ruedas.", "poi_bus_stop", "step_free"),
    (
        "Necesito ir al punto de información, me cansan mucho las cuestas.",
        "poi_kiosk",
        "avoid_slopes",
    ),
    (
        "Porta'm a la marquesina del 9B, per favor. No puc pujar escales.",
        "poi_shelter_9b",
        "step_free",
    ),
    ("On és l'aparcament de bicicletes?", "poi_bike_rack", "default"),
    (
        "Could you take me to the building 9B door? I'm on crutches.",
        "poi_library_door",
        "step_free",
    ),
    ("Where is the south arcade?", "poi_shelter_south", "default"),
]


async def test_guidance_requests_in_three_languages_with_the_robots_model(tmp_path: Path) -> None:
    """E3 with the cloud model: the conversation proposal (objective, constraint, profile)
    is accepted by the validators and code resolves the right place and profile."""
    cfg = load_config("config", fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s", robot_model=chat_model(ModelRole.ONBOARD_VLM, cfg.models))
    await stack.start()
    rows = []
    try:
        stack.world.pause()
        agent = stack.robots["go2_01"].agent
        for text, dest, profile in GUIDANCE:
            answer = await agent.say(text)
            iid = next(iter(agent.deps.local))
            content = agent.deps.local[iid]["content"]
            rows.append(
                {
                    "lang": content["lang"],
                    "destination": content["destination"]["id"],
                    "profile": content["profile"],
                    "expected": [dest, profile],
                    "disclosed": "robot" in answer.split(".")[0],
                }
            )
            await agent.say("no")  # declined: the interaction is forgotten
    finally:
        await stack.stop()
    decisions = [
        d
        for d in stack.log.read()
        if d.actor == "go2_01.agent" and d.output.get("stage") == "confirming"
    ]
    policies = Counter(d.policy.get("model_policy") for d in decisions)
    calls = model_summary(load_records([stack.workdir / "comms.jsonl"]))
    metrics = {
        "scenario": "guidance requests es/ca/en",
        "model": cfg.models.roles[ModelRole.ONBOARD_VLM].model,
        "rows": rows,
        "decisions_by_policy": dict(policies),
        "validator_acceptance_rate": round(policies.get("model", 0) / max(1, len(decisions)), 3),
        "model_calls": calls,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"e3_guidance_model_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(metrics, indent=2, ensure_ascii=False))
    print(json.dumps(metrics, indent=2, ensure_ascii=False))
    assert [[r["destination"], r["profile"]] for r in rows] == [r["expected"] for r in rows]
    assert all(r["disclosed"] for r in rows)
    assert [r["lang"] for r in rows] == ["es", "es", "ca", "ca", "en", "en"]
