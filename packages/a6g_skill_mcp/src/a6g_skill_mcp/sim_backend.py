"""``SimBackend``: the skill primitives on top of :mod:`a6g_sim`."""

import asyncio
import base64
import itertools
from datetime import datetime
from pathlib import Path

from a6g_common.schemas import Pose2D, RobotState, SkillOutcome
from a6g_sim.world import ForbiddenMotionError, Job, SimWorld

from a6g_skill_mcp.backend import MotionResult, SkillBackend, purge_expired_images

# An 8x8 grey-green JPEG (631 bytes) standing in for a camera frame.
_SIM_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdASFxOQERX"
    "RTc4UG1RV19iZ2hnPk1xeXBkeFxlZ2P/2wBDARESEhgVGC8aGi9jQjhCY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2Nj"
    "Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2P/wAARCAAIAAgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAEC"
    "AwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2Jy"
    "ggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWW"
    "l5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
    "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMi"
    "MoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0"
    "dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn"
    "6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCrRRRWRif/2Q=="
)


class SimBackend(SkillBackend):
    def __init__(self, world: SimWorld, robot_id: str, image_dir: Path | str) -> None:
        super().__init__(world.robot(robot_id).manifest)
        self.world = world
        self.image_dir = Path(image_dir)
        self._image_seq = itertools.count(1)

    def now(self) -> datetime:
        return self.world.clock.now()

    async def get_state(self) -> RobotState:
        return self.world.state(self.robot_id)

    async def scan_min_range(self) -> float:
        return self.world.scan_min_range(self.robot_id)

    async def _await(self, job: Job) -> MotionResult:
        try:
            result = await job.future
        except asyncio.CancelledError:
            self.world.cancel(self.robot_id)
            raise
        return MotionResult(result.outcome, result.detail, result.native_code)

    async def navigate(self, goal: Pose2D, timeout_s: float) -> MotionResult:
        try:
            job = self.world.start_navigation(self.robot_id, goal, timeout_s)
        except ForbiddenMotionError as exc:
            return MotionResult(SkillOutcome.REFUSED_PRECONDITION, str(exc))
        return await self._await(job)

    async def dwell(self, seconds: float, timeout_s: float) -> MotionResult:
        return await self._await(self.world.start_hold(self.robot_id, seconds, timeout_s))

    async def cancel(self) -> bool:
        return self.world.cancel(self.robot_id)

    async def stop(self) -> None:
        self.world.stop(self.robot_id)

    async def capture_image(self, max_kb: int) -> str:
        if len(_SIM_JPEG) > max_kb * 1024:
            raise ValueError(f"image exceeds {max_kb} KB")
        self.image_dir.mkdir(parents=True, exist_ok=True)
        path = self.image_dir / f"{self.robot_id}_{next(self._image_seq):05d}.jpg"
        path.write_bytes(_SIM_JPEG)
        purge_expired_images(self.image_dir, self.image_retention_h)
        return path.resolve().as_uri()
