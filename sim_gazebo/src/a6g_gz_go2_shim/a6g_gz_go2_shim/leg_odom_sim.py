"""leg_odom_sim: the Go2's own odometry (``/utlidar/robot_odom``) with leg-odometry drift.

Integrates the true body twist from Gazebo's ground truth (``ground_truth``) with a scale
error, a yaw-rate bias and noise (parameters; mild by default). Starts at the ``odom``
origin, like the real robot after power-up. Simulation only.
"""

import rclpy
import rclpy.executors
from nav_msgs.msg import Odometry
from rclpy.node import Node

from a6g_gz_go2_shim.logic import LegOdometry, quat_from_yaw


class LegOdomSim(Node):
    def __init__(self) -> None:
        super().__init__("leg_odom_sim")
        d = LegOdometry()
        self.odom = LegOdometry(
            linear_scale_error=float(
                self.declare_parameter("linear_scale_error", d.linear_scale_error).value
            ),
            yaw_rate_bias=float(self.declare_parameter("yaw_rate_bias", d.yaw_rate_bias).value),
            linear_noise=float(self.declare_parameter("linear_noise", d.linear_noise).value),
            angular_noise=float(self.declare_parameter("angular_noise", d.angular_noise).value),
            seed=int(self.declare_parameter("seed", 0).value),
        )
        self.odom_frame = self.declare_parameter("odom_frame", "odom").value
        self.base_frame = self.declare_parameter("base_frame", "base_link").value
        self.last_t: float | None = None
        self.pub = self.create_publisher(Odometry, "/utlidar/robot_odom", 20)
        self.create_subscription(Odometry, "ground_truth", self._on_truth, 50)

    def _on_truth(self, gt: Odometry) -> None:
        t = gt.header.stamp.sec + gt.header.stamp.nanosec * 1e-9
        dt = 0.0 if self.last_t is None else max(0.0, min(0.2, t - self.last_t))
        self.last_t = t
        v, w = gt.twist.twist.linear, gt.twist.twist.angular
        vx, vy, wz = self.odom.step(v.x, v.y, w.z, dt)
        out = Odometry()
        out.header.stamp = gt.header.stamp
        out.header.frame_id = self.odom_frame
        out.child_frame_id = self.base_frame
        out.pose.pose.position.x = self.odom.x
        out.pose.pose.position.y = self.odom.y
        out.pose.pose.position.z = gt.pose.pose.position.z
        q = quat_from_yaw(self.odom.yaw)
        o = out.pose.pose.orientation
        o.x, o.y, o.z, o.w = q
        out.twist.twist.linear.x, out.twist.twist.linear.y = vx, vy
        out.twist.twist.angular.z = wz
        self.pub.publish(out)


def main() -> None:
    rclpy.init()
    node = LegOdomSim()
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
