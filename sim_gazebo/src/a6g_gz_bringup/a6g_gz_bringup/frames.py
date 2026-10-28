"""Planar transform helpers (ROS-free, unit-tested)."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Pose2:
    x: float
    y: float
    yaw: float

    def __mul__(self, other: "Pose2") -> "Pose2":
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        return Pose2(
            self.x + c * other.x - s * other.y,
            self.y + s * other.x + c * other.y,
            wrap(self.yaw + other.yaw),
        )

    def inverse(self) -> "Pose2":
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        return Pose2(-(c * self.x + s * self.y), -(-s * self.x + c * self.y), wrap(-self.yaw))


def wrap(a: float) -> float:
    return math.atan2(math.sin(a), math.cos(a))


def yaw_of(q) -> float:
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


def map_to_odom(world_base: Pose2, odom_base: Pose2) -> Pose2:
    """map -> odom such that map -> base equals the ground truth (map == world)."""
    return world_base * odom_base.inverse()
