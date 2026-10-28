"""cloud_to_scan (re-implementation of go2_nav's, which may not build on Jazzy).

``/utlidar/cloud_deskewed`` -> ``/scan`` in ``base_link``: points between ``min_height``
(-0.15 m) and ``max_height`` (+0.30 m) binned at 0.5 deg over 360 deg, self-hits removed.
"""

import math

import rclpy
import rclpy.executors
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import LaserScan, PointCloud2
from tf2_ros import Buffer, TransformException, TransformListener

from a6g_gz_go2_shim.logic import ScanBinning, transform_points
from a6g_gz_go2_shim.tf_util import matrix, xyz


class CloudToScan(Node):
    def __init__(self) -> None:
        super().__init__("cloud_to_scan")
        d = ScanBinning()
        self.binning = ScanBinning(
            min_height=float(self.declare_parameter("min_height", d.min_height).value),
            max_height=float(self.declare_parameter("max_height", d.max_height).value),
            angle_increment=math.radians(
                float(self.declare_parameter("angle_increment_deg", 0.5).value)
            ),
            range_min=float(self.declare_parameter("range_min", d.range_min).value),
            range_max=float(self.declare_parameter("range_max", d.range_max).value),
        )
        self.frame = self.declare_parameter("target_frame", "base_link").value
        self.scan_time = 1.0 / float(self.declare_parameter("cloud_rate_hz", 10.0).value)
        self.buffer = Buffer()
        self.listener = TransformListener(
            self.buffer, None, spin_thread=True
        )  # own node and thread
        self.pub = self.create_publisher(LaserScan, "/scan", qos_profile_sensor_data)
        self.create_subscription(
            PointCloud2, "/utlidar/cloud_deskewed", self._on_cloud, qos_profile_sensor_data
        )

    def _on_cloud(self, cloud: PointCloud2) -> None:
        try:
            t = self.buffer.lookup_transform(
                self.frame,
                cloud.header.frame_id,
                Time.from_msg(cloud.header.stamp),
                timeout=Duration(seconds=0.1),
            )
        except TransformException as exc:
            self.get_logger().warn(f"no transform for cloud: {exc}", throttle_duration_sec=5.0)
            return
        b = self.binning
        scan = LaserScan()
        scan.header.stamp = cloud.header.stamp
        scan.header.frame_id = self.frame
        scan.angle_min = b.angle_min
        scan.angle_increment = b.angle_increment
        scan.angle_max = b.angle_min + (b.n_bins - 1) * b.angle_increment
        scan.scan_time = self.scan_time
        scan.range_min = b.range_min
        scan.range_max = b.range_max
        scan.ranges = b.ranges(transform_points(matrix(t), xyz(cloud))).tolist()
        self.pub.publish(scan)


def main() -> None:
    rclpy.init()
    node = CloudToScan()
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
