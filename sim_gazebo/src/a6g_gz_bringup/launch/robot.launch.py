"""One robot in its own ROS domain: spawn, bridge, platform nodes, localization, Nav2.

    ros2 launch a6g_gz_bringup robot.launch.py robot:=go2_01 [localization:=ground_truth]

The domain, localization profile and spawn pose come from config/profiles/<profile>.yaml;
``x``/``y``/``yaw`` override the spawn pose. Every process started here gets that robot's
``ROS_DOMAIN_ID``; use the same domain to talk to it from the command line.
"""

from a6g_gz_bringup.a6g_config import robot_setup
from a6g_gz_bringup.robot_actions import robot_actions
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, LogInfo, OpaqueFunction
from launch.substitutions import LaunchConfiguration


def _setup(context):
    def arg(name: str) -> str:
        return LaunchConfiguration(name).perform(context)

    s = robot_setup(arg("robot"), profile=arg("profile"))
    spawn = list(s.spawn)
    for i, name in enumerate(("x", "y", "yaw")):
        if arg(name):
            spawn[i] = float(arg(name))
    loc = arg("localization") or s.localization
    speed = float(arg("max_speed")) if arg("max_speed") else s.max_speed
    return [
        LogInfo(
            msg=f"{s.robot_id} ({s.platform}): ROS_DOMAIN_ID={s.domain_id}, localization={loc}, "
            f"spawn={tuple(spawn)}, max_speed={speed} m/s"
        ),
        *robot_actions(
            s,
            localization=loc,
            spawn=tuple(spawn),
            nav=arg("nav").lower() in ("1", "true", "yes"),
            out_dir=arg("out_dir") or None,
            go2_spin_recovery=arg("go2_spin_recovery").lower() in ("1", "true", "yes"),
            odom_drift=arg("odom_drift").lower() in ("1", "true", "yes"),
            gnss_noise=arg("gnss_noise") or None,
            max_speed=float(arg("max_speed")) if arg("max_speed") else None,
        ),
    ]


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument("robot", description="robot id, e.g. summit_01 or go2_01"),
            DeclareLaunchArgument("profile", default_value="gazebo"),
            DeclareLaunchArgument(
                "localization",
                default_value="",
                description="ground_truth|amcl_prior|gnss_ekf|gnss_ekf_map (default: profile)",
            ),
            DeclareLaunchArgument("x", default_value=""),
            DeclareLaunchArgument("y", default_value=""),
            DeclareLaunchArgument("yaw", default_value=""),
            DeclareLaunchArgument("nav", default_value="true"),
            DeclareLaunchArgument("go2_spin_recovery", default_value="false"),
            DeclareLaunchArgument(
                "max_speed",
                default_value="",
                description="m/s; default: the profile's max_speed_mps, else the manifest's",
            ),
            DeclareLaunchArgument(
                "gnss_noise",
                default_value="",
                description="rtk | standard | degraded (default: from the profile)",
            ),
            DeclareLaunchArgument(
                "odom_drift",
                default_value="true",
                description="Go2 leg-odometry drift and noise (mild by default)",
            ),
            DeclareLaunchArgument("out_dir", default_value="", description="default <a6g>/var/gz"),
            OpaqueFunction(function=_setup),
        ]
    )
