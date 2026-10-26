"""Robotnik's rbsummit description, adapted for the shared Gazebo server (SPEC A1 7.4).

Robotnik's simulation drives the base with ``robotnik_controllers`` (not distributed in
``~/robotnik_ws``) through ``gz_ros2_control``, which would also put a ROS node inside the
Gazebo server's ROS domain (Rule 17). This adapter keeps Robotnik's links, meshes and
sensors and instead:

- removes ``gz_ros2_control`` and the ``<ros2_control>`` block;
- drives the four wheels with Gazebo's DiffDrive (skid steer), odometry on
  ``/model/<name>/odometry_wheels`` (``robot_odom`` -> ``robot_base_footprint``) and the
  command on ``/model/<name>/cmd_vel``;
- adds JointStatePublisher and a ground-truth OdometryPublisher (``/model/<name>/odometry``);
- caps sensor cost for CPU rendering: camera ``camera_size`` at ``camera_rate_hz`` and
  ``lidar_samples`` horizontal samples on the 3D LiDAR;
- IMU orientation in ENU; NavSat noise-free at ``gnss_rate_hz`` (noise is added per GNSS
  profile by ``a6g_gz_bringup/gnss_noise_sim``).
"""

import xml.etree.ElementTree as ET

WHEELS = ("front_left", "back_left", "front_right", "back_right")


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrib: str) -> ET.Element:
    e = ET.SubElement(parent, tag, attrib)
    if text is not None:
        e.text = text
    return e


def adapt(
    urdf: str,
    *,
    model_name: str,
    prefix: str = "robot_",
    wheel_separation: float | None = None,
    wheel_radius: float = 0.11,
    max_velocity: float = 1.0,
    max_acceleration: float = 1.0,
    camera_size: tuple[int, int] = (640, 480),
    camera_rate_hz: float = 15.0,
    lidar_samples: int = 900,
    gnss_rate_hz: float = 5.0,
) -> str:
    root = ET.fromstring(urdf)
    for parent in list(root.iter()):
        for child in list(parent):
            if child.tag == "ros2_control" or (
                child.tag == "plugin" and "gz_ros2_control" in (child.get("filename") or "")
            ):
                parent.remove(child)
    # Drop now-empty <gazebo> blocks.
    for g in [g for g in root.findall("gazebo") if len(g) == 0 and not (g.text or "").strip()]:
        root.remove(g)

    joints = {j.get("name"): j for j in root.findall("joint")}
    names = {w: f"{prefix}{w}_wheel_joint" for w in WHEELS}
    missing = [n for n in names.values() if n not in joints]
    if missing:
        raise ValueError(f"wheel joints not found in the description: {missing}")
    if wheel_separation is None:
        ys = [float(joints[names[w]].find("origin").get("xyz").split()[1]) for w in WHEELS]
        wheel_separation = max(ys) - min(ys)

    for sensor in root.iter("sensor"):
        if sensor.get("type") == "camera":
            img = sensor.find("camera/image")
            if img is not None:
                img.find("width").text = str(camera_size[0])
                img.find("height").text = str(camera_size[1])
            rate = sensor.find("update_rate")
            if rate is not None:
                rate.text = str(camera_rate_hz)
        if sensor.get("type") in ("gpu_lidar", "lidar"):
            h = sensor.find("lidar/scan/horizontal/samples")
            if h is not None:
                h.text = str(lidar_samples)
        if sensor.get("type") == "imu":
            imu = sensor.find("imu")
            if imu is None:
                imu = ET.SubElement(sensor, "imu")
            ref = imu.find("orientation_reference_frame")
            if ref is None:
                ref = ET.SubElement(imu, "orientation_reference_frame")
                _sub(ref, "localization", "ENU")
        if sensor.get("type") == "navsat":
            # Noise-free: a6g_gz_bringup/gnss_noise_sim applies the profile's GNSS noise.
            navsat = sensor.find("navsat")
            if navsat is not None:
                sensor.remove(navsat)
            rate = sensor.find("update_rate")
            if rate is not None:
                rate.text = str(gnss_rate_hz)
        # Robotnik pads some <topic> values with whitespace; gz takes them literally.
        for tag in ("topic", "camera_info_topic"):
            e = sensor.find(tag)
            if e is not None and e.text:
                e.text = e.text.strip()

    g = _sub(root, "gazebo")
    dd = _sub(g, "plugin", filename="gz-sim-diff-drive-system", name="gz::sim::systems::DiffDrive")
    for w in ("front_left", "back_left"):
        _sub(dd, "left_joint", names[w])
    for w in ("front_right", "back_right"):
        _sub(dd, "right_joint", names[w])
    for tag, value in (
        ("wheel_separation", f"{wheel_separation:.4f}"),
        ("wheel_radius", f"{wheel_radius:.4f}"),
        ("odom_publish_frequency", "50"),
        ("topic", f"/model/{model_name}/cmd_vel"),
        ("odom_topic", f"/model/{model_name}/odometry_wheels"),
        ("tf_topic", f"/model/{model_name}/tf_wheels"),
        ("frame_id", f"{prefix}odom"),
        ("child_frame_id", f"{prefix}base_footprint"),
        ("min_velocity", f"{-max_velocity}"),
        ("max_velocity", f"{max_velocity}"),
        ("min_acceleration", f"{-max_acceleration}"),
        ("max_acceleration", f"{max_acceleration}"),
    ):
        _sub(dd, tag, value)
    js = _sub(
        g,
        "plugin",
        filename="gz-sim-joint-state-publisher-system",
        name="gz::sim::systems::JointStatePublisher",
    )
    _sub(js, "topic", f"/model/{model_name}/joint_states")
    gt = _sub(
        g,
        "plugin",
        filename="gz-sim-odometry-publisher-system",
        name="gz::sim::systems::OdometryPublisher",
    )
    for tag, value in (
        ("odom_frame", "world"),
        ("robot_base_frame", f"{prefix}base_footprint"),
        ("dimensions", "3"),
        ("odom_publish_frequency", "50"),
        ("odom_topic", f"/model/{model_name}/odometry"),
    ):
        _sub(gt, tag, value)
    return ET.tostring(root, encoding="unicode")
