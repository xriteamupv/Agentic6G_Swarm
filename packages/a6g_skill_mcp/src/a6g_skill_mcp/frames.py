"""``campus_map`` <-> a robot's Nav2 map frame.

``map_to_campus`` (from the profile) is the pose of the robot's map frame in ``campus_map``:
``p_campus = T * p_map``. Gazebo: identity. Real indoor: the lab map placed on campus.
"""

import math

from a6g_common.config import PlanarTransform
from a6g_common.schemas import Pose2D


def _wrap(a: float) -> float:
    return math.atan2(math.sin(a), math.cos(a))


def is_identity(t: PlanarTransform) -> bool:
    return t.x == 0 and t.y == 0 and _wrap(t.yaw) == 0


def to_campus(t: PlanarTransform, x: float, y: float, yaw: float) -> tuple[float, float, float]:
    c, s = math.cos(t.yaw), math.sin(t.yaw)
    return t.x + c * x - s * y, t.y + s * x + c * y, _wrap(t.yaw + yaw)


def to_map(t: PlanarTransform, pose: Pose2D) -> tuple[float, float, float]:
    c, s = math.cos(t.yaw), math.sin(t.yaw)
    dx, dy = pose.x - t.x, pose.y - t.y
    return c * dx + s * dy, -s * dx + c * dy, _wrap(pose.yaw - t.yaw)
