"""Launch actions for one robot in its own ROS domain (SPEC A1 7.3, 8).

Spawn, bridge, platform nodes (Go2 shims / Summit description, scan and twist mux),
localization (``ground_truth`` or ``amcl_prior``), Nav2 and the simulation monitors.

Nodes that only stamp from incoming messages run without ``use_sim_time``: in Python,
following Gazebo's 250 Hz ``/clock`` costs about a third of a core per node.
"""

import math
import os
import tempfile
import zlib
from pathlib import Path

import yaml
from ament_index_python.packages import get_package_share_directory
from launch.actions import LogInfo, SetEnvironmentVariable
from launch_ros.actions import Node

from a6g_gz_bringup.a6g_config import RobotSetup
from a6g_gz_bringup.bridges import SIM, go2_bridge, namespaced, summit_bridge
from a6g_gz_bringup.speed import apply_max_speed, check_speed

SUPPORTED_LOCALIZATION = ("ground_truth", "amcl_prior", "gnss_ekf", "gnss_ekf_map")
GNSS_LOCALIZATION = ("gnss_ekf", "gnss_ekf_map")
#: Odometry, IMU and GNSS topics the EKFs fuse (same names as on the robots).
SENSORS = {
    "go2": {"odom": "/utlidar/robot_odom", "imu": "/utlidar/imu", "gnss": "/gnss/fix"},
    "summit": {
        "odom": "/robot/robotnik_base_control/odom",
        "imu": "/robot/imu/data",  # the real Summit has one (not configured yet): TODO(UPV) topic
        "gnss": "/robot/gps/fix",
    },
}
GO2_Z = 0.31
SUMMIT_Z = 0.12
NAV2_NODES = [
    "controller_server",
    "smoother_server",
    "planner_server",
    "behavior_server",
    "velocity_smoother",
    "bt_navigator",
]


def _share(pkg: str) -> Path:
    return Path(get_package_share_directory(pkg))


def _tmp(prefix: str, suffix: str, text: str) -> str:
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


def bt_files(s: RobotSetup, go2_spin_recovery: bool = False) -> tuple[str, str]:
    """(navigate_to_pose, navigate_through_poses) behaviour tree XMLs."""
    if s.platform == "summit":
        bt = s.root / "deploy" / "summit" / "bt"
        return str(bt / "navigate_to_pose_no_backup.xml"), str(
            bt / "navigate_through_poses_no_backup.xml"
        )
    if go2_spin_recovery:
        stock = _share("nav2_bt_navigator") / "behavior_trees"
        return (
            str(stock / "navigate_to_pose_w_replanning_and_recovery.xml"),
            str(stock / "navigate_through_poses_w_replanning_and_recovery.xml"),
        )
    bt = _share("a6g_gz_bringup") / "config" / "bt"
    return str(bt / "go2_navigate_to_pose_no_spin.xml"), str(
        bt / "go2_navigate_through_poses_no_spin.xml"
    )


def nav2_params(
    s: RobotSetup, spawn, go2_spin_recovery: bool = False, max_speed: float | None = None
) -> str:
    """The platform's Nav2 file with run-time values, nested under the namespace."""
    with open(_share("a6g_gz_bringup") / "config" / "nav2" / f"{s.platform}.yaml") as f:
        params = yaml.safe_load(f)
    if max_speed is not None:
        apply_max_speed(params, s.platform, max_speed)
    to_pose, through = bt_files(s, go2_spin_recovery)
    bt = params["bt_navigator"]["ros__parameters"]
    bt["default_nav_to_pose_bt_xml"] = to_pose
    bt["default_nav_through_poses_bt_xml"] = through
    params["map_server"]["ros__parameters"]["yaml_filename"] = str(s.map_yaml)
    params["map_server_loc"]["ros__parameters"]["yaml_filename"] = str(
        s.map_yaml.with_name(s.map_yaml.stem + "_loc.yaml")
    )
    x, y, yaw = spawn
    params["amcl"]["ros__parameters"]["initial_pose"] = {"x": x, "y": y, "z": 0.0, "yaw": yaw}
    if s.platform == "go2" and go2_spin_recovery:
        params["controller_server"]["ros__parameters"]["progress_checker"][
            "movement_time_allowance"
        ] = 60.0
    for node in params.values():  # every node runs on Gazebo's clock
        if "ros__parameters" in node:
            node["ros__parameters"]["use_sim_time"] = True
        else:
            for inner in node.values():
                inner["ros__parameters"]["use_sim_time"] = True
    return _tmp(f"a6g_nav2_{s.robot_id}_", ".yaml", yaml.safe_dump(namespaced(params, s.namespace)))


def _static_tf(parent: str, child: str, xyz, rpy=(0.0, 0.0, 0.0)) -> Node:
    return Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name=f"tf_{child}",
        arguments=[
            "--x",
            str(xyz[0]),
            "--y",
            str(xyz[1]),
            "--z",
            str(xyz[2]),
            "--roll",
            str(rpy[0]),
            "--pitch",
            str(rpy[1]),
            "--yaw",
            str(rpy[2]),
            "--frame-id",
            parent,
            "--child-frame-id",
            child,
        ],
    )


def _bridge(s: RobotSetup, entries: list[dict]) -> Node:
    path = _tmp(f"a6g_bridge_{s.robot_id}_", ".yaml", yaml.safe_dump(entries))
    return Node(
        package="ros_gz_bridge",
        executable="parameter_bridge",
        name=f"bridge_{s.robot_id}",
        parameters=[{"config_file": path}],
        output="log",
    )


def _spawn(s: RobotSetup, spawn, source: list[str], z: float) -> Node:
    x, y, yaw = spawn
    return Node(
        package="ros_gz_sim",
        executable="create",
        name=f"spawn_{s.robot_id}",
        arguments=[
            "-world",
            s.world,
            "-name",
            s.robot_id,
            *source,
            "-x",
            str(x),
            "-y",
            str(y),
            "-z",
            str(z),
            "-Y",
            str(yaw),
        ],
        output="log",
    )


def go2_actions(
    s: RobotSetup,
    spawn,
    odom_drift: bool = True,
    odom_tf: bool = True,
    max_speed: float | None = None,
) -> list:
    model = _share("a6g_gz_robots") / "models" / "go2_proxy" / "model.sdf"
    camera = s.manifest.get("camera_topic", "/camera/image_raw")
    sim = {"use_sim_time": True}
    shim = "a6g_gz_go2_shim"
    return [
        _spawn(s, spawn, ["-file", str(model)], GO2_Z),
        _bridge(s, go2_bridge(s.world, s.robot_id, camera)),
        # Sensor frames of the proxy (the dock publishes these from the Go2 description).
        _static_tf("base_link", "utlidar_lidar", (0.30, 0.0, 0.12)),
        _static_tf(
            "base_link",
            "camera_color_optical_frame",
            (0.36, 0.0, 0.03),
            (-math.pi / 2, 0.0, -math.pi / 2),
        ),
        _static_tf("base_link", "imu", (0.0, 0.0, 0.0)),
        _static_tf("base_link", "gnss", (-0.10, 0.0, 0.15)),
        Node(
            package=shim,
            executable="leg_odom_sim",
            parameters=[
                {}
                if odom_drift
                else {
                    "linear_scale_error": 0.0,
                    "yaw_rate_bias": 0.0,
                    "linear_noise": 0.0,
                    "angular_noise": 0.0,
                }
            ],
            remappings=[("ground_truth", f"{SIM}/ground_truth")],
        ),
        Node(package=shim, executable="odom_to_tf", parameters=[{"publish_tf": odom_tf}]),
        Node(
            package=shim,
            executable="cloud_deskew_sim",
            remappings=[("cloud_raw", "/utlidar/cloud")],
        ),
        Node(package=shim, executable="cloud_to_scan"),
        Node(
            package=shim,
            executable="cmd_vel_to_sport_sim",
            # The simulated platform cap follows max_speed (the real dock clamps at 0.30),
            # the in-place turn the profile's turn_rate_radps (real static walk: 0.105).
            parameters=[{**sim, "vx_max": max_speed or s.max_speed, **_turn_params(s)}],
            remappings=[("proxy_cmd_vel", f"{SIM}/proxy_cmd_vel"), ("stop", f"{SIM}/stop")],
        ),
        Node(package=shim, executable="sport_api_sim", remappings=[("stop", f"{SIM}/stop")]),
    ]


def summit_actions(
    s: RobotSetup,
    spawn,
    odom_drift: bool = True,
    odom_tf: bool = True,
    max_speed: float | None = None,
) -> list:
    # Wheel odometry from DiffDrive drifts by itself (slip); the DiffDrive cap stays at
    # SUMMIT_BASE_MAX, so max_speed only lowers Nav2's speed.
    del odom_drift, max_speed
    import xacro
    from a6g_gz_robots.summit import adapt

    ns = s.namespace or "robot"
    xacro_path = _share("robotnik_description") / "robots" / "rbsummit" / "rbsummit.urdf.xacro"
    doc = xacro.process_file(
        str(xacro_path), mappings={"prefix": f"{ns}_", "namespace": ns, "gazebo_ignition": "true"}
    )
    urdf = adapt(doc.toxml(), model_name=s.robot_id, prefix=f"{ns}_")
    urdf_path = _tmp(f"a6g_{s.robot_id}_", ".urdf", urdf)
    camera = s.manifest.get("camera_topic", f"/{ns}/front_camera/color/image_raw")
    stop = s.manifest.get("ros", {}).get("stop", {}).get("topic", f"/{ns}/web_teleop/cmd_vel")
    sim = {"use_sim_time": True}
    return [
        Node(
            package="robot_state_publisher",
            executable="robot_state_publisher",
            namespace=ns,
            parameters=[{"robot_description": urdf, "use_sim_time": True}],
        ),
        _spawn(s, spawn, ["-file", urdf_path], SUMMIT_Z),
        _bridge(s, summit_bridge(s.robot_id, ns, camera, odom_tf=odom_tf)),
        # 3D LiDAR -> the derived 2D scan with the robot's +-132 deg field of view.
        Node(
            package="pointcloud_to_laserscan",
            executable="pointcloud_to_laserscan_node",
            name="top_3d_laser_to_scan",
            namespace=ns,
            remappings=[
                ("cloud_in", f"/{ns}/top_3d_laser/points"),
                ("scan", f"/{ns}/top_3d_laser/scan"),
            ],
            parameters=[
                {
                    "use_sim_time": True,
                    "target_frame": f"{ns}_base_footprint",
                    "transform_tolerance": 0.05,
                    "min_height": 0.10,
                    "max_height": 1.50,
                    "angle_min": -math.radians(132),
                    "angle_max": math.radians(132),
                    "angle_increment": math.radians(0.5),
                    "scan_time": 0.1,
                    "range_min": 0.45,
                    "range_max": 60.0,
                    "use_inf": True,
                }
            ],
        ),
        # twist_mux: Nav2 (10) < web_teleop (40, the manifest's stop path).
        Node(
            package="a6g_gz_bringup",
            executable="twist_mux_lite",
            namespace=ns,
            parameters=[
                {
                    **sim,
                    "topics": [f"/{ns}/cmd_vel", stop],
                    "priorities": [10, 40],
                    "timeouts": [0.5, 1.0],
                }
            ],
            remappings=[("cmd_vel_out", f"/{ns}/cmd_vel_muxed")],
        ),
        # Hard reverse limits (manifest reverse_limits) on everything that reaches the base.
        Node(
            package="a6g_gz_bringup",
            executable="reverse_governor",
            namespace=ns,
            parameters=[{**sim, **_reverse_limits(s)}],
            remappings=[
                ("cmd_vel_in", f"/{ns}/cmd_vel_muxed"),
                ("odom", f"/{ns}/robotnik_base_control/odom"),
                ("cmd_vel_out", f"/{ns}/robotnik_base_control/cmd_vel_unstamped"),
            ],
        ),
    ]


def _turn_params(s: RobotSetup) -> dict:
    if s.turn_rate is None:
        return {}
    return {"static_walk_wz_max": s.turn_rate, "wz_max": max(0.60, s.turn_rate)}


def _reverse_limits(s: RobotSetup) -> dict:
    """Governor parameters from the manifest; reverse forbidden -> 0 m allowed."""
    limits = s.manifest.get("reverse_limits")
    if "reverse" in (s.manifest.get("forbidden_motions") or []) or not limits:
        return {"max_speed_mps": 0.0, "max_distance_m": 0.0}
    return {
        "max_speed_mps": float(limits["max_speed_mps"]),
        "max_distance_m": float(limits["max_distance_m"]),
    }


def gnss_noise_action(s: RobotSetup, noise: str | None = None) -> Node:
    """The simulated receiver: Gazebo's noise-free fix + the profile's GNSS error."""
    import json

    name = noise or s.gnss_noise
    params = dict(s.gnss_noise_params or {"sigma_m": 0.02})
    if noise and noise != s.gnss_noise:
        with open(s.root / "config" / "profiles" / "gazebo.yaml") as f:
            params = dict(yaml.safe_load(f)["gnss_noise_profiles"][noise])
    return Node(
        package="a6g_gz_bringup",
        executable="gnss_noise_sim",
        name="gnss_noise_sim",
        namespace=SIM.strip("/"),
        parameters=[
            {
                **{k: float(v) for k, v in params.items()},
                "profile": name,
                "seed": zlib.crc32(s.robot_id.encode()) % 10000,  # deterministic per robot
                "dropout_in_shadows": s.gnss_dropout_in_shadows,
                "shadows_json": json.dumps([list(map(list, p)) for p in s.shadows]),
            }
        ],
        remappings=[
            ("raw", f"{SIM}/gnss_raw"),
            ("fix", SENSORS[s.platform]["gnss"]),
            ("ground_truth", f"{SIM}/ground_truth"),
        ],
    )


def ekf_params(s: RobotSetup, spawn) -> dict:
    """robot_localization dual EKF + navsat_transform (SPEC A1 8, ``gnss_ekf``).

    Local EKF (odom frame): odometry twist + IMU yaw rate, publishes odom -> base.
    Global EKF (map frame): + IMU yaw (ENU) + GNSS position via navsat_transform, publishes
    map -> odom. navsat_transform's datum is the campus datum, so map == campus_map.
    """
    io = SENSORS[s.platform]
    common = {
        "use_sim_time": True,
        "frequency": 30.0,
        "sensor_timeout": 0.5,
        "two_d_mode": True,
        "publish_tf": True,
        "map_frame": s.map_frame,
        "odom_frame": s.odom_frame,
        "base_link_frame": s.base_frame,
        "odom0": io["odom"],
        "odom0_config": [False] * 6 + [True, True, False] + [False, False, True] + [False] * 3,
        "imu0": io["imu"],
        "imu0_remove_gravitational_acceleration": True,
    }
    x, y, yaw = spawn
    local = {
        **common,
        "world_frame": s.odom_frame,
        "imu0_config": [False] * 9 + [False, False, True] + [False] * 3,  # yaw rate
    }
    global_ = {
        **common,
        "world_frame": s.map_frame,
        "imu0_config": [False] * 5 + [True] + [False] * 3 + [False, False, True] + [False] * 3,
        "odom1": "odometry/gps",
        "odom1_config": [True, True] + [False] * 13,
        "initial_state": [x, y, 0.0, 0.0, 0.0, yaw] + [0.0] * 9,
    }
    lat, lon = s.datum
    navsat = {
        "use_sim_time": True,
        "frequency": 10.0,
        "delay": 2.0,
        "magnetic_declination_radians": 0.0,
        "yaw_offset": 0.0,  # the IMU reports ENU yaw (0 = east)
        "zero_altitude": True,
        "broadcast_cartesian_transform": False,
        "publish_filtered_gps": False,
        "use_odometry_yaw": False,
        "wait_for_datum": True,
        "datum": [lat, lon, 0.0],  # campus datum, map x axis = east
    }
    return {"ekf_local": local, "ekf_global": global_, "navsat_transform": navsat}


def gnss_ekf_actions(s: RobotSetup, spawn) -> list:
    io = SENSORS[s.platform]
    p = ekf_params(s, spawn)
    ns = s.namespace
    return [
        Node(package="robot_localization", executable="ekf_node", name="ekf_local",
             namespace=ns, parameters=[p["ekf_local"]],
             remappings=[("odometry/filtered", "odometry/local")]),
        Node(package="robot_localization", executable="ekf_node", name="ekf_global",
             namespace=ns, parameters=[p["ekf_global"]],
             remappings=[("odometry/filtered", "odometry/global")]),
        Node(package="robot_localization", executable="navsat_transform_node",
             name="navsat_transform", namespace=ns, parameters=[p["navsat_transform"]],
             remappings=[("imu", io["imu"]), ("gps/fix", io["gnss"]),
                         ("odometry/filtered", "odometry/global")]),
    ]  # fmt: skip


def localization_actions(s: RobotSetup, localization: str, params: str, spawn=None) -> list:
    ns = s.namespace
    sim = {"use_sim_time": True}
    nodes = ["map_server"]
    actions = [
        Node(
            package="nav2_map_server",
            executable="map_server",
            name="map_server",
            namespace=ns,
            parameters=[params],
        )
    ]
    if localization == "amcl_prior":
        nodes += ["map_server_loc", "amcl"]
        actions.append(
            Node(
                package="nav2_map_server",
                executable="map_server",
                name="map_server_loc",
                namespace=ns,
                parameters=[params],
            )
        )
        actions.append(
            Node(
                package="nav2_amcl",
                executable="amcl",
                name="amcl",
                namespace=ns,
                parameters=[params],
            )
        )
    elif localization == "ground_truth":
        actions.append(
            Node(
                package="a6g_gz_bringup",
                executable="gt_localizer",
                namespace=ns,
                parameters=[
                    {
                        "map_frame": s.map_frame,
                        "odom_frame": s.odom_frame,
                        "base_frame": s.base_frame,
                    }
                ],
                remappings=[("ground_truth", f"{SIM}/ground_truth")],
            )
        )
    elif localization in GNSS_LOCALIZATION:
        actions += gnss_ekf_actions(s, spawn or s.spawn)
        if localization == "gnss_ekf_map":
            # Interface and launch skeleton only (SPEC A1 8): scan-to-map registration against
            # the prior map is implemented in P4c with the real map.
            actions.append(
                LogInfo(
                    msg="gnss_ekf_map: scan-to-map registration is a P4c item; running gnss_ekf"
                )
            )
    else:
        raise ValueError(
            f"localization {localization!r} is not available "
            f"(supported: {', '.join(SUPPORTED_LOCALIZATION)})"
        )
    actions.append(
        Node(
            package="nav2_lifecycle_manager",
            executable="lifecycle_manager",
            name="lifecycle_manager_localization",
            namespace=ns,
            parameters=[{**sim, "autostart": True, "node_names": nodes, "bond_timeout": 10.0}],
        )
    )
    return actions


def nav2_actions(s: RobotSetup, params: str) -> list:
    ns = s.namespace
    sim = {"use_sim_time": True}
    actions = []
    for name in NAV2_NODES:
        pkg = {
            "velocity_smoother": "nav2_velocity_smoother",
            "bt_navigator": "nav2_bt_navigator",
            "behavior_server": "nav2_behaviors",
            "smoother_server": "nav2_smoother",
            "planner_server": "nav2_planner",
            "controller_server": "nav2_controller",
        }[name]
        executable = "behavior_server" if name == "behavior_server" else name
        remap = []
        if name == "controller_server":
            remap = [("cmd_vel", "cmd_vel_nav")]
        elif name == "velocity_smoother":
            remap = [("cmd_vel", "cmd_vel_nav"), ("cmd_vel_smoothed", "cmd_vel")]
        actions.append(
            Node(
                package=pkg,
                executable=executable,
                name=name,
                namespace=ns,
                parameters=[params],
                remappings=remap,
                output="log",
            )
        )
    actions.append(
        Node(
            package="nav2_lifecycle_manager",
            executable="lifecycle_manager",
            name="lifecycle_manager_navigation",
            namespace=ns,
            parameters=[{**sim, "autostart": True, "node_names": NAV2_NODES, "bond_timeout": 10.0}],
        )
    )
    return actions


def monitor_actions(s: RobotSetup, localization: str, out_dir: str) -> list:
    gt = [("ground_truth", f"{SIM}/ground_truth")]
    return [
        Node(
            package="a6g_gz_bringup",
            executable="velocity_monitor",
            namespace=SIM.strip("/"),
            parameters=[{"output_file": os.path.join(out_dir, f"{s.robot_id}_velocity.json")}],
            remappings=gt,
        ),
        Node(
            package="a6g_gz_bringup",
            executable="loc_eval",
            namespace=SIM.strip("/"),
            parameters=[
                {
                    "map_frame": s.map_frame,
                    "base_frame": s.base_frame,
                    "robot_id": s.robot_id,
                    "localization": localization,
                    "output_dir": os.path.join(out_dir, "loc_eval"),
                }
            ],
            remappings=gt,
        ),
    ]


def robot_actions(
    s: RobotSetup,
    *,
    localization: str | None = None,
    spawn=None,
    nav: bool = True,
    out_dir: str | None = None,
    go2_spin_recovery: bool = False,
    odom_drift: bool = True,
    gnss_noise: str | None = None,
    max_speed: float | None = None,
) -> list:
    """``max_speed`` (m/s) overrides the profile / manifest speed (see ``speed.py``)."""
    localization = localization or s.localization
    speed = max_speed if max_speed is not None else s.max_speed
    check_speed(s.platform, speed)
    spawn = spawn or s.spawn
    out_dir = out_dir or str(s.root / "var" / "gz")
    os.makedirs(out_dir, exist_ok=True)
    params = nav2_params(s, spawn, go2_spin_recovery, speed)
    platform = {"go2": go2_actions, "summit": summit_actions}[s.platform]
    actions = [SetEnvironmentVariable("ROS_DOMAIN_ID", str(s.domain_id))]
    gnss = localization in GNSS_LOCALIZATION
    actions += platform(s, spawn, odom_drift, odom_tf=not gnss, max_speed=speed)
    actions.append(gnss_noise_action(s, gnss_noise))
    actions += localization_actions(s, localization, params, spawn)
    if nav:
        actions += nav2_actions(s, params)
    actions += monitor_actions(s, localization, out_dir)
    return actions
