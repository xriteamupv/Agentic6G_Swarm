"""The adapter's operations, independent of ROS.

``AdapterCore`` validates requests and calls a platform IO object (``RosIO`` on robots and
in Gazebo, a fake in unit tests). The operation list is fixed: there is no generic topic,
service or velocity operation. Goals arrive already in the robot's own map
frame; ``campus_map`` conversion happens in ``a6g_skill_mcp``.

Python 3.8 compatible: stdlib only here.
"""

import logging
import math
import threading
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("a6g_ros_adapter")

OPS = (
    "ping",
    "info",
    "get_state",
    "scan_min_range",
    "navigate",
    "cancel",
    "stop",
    "capture_image",
)

#: Outcomes shared with ``a6g_common.schemas.SkillOutcome`` (kept as strings here: 3.8).
SUCCEEDED = "succeeded"
FAILED = "failed"
TIMEOUT = "timeout"
CANCELLED = "cancelled"
REFUSED_PRECONDITION = "refused_precondition"
UNKNOWN = "unknown"


class AdapterError(Exception):
    """A request the adapter rejects (reported to the caller, never fatal)."""


class ProfileGuardError(Exception):
    """Start-up refused by the simulation-clock guard."""


def check_profile(profile: str, clock_publishers: int) -> str:
    """Rule 14: ``real`` needs no /clock publisher, ``gazebo`` needs one. Returns a log line."""
    if profile == "real" and clock_publishers > 0:
        raise ProfileGuardError(
            f"profile 'real' but /clock has {clock_publishers} publisher(s): a simulator is "
            "running in this ROS domain; refusing to start"
        )
    if profile == "gazebo" and clock_publishers == 0:
        raise ProfileGuardError(
            "profile 'gazebo' but nothing publishes /clock: this is not a simulation; "
            "refusing to start"
        )
    if profile not in ("real", "gazebo"):
        raise ProfileGuardError(f"unknown profile {profile!r} (expected real or gazebo)")
    return f"profile guard OK: profile {profile}, {clock_publishers} /clock publisher(s)"


def robust_min_range(
    ranges: List[float], range_min: float, range_max: float, window: int = 5
) -> float:
    """Closest obstacle in a scan, robust to single noisy returns.

    The minimum of a running median over ``window`` adjacent beams (invalid beams count as
    ``range_max``): a lone short reading is noise, a real obstacle spans several beams.
    """
    clean = [r if math.isfinite(r) and range_min <= r <= range_max else range_max for r in ranges]
    if not clean:
        return float(range_max)
    half = window // 2
    best = float(range_max)
    for i in range(len(clean)):
        neighbours = sorted(clean[max(0, i - half) : i + half + 1])
        best = min(best, neighbours[len(neighbours) // 2])
    return float(best)


def _number(args: Dict[str, Any], key: str, default: Optional[float] = None) -> float:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdapterError(f"{key} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise AdapterError(f"{key} must be finite")
    return value


class AdapterCore:
    def __init__(self, config: Dict[str, Any], io: Any, dry_run: bool = False) -> None:
        self.config = config
        self.io = io
        self.dry_run = dry_run
        self.robot_id = config["robot_id"]
        self._nav_lock = threading.Lock()
        self._navigating = False

    # ------------------------------------------------------------------ dispatch

    def handle(self, op: Any, args: Dict[str, Any]) -> Any:
        if op not in OPS:
            raise AdapterError("unknown op {!r} (allowed: {})".format(op, ", ".join(OPS)))
        if not isinstance(args, dict):
            raise AdapterError("args must be an object")
        return getattr(self, "op_" + op)(args)

    # ------------------------------------------------------------------ ops

    def op_ping(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return {"robot_id": self.robot_id, "time": time.time()}

    def op_info(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "robot_id": self.robot_id,
            "platform": self.config["platform"],
            "profile": self.config["profile"],
            "dry_run": self.dry_run,
            "nav_mode": self.config["nav_mode"],
            "map_frame": self.config["ros"]["map_frame"],
        }

    def op_get_state(self, args: Dict[str, Any]) -> Dict[str, Any]:
        pose = self.io.pose()
        return {
            "pose": pose,  # {x, y, yaw, cov_xy, frame} in the robot's map frame, or None
            "battery_pct": self.io.battery_pct(),
            "navigating": self._navigating,
        }

    def op_scan_min_range(self, args: Dict[str, Any]) -> Optional[float]:
        return self.io.scan_min_range()

    def op_navigate(self, args: Dict[str, Any]) -> Dict[str, Any]:
        x, y = _number(args, "x"), _number(args, "y")
        yaw = _number(args, "yaw", 0.0)
        timeout_s = _number(args, "timeout_s")
        if timeout_s <= 0:
            raise AdapterError("timeout_s must be > 0")
        frame = self.config["ros"]["map_frame"]
        if self.dry_run:
            log.warning(
                "DRY-RUN %s: would send %s goal x=%.3f y=%.3f yaw=%.3f in %s (timeout %.0f s)",
                self.robot_id, self.config["nav_mode"], x, y, yaw, frame, timeout_s,
            )  # fmt: skip
            return {
                "outcome": REFUSED_PRECONDITION,
                "detail": "dry-run: goal logged, not sent",
                "native_code": None,
            }
        with self._nav_lock:
            if self._navigating:
                raise AdapterError("a navigation goal is already active; cancel it first")
            self._navigating = True
        try:
            log.info("%s: navigate to x=%.3f y=%.3f yaw=%.3f (%s)", self.robot_id, x, y, yaw, frame)
            result = self.io.navigate(x, y, yaw, timeout_s)
            log.info("%s: navigate result %s", self.robot_id, result)
            return result
        finally:
            self._navigating = False

    def op_cancel(self, args: Dict[str, Any]) -> bool:
        if self.dry_run:
            log.warning("DRY-RUN %s: would cancel the active goal", self.robot_id)
            return False
        return bool(self.io.cancel())

    def op_stop(self, args: Dict[str, Any]) -> bool:
        stop = self.config["ros"]["stop"]
        if self.dry_run:
            log.warning("DRY-RUN %s: would cancel and stop (%s)", self.robot_id, stop["method"])
            return False
        # Request the cancel without waiting, so the platform stop is not delayed; Nav2 stops
        # publishing velocities as the cancel lands (and twist_mux / StopMove win meanwhile).
        self.io.cancel(wait=False)
        self.io.stop()
        return True

    def op_capture_image(self, args: Dict[str, Any]) -> Optional[str]:
        """Latest camera frame saved as PNG under the configured image dir; path or None."""
        return self.io.save_image()
