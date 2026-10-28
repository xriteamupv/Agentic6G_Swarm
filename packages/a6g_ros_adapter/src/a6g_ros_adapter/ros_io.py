"""rclpy implementation of the adapter's platform IO (Foxy and Jazzy; Python 3.8 syntax).

Reads: pose (TF ``map_frame -> base_frame``, covariance from ``pose_topic``), scan, camera.
Writes, and nothing else:

- Nav2 ``NavigateToPose`` goals (``nav_mode: action``) or ``PoseStamped`` on
  ``goal_pose_topic`` (``nav_mode: goal_pose``), plus cancel / Nav2 lifecycle pause-resume;
- the platform stop from the manifest: Sport API StopMove (api_id 1003, the only Sport API
  call allowed) or a zero ``Twist`` on the twist_mux stop input for ``duration_s``.

No velocity other than an all-zero Twist is ever published, so the adapter cannot reverse.
"""

import logging
import math
import os
import threading
import time
from typing import Any, Dict, Optional

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped, Twist
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import (
    DurabilityPolicy,
    QoSProfile,
    ReliabilityPolicy,
    qos_profile_sensor_data,
)
from rclpy.time import Time
from sensor_msgs.msg import Image, LaserScan
from tf2_ros import Buffer, TransformException, TransformListener

from a6g_ros_adapter.core import (
    CANCELLED,
    FAILED,
    SUCCEEDED,
    TIMEOUT,
    UNKNOWN,
    robust_min_range,
)
from a6g_ros_adapter.image import encode_png

log = logging.getLogger("a6g_ros_adapter")

SPORT_STOP_MOVE = 1003  # the only Sport API call the adapter may make
SCAN_STALE_S = 2.0
POSE_TYPES = {
    "geometry_msgs/msg/PoseWithCovarianceStamped": (
        "geometry_msgs.msg",
        "PoseWithCovarianceStamped",
    ),
    "nav_msgs/msg/Odometry": ("nav_msgs.msg", "Odometry"),
}


def _yaw(q: Any) -> float:
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))


def _wait(future: Any, timeout_s: float) -> bool:
    done = threading.Event()
    future.add_done_callback(lambda _f: done.set())
    if future.done():
        return True
    return done.wait(timeout_s)


class RosIO:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        ros = config["ros"]
        self.ros = ros
        # No use_sim_time even in Gazebo: following /clock (250 Hz) costs a third of a core in
        # Python, and nothing here needs it (TF lookups use the latest data, goals stamp 0).
        self.use_sim_time = False
        self.node = Node(
            "a6g_ros_adapter_" + config["robot_id"],
            parameter_overrides=[Parameter("use_sim_time", Parameter.Type.BOOL, self.use_sim_time)],
        )
        self.group = ReentrantCallbackGroup()
        self.buffer = Buffer()
        self.listener = TransformListener(self.buffer, self.node)
        self._lock = threading.Lock()
        self._cov = None  # type: Optional[float]
        self._scan = None  # type: Optional[LaserScan]
        self._scan_at = 0.0
        self._image = None  # type: Optional[Image]
        self._pose_sub = None
        self._goal_handle = None
        self._cancel = threading.Event()

        self.node.create_subscription(
            LaserScan, ros["scan_topic"], self._on_scan, qos_profile_sensor_data,
            callback_group=self.group,
        )  # fmt: skip
        # The camera is subscribed only while an event image is being captured: decoding
        # every 640x480 frame in Python would cost a core for nothing.
        self._image_wanted = threading.Event()
        self.node.create_timer(1.0, self._find_pose_topic, callback_group=self.group)

        self.nav = None
        self.goal_pub = None
        if config["nav_mode"] == "action":
            from nav2_msgs.action import NavigateToPose

            self._NavigateToPose = NavigateToPose
            self.nav = ActionClient(
                self.node, NavigateToPose, ros["nav_action"], callback_group=self.group
            )
        else:
            self.goal_pub = self.node.create_publisher(PoseStamped, ros["goal_pose_topic"], 10)

        stop = ros["stop"]
        if stop["method"] == "sport_stop_move":
            if stop.get("api_id") != SPORT_STOP_MOVE:
                raise ValueError("sport_stop_move must use api_id 1003 (StopMove)")
            from unitree_api.msg import Request

            self._Request = Request
            self.stop_pub = self.node.create_publisher(Request, stop["topic"], 10)
        elif stop["method"] == "zero_twist":
            self.stop_pub = self.node.create_publisher(Twist, stop["topic"], 10)
        else:
            raise ValueError("unknown stop method {!r}".format(stop["method"]))

    def close(self) -> None:
        """Release the TF listener (it owns a subscription) and the node."""
        self.listener.unregister()
        self.node.destroy_node()

    # ------------------------------------------------------------------ subscriptions

    def _on_scan(self, msg: LaserScan) -> None:
        with self._lock:
            self._scan, self._scan_at = msg, time.monotonic()

    def _on_image(self, msg: Image) -> None:
        with self._lock:
            self._image = msg
        self._image_wanted.set()

    def _on_pose(self, msg: Any) -> None:
        cov = msg.pose.covariance
        with self._lock:
            self._cov = float(cov[0] + cov[7])

    def _find_pose_topic(self) -> None:
        if self._pose_sub is not None:
            return
        topic = self.ros["pose_topic"]
        for name, types in self.node.get_topic_names_and_types():
            if name != topic:
                continue
            for t in types:
                if t in POSE_TYPES:
                    module, cls = POSE_TYPES[t]
                    msg_type = getattr(__import__(module, fromlist=[cls]), cls)
                    latched = cls == "PoseWithCovarianceStamped"  # amcl_pose is latched
                    qos = QoSProfile(
                        depth=1,
                        reliability=ReliabilityPolicy.RELIABLE,
                        durability=(
                            DurabilityPolicy.TRANSIENT_LOCAL
                            if latched
                            else DurabilityPolicy.VOLATILE
                        ),
                    )
                    self._pose_sub = self.node.create_subscription(
                        msg_type, topic, self._on_pose, qos, callback_group=self.group
                    )
                    log.info("pose covariance from %s (%s)", topic, t)
                    return

    # ------------------------------------------------------------------ reads

    def clock_publishers(self) -> int:
        return self.node.count_publishers("/clock")

    def pose(self) -> Optional[Dict[str, Any]]:
        try:
            t = self.buffer.lookup_transform(self.ros["map_frame"], self.ros["base_frame"], Time())
        except TransformException:
            return None
        tr = t.transform.translation
        with self._lock:
            cov = self._cov
        return {
            "x": tr.x,
            "y": tr.y,
            "yaw": _yaw(t.transform.rotation),
            "cov_xy": cov,
            "frame": self.ros["map_frame"],
        }

    def battery_pct(self) -> Optional[float]:
        return None  # no battery topic is read on either robot (the Summit estimate is unreliable)

    def scan_min_range(self) -> Optional[float]:
        with self._lock:
            scan, at = self._scan, self._scan_at
        if scan is None or time.monotonic() - at > SCAN_STALE_S:
            return None
        return robust_min_range(list(scan.ranges), scan.range_min, scan.range_max)

    def save_image(self, timeout_s: float = 3.0) -> Optional[str]:
        """Subscribe, take the next frame, unsubscribe; None if no frame within the timeout."""
        with self._lock:
            self._image = None
        self._image_wanted.clear()
        sub = self.node.create_subscription(
            Image, self.config["camera_topic"], self._on_image, qos_profile_sensor_data,
            callback_group=self.group,
        )  # fmt: skip
        try:
            self._image_wanted.wait(timeout_s)
        finally:
            self.node.destroy_subscription(sub)
        with self._lock:
            img = self._image
        if img is None:
            return None
        directory = self.config.get("image_dir") or "/tmp/a6g_images"
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(
            directory, "{}_{}.png".format(self.config["robot_id"], int(time.time() * 1000))
        )
        with open(path, "wb") as f:
            f.write(encode_png(img.width, img.height, img.encoding, img.step, bytes(img.data)))
        return path

    # ------------------------------------------------------------------ motion

    def _goal(self, x: float, y: float, yaw: float) -> PoseStamped:
        goal = PoseStamped()
        goal.header.frame_id = self.ros["map_frame"]
        # Stamp 0 = "latest": Nav2 transforms the goal with the newest TF, on any clock.
        goal.pose.position.x, goal.pose.position.y = x, y
        goal.pose.orientation.z, goal.pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
        return goal

    def navigate(self, x: float, y: float, yaw: float, timeout_s: float) -> Dict[str, Any]:
        self._cancel.clear()
        if self.nav is not None:
            return self._navigate_action(x, y, yaw, timeout_s)
        return self._navigate_goal_pose(x, y, yaw, timeout_s)

    def _navigate_action(self, x: float, y: float, yaw: float, timeout_s: float) -> Dict[str, Any]:
        if not self.nav.wait_for_server(timeout_sec=10.0):
            return {
                "outcome": FAILED,
                "detail": "navigation action server unavailable",
                "native_code": None,
            }
        goal = self._NavigateToPose.Goal()
        goal.pose = self._goal(x, y, yaw)
        send = self.nav.send_goal_async(goal)
        if not _wait(send, 10.0) or not send.result().accepted:
            return {"outcome": FAILED, "detail": "goal rejected by Nav2", "native_code": None}
        handle = send.result()
        self._goal_handle = handle
        try:
            result = handle.get_result_async()
            if not _wait(result, timeout_s):
                cancel = handle.cancel_goal_async()
                _wait(cancel, 5.0)
                return {
                    "outcome": TIMEOUT,
                    "detail": f"no result within {timeout_s:.0f} s",
                    "native_code": None,
                }
            res = result.result()
        finally:
            self._goal_handle = None
        code = int(getattr(res.result, "error_code", 0) or 0)  # Foxy results carry no code
        msg = getattr(res.result, "error_msg", "") or ""
        if res.status == GoalStatus.STATUS_SUCCEEDED:
            return {"outcome": SUCCEEDED, "detail": "goal reached", "native_code": None}
        if res.status == GoalStatus.STATUS_CANCELED:
            return {"outcome": CANCELLED, "detail": "goal cancelled", "native_code": None}
        if code:
            return {
                "outcome": UNKNOWN,
                "detail": msg or f"Nav2 error {code}",
                "native_code": code,
            }
        return {
            "outcome": FAILED,
            "detail": msg or f"Nav2 aborted (status {res.status})",
            "native_code": None,
        }

    def _navigate_goal_pose(
        self, x: float, y: float, yaw: float, timeout_s: float
    ) -> Dict[str, Any]:
        """Foxy fallback: publish the goal, track distance-to-goal and the timeout."""
        tol = (self.config.get("goal_tolerance") or {}).get("xy_m", 0.25)
        self.goal_pub.publish(self._goal(x, y, yaw))
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if self._cancel.wait(0.5):
                return {"outcome": CANCELLED, "detail": "goal cancelled", "native_code": None}
            p = self.pose()
            if p is not None and math.hypot(p["x"] - x, p["y"] - y) <= tol:
                return {
                    "outcome": SUCCEEDED,
                    "detail": f"within {tol:.2f} m of the goal",
                    "native_code": None,
                }
        self._pause_resume()
        return {
            "outcome": TIMEOUT,
            "detail": f"goal not reached within {timeout_s:.0f} s",
            "native_code": None,
        }

    def _pause_resume(self) -> None:
        """Nav2 lifecycle PAUSE then RESUME: drops the active goal (goal_pose mode)."""
        service = self.ros.get("lifecycle_service")
        if not service:
            return
        from nav2_msgs.srv import ManageLifecycleNodes

        client = self.node.create_client(ManageLifecycleNodes, service, callback_group=self.group)
        try:
            if not client.wait_for_service(timeout_sec=2.0):
                log.warning("lifecycle service %s unavailable", service)
                return
            for command in (
                ManageLifecycleNodes.Request.PAUSE,
                ManageLifecycleNodes.Request.RESUME,
            ):
                req = ManageLifecycleNodes.Request()
                req.command = command
                _wait(client.call_async(req), 10.0)
        finally:
            self.node.destroy_client(client)

    def cancel(self, wait: bool = True) -> bool:
        """Cancel the active goal; ``wait=False`` only requests it (stop path: no delay)."""
        self._cancel.set()
        handle = self._goal_handle
        if handle is not None:
            future = handle.cancel_goal_async()
            if wait:
                _wait(future, 5.0)
            return True
        if self.goal_pub is not None:
            self._pause_resume()
        return False

    def stop(self) -> None:
        stop = self.ros["stop"]
        if stop["method"] == "sport_stop_move":
            req = self._Request()
            req.header.identity.api_id = SPORT_STOP_MOVE
            self.stop_pub.publish(req)
            log.warning("%s: Sport API StopMove sent", self.config["robot_id"])
            return
        end = time.monotonic() + float(stop["duration_s"])
        zero = Twist()  # all zero: the only raw velocity the adapter publishes
        while time.monotonic() < end:
            self.stop_pub.publish(zero)
            time.sleep(0.1)
        log.warning(
            "%s: zero twist on %s for %.1f s",
            self.config["robot_id"],
            stop["topic"],
            stop["duration_s"],
        )
