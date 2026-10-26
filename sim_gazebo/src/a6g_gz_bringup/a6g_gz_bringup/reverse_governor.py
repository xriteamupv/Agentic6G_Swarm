"""reverse_governor: enforce the Summit's reverse_limits between twist_mux and the base.

Subscribes ``cmd_vel_in`` (Twist) and ``odom`` (the base's wheel odometry, body-frame
twist); publishes ``cmd_vel_out``. See ``a6g_gz_bringup.reverse`` for the rules.
"""

import rclpy
import rclpy.executors
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from rclpy.node import Node

from a6g_gz_bringup.reverse import ReverseGovernor


class ReverseGovernorNode(Node):
    def __init__(self) -> None:
        super().__init__("reverse_governor")
        self.gov = ReverseGovernor(
            max_speed_mps=float(self.declare_parameter("max_speed_mps", 0.15).value),
            max_distance_m=float(self.declare_parameter("max_distance_m", 0.5).value),
            rearm_forward_m=float(self.declare_parameter("rearm_forward_m", 0.2).value),
        )
        self._last_t: float | None = None
        self._was_locked = False
        self.pub = self.create_publisher(Twist, "cmd_vel_out", 10)
        self.create_subscription(Twist, "cmd_vel_in", self._on_cmd, 10)
        self.create_subscription(Odometry, "odom", self._on_odom, 50)

    def _on_odom(self, msg: Odometry) -> None:
        t = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
        dt = 0.0 if self._last_t is None else max(0.0, min(t - self._last_t, 0.5))
        self._last_t = t
        self.gov.on_odom(msg.twist.twist.linear.x, dt)
        if self.gov.locked and not self._was_locked:
            self.get_logger().warning(
                f"reversed {self.gov.reversed_m:.2f} m: further reverse blocked until the "
                f"robot drives {self.gov.rearm_forward_m} m forward"
            )
        self._was_locked = self.gov.locked

    def _on_cmd(self, msg: Twist) -> None:
        msg.linear.x = self.gov.filter(msg.linear.x)
        self.pub.publish(msg)


def main() -> None:
    rclpy.init()
    node = ReverseGovernorNode()
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
