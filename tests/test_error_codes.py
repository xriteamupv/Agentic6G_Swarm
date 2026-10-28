"""Every Nav2 error constant the backends can receive is mapped by the manifest.

The constants are parsed from the **installed** ``nav2_msgs`` action definitions (ROS 2
Jazzy) for the actions named in each platform's Nav2 ``error_code_names``. Each must be a
key of the robot's ``error_code_map`` (``unknown`` is an explicit, allowed mapping).
"""

import re
from pathlib import Path

import pytest
import yaml
from a6g_common.config import load_manifest

REPO = Path(__file__).resolve().parents[1]
ACTIONS = Path("/opt/ros/jazzy/share/nav2_msgs/action")
NAV2 = REPO / "sim_gazebo" / "src" / "a6g_gz_bringup" / "config" / "nav2"
#: BT blackboard key -> the action whose result codes it carries.
KEY_ACTION = {
    "compute_path_error_code": "ComputePathToPose",
    "follow_path_error_code": "FollowPath",
    "spin_error_code": "Spin",
    "backup_error_code": "BackUp",
    "smoother_error_code": "SmoothPath",
    "compute_path_through_poses_error_code": "ComputePathThroughPoses",
}
ROBOTS = {"summit_01": "summit", "go2_01": "go2", "go2_02": "go2", "go2_03": "go2"}

pytestmark = pytest.mark.skipif(not ACTIONS.exists(), reason="nav2_msgs (Jazzy) not installed")


def error_constants(action: str) -> dict[str, int]:
    """``uint16 NAME=value`` constants of the result section, without NONE."""
    text = (ACTIONS / f"{action}.action").read_text()
    result = text.split("---")[1]
    found = dict(re.findall(r"^\s*uint16\s+([A-Z_]+)\s*=\s*(\d+)", result, re.M))
    return {k: int(v) for k, v in found.items() if k != "NONE"}


def test_parser_reads_installed_definitions() -> None:
    assert error_constants("ComputePathToPose")["NO_VALID_PATH"] == 208
    assert error_constants("Spin")["TF_ERROR"] == 702


@pytest.mark.parametrize("robot_id", sorted(ROBOTS))
def test_every_reported_code_is_mapped(robot_id: str) -> None:
    params = yaml.safe_load((NAV2 / f"{ROBOTS[robot_id]}.yaml").read_text())
    keys = params["bt_navigator"]["ros__parameters"]["error_code_names"]
    manifest = load_manifest(REPO / "config" / "robots" / f"{robot_id}.yaml")
    missing = {}
    for key in keys:
        action = KEY_ACTION[key]
        for name, code in error_constants(action).items():
            if code not in manifest.error_code_map:
                missing[f"{action}.{name}"] = code
    assert not missing, f"{robot_id}: unmapped Nav2 error codes {missing}"


def test_summit_reports_spin_codes() -> None:
    params = yaml.safe_load((NAV2 / "summit.yaml").read_text())
    assert "spin_error_code" in params["bt_navigator"]["ros__parameters"]["error_code_names"]
