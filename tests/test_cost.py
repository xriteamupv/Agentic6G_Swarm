"""The shared cost function (SPEC 9.1)."""

from datetime import UTC, datetime, timedelta

import pytest
from a6g_allocator.cost import (
    Estimate,
    Infeasible,
    best_bid,
    cost,
    estimate,
    order_nearest_first,
    task_path,
    virtual_state,
)
from a6g_common.schemas import Platform, PoseWithCovariance, RobotState
from conftest import inspect_poi, return_home, survey

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def state(robot_id: str, platform: str, x=0.0, y=0.0, battery=100.0, **kw) -> RobotState:
    return RobotState(
        robot_id=robot_id,
        platform=Platform(platform),
        pose=PoseWithCovariance(x=x, y=y, yaw=0.0, cov_xy=0.01),
        battery_pct=battery,
        capabilities=[],
        stamp=T0,
        **kw,
    )


def test_cost_is_deterministic_and_positive(cfg) -> None:
    go2 = cfg.manifests["go2_01"]
    task = survey("sec_north")
    values = {cost(state("go2_01", "go2"), task, go2, cfg.campus, cfg.allocator) for _ in range(5)}
    assert len(values) == 1
    value = values.pop()
    assert isinstance(value, float) and value > 0


def test_closer_robot_is_cheaper(cfg) -> None:
    go2 = cfg.manifests["go2_01"]
    near = cost(
        state("go2_01", "go2", x=20, y=25), survey("sec_north"), go2, cfg.campus, cfg.allocator
    )
    far = cost(
        state("go2_01", "go2", x=0, y=-50), survey("sec_north"), go2, cfg.campus, cfg.allocator
    )
    assert near < far


def test_capability_mismatch_is_infeasible(cfg) -> None:
    summit = cfg.manifests["summit_01"]  # no escort, no voice
    task = inspect_poi("poi_kiosk").model_copy(update={"required_capabilities": ["escort"]})
    result = cost(state("summit_01", "summit"), task, summit, cfg.campus, cfg.allocator)
    assert isinstance(result, Infeasible) and result.reason == "capability"


def test_battery_floor_and_unknown_battery(cfg) -> None:
    go2, summit = cfg.manifests["go2_01"], cfg.manifests["summit_01"]
    low = cost(
        state("go2_01", "go2", battery=26.0), survey("sec_east"), go2, cfg.campus, cfg.allocator
    )
    assert isinstance(low, Infeasible) and low.reason == "battery"
    # Summit's battery is unreliable: the configured unknown level is used instead.
    s = state("summit_01", "summit", battery=1.0)
    vs = virtual_state(s, summit, cfg.allocator)
    assert vs.battery_pct == cfg.allocator.unknown_battery_pct
    assert isinstance(cost(s, survey("sec_east"), summit, cfg.campus, cfg.allocator), float)


def test_commitment_and_priority_terms(cfg) -> None:
    go2 = cfg.manifests["go2_01"]
    task = inspect_poi("poi_kiosk")
    free = cost(state("go2_01", "go2"), task, go2, cfg.campus, cfg.allocator)
    busy = cost(
        state("go2_01", "go2", commitment_until=T0 + timedelta(minutes=10)),
        task,
        go2,
        cfg.campus,
        cfg.allocator,
    )
    urgent = cost(
        state("go2_01", "go2"),
        task.model_copy(update={"priority": 100}),
        go2,
        cfg.campus,
        cfg.allocator,
    )
    w = cfg.allocator.weights
    discount = 1 - w.priority * task.priority / 100  # applies to the whole base cost
    assert busy == pytest.approx(free + w.commitment * 600 * discount)
    assert urgent < free


def test_estimate_reports_battery_after(cfg) -> None:
    go2 = cfg.manifests["go2_01"]
    est = estimate(
        virtual_state(state("go2_01", "go2"), go2, cfg.allocator),
        survey("sec_north"),
        go2,
        cfg.campus,
        cfg.allocator,
        20.0,
    )
    assert isinstance(est, Estimate)
    assert est.battery_after_pct == pytest.approx(100 - est.battery_used_pct)
    assert est.distance_m > 100  # travel + survey route


def test_ordering_and_tie_breaks(cfg) -> None:
    from a6g_common.schemas import Pose2D

    tasks = [
        inspect_poi("poi_library_door"),
        inspect_poi("poi_kiosk"),
        return_home(depends_on=["t_inspect_poi_kiosk", "t_inspect_poi_library_door"]),
    ]
    kiosk = cfg.campus.poi("poi_kiosk").pose  # starting at the 8K door: it comes first
    order = [
        t.task_id for t in order_nearest_first(Pose2D(x=kiosk.x, y=kiosk.y), tasks, cfg.campus)
    ]
    assert order == ["t_inspect_poi_kiosk", "t_inspect_poi_library_door", "t_home"]
    assert best_bid([("summit_01", 10.0), ("go2_02", 10.0), ("go2_01", 12.0)]) == ("go2_02", 10.0)
    assert best_bid([]) is None


def test_a_patrol_lap_runs_from_the_nearer_end(cfg) -> None:
    """A robot that has just finished a lap stands at the far end of the open survey route:
    the next lap runs back from there instead of walking ~200 m to the start, so the
    allocator does not find a busy robot cheaper than the one that is free (2026-10-19)."""
    go2 = cfg.manifests["go2_01"]
    wps = cfg.campus.sector("sec_north").survey_waypoints
    first, last = wps[0], wps[-1]
    lap = survey("sec_north")
    assert task_path(lap, cfg.campus, last)[0] == last  # reversed from the far end
    assert task_path(lap, cfg.campus, first)[0] == first
    assert task_path(lap, cfg.campus)[0] == first  # no pose: as drawn

    def lap_cost(x: float, y: float, task=lap) -> float:
        return cost(state("go2_01", "go2", x=x, y=y), task, go2, cfg.campus, cfg.allocator)

    at_end, at_start = lap_cost(last.x, last.y), lap_cost(first.x, first.y)
    assert at_end == pytest.approx(at_start, rel=0.05)
    forward = lap.model_copy(update={"target": lap.target.model_copy(update={"reverse": False})})
    assert lap_cost(last.x, last.y, forward) > at_end * 1.5  # the old way: back to the start
    # A remainder keeps the direction it was given.
    rem = lap.model_copy(update={"target": lap.target.model_copy(update={"from_waypoint": 2})})
    assert task_path(rem, cfg.campus, last)[0] == wps[2]


def test_a_lap_starts_where_the_sector_was_patrolled_longest_ago(cfg) -> None:
    """User report (2026-10-21): coming out of the coverage shadow at the East gate, a robot
    turned back into the shadow to start the next South plaza lap at the end it had just
    patrolled. The lap now starts where the previous one started (patrolled longest ago),
    unless that end is more than half the route farther than the nearer end."""
    from a6g_common.schemas import Pose2D

    campus = cfg.campus
    wps = campus.sector("sec_south").survey_waypoints
    gate = Pose2D(x=81.918, y=5.507)  # where the East gate was (removed 2026-10-26)
    lap = survey("sec_south")
    assert task_path(lap, campus, gate)[0] == wps[-1]  # nearer end: back into the shadow
    stale = lap.model_copy(
        update={"target": lap.target.model_copy(update={"prefer_reverse": False})}
    )
    assert task_path(stale, campus, gate)[0] == wps[0]  # the end patrolled longest ago
    # Just finished at the far end (no shadow): walking the whole route back to the stale
    # end is too far; the lap runs back from where the robot stands (2026-10-19 rule).
    end = Pose2D(x=wps[-1].x, y=wps[-1].y)
    assert task_path(stale, campus, end)[0] == wps[-1]


def test_a_robot_in_coverage_does_not_start_a_lap_in_the_lost_area(cfg) -> None:
    """User report (2026-10-27): after recovering at staging, the next South plaza lap
    started at the arcade (lost coverage), so the robot drove straight back into the lost
    area. A robot with coverage starts at the covered end, whatever end was patrolled
    longest ago."""
    from a6g_common.schemas import Pose2D

    campus = cfg.campus
    wps = campus.sector("sec_south").survey_waypoints
    assert campus.link_level_at(wps[-1].x, wps[-1].y) == "lost"  # the arcade end
    assert campus.link_level_at(wps[0].x, wps[0].y) != "lost"
    staging = Pose2D(x=campus.staging.x, y=campus.staging.y)
    assert campus.link_level_at(staging.x, staging.y) != "lost"
    lap = survey("sec_south")
    for prefer in (None, True, False):
        t = lap.model_copy(
            update={"target": lap.target.model_copy(update={"prefer_reverse": prefer})}
        )
        assert task_path(t, campus, staging)[0] == wps[0]
    # Still inside the lost area: the nearer end, as before (it walks out along the lap).
    arcade = Pose2D(x=wps[-1].x, y=wps[-1].y)
    assert task_path(lap, campus, arcade)[0] == wps[-1]
