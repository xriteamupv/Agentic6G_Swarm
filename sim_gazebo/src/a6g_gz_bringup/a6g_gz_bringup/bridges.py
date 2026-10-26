"""Per-robot ros_gz bridge tables (SPEC A1 7.3; ROS-free, unit-tested).

Each robot gets its own ``parameter_bridge`` in its own ROS domain, bridging only that
model's topics plus ``/clock``. ROS names inside the domain are the real robot's; the
simulation-only topics live under ``/a6g_sim``.
"""

GZ_TO_ROS = "GZ_TO_ROS"
ROS_TO_GZ = "ROS_TO_GZ"

SIM = "/a6g_sim"
GNSS_RAW = f"{SIM}/gnss_raw"


def _entry(ros: str, gz: str, ros_type: str, gz_type: str, direction: str) -> dict:
    return {
        "ros_topic_name": ros,
        "gz_topic_name": gz,
        "ros_type_name": ros_type,
        "gz_type_name": gz_type,
        "direction": direction,
    }


def clock() -> dict:
    return _entry("/clock", "/clock", "rosgraph_msgs/msg/Clock", "gz.msgs.Clock", GZ_TO_ROS)


def go2_bridge(world: str, model: str, camera_topic: str = "/camera/image_raw") -> list[dict]:
    sensor = f"/world/{world}/model/{model}/link/base_link/sensor"
    cam_ns = camera_topic.rsplit("/", 1)[0] or "/camera"
    return [
        clock(),
        _entry(
            f"{SIM}/ground_truth",
            f"/model/{model}/odometry",
            "nav_msgs/msg/Odometry",
            "gz.msgs.Odometry",
            GZ_TO_ROS,
        ),
        _entry(
            f"{SIM}/proxy_cmd_vel",
            f"/model/{model}/cmd_vel",
            "geometry_msgs/msg/Twist",
            "gz.msgs.Twist",
            ROS_TO_GZ,
        ),
        _entry(
            "/utlidar/cloud",
            f"{sensor}/utlidar/scan/points",
            "sensor_msgs/msg/PointCloud2",
            "gz.msgs.PointCloudPacked",
            GZ_TO_ROS,
        ),
        _entry(
            "/utlidar/imu", f"{sensor}/imu/imu", "sensor_msgs/msg/Imu", "gz.msgs.IMU", GZ_TO_ROS
        ),
        _entry(
            GNSS_RAW,  # gnss_noise_sim publishes the robot's /gnss/fix
            f"{sensor}/navsat/navsat",
            "sensor_msgs/msg/NavSatFix",
            "gz.msgs.NavSat",
            GZ_TO_ROS,
        ),
        _entry(
            camera_topic,
            f"{sensor}/camera/image",
            "sensor_msgs/msg/Image",
            "gz.msgs.Image",
            GZ_TO_ROS,
        ),
        _entry(
            f"{cam_ns}/camera_info",
            f"{sensor}/camera/camera_info",
            "sensor_msgs/msg/CameraInfo",
            "gz.msgs.CameraInfo",
            GZ_TO_ROS,
        ),
        _entry(
            f"{cam_ns}/depth/image_raw",
            f"{sensor}/camera/depth_image",
            "sensor_msgs/msg/Image",
            "gz.msgs.Image",
            GZ_TO_ROS,
        ),
    ]


def summit_bridge(
    model: str,
    ns: str = "robot",
    camera_topic: str = "/robot/front_camera/color/image_raw",
    odom_tf: bool = True,
) -> list[dict]:
    """Robotnik's sensors publish on ``<ns>/...`` gz topics (one Summit per world).

    ``odom_tf=False`` leaves ``odom -> base`` to the local EKF (GNSS localization).
    """
    cam_ns = camera_topic.rsplit("/", 1)[0]
    entries = [
        clock(),
        _entry(
            f"{SIM}/ground_truth",
            f"/model/{model}/odometry",
            "nav_msgs/msg/Odometry",
            "gz.msgs.Odometry",
            GZ_TO_ROS,
        ),
        _entry(
            f"/{ns}/robotnik_base_control/odom",
            f"/model/{model}/odometry_wheels",
            "nav_msgs/msg/Odometry",
            "gz.msgs.Odometry",
            GZ_TO_ROS,
        ),
        _entry(
            "/tf",
            f"/model/{model}/tf_wheels",
            "tf2_msgs/msg/TFMessage",
            "gz.msgs.Pose_V",
            GZ_TO_ROS,
        ),
        _entry(
            f"/{ns}/robotnik_base_control/cmd_vel_unstamped",
            f"/model/{model}/cmd_vel",
            "geometry_msgs/msg/Twist",
            "gz.msgs.Twist",
            ROS_TO_GZ,
        ),
        _entry(
            f"/{ns}/joint_states",
            f"/model/{model}/joint_states",
            "sensor_msgs/msg/JointState",
            "gz.msgs.Model",
            GZ_TO_ROS,
        ),
        _entry(
            f"/{ns}/top_3d_laser/points",
            f"/{ns}/top_lidar/scan/points",
            "sensor_msgs/msg/PointCloud2",
            "gz.msgs.PointCloudPacked",
            GZ_TO_ROS,
        ),
        _entry(
            f"/{ns}/imu/data", f"/{ns}/imu/data", "sensor_msgs/msg/Imu", "gz.msgs.IMU", GZ_TO_ROS
        ),
        _entry(
            GNSS_RAW,  # gnss_noise_sim publishes the robot's gps/fix
            f"/{ns}/gps/data",
            "sensor_msgs/msg/NavSatFix",
            "gz.msgs.NavSat",
            GZ_TO_ROS,
        ),
        _entry(
            camera_topic,
            f"/{ns}/front_camera_color/color/image_raw",
            "sensor_msgs/msg/Image",
            "gz.msgs.Image",
            GZ_TO_ROS,
        ),
        _entry(
            f"{cam_ns}/camera_info",
            f"/{ns}/front_camera_color/color/camera_info",
            "sensor_msgs/msg/CameraInfo",
            "gz.msgs.CameraInfo",
            GZ_TO_ROS,
        ),
    ]
    return [e for e in entries if odom_tf or e["ros_topic_name"] != "/tf"]


def namespaced(params: dict, ns: str) -> dict:
    """Nest a Nav2 parameter file's node keys under ``ns`` (like nav2's RewrittenYaml root_key)."""
    return {ns: params} if ns else params
