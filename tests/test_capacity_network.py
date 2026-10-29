"""The network stand-in that can refuse (SPEC A4.7, stage N4): admission from the cell's
capacity and the coverage where a flow streams, release, re-evaluation, and scenario G13
(the degradation loop keeps I1-I3 with refusals)."""

from pathlib import Path

import pytest
from a6g_common.config import load_config
from a6g_common.coverage import CoverageMap
from a6g_coordinator.network import CapacityNetwork


def _net(capacity: float = 22.0):
    cfg = load_config(
        "config", env={"A6G_UL_CAPACITY_MBPS": str(capacity)}, fleet_file="sim/fleet.yaml"
    )
    return cfg.network


def _map(level_at: dict[tuple[float, float], tuple[str, float]]) -> CoverageMap:
    return CoverageMap(
        samples=[
            {"edge": f"e{i}", "x": x, "y": y, "level": lvl, "sinr": sinr, "cell": "n40_nodo2_cell9"}
            for i, ((x, y), (lvl, sinr)) in enumerate(level_at.items())
        ]
    )


GOOD = (0.0, 0.0)
EDGE = (100.0, 0.0)
LOST = (200.0, 0.0)
COVERAGE = _map({GOOD: ("good", 25.0), EDGE: ("medium", 6.0), LOST: ("lost", -9.0)})


def _flow(fid: str, cls: str, xy: tuple[float, float], priority: int = 90) -> dict:
    return {
        "flow_id": fid,
        "task_id": fid,
        "robot_id": "r",
        "flow_class": cls,
        "priority": priority,
        "risk": 0,
        "x": xy[0],
        "y": xy[1],
    }


def _stub(capacity: float = 22.0, accounting_only: bool = False) -> CapacityNetwork:
    return CapacityNetwork(
        _net(capacity),
        COVERAGE,
        where=lambda f: (f["x"], f["y"]) if "x" in f else None,
        accounting_only=accounting_only,
    )


async def test_admission_respects_cell_capacity_in_priority_order() -> None:
    stub = _stub(10.0)  # two 4 Mbps video flows fit at the anchor SINR, a third does not
    answers = await stub.request_flows(
        "m",
        [
            _flow("low", "video_control", GOOD, priority=50),
            _flow("high", "video_control", GOOD, priority=95),
            _flow("mid", "video_control", GOOD, priority=90),
        ],
    )
    assert answers["high"]["state"] == "granted" and answers["mid"]["state"] == "granted"
    assert answers["low"]["state"] == "rejected" and answers["low"]["reason"].startswith("capacity")


async def test_admission_is_deterministic() -> None:
    flows = [_flow(f"f{i}", "video_control", GOOD, priority=90) for i in range(4)]
    a = await _stub(10.0).request_flows("m", flows)
    b = await _stub(10.0).request_flows("m", list(reversed(flows)))
    assert {k: v["state"] for k, v in a.items()} == {k: v["state"] for k, v in b.items()}


async def test_a_flow_is_rejected_where_coverage_is_too_poor() -> None:
    stub = _stub(100.0)
    answers = await stub.request_flows(
        "m",
        [
            _flow("lost", "telemetry_plus", LOST),
            _flow("edge_video", "video_control", EDGE),  # 4 Mbps fits 5.4 Mbps at 5 dB...
            _flow("edge_burst", "verification_burst", EDGE),  # ...8 Mbps does not
        ],
    )
    assert answers["lost"]["state"] == "rejected" and answers["lost"]["reason"].startswith(
        "coverage"
    )
    assert answers["edge_video"]["state"] == "granted"
    assert answers["edge_burst"]["state"] == "rejected"
    assert answers["edge_burst"]["reason"].startswith("coverage")


async def test_rtt_requirement_is_checked_per_level() -> None:
    net = _net(100.0)
    net.link_profiles["medium"].rtt_ms = 200.0  # above conversational's 150 ms
    stub = CapacityNetwork(net, COVERAGE, where=lambda f: (f["x"], f["y"]))
    answers = await stub.request_flows("m", [_flow("talk", "conversational", EDGE)])
    assert answers["talk"]["state"] == "rejected"


async def test_a_poor_location_costs_more_of_the_cell() -> None:
    stub = _stub()
    assert stub.cost(4.0, 25.0) == pytest.approx(4.0)
    assert stub.cost(4.0, 6.0) > 3 * stub.cost(4.0, 25.0)


async def test_release_frees_capacity() -> None:
    stub = _stub(5.0)
    first = await stub.request_flows("m", [_flow("a", "video_control", GOOD)])
    second = await stub.request_flows("m", [_flow("b", "video_control", GOOD)])
    assert first["a"]["state"] == "granted" and second["b"]["state"] == "rejected"
    await stub.release_flows("m", ["a"])
    third = await stub.request_flows("m", [_flow("b", "video_control", GOOD)])
    assert third["b"]["state"] == "granted"


async def test_best_effort_is_degraded_with_the_achieved_rate() -> None:
    stub = _stub(1.5)
    answers = await stub.request_flows(
        "m",
        [_flow(f"s{i}", "survey_uplink", GOOD) for i in range(3)],  # 3 x 1 Mbps asked
    )
    assert all(a["state"] == "degraded" for a in answers.values())
    assert sum(a["granted"]["ul_mbps"] for a in answers.values()) == pytest.approx(1.5)


async def test_reevaluation_on_a_level_change() -> None:
    stub = _stub(100.0)
    await stub.request_flows("m", [_flow("a", "video_control", GOOD)])
    assert stub.reevaluate() == []
    stub.coverage = _map({GOOD: ("medium", 6.0)})
    events = stub.reevaluate()
    assert events and events[0]["from"][1] == "good" and events[0]["to"][1] == "medium"


async def test_without_admission_the_cell_is_shared_by_fair_share() -> None:
    stub = _stub(8.0, accounting_only=True)
    answers = await stub.request_flows(
        "m", [_flow(f"v{i}", "video_control", GOOD) for i in range(3)]
    )
    assert all(a["state"] == "granted" for a in answers.values())  # no admission
    share = stub.fair_share()
    assert all(not s["usable"] for s in share.values())
    assert share["v0"]["oversubscription"] == pytest.approx(1.5)


@pytest.mark.scenario
@pytest.mark.parametrize("capacity", [4.0, 44.0])
async def test_g13_keeps_the_invariants_with_refusals(tmp_path: Path, capacity: float) -> None:
    from a6g_devstack import scenario_g13 as G

    cfg = G.g13_config("config", capacity, "reconcile")
    r = await G.run(cfg, tmp_path / "run", fleet=3, seed=0, variant="reconcile")
    assert "error" not in r, r.get("error")
    assert r["i1_violations"] == 0 and r["i2_violations"] == 0 and r["i3_violations"] == 0
    assert r["posts_held"] == r["exposed_pois"]
    # a post the network cannot serve at all is held without sensing and escalated
    assert r["posts_sensing_usable"] + r["escalations"] >= r["exposed_pois"]
    # every post either keeps video or was switched to telemetry (capacity or coverage: the
    # bike rack is in low coverage, so even 44 Mbps does not carry its video)
    assert r["posts_video_usable"] + r["switched_to_telemetry"] >= r["exposed_pois"]
    if capacity < 44:
        assert r["switched_to_telemetry"] > 0


@pytest.mark.scenario
async def test_g13_with_spare_capacity_reconcile_and_off_agree(tmp_path: Path) -> None:
    """Sanity (SPEC A4.7): when capacity exceeds demand both give the same video coverage."""
    from a6g_devstack import scenario_g13 as G

    out = {}
    for variant in ("reconcile", "reconcile_off"):
        # 200 Mbps: above the demand even with the bike rack's low-coverage video (its
        # poor spectral efficiency makes 4 Mbps cost ~55 cell units)
        cfg = G.g13_config("config", 200.0, variant)
        out[variant] = await G.run(cfg, tmp_path / variant, fleet=3, seed=1, variant=variant)
    assert out["reconcile"]["posts_video_usable"] == out["reconcile_off"]["posts_video_usable"]


@pytest.mark.scenario
async def test_g13_relocate_moves_a_post_rejected_for_coverage(tmp_path: Path) -> None:
    """SPEC A4.7 variant (run because exposed POIs lie below `good` on the radio DT map):
    a post whose video is rejected for coverage is held from a nearby viewpoint with better
    coverage, in sight of the POI, and the invariants still hold."""
    from a6g_devstack import scenario_g13 as G

    cfg = G.g13_config("config", 44.0, "reconcile_relocate")
    r = await G.run(cfg, tmp_path / "run", fleet=3, seed=0, variant="reconcile_relocate")
    assert "error" not in r, r.get("error")
    assert r["relocations"] >= 1
    assert 0 < r["relocation_distance_m"] <= 20.0 * r["relocations"]
    assert r["i1_violations"] == 0 and r["i2_violations"] == 0 and r["i3_violations"] == 0
    assert r["posts_held"] == r["exposed_pois"]


@pytest.mark.scenario
@pytest.mark.parametrize(("capacity", "video"), [(4.0, 0), (12.0, 1)])
async def test_g13_admission_only_refuses_without_adapting(
    tmp_path: Path, capacity: float, video: int
) -> None:
    """The admission-only baseline: the same admission as ``reconcile``, but a refused video
    flow is not adapted (no telemetry, no relocation, one round); the post is held without
    a stream and escalated, and I1-I3 hold. The medium post's video fits from 12 Mbps; the
    low posts' video is refused for coverage at any capacity."""
    from a6g_devstack import scenario_g13 as G

    cfg = G.g13_config("config", capacity, "admission_only")
    r = await G.run(cfg, tmp_path / "run", fleet=3, seed=0, variant="admission_only")
    assert "error" not in r, r.get("error")
    assert r["i1_violations"] == 0 and r["i2_violations"] == 0 and r["i3_violations"] == 0
    assert r["posts_held"] == r["exposed_pois"]
    assert r["posts_video_usable"] == r["posts_sensing_usable"] == video
    assert r["switched_to_telemetry"] == 0 and r["relocations"] == 0
    assert r["escalations"] == r["exposed_pois"] - video
    assert r["reconcile_iterations_max"] == 1
