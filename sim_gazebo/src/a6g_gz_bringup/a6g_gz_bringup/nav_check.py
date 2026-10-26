"""nav_check: send one Nav2 goal from the command line and check the outcome (SPEC A1 A2).

    ros2 run a6g_gz_bringup nav_check --goal 30 0 [--ns /robot] [--frame robot_map]

Waits for Nav2 and localization, sends ``NavigateToPose``, follows the ground truth and
prints a JSON summary: Nav2 result, straight-line goal distance from the start, final
ground-truth error, and (when available) the velocity monitor's statistics and the
sport_api_sim violation count. Exit status 0 only if every check passes.
"""

import argparse
import json
import math
import sys
import time

import rclpy
from action_msgs.msg import GoalStatus
from lifecycle_msgs.msg import State
from lifecycle_msgs.srv import GetState
from nav2_msgs.action import NavigateToPose
from nav_msgs.msg import Odometry
from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.time import Time
from std_msgs.msg import String, UInt32
from tf2_ros import Buffer, TransformException, TransformListener

from a6g_gz_bringup.frames import yaw_of

STATUS = {
    GoalStatus.STATUS_SUCCEEDED: "succeeded",
    GoalStatus.STATUS_ABORTED: "aborted",
    GoalStatus.STATUS_CANCELED: "canceled",
}


def _ns(ns: str, name: str) -> str:
    return f"{ns.rstrip('/')}/{name}" if ns else f"/{name}"


class NavCheck(Node):
    def __init__(self, a: argparse.Namespace) -> None:
        super().__init__("nav_check", parameter_overrides=[Parameter("use_sim_time", value=True)])
        self.a = a
        self.gt: Odometry | None = None
        self.start: Odometry | None = None
        self.velocity: dict | None = None
        self.violations: int | None = None
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(Odometry, a.ground_truth, self._on_truth, 20)
        if a.velocity_stats:
            self.create_subscription(String, a.velocity_stats, self._on_velocity, latched)
        if a.sport_violations:
            self.create_subscription(UInt32, a.sport_violations, self._on_violations, latched)
        self.buffer = Buffer()
        self.listener = TransformListener(
            self.buffer, None, spin_thread=True
        )  # own node and thread
        self.client = ActionClient(self, NavigateToPose, _ns(a.ns, "navigate_to_pose"))
        self.bt_state = self.create_client(GetState, _ns(a.ns, "bt_navigator/get_state"))

    def nav2_active(self) -> bool:
        """The action server exists before activation; goals are rejected until then."""
        if not self.bt_state.service_is_ready():
            return False
        future = self.bt_state.call_async(GetState.Request())
        self.spin_until(future.done, 5.0)
        return future.done() and future.result().current_state.id == State.PRIMARY_STATE_ACTIVE

    def _on_truth(self, m: Odometry) -> None:
        self.gt = m

    def _on_velocity(self, m: String) -> None:
        self.velocity = json.loads(m.data)

    def _on_violations(self, m: UInt32) -> None:
        self.violations = int(m.data)

    def spin_until(self, cond, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            rclpy.spin_once(self, timeout_sec=0.1)
            if cond():
                return True
        return False

    def localized(self) -> bool:
        try:
            self.buffer.lookup_transform(self.a.frame, self.a.base_frame, Time())
            return True
        except TransformException:
            return False

    def run(self) -> dict:
        a = self.a
        out: dict = {"goal": [a.goal[0], a.goal[1], a.yaw], "frame": a.frame}
        if not self.client.wait_for_server(timeout_sec=a.startup_timeout):
            return {**out, "error": "navigate_to_pose action server not available"}
        if not self.spin_until(lambda: self.gt is not None and self.localized(), a.startup_timeout):
            return {**out, "error": f"no ground truth or no {a.frame} -> {a.base_frame}"}
        end = time.monotonic() + a.startup_timeout
        while not self.nav2_active():
            if time.monotonic() > end:
                return {**out, "error": "Nav2 (bt_navigator) did not become active"}
            self.spin_until(lambda: False, 1.0)
        self.start = self.gt
        sp = self.start.pose.pose.position
        out["start"] = [round(sp.x, 3), round(sp.y, 3)]
        out["goal_distance_m"] = round(math.hypot(a.goal[0] - sp.x, a.goal[1] - sp.y), 3)

        goal = NavigateToPose.Goal()
        goal.pose.header.frame_id = a.frame
        goal.pose.header.stamp = self.get_clock().now().to_msg()
        goal.pose.pose.position.x, goal.pose.pose.position.y = a.goal
        goal.pose.pose.orientation.z = math.sin(a.yaw / 2)
        goal.pose.pose.orientation.w = math.cos(a.yaw / 2)
        t0 = self.get_clock().now()
        wall0 = time.monotonic()
        send = self.client.send_goal_async(goal)
        if not self.spin_until(send.done, 30.0) or not send.result().accepted:
            return {**out, "error": "goal rejected"}
        result = send.result().get_result_async()
        travelled, last = 0.0, (sp.x, sp.y)

        def step() -> bool:
            nonlocal travelled, last
            if self.gt is not None:
                p = self.gt.pose.pose.position
                travelled += math.hypot(p.x - last[0], p.y - last[1])
                last = (p.x, p.y)
            return result.done()

        if not self.spin_until(step, a.timeout):
            send.result().cancel_goal_async()
            self.spin_until(lambda: False, 2.0)
            out["result"] = "timeout"
        else:
            r = result.result()
            out["result"] = STATUS.get(r.status, f"status {r.status}")
            out["nav2_error_code"] = int(getattr(r.result, "error_code", 0))
        # Let latched statistics arrive.
        self.spin_until(lambda: False, 1.5)
        p = self.gt.pose.pose
        out["final_gt"] = [
            round(p.position.x, 3),
            round(p.position.y, 3),
            round(yaw_of(p.orientation), 3),
        ]
        out["final_error_m"] = round(
            math.hypot(p.position.x - a.goal[0], p.position.y - a.goal[1]), 3
        )
        out["travelled_m"] = round(travelled, 2)
        out["sim_time_s"] = round((self.get_clock().now() - t0).nanoseconds * 1e-9, 1)
        out["wall_time_s"] = round(time.monotonic() - wall0, 1)
        if self.velocity is not None:
            out["velocity"] = self.velocity
        if self.violations is not None:
            out["sport_api_violations"] = self.violations

        checks = {
            "succeeded": out.get("result") == "succeeded",
            "goal_at_least_m": out["goal_distance_m"] >= a.min_distance,
            "final_error_ok": out["final_error_m"] <= a.tolerance,
        }
        if a.velocity_stats:
            checks["reverse_within_limits"] = (
                self.velocity is not None
                and self.velocity["below_threshold"] == 0
                and self.velocity.get("reverse_too_long", 0) == 0
            )
        if a.sport_violations:
            checks["no_sport_violations"] = self.violations == 0
        out["checks"] = checks
        out["ok"] = all(checks.values())
        return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--goal", nargs=2, type=float, required=True, metavar=("X", "Y"))
    p.add_argument("--yaw", type=float, default=0.0)
    p.add_argument("--ns", default="", help="robot namespace, e.g. /robot (Summit)")
    p.add_argument("--frame", default="map")
    p.add_argument("--base-frame", default="base_link")
    p.add_argument("--ground-truth", default="/a6g_sim/ground_truth")
    p.add_argument("--velocity-stats", default="", help="velocity_monitor ~/stats topic")
    p.add_argument("--sport-violations", default="", help="sport_api_sim ~/violations topic")
    p.add_argument("--min-distance", type=float, default=29.5)
    p.add_argument("--tolerance", type=float, default=0.5, help="final ground-truth error, m")
    p.add_argument("--timeout", type=float, default=600.0, help="wall seconds for the goal")
    p.add_argument("--startup-timeout", type=float, default=180.0)
    p.add_argument("--json", default="", help="also write the summary to this file")
    args, ros_args = p.parse_known_args(argv)
    rclpy.init(args=ros_args)
    node = NavCheck(args)
    try:
        summary = node.run()
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
    text = json.dumps(summary, indent=1)
    print(text)
    if args.json:
        with open(args.json, "w") as f:
            f.write(text)
    sys.exit(0 if summary.get("ok") else 1)
