"""The Summit's behaviour trees contain no BackUp; the Go2 no-spin trees no Spin."""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SUMMIT_BT = sorted((REPO / "deploy" / "summit" / "bt").glob("*.xml"))
GO2_BT = sorted((REPO / "sim_gazebo" / "src" / "a6g_gz_bringup" / "config" / "bt").glob("*.xml"))


def _tags(path: Path) -> set[str]:
    return {e.tag for e in ET.parse(path).getroot().iter()}


def test_summit_trees_exist() -> None:
    names = {p.name for p in SUMMIT_BT}
    assert {"navigate_to_pose_no_backup.xml", "navigate_through_poses_no_backup.xml"} <= names


@pytest.mark.parametrize("path", SUMMIT_BT, ids=lambda p: p.name)
def test_summit_tree_never_backs_up(path: Path) -> None:
    tags = _tags(path)
    assert "BackUp" not in tags
    assert {"ComputePathToPose", "FollowPath"} & tags or {"ComputePathThroughPoses"} & tags


@pytest.mark.parametrize("path", GO2_BT, ids=lambda p: p.name)
def test_go2_no_spin_tree(path: Path) -> None:
    assert "Spin" not in _tags(path)
