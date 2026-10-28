"""``SkillBackend``: the platform primitives every skill is composed from.

Backends know nothing about arming, preconditions or decision logging; those live in
:class:`a6g_skill_mcp.service.SkillService`, so they are identical for every platform.
Implementations: :class:`~a6g_skill_mcp.sim_backend.SimBackend` (tests), and the Go2 and
Summit backends over ``ros_adapter`` (:mod:`a6g_skill_mcp.ros_backend`).
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from a6g_common.config import RobotManifest
from a6g_common.schemas import Pose2D, RobotState, SkillOutcome


@dataclass(frozen=True)
class MotionResult:
    outcome: SkillOutcome
    detail: str = ""
    native_code: int | None = None


def purge_expired_images(image_dir: Path, retention_h: float) -> int:
    """Delete event images older than ``retention_h`` (images that may show
    people have limited retention; findings keep the URI and the SHA-256)."""
    if not image_dir.is_dir():
        return 0
    cutoff = time.time() - retention_h * 3600.0
    gone = 0
    for path in image_dir.glob("*.jpg"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                gone += 1
        except OSError:
            continue
    return gone


class SkillBackend(ABC):
    #: Event images are deleted after this (``interactions.image_retention_h``).
    image_retention_h: float = 24.0

    def __init__(self, manifest: RobotManifest) -> None:
        self.manifest = manifest

    @property
    def robot_id(self) -> str:
        return self.manifest.robot_id

    @abstractmethod
    def now(self) -> datetime:
        """Backend time base (simulated time for the simulator, wall clock on robots)."""

    @abstractmethod
    async def get_state(self) -> RobotState:
        """Current state; ``armed`` is filled in by the service."""

    @abstractmethod
    async def scan_min_range(self) -> float:
        """Closest return in the latest scan (m), for the clearance precondition."""

    @abstractmethod
    async def navigate(self, goal: Pose2D, timeout_s: float) -> MotionResult:
        """Navigate to ``goal`` (``campus_map``) and wait for the result.

        Must refuse (``refused_precondition``) any motion the manifest forbids.
        """

    @abstractmethod
    async def dwell(self, seconds: float, timeout_s: float) -> MotionResult:
        """Hold position for ``seconds``; ends early with ``cancelled`` on cancel/stop."""

    @abstractmethod
    async def cancel(self) -> bool:
        """Cancel the active goal; False if nothing was active."""

    @abstractmethod
    async def stop(self) -> None:
        """Immediate platform stop. Must work while disarmed."""

    @abstractmethod
    async def capture_image(self, max_kb: int) -> str:
        """Save a JPEG of at most ``max_kb`` KB locally and return its URI."""
