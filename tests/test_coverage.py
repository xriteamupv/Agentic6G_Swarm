"""Coverage levels from the network twin (SPEC A4.6, stage N3): the 38.901 model and its
calibration, the radio-DT import, the serving cell, hysteresis and dwell, and the simulator."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from a6g_common import coverage as cv
from a6g_common.config import load_config
from a6g_common.netconfig import NetworkConfig

T0 = datetime(2026, 1, 1, 8, tzinfo=UTC)


def _cfg(source: str = "model"):
    return load_config("config", env={"A6G_COVERAGE_SOURCE": source}, fleet_file="sim/fleet.yaml")


def test_defaults_keep_the_shadows_and_always_grant() -> None:
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    assert cfg.network.mode == "always_grant" and cfg.network.coverage.source == "shadows"


def test_derived_rates_of_the_campus_cell() -> None:
    """SPEC A4.6.1: TS 38.306 peaks and the anchored uplink rates per level edge."""
    net = _cfg().network
    d = net.derived()
    assert d["prb"] == 51
    assert d["ul_peak_1layer_64qam_mbps"] == pytest.approx(26.3, abs=0.1)
    assert d["dl_peak_4layer_256qam_mbps"] == pytest.approx(305.6, abs=0.5)
    assert net.uplink_rate.rate_mbps(25) == pytest.approx(22.0)
    assert net.uplink_rate.rate_mbps(13) == pytest.approx(11.6, abs=0.1)
    assert net.uplink_rate.rate_mbps(5) == pytest.approx(5.4, abs=0.1)
    assert net.uplink_rate.rate_mbps(-5) == pytest.approx(1.0, abs=0.1)
    assert net.uplink_rate.rate_mbps(40) == pytest.approx(22.0)  # capped at the anchor


def test_pathloss_grows_with_distance_and_nlos() -> None:
    near = cv.pathloss_uma(50, 20, 1.5, 2.38, los=True)
    far = cv.pathloss_uma(300, 20, 1.5, 2.38, los=True)
    assert far > near
    assert cv.pathloss_uma(300, 20, 1.5, 2.38, los=False) > far


def test_calibration_and_the_map_are_deterministic() -> None:
    cfg = _cfg()
    a = cv.calibrate(cfg.campus, cfg.network)
    b = cv.calibrate(cfg.campus, cfg.network)
    assert a == b and a["rsrp_dbm"]["median"] == pytest.approx(-86.0, abs=0.05)
    m1 = cv.build_model_map(cfg.campus, cfg.network, a)
    m2 = cv.build_model_map(cfg.campus, cfg.network, b)
    assert json.dumps(m1.to_json()) == json.dumps(m2.to_json())
    assert m1.meta["source"] == "model" and "38.901" in m1.meta["label"]


def test_placeholder_shadows_stay_lost_and_are_labelled_injected() -> None:
    cfg = _cfg()
    m = cv.build_model_map(cfg.campus, cfg.network, cv.calibrate(cfg.campus, cfg.network))
    in_shadow = [s for s in m.samples if s["injected"]]
    assert in_shadow and all(s["level"] == "lost" for s in in_shadow)


def test_levels_from_sinr_and_rsrp() -> None:
    net = NetworkConfig()
    assert cv.level_of(net, 20) == "good"
    assert cv.level_of(net, 8) == "cell_edge"
    assert cv.level_of(net, 0) == "degraded"
    assert cv.level_of(net, -8) == "lost"
    assert cv.level_of(net, 20, rsrp=-120) == "lost"


def test_radio_dt_import_interpolates_and_picks_the_strongest_cell(tmp_path: Path) -> None:
    cfg = _cfg("radio_dt")
    rows = ["lat,lon,cell_id,rsrp_dbm"]
    # cell A strong everywhere, cell B weaker: A serves; values constant -> exact
    for lat in (39.4776, 39.4790, 39.4802):
        for lon in (-0.3372, -0.3350, -0.3328):
            rows.append(f"{lat},{lon},A,-80")
            rows.append(f"{lat},{lon},B,-100")
    csv = tmp_path / "rsrp.csv"
    csv.write_text("\n".join(rows) + "\n")
    from a6g_common.geo import Datum

    d = cfg.campus.datum
    datum = Datum(d["lat_deg"], d["lon_deg"], d.get("alt_m", 0.0), d.get("heading_deg", 0.0))
    m = cv.build_radio_dt_map(
        cfg.campus, cfg.network, csv, lambda lat, lon: datum.to_enu(lat, lon)[:2]
    )
    assert m.samples and {s["cell"] for s in m.samples} == {"A"}
    assert all(s["rsrp"] == pytest.approx(-80.0) for s in m.samples)
    assert all(s["sinr"] == pytest.approx(21.0) for s in m.samples if not s["injected"])


def test_nearest_sample_lookup() -> None:
    m = cv.CoverageMap(
        samples=[
            {"edge": "a", "x": 0.0, "y": 0.0, "level": "good", "sinr": 20, "cell": "c"},
            {"edge": "b", "x": 30.0, "y": 0.0, "level": "lost", "sinr": -9, "cell": "c"},
        ]
    )
    assert m.nearest(2, 1)["edge"] == "a"
    assert m.nearest(28, -1)["edge"] == "b"


def test_hysteresis_and_minimum_dwell() -> None:
    net = NetworkConfig()  # good >= 13 dB, hysteresis 3 dB, dwell 2 s

    def s(sinr: float, level: str) -> dict:
        return {"sinr": sinr, "level": level, "cell": "c"}

    t = cv.LevelTracker(net)
    assert t.update(s(20, "good"), T0) == "good"
    # just below the good threshold, inside the hysteresis band: stays good
    assert t.update(s(12, "cell_edge"), T0 + timedelta(seconds=1)) == "good"
    # well below: the change waits for the dwell
    assert t.update(s(9, "cell_edge"), T0 + timedelta(seconds=2)) == "good"
    assert t.update(s(9, "cell_edge"), T0 + timedelta(seconds=3)) == "good"
    assert t.update(s(9, "cell_edge"), T0 + timedelta(seconds=4.1)) == "cell_edge"
    # back up needs the threshold plus the margin, then the dwell
    assert t.update(s(14, "good"), T0 + timedelta(seconds=5)) == "cell_edge"
    assert t.update(s(16, "good"), T0 + timedelta(seconds=6)) == "cell_edge"
    assert t.update(s(16, "good"), T0 + timedelta(seconds=8.1)) == "good"


async def test_the_simulator_reports_level_and_cell(tmp_path: Path) -> None:
    from a6g_devstack import SimStack

    stack = SimStack(_cfg(), tmp_path / "s")
    await stack.start()
    try:
        state = stack.world.state("go2_01")
    finally:
        await stack.stop()
    assert state.link_level == "good" and state.cell_id == "n40_nodo2_cell9"
    assert state.link_quality.value == "ok"


@pytest.mark.scenario
async def test_g10_passes_with_the_coverage_map(tmp_path: Path) -> None:
    """The scenario tests also pass with the new map (shadows override: the G10 link loss
    is an injected shadow, a stress case)."""
    from a6g_devstack import scenario_g10 as G

    result = await G.run(_cfg(), tmp_path / "run", seed=0, timeout_s=300)
    assert result["error"] is None, result["error"]
    for name in ("interventions", "guidance", "link_loss", "forget", "invariants"):
        assert result["checks"][name]["ok"], (name, result["checks"][name])


def test_the_whole_pilot_area_is_covered_outdoors() -> None:
    """2026-10-26: coverage on any outdoor point of the pilot area, not only footpaths."""
    from a6g_common.geometry import point_in_polygon

    cfg = _cfg()
    m = cv.build_model_map(cfg.campus, cfg.network, cv.calibrate(cfg.campus, cfg.network))
    area = m.area_samples
    assert len(area) > 1000
    assert all(point_in_polygon((s["x"], s["y"]), cfg.campus.pilot_area) for s in area)
    assert not any(
        point_in_polygon((s["x"], s["y"]), b) for s in area[::25] for b in cfg.campus.obstacles
    )
    summary = m.summary(cfg.campus)
    assert summary["area"]["samples"] == len(area)
    assert summary["footpath_m"] == pytest.approx(len(m.path_samples) * 2.0)


def test_a_point_near_a_footpath_takes_the_footpath_level() -> None:
    m = cv.CoverageMap(
        samples=[
            {"kind": "path", "edge": "a", "x": 0.0, "y": 0.0, "level": "good", "sinr": 20},
            {"kind": "area", "edge": None, "x": 1.0, "y": 0.0, "level": "lost", "sinr": -9},
            {"kind": "area", "edge": None, "x": 40.0, "y": 0.0, "level": "cell_edge", "sinr": 7},
        ]
    )
    assert m.nearest(1.2, 0.0)["level"] == "good"  # within 3 m of the path
    assert m.nearest(39.0, 0.0)["level"] == "cell_edge"  # open ground: the area grid


def test_line_of_sight_uses_building_heights() -> None:
    """3D buildings (2026-10-26): a ray over a roof is clear, one through a wall is not."""
    box = [(10.0, -5.0), (20.0, -5.0), (20.0, 5.0), (10.0, 5.0)]
    fp = cv.Footprints([box, [(40.0, -1.0), (41.0, -1.0), (41.0, 1.0)]], [8.0, None])
    assert fp.blocked((0.0, 0.0), (30.0, 0.0), h_a=6.0, h_b=1.5)  # below the 8 m roof
    assert not fp.blocked((0.0, 0.0), (30.0, 0.0), h_a=30.0, h_b=1.5)  # well above it
    assert not fp.blocked((35.0, 0.0), (45.0, 0.0), h_a=2.0, h_b=1.5)  # a tree: no obstruction


def test_the_gnb_height_is_its_rooftop_plus_the_mast() -> None:
    site = _cfg().campus.network_sites[0]
    assert site["host_building"] is not None and site["height_source"].startswith("rooftop")
    assert site["height_m"] > 3.0
