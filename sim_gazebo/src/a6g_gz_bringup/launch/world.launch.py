"""Gazebo server for the campus world (one process for every robot).

    ros2 launch a6g_gz_bringup world.launch.py [gui:=true]

Runs headless by default (sensors render offscreen with EGL). The world is the one generated
by ``a6g campus build`` for the fleet's campus.
"""

import contextlib
import os

from a6g_gz_bringup.a6g_config import a6g_root, robot_setup
from ament_index_python.packages import PackageNotFoundError, get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    ExecuteProcess,
    OpaqueFunction,
    SetEnvironmentVariable,
)
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration


def _resource_path() -> str:
    paths = [
        os.path.join(get_package_share_directory("a6g_gz_worlds"), "models"),
        os.path.join(get_package_share_directory("a6g_gz_robots"), "models"),
    ]
    # package://robotnik_* mesh URIs resolve against the share directories' parents.
    for pkg in ("robotnik_description", "robotnik_sensors"):
        with contextlib.suppress(PackageNotFoundError):  # Robotnik workspace not sourced
            paths.append(os.path.dirname(get_package_share_directory(pkg)))
    existing = os.environ.get("GZ_SIM_RESOURCE_PATH", "")
    return os.pathsep.join(paths + ([existing] if existing else []))


def _setup(context):
    s = robot_setup("summit_01", root=a6g_root())
    world = s.map_yaml.parent.parent / "worlds" / f"{s.world}.sdf"
    markers = world.with_name(f"{s.world}.markers.json")
    headless = LaunchConfiguration("headless").perform(context).lower() in ("1", "true", "yes")
    cmd = ["gz", "sim", "-s", "-r"] + (["--headless-rendering"] if headless else []) + [str(world)]
    return [
        SetEnvironmentVariable("GZ_SIM_RESOURCE_PATH", _resource_path()),
        ExecuteProcess(cmd=cmd, name="gz_server", output="log"),
        ExecuteProcess(
            cmd=["gz", "sim", "-g"],
            name="gz_gui",
            output="log",
            condition=IfCondition(LaunchConfiguration("gui")),
        ),
        # POIs, sectors, shadows... drawn in the GUI only (never seen by robot sensors).
        ExecuteProcess(
            cmd=["ros2", "run", "a6g_gz_bringup", "campus_markers", "--file", str(markers)],
            name="campus_markers",
            output="log",
            condition=IfCondition(LaunchConfiguration("gui")),
        ),
    ]


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument("gui", default_value="false"),
            DeclareLaunchArgument(
                "headless",
                default_value="true",
                description="render sensors offscreen (no display needed)",
            ),
            OpaqueFunction(function=_setup),
        ]
    )
