"""``world.scale`` (A6G_WORLD_SCALE): a smaller campus driven by real-size robots."""

import pytest
from a6g_common.campus import world_name
from a6g_common.config import load_config


@pytest.fixture(scope="module")
def full():
    return load_config("config", env={}).campus


@pytest.fixture(scope="module")
def small():
    return load_config("config", env={"A6G_WORLD_SCALE": "0.1"}).campus


def test_scale_one_is_unchanged(full) -> None:
    assert full.scale == 1.0 and full.world_name == "upv_vera_cpi"
    assert world_name("upv_vera_cpi", 0.25) == "upv_vera_cpi_s0p25"


def test_geometry_scales_uniformly(full, small) -> None:
    assert small.world_name == "upv_vera_cpi_s0p1"
    assert small.map.tile_uri.endswith("maps/upv_vera_cpi_s0p1.yaml")
    for a, b in zip(full.pilot_area, small.pilot_area, strict=True):
        assert (b[0], b[1]) == pytest.approx((a[0] * 0.1, a[1] * 0.1), abs=0.002)
    assert len(small.obstacles) == len(full.obstacles)
    b0, s0 = full.obstacles[0], small.obstacles[0]
    assert s0[0] == pytest.approx((b0[0][0] * 0.1, b0[0][1] * 0.1), abs=0.002)
    # Points only move where the shrunk campus leaves a real-size robot no room.
    kiosk_f, kiosk_s = full.poi("poi_kiosk").pose, small.poi("poi_kiosk").pose
    assert (kiosk_s.x, kiosk_s.y) == pytest.approx((kiosk_f.x * 0.1, kiosk_f.y * 0.1), abs=0.002)
    loop_f, loop_s = full.calibration_loops[0]["polygon"], small.calibration_loops[0]["polygon"]
    assert loop_s[1] == pytest.approx((loop_f[1][0] * 0.1, loop_f[1][1] * 0.1), abs=0.002)


def test_every_target_is_reachable_with_room_at_0_1(small) -> None:
    from a6g_sim.planner import footpath_planner_for

    assert small.footpaths_only  # the robots walk the (shrunk) footpaths
    planner = footpath_planner_for(small.route_graph, small.walk_corridor_m)
    start = (small.staging.x, small.staging.y)
    targets = [(p.pose.x, p.pose.y) for p in small.pois]
    targets += [(r.pose.x, r.pose.y) for r in small.link_recovery_points]
    targets += [(w.x, w.y) for s in small.sectors for w in s.survey_waypoints]
    for t in targets:
        assert planner.plan(start, t) is not None, t
