"""Lightweight 2D fleet simulator, scenarios and fault injection (SPEC Section 12)."""

from a6g_sim.runner import SimRunner
from a6g_sim.world import (
    ForbiddenMotionError,
    Primitive,
    PrimitiveKind,
    SimRobot,
    SimWorld,
    check_primitive,
    plan_motion,
)

__all__ = [
    "ForbiddenMotionError",
    "Primitive",
    "PrimitiveKind",
    "SimRobot",
    "SimRunner",
    "SimWorld",
    "check_primitive",
    "plan_motion",
]
