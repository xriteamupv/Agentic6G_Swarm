"""Scenario G10 (SPEC A3.11, stage E6): the D2.1 consolidated story in one run, with
seeded trigger times. Every check must hold on every seed."""

import json
from pathlib import Path

import pytest
from a6g_common.config import load_config
from a6g_devstack import scenario_g10 as G

pytestmark = pytest.mark.scenario


@pytest.mark.parametrize("seed", [0, 1])
async def test_g10_the_consolidated_story(tmp_path: Path, seed: int) -> None:
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    result = await G.run(cfg, tmp_path / "run", seed=seed, timeout_s=300)
    assert result["error"] is None, result["error"]
    events = [e["event"] for e in result["timeline"]]
    assert events == [
        "survey_started",
        "request",
        "escort_departed",
        "weather_alert",
        "escort_ended",
        "all_clear",
        "survey_resumed",
    ]
    checks = result["checks"]
    for name in ("interventions", "guidance", "link_loss", "forget", "invariants"):
        assert checks[name]["ok"], (name, checks[name])
    assert checks["decision_chain"]["ok"] and checks["decision_chain"]["records"] > 0
    assert checks["separation"]["samples"] > 0
    # The run directory holds what `a6g kpi report --run` reads.
    run = tmp_path / "run"
    for name in ("run.json", "decisions.jsonl", "comms.jsonl", "telemetry.jsonl"):
        assert (run / name).exists(), name
    assert json.loads((run / "run.json").read_text())["seed"] == seed
    if seed:
        return
    # `a6g kpi report` and the DS09 export of the run.
    from a6g_devstack import dataset, kpi_report

    rep = kpi_report.report(run, cfg, results=tmp_path / "no-bench-results")
    kpis = {r["kpi"]: r for r in rep["kpis"]}
    assert set(kpis) == {f"UC2-KPI-{i}" for i in range(1, 16)}
    # Without bench results the bench KPIs say how to get them.
    assert kpis["UC2-KPI-2"]["value"] == "n/a" and "bench baselines" in kpis["UC2-KPI-2"]["note"]
    assert kpis["UC2-KPI-9"]["value"] == "n/a"
    assert kpis["UC2-KPI-14"]["value"]["disruptions"] >= 1  # the link loss in the shadow
    assert rep["run_metrics"]["manual_interventions"] == 0
    out = dataset.export(run, tmp_path / "ds09", cfg)
    assert "kpi.json" in out["files"]
    exported = "".join(p.read_text() for p in (tmp_path / "ds09").glob("*.json"))
    assert G.CANARY not in exported


def test_trigger_times_depend_only_on_the_seed() -> None:
    assert G.triggers(3) == G.triggers(3) and G.triggers(3) != G.triggers(4)
