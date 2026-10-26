"""twist_mux_lite: the Summit's twist_mux priorities, for simulation.

The real Summit runs twist_mux (``ros-jazzy-twist-mux`` is not installed here). Inputs are
given as parallel lists ``topics`` / ``priorities`` / ``timeouts``; the highest-priority
input that published within its timeout wins and is forwarded to ``cmd_vel_out``. The
manifest's stop path (zero twist on ``web_teleop/cmd_vel``, priority 40) overrides Nav2.
"""

import rclpy
import rclpy.executors
from geometry_msgs.msg import Twist
from rclpy.node import Node

from a6g_gz_bringup.mux import PriorityMux


class TwistMuxLite(Node):
    def __init__(self) -> None:
        super().__init__("twist_mux")
        topics = list(self.declare_parameter("topics", ["cmd_vel"]).value)
        prios = [int(p) for p in self.declare_parameter("priorities", [10]).value]
        timeouts = [float(t) for t in self.declare_parameter("timeouts", [0.5]).value]
        if not (len(topics) == len(prios) == len(timeouts)):
            raise ValueError("topics, priorities and timeouts must have the same length")
        self.mux = PriorityMux(
            dict(zip(topics, prios, strict=True)), dict(zip(topics, timeouts, strict=True))
        )
        self.pub = self.create_publisher(Twist, "cmd_vel_out", 10)
        for topic in topics:
            self.create_subscription(Twist, topic, lambda m, t=topic: self._on(t, m), 10)

    def _on(self, topic: str, msg: Twist) -> None:
        if self.mux.accept(topic, self.get_clock().now().nanoseconds * 1e-9):
            self.pub.publish(msg)


def main() -> None:
    rclpy.init()
    node = TwistMuxLite()
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
