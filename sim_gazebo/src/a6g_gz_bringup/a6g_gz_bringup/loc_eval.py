"""loc_eval: localization error against ground truth, logged to CSV for every run.

Every ``period_s`` it compares ``map_frame -> base_frame`` (the localization estimate) with
Gazebo's ground truth (``map`` == ``campus_map`` == Gazebo world).
"""

import csv
import math
import os
import time

import rclpy
import rclpy.executors
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.time import Time
from tf2_ros import Buffer, TransformException, TransformListener

from a6g_gz_bringup.frames import wrap, yaw_of


class LocEval(Node):
    def __init__(self) -> None:
        super().__init__("loc_eval")
        self.map_frame = self.declare_parameter("map_frame", "map").value
        self.base_frame = self.declare_parameter("base_frame", "base_link").value
        robot = self.declare_parameter("robot_id", "robot").value
        loc = self.declare_parameter("localization", "unknown").value
        out_dir = self.declare_parameter("output_dir", "").value or os.path.expanduser(
            "~/.ros/a6g_loc_eval"
        )
        os.makedirs(out_dir, exist_ok=True)
        self.path = os.path.join(out_dir, f"{robot}_{loc}_{time.strftime('%Y%m%dT%H%M%S')}.csv")
        self.file = open(self.path, "w", newline="")  # noqa: SIM115 (closed in close())
        self.csv = csv.writer(self.file)
        self.csv.writerow(
            ["t", "gt_x", "gt_y", "gt_yaw", "est_x", "est_y", "est_yaw", "err_xy", "err_yaw"]
        )
        self.gt: Odometry | None = None
        self.n, self.sum_err, self.max_err = 0, 0.0, 0.0
        self.buffer = Buffer()
        self.listener = TransformListener(
            self.buffer, None, spin_thread=True
        )  # own node and thread
        self.create_subscription(Odometry, "ground_truth", self._on_truth, 10)
        period = float(self.declare_parameter("period_s", 0.5).value)
        self.create_timer(period, self._tick)
        self.get_logger().info(f"logging localization error to {self.path}")

    def _on_truth(self, gt: Odometry) -> None:
        self.gt = gt

    def _tick(self) -> None:
        if self.gt is None:
            return
        gt = self.gt
        try:
            t = self.buffer.lookup_transform(self.map_frame, self.base_frame, Time())
        except TransformException:
            return
        gx, gy = gt.pose.pose.position.x, gt.pose.pose.position.y
        gyaw = yaw_of(gt.pose.pose.orientation)
        ex, ey = t.transform.translation.x, t.transform.translation.y
        eyaw = yaw_of(t.transform.rotation)
        err = math.hypot(ex - gx, ey - gy)
        stamp = gt.header.stamp.sec + gt.header.stamp.nanosec * 1e-9
        self.csv.writerow(
            [f"{stamp:.3f}"]
            + [f"{v:.4f}" for v in (gx, gy, gyaw, ex, ey, eyaw, err, abs(wrap(eyaw - gyaw)))]
        )
        self.n += 1
        self.sum_err += err
        self.max_err = max(self.max_err, err)

    def close(self) -> None:
        self.file.close()
        if self.n:
            self.get_logger().info(
                f"{self.path}: {self.n} samples, mean error {self.sum_err / self.n:.3f} m, "
                f"max {self.max_err:.3f} m"
            )


def main() -> None:
    rclpy.init()
    node = LocEval()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    except Exception:
        if rclpy.ok():  # otherwise a shutdown race inside spin()
            raise
    finally:
        node.close()
        node.destroy_node()
        rclpy.try_shutdown()
