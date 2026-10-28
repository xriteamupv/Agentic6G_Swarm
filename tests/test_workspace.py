"""Workspace-level guards that later phases rely on."""

import ast
import importlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGES = sorted(p.name for p in (REPO / "packages").iterdir() if p.is_dir())


def test_all_packages_present() -> None:
    assert PACKAGES == [
        "a6g_allocator",
        "a6g_campus_tools",
        "a6g_cli",
        "a6g_common",
        "a6g_coordinator",
        "a6g_devstack",
        "a6g_interactions",
        "a6g_kb",
        "a6g_memory",
        "a6g_observability",
        "a6g_robot_agent",
        "a6g_ros_adapter",
        "a6g_sim",
        "a6g_skill_mcp",
        "a6g_transport",
    ]


@pytest.mark.parametrize("package", PACKAGES)
def test_package_importable(package: str) -> None:
    importlib.import_module(package)


def test_ros_adapter_is_python38_syntax() -> None:
    """ros_adapter runs under Foxy's Python 3.8: no match, no X | Y, no list[str] at runtime."""
    src = REPO / "packages" / "a6g_ros_adapter" / "src"
    files = list(src.rglob("*.py"))
    assert files
    for path in files:
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 8))
