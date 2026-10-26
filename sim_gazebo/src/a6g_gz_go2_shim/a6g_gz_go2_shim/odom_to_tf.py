"""odom_to_tf (re-implementation; TODO(UPV): replace with go2_nav's if it builds on Jazzy).

Republishes the robot's odometry (``/utlidar/robot_odom``) as ``/odom`` and as the
``odom -> base_link`` transform.
"""

import rclpy
import rclpy.executors
from geometry_msgs.msg import TransformStamped
from nav_msgs.msg import Odometry
from rclpy.node import Node
from tf2_ros import TransformBroadcaster


class OdomToTf(Node):
    def __init__(self) -> None:
        super().__init__("odom_to_tf")
        self.odom_frame = self.declare_parameter("odom_frame", "odom").value
        self.base_frame = self.declare_parameter("base_frame", "base_link").value
        # False when a local EKF owns odom -> base_link (GNSS localization).
        self.publish_tf = bool(self.declare_parameter("publish_tf", True).value)
        self.tf = TransformBroadcaster(self)
        self.pub = self.create_publisher(Odometry, "/odom", 10)
        self.create_subscription(Odometry, "/utlidar/robot_odom", self._on_odom, 20)

    def _on_odom(self, msg: Odometry) -> None:
        msg.header.frame_id = self.odom_frame
        msg.child_frame_id = self.base_frame
        t = TransformStamped()
        t.header = msg.header
        t.child_frame_id = self.base_frame
        p = msg.pose.pose.position
        t.transform.translation.x, t.transform.translation.y, t.transform.translation.z = (
            p.x,
            p.y,
            p.z,
        )
        t.transform.rotation = msg.pose.pose.orientation
        if self.publish_tf:
            self.tf.sendTransform(t)
        self.pub.publish(msg)


def main() -> None:
    rclpy.init()
    node = OdomToTf()
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
