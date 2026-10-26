"""SPEC A1 stage A2 acceptance in Gazebo (run with ``pytest --gazebo``; several minutes each).

Each robot, alone in the campus world and in its own ROS domain, reaches a goal 30 m away
through Nav2 from the command line (``nav_check``), with ``ground_truth`` and ``amcl_prior``
localization. The Summit's ground-truth base-frame forward velocity never drops below
-0.01 m/s; the Go2's ``sport_api_sim`` counts no Sport API violation.

The Go2's AMCL route runs along a facade: its 2D scan band (-0.15..+0.30 m about base_link,
sparse L1) only sees walls within ~5 m, so in the open plaza AMCL has nothing to match.
"""

import os
from pathlib import Path

import pytest
from a6g_common.config import load_config
from gz_harness import available, gazebo_robot

pytestmark = pytest.mark.gazebo

SUMMIT = ["--ns", "/robot", "--frame", "robot_map", "--base-frame", "robot_base_footprint"]
CASES = {
    # id: (robot, localization, launch args, goal x y yaw, final ground-truth tolerance m)
    "go2_ground_truth": ("go2_01", "ground_truth", [], (0.0, 33.0, 0.0), 0.5),
    "go2_amcl_prior": (
        "go2_01",
        "amcl_prior",
        # Along the facade east of the staging area, ending 2.7 m short of a wall: a straight
        # facade only fixes the cross-track error, the wall ahead fixes the along-track one.
        ["x:=93", "y:=14", "yaw:=-0.2618"],
        (122.0, 6.2, -0.2618),
        0.5,
    ),
    "summit_ground_truth": ("summit_01", "ground_truth", [], (30.0, 0.0, 0.0), 0.5),
    # Open plaza, far facades only: AMCL drifts along track (A3/G1 tightens this to 0.30 m).
    "summit_amcl_prior": ("summit_01", "amcl_prior", [], (30.0, 0.0, 0.0), 1.0),
}


@pytest.fixture(scope="module", autouse=True)
def _gazebo_available() -> None:
    reason = available()
    if reason:
        pytest.skip(reason)
    if float(os.environ.get("A6G_WORLD_SCALE") or 1.0) != 1.0:
        pytest.skip("routes and tolerances are designed for the full-scale campus")


@pytest.mark.parametrize("case", CASES)
def test_robot_reaches_30m_goal_through_nav2(case: str, tmp_path: Path) -> None:
    robot, loc, args, (gx, gy, gyaw), tolerance = CASES[case]
    domain = load_config(env={}, profile="gazebo").profile.robots[robot].ros_domain_id
    check = [
        f"--goal {gx} {gy}",
        f"--yaw {gyaw}",
        f"--tolerance {tolerance}",
        "--velocity-stats /a6g_sim/velocity_monitor/stats",
    ]
    if robot.startswith("summit"):
        check += SUMMIT
    else:
        check += ["--sport-violations /sport_api_sim/violations"]
    with gazebo_robot(robot, domain, tmp_path, f"localization:={loc}", *args) as run:
        run.wait_nav2_active("/robot" if robot.startswith("summit") else "")  # stalled bringups
        summary = run.nav_check(*check)
    print(summary)
    assert summary.get("error") is None, summary
    checks = summary["checks"]
    assert checks["succeeded"] and checks["goal_at_least_m"], summary
    assert checks["final_error_ok"], summary
    if robot.startswith("summit"):
        assert checks["reverse_within_limits"], summary["velocity"]
    else:
        assert summary["sport_api_violations"] == 0
