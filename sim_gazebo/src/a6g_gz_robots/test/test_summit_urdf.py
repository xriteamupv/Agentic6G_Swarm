import xml.etree.ElementTree as ET

import pytest
from a6g_gz_robots.summit import adapt

URDF = """<robot name="summit_xl">
  <link name="robot_base_footprint"/>
  <link name="robot_base_link"/>
  {joints}
  <gazebo>
    <plugin filename="gz_ros2_control-system" name="gz_ros2_control::GazeboSimROS2ControlPlugin"/>
  </gazebo>
  <ros2_control name="GazeboSystem" type="system"/>
  <gazebo reference="robot_cam">
    <sensor name="cam" type="camera">
      <camera><image><width>1920</width><height>1080</height></image></camera>
      <update_rate>30</update_rate>
      <topic>
        robot/cam/image_raw
      </topic>
    </sensor>
  </gazebo>
  <gazebo reference="robot_lidar">
    <sensor name="top" type="gpu_lidar">
      <lidar><scan><horizontal><samples>1800</samples></horizontal></scan></lidar>
    </sensor>
  </gazebo>
</robot>"""
JOINT = '<joint name="robot_{w}_wheel_joint" type="continuous"><origin xyz="{x} {y} 0"/></joint>'


def make(wheels=("front_left", "back_left", "front_right", "back_right")):
    pos = {
        "front_left": (0.229, 0.235),
        "back_left": (-0.229, 0.235),
        "front_right": (0.229, -0.235),
        "back_right": (-0.229, -0.235),
    }
    joints = "".join(JOINT.format(w=w, x=pos[w][0], y=pos[w][1]) for w in wheels)
    return URDF.format(joints=joints)


def test_adapt_swaps_control_and_caps_sensors():
    root = ET.fromstring(adapt(make(), model_name="summit_01"))
    assert root.find("ros2_control") is None
    plugins = {p.get("name"): p for p in root.iter("plugin")}
    assert not any("ros2_control" in n for n in plugins)
    dd = plugins["gz::sim::systems::DiffDrive"]
    assert [e.text for e in dd.findall("left_joint")] == [
        "robot_front_left_wheel_joint",
        "robot_back_left_wheel_joint",
    ]
    assert float(dd.find("wheel_separation").text) == pytest.approx(0.47)
    assert dd.find("topic").text == "/model/summit_01/cmd_vel"
    assert dd.find("child_frame_id").text == "robot_base_footprint"
    gt = plugins["gz::sim::systems::OdometryPublisher"]
    assert gt.find("odom_topic").text == "/model/summit_01/odometry"
    cam = next(s for s in root.iter("sensor") if s.get("type") == "camera")
    assert cam.find("camera/image/width").text == "640"
    assert cam.find("update_rate").text == "15.0"
    assert cam.find("topic").text == "robot/cam/image_raw"
    lidar = next(s for s in root.iter("sensor") if s.get("type") == "gpu_lidar")
    assert lidar.find("lidar/scan/horizontal/samples").text == "900"


def test_missing_wheel_is_an_error():
    with pytest.raises(ValueError, match="back_right"):
        adapt(make(wheels=("front_left", "back_left", "front_right")), model_name="s")
