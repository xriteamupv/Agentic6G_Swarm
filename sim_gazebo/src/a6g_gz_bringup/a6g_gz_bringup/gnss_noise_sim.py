"""gnss_noise_sim: the simulated receiver.

Gazebo's NavSat is noise-free (``raw``); this node adds the profile's error and publishes
the fix on the robot's GNSS topic (``fix``) with a matching covariance. While dropped, no
fix is published. Coverage shadows (JSON list of campus_map polygons) drop the fix only
with ``dropout_in_shadows``; the true position comes from ``ground_truth``.
"""

import json

import rclpy
import rclpy.executors
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import NavSatFix, NavSatStatus

from a6g_gz_bringup.gnss import GnssNoiseModel, offset_latlon


class GnssNoiseSim(Node):
    def __init__(self) -> None:
        super().__init__("gnss_noise_sim")
        p = {
            name: self.declare_parameter(name, default).value
            for name, default in (
                ("sigma_m", 0.02),
                ("bias_sigma_m", 0.0),
                ("bias_tau_s", 300.0),
                ("dropout_per_min", 0.0),
                ("dropout_s", 0.0),
                ("seed", 0),
                ("dropout_in_shadows", False),
                ("shadows_json", "[]"),
                ("profile", "rtk"),
            )
        }
        self.model = GnssNoiseModel(
            sigma_m=float(p["sigma_m"]),
            bias_sigma_m=float(p["bias_sigma_m"]),
            bias_tau_s=float(p["bias_tau_s"]),
            dropout_per_min=float(p["dropout_per_min"]),
            dropout_s=float(p["dropout_s"]),
            seed=int(p["seed"]),
            shadows=[[tuple(v) for v in poly] for poly in json.loads(p["shadows_json"])],
            dropout_in_shadows=bool(p["dropout_in_shadows"]),
        )
        self.xy = (0.0, 0.0)
        self.pub = self.create_publisher(NavSatFix, "fix", 10)
        self.create_subscription(NavSatFix, "raw", self._on_fix, qos_profile_sensor_data)
        self.create_subscription(Odometry, "ground_truth", self._on_truth, 10)
        self.get_logger().info(
            f"GNSS noise profile {p['profile']}: sigma {self.model.sigma_m} m, "
            f"bias {self.model.bias_sigma_m} m (tau {self.model.bias_tau_s} s)"
        )

    def _on_truth(self, msg: Odometry) -> None:
        self.xy = (msg.pose.pose.position.x, msg.pose.pose.position.y)

    def _on_fix(self, raw: NavSatFix) -> None:
        t = raw.header.stamp.sec + raw.header.stamp.nanosec * 1e-9
        err = self.model.step(t, *self.xy)
        if err is None:
            return
        fix = NavSatFix()
        fix.header = raw.header
        fix.status.status = NavSatStatus.STATUS_FIX
        fix.status.service = NavSatStatus.SERVICE_GPS
        fix.latitude, fix.longitude = offset_latlon(raw.latitude, raw.longitude, *err)
        fix.altitude = raw.altitude
        var = max(self.model.variance_m2, 1e-4)
        fix.position_covariance = [var, 0.0, 0.0, 0.0, var, 0.0, 0.0, 0.0, 4 * var]
        fix.position_covariance_type = NavSatFix.COVARIANCE_TYPE_DIAGONAL_KNOWN
        self.pub.publish(fix)


def main() -> None:
    rclpy.init()
    node = GnssNoiseSim()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    except Exception:
        if rclpy.ok():
            raise
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
