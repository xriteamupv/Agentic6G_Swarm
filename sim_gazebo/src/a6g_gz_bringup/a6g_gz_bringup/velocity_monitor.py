"""velocity_monitor: ground-truth base-frame velocity statistics for the whole run.

The Summit may reverse only within its manifest ``reverse_limits`` (0.15 m/s, 0.5 m per
move): ``below_threshold`` counts samples faster backwards than ``reverse_threshold``
(cap + physics margin), and every continuous reverse move is measured; moves longer than
``reverse_max_m`` count in ``reverse_too_long``. Gazebo's OdometryPublisher twist is in the
body frame. Statistics are published (latched JSON) on ``~/stats`` and written to
``output_file`` on shutdown.
"""

import json

import rclpy
import rclpy.executors
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import String

#: Below this body-frame vx the base is moving backwards (physics jitter is ~0.005 m/s).
REVERSING_VX = -0.02


class VelocityMonitor(Node):
    def __init__(self) -> None:
        super().__init__("velocity_monitor")
        self.threshold = float(self.declare_parameter("reverse_threshold", -0.18).value)
        self.reverse_max = float(self.declare_parameter("reverse_max_m", 0.6).value)
        self.output = self.declare_parameter("output_file", "").value
        self.stats = {
            "samples": 0,
            "min_vx": 0.0,
            "max_vx": 0.0,
            "below_threshold": 0,
            "threshold": self.threshold,
            "first_violation_t": None,
            "reverse_moves": 0,
            "reverse_total_m": 0.0,
            "longest_reverse_m": 0.0,
            "reverse_too_long": 0,
            "reverse_max_m": self.reverse_max,
        }
        self._last_t: float | None = None
        self._segment = 0.0  # current continuous reverse move (m)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.pub = self.create_publisher(String, "~/stats", latched)
        self.create_subscription(Odometry, "ground_truth", self._on_truth, 100)
        self.create_timer(1.0, self._publish)

    def _on_truth(self, gt: Odometry) -> None:
        vx = gt.twist.twist.linear.x
        s = self.stats
        s["samples"] += 1
        t = gt.header.stamp.sec + gt.header.stamp.nanosec * 1e-9
        dt = 0.0 if self._last_t is None else max(0.0, min(t - self._last_t, 0.5))
        self._last_t = t
        if vx < REVERSING_VX:
            if self._segment == 0.0:
                s["reverse_moves"] += 1
            step = -vx * dt
            self._segment += step
            s["reverse_total_m"] = round(s["reverse_total_m"] + step, 3)
            s["longest_reverse_m"] = round(max(s["longest_reverse_m"], self._segment), 3)
        elif self._segment:
            if self._segment > self.reverse_max:
                s["reverse_too_long"] += 1
                self.get_logger().error(
                    f"reverse move of {self._segment:.2f} m > {self.reverse_max} m"
                )
            self._segment = 0.0
        s["min_vx"] = min(s["min_vx"], vx)
        s["max_vx"] = max(s["max_vx"], vx)
        if vx < self.threshold:
            s["below_threshold"] += 1
            if s["first_violation_t"] is None:
                s["first_violation_t"] = gt.header.stamp.sec + gt.header.stamp.nanosec * 1e-9
                self.get_logger().error(f"base-frame vx {vx:.3f} m/s < {self.threshold}")

    def _publish(self) -> None:
        self.pub.publish(String(data=json.dumps(self.stats)))

    def write(self) -> None:
        if self._segment > self.reverse_max:  # a reverse move still under way at shutdown
            self.stats["reverse_too_long"] += 1
            self._segment = 0.0
        if self.output:
            with open(self.output, "w") as f:
                json.dump(self.stats, f, indent=1)


def main() -> None:
    rclpy.init()
    node = VelocityMonitor()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    except Exception:
        if rclpy.ok():  # otherwise a shutdown race inside spin()
            raise
    finally:
        node.write()
        node.destroy_node()
        rclpy.try_shutdown()
