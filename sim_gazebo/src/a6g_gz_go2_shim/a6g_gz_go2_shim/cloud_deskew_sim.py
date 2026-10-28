"""cloud_deskew_sim: the L1 cloud as the dock publishes it (``/utlidar/cloud_deskewed``).

Gazebo emits the cloud in the sensor frame; the dock's deskewed cloud is expressed in
``target_frame`` (``odom`` by default; the frame_id on the dock is assumed).
Simulation only.
"""

import rclpy
import rclpy.executors
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import PointCloud2
from tf2_ros import Buffer, TransformException, TransformListener

from a6g_gz_go2_shim.logic import transform_points
from a6g_gz_go2_shim.tf_util import make_cloud, matrix, xyz


class CloudDeskewSim(Node):
    def __init__(self) -> None:
        super().__init__("cloud_deskew_sim")
        self.target = self.declare_parameter("target_frame", "odom").value
        self.buffer = Buffer()
        self.listener = TransformListener(
            self.buffer, None, spin_thread=True
        )  # own node and thread
        self.pub = self.create_publisher(PointCloud2, "/utlidar/cloud_deskewed", 5)
        self.create_subscription(PointCloud2, "cloud_raw", self._on_cloud, qos_profile_sensor_data)

    def _on_cloud(self, cloud: PointCloud2) -> None:
        try:
            t = self.buffer.lookup_transform(
                self.target,
                cloud.header.frame_id,
                Time.from_msg(cloud.header.stamp),
                timeout=Duration(seconds=0.1),
            )
        except TransformException as exc:
            self.get_logger().warn(f"no transform for cloud: {exc}", throttle_duration_sec=5.0)
            return
        header = cloud.header
        header.frame_id = self.target
        self.pub.publish(make_cloud(header, transform_points(matrix(t), xyz(cloud))))


def main() -> None:
    rclpy.init()
    node = CloudDeskewSim()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    except Exception:
        if rclpy.ok():  # otherwise a shutdown race inside spin()
            raise
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
