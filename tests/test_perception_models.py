"""Both triage modes from models.yaml (on-board VLM, edge confirmation)."""

from datetime import UTC, datetime

from a6g_common.config import load_config
from a6g_common.models import fake_model
from a6g_common.schemas import Pose2D
from a6g_robot_agent.perception import (
    Candidate,
    EdgeConfirmTriage,
    OnboardVLMTriage,
    PerceptionPipeline,
    vlm_triage,
)

CANDIDATE = Candidate(
    robot_id="go2_01",
    mission_id="m1",
    pose=Pose2D(x=1.0, y=2.0),
    image_uri=None,
    poi_id="poi_kiosk",
    stamp=datetime(2026, 10, 15, tzinfo=UTC),
)
SUSPICIOUS = '{"notable": true, "label": "backpack", "novel": true, "confidence": 0.4}'


def test_vlm_triage_uses_the_models_yaml_endpoints() -> None:
    env = {
        "A6G_ONBOARD_VLM_URL": "http://dock:8080/v1",
        "A6G_ONBOARD_VLM_MODEL": "qwen2.5-vl-3b",
        "A6G_EDGE_VLM_URL": "http://edge:8000/v1",
        "A6G_EDGE_VLM_MODEL": "qwen2.5-vl-7b",
    }
    cfg = load_config("config", env=env, fleet_file="sim/fleet.yaml")
    onboard, edge = vlm_triage(cfg.models, env)
    assert isinstance(onboard, OnboardVLMTriage) and isinstance(edge, EdgeConfirmTriage)
    assert (onboard.model_id, edge.model_id) == ("qwen2.5-vl-3b", "qwen2.5-vl-7b")
    assert "dock:8080" in str(onboard.model.openai_api_base)
    assert "edge:8000" in str(edge.model.openai_api_base)


async def test_edge_confirms_an_uncertain_onboard_verdict() -> None:
    onboard = OnboardVLMTriage(fake_model([SUSPICIOUS]), "onboard")
    edge = EdgeConfirmTriage(
        fake_model(['{"notable": true, "label": "backpack", "novel": false, "confidence": 0.9}']),
        "edge",
    )
    obs = await PerceptionPipeline(onboard, edge).run(CANDIDATE)
    assert obs is not None and obs.label == "backpack"
    assert obs.evidence.model_id == "edge" and not obs.requires_confirmation


async def test_edge_rejection_drops_the_observation() -> None:
    onboard = OnboardVLMTriage(fake_model([SUSPICIOUS]), "onboard")
    edge = EdgeConfirmTriage(fake_model(['{"notable": false}']), "edge")
    assert await PerceptionPipeline(onboard, edge).run(CANDIDATE) is None


async def test_unusable_onboard_output_is_flagged_for_a_human() -> None:
    """Invalid JSON twice: the on-board fallback flags the image; without an edge model the
    observation still reaches the coordinator, marked for confirmation."""
    onboard = OnboardVLMTriage(fake_model(["not json", "still not json"]), "onboard")
    obs = await PerceptionPipeline(onboard, None).run(CANDIDATE)
    assert obs is not None and obs.label == "unclassified" and obs.requires_confirmation


async def test_onboard_vlm_benchmark_writes_a_csv(tmp_path, monkeypatch) -> None:
    """bench/onboard_vlm.py produces a CSV (fake model, no network)."""
    import csv
    import importlib.util
    import sys
    from argparse import Namespace
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "bench" / "onboard_vlm.py"
    spec = importlib.util.spec_from_file_location("onboard_vlm_bench", path)
    bench = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, bench)  # dataclasses look the module up
    spec.loader.exec_module(bench)
    monkeypatch.setattr(
        bench, "build_model", lambda args: (fake_model([SUSPICIOUS]), "fake-vlm", "test")
    )
    out = await bench.run(
        Namespace(images=None, runs=3, warmup=0, server_pid=None, out=str(tmp_path / "b.csv"))
    )
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 3 and all(r["valid"] == "True" and r["model"] == "fake-vlm" for r in rows)
    assert {"latency_s", "ttft_s", "output_tok_s", "server_rss_mb", "power_w"} <= set(rows[0])
