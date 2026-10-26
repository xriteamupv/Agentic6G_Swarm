"""cmd_vel_to_sport_sim: the real cmd_vel_to_sport contract, driving the Gazebo proxy.

Subscribes ``cmd_vel`` (from Nav2) and publishes the executed velocity on ``proxy_cmd_vel``
(bridged to the model's VelocityControl) at ``rate_hz``. ``stop`` (from sport_api_sim's
StopMove) returns the proxy to standstill.
"""

import rclpy
import rclpy.executors
from geometry_msgs.msg import Twist
from rclpy.node import Node
from std_msgs.msg import Empty

from a6g_gz_go2_shim.logic import SportCommandFilter, SportLimits


class CmdVelToSportSim(Node):
    def __init__(self) -> None:
        super().__init__("cmd_vel_to_sport")
        d = SportLimits()
        limits = SportLimits(
            **{
                name: float(self.declare_parameter(name, getattr(d, name)).value)
                for name in (
                    "vx_max",
                    "wz_max",
                    "static_walk_wz_max",
                    "static_walk_vx_threshold",
                    "timeout_s",
                    "settle_s",
                )
            }
        )
        rate = float(self.declare_parameter("rate_hz", 20.0).value)
        self.filter = SportCommandFilter(limits)
        self.pub = self.create_publisher(Twist, "proxy_cmd_vel", 10)
        self.create_subscription(Twist, "cmd_vel", self._on_cmd, 10)
        self.create_subscription(Empty, "stop", self._on_stop, 10)
        self.create_timer(1.0 / rate, self._tick)

    def _now(self) -> float:
        return self.get_clock().now().nanoseconds * 1e-9

    def _on_cmd(self, msg: Twist) -> None:
        self.filter.command(msg.linear.x, msg.linear.y, msg.angular.z, self._now())

    def _on_stop(self, _msg: Empty) -> None:
        self.filter.stop()
        self.pub.publish(Twist())

    def _tick(self) -> None:
        vx, vy, wz = self.filter.output(self._now())
        out = Twist()
        out.linear.x, out.linear.y, out.angular.z = vx, vy, wz
        self.pub.publish(out)


def main() -> None:
    rclpy.init()
    node = CmdVelToSportSim()
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
