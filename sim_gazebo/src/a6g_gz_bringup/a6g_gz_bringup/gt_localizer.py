"""gt_localizer: the ``ground_truth`` localization profile (SPEC A1 8; debugging only).

Publishes ``map -> odom`` so that ``map -> base`` equals Gazebo's ground truth (the Gazebo
world frame is ``campus_map`` and the maps are generated in it). Also publishes the pose on
``amcl_pose`` so consumers see the same interface as with AMCL. Refused by the profile loader
under ``profile: real``.
"""

import math

import rclpy
import rclpy.executors
from geometry_msgs.msg import PoseWithCovarianceStamped, TransformStamped
from nav_msgs.msg import Odometry
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.time import Time
from tf2_ros import Buffer, TransformBroadcaster, TransformException, TransformListener

from a6g_gz_bringup.frames import Pose2, map_to_odom, yaw_of


class GtLocalizer(Node):
    def __init__(self) -> None:
        super().__init__("gt_localizer")
        self.map_frame = self.declare_parameter("map_frame", "map").value
        self.odom_frame = self.declare_parameter("odom_frame", "odom").value
        self.base_frame = self.declare_parameter("base_frame", "base_link").value
        self.tolerance = Duration(
            seconds=float(self.declare_parameter("transform_tolerance", 0.2).value)
        )
        self.buffer = Buffer()
        self.listener = TransformListener(
            self.buffer, None, spin_thread=True
        )  # own node and thread
        self.tf = TransformBroadcaster(self)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.pose_pub = self.create_publisher(PoseWithCovarianceStamped, "amcl_pose", latched)
        self.count = 0
        self.create_subscription(Odometry, "ground_truth", self._on_truth, 50)

    def _on_truth(self, gt: Odometry) -> None:
        try:
            t = self.buffer.lookup_transform(
                self.odom_frame, self.base_frame, Time.from_msg(gt.header.stamp)
            )
        except TransformException:
            try:
                t = self.buffer.lookup_transform(self.odom_frame, self.base_frame, Time())
            except TransformException:
                return
        p, q = gt.pose.pose.position, gt.pose.pose.orientation
        world_base = Pose2(p.x, p.y, yaw_of(q))
        tr = t.transform.translation
        odom_base = Pose2(tr.x, tr.y, yaw_of(t.transform.rotation))
        m = map_to_odom(world_base, odom_base)
        out = TransformStamped()
        out.header.stamp = (Time.from_msg(gt.header.stamp) + self.tolerance).to_msg()
        out.header.frame_id = self.map_frame
        out.child_frame_id = self.odom_frame
        out.transform.translation.x, out.transform.translation.y = m.x, m.y
        out.transform.rotation.z = math.sin(m.yaw / 2)
        out.transform.rotation.w = math.cos(m.yaw / 2)
        self.tf.sendTransform(out)
        self.count += 1
        if self.count % 10 == 0:  # 5 Hz
            pose = PoseWithCovarianceStamped()
            pose.header.stamp = gt.header.stamp
            pose.header.frame_id = self.map_frame
            pose.pose.pose = gt.pose.pose
            pose.pose.covariance[0] = pose.pose.covariance[7] = 1e-4
            pose.pose.covariance[35] = 1e-4
            self.pose_pub.publish(pose)


def main() -> None:
    rclpy.init()
    node = GtLocalizer()
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
