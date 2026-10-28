"""Go2 and Summit backends over ``ros_adapter``.

The same code drives Gazebo and the hardware: everything that differs (ROS names, stop
method, nav mode, ``map_to_campus``, sim-clock guard) is configuration, applied by the
adapter or by this backend. Poses cross the socket in the robot's own map frame and are
converted from / to ``campus_map`` here.
"""

import asyncio
import contextlib
import io
import itertools
import logging
import math
from datetime import UTC, datetime
from pathlib import Path

from a6g_common.config import PlanarTransform, RobotManifest
from a6g_common.schemas import (
    Platform,
    Pose2D,
    PoseWithCovariance,
    RobotState,
    SkillOutcome,
)

from a6g_skill_mcp.adapter_client import AdapterClient
from a6g_skill_mcp.backend import MotionResult, SkillBackend, purge_expired_images
from a6g_skill_mcp.frames import is_identity, to_campus, to_map

log = logging.getLogger(__name__)

#: Extra seconds the client waits beyond the adapter's own navigation timeout.
RESULT_MARGIN_S = 30.0


class AdapterBackend(SkillBackend):
    platform: Platform

    def __init__(
        self,
        manifest: RobotManifest,
        client: AdapterClient,
        *,
        map_to_campus: PlanarTransform | None = None,
        image_dir: Path | str = "var/images",
        real_robot: bool = False,
    ) -> None:
        if manifest.platform is not self.platform:
            raise ValueError(f"{type(self).__name__} needs a {self.platform.value} manifest")
        super().__init__(manifest)
        self.client = client
        self.map_to_campus = map_to_campus or PlanarTransform()
        self.image_dir = Path(image_dir)
        self._image_seq = itertools.count(1)
        self._interrupt = asyncio.Event()
        self._last_pose = PoseWithCovariance(x=0.0, y=0.0)
        if real_robot and is_identity(self.map_to_campus):
            log.warning(
                "%s: map_to_campus is identity under profile real; set the measured "
                "transform in config/profiles/real.yaml (TODO(UPV))",
                self.robot_id,
            )

    def now(self) -> datetime:
        return datetime.now(UTC)

    # ------------------------------------------------------------------ reads

    async def get_state(self) -> RobotState:
        state = await self.client.request("get_state", timeout_s=5.0)
        pose = state.get("pose")
        if pose is not None:
            x, y, yaw = to_campus(self.map_to_campus, pose["x"], pose["y"], pose["yaw"])
            self._last_pose = PoseWithCovariance(x=x, y=y, yaw=yaw, cov_xy=pose.get("cov_xy"))
        else:  # no localization: keep the last pose, mark it unusable for motion
            self._last_pose = self._last_pose.model_copy(update={"cov_xy": None})
        battery = state.get("battery_pct") if self.manifest.battery_reliable else None
        return RobotState(
            robot_id=self.robot_id,
            platform=self.manifest.platform,
            pose=self._last_pose,
            battery_pct=battery,
            capabilities=list(self.manifest.capabilities),
            stamp=self.now(),
        )

    async def scan_min_range(self) -> float:
        value = await self.client.request("scan_min_range", timeout_s=5.0)
        return 0.0 if value is None else float(value)  # no recent scan: no clearance

    # ------------------------------------------------------------------ motion

    async def navigate(self, goal: Pose2D, timeout_s: float) -> MotionResult:
        x, y, yaw = to_map(self.map_to_campus, goal)
        self._interrupt.clear()
        args = {"x": x, "y": y, "yaw": yaw, "timeout_s": timeout_s}
        try:
            result = await self.client.request(
                "navigate", args, timeout_s=timeout_s + RESULT_MARGIN_S
            )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.shield(self.client.request("cancel", timeout_s=10.0))
            raise
        except TimeoutError:
            with contextlib.suppress(Exception):
                await self.client.request("cancel", timeout_s=10.0)
            return MotionResult(SkillOutcome.TIMEOUT, "adapter did not answer in time")
        return MotionResult(
            SkillOutcome(result["outcome"]), result.get("detail", ""), result.get("native_code")
        )

    async def dwell(self, seconds: float, timeout_s: float) -> MotionResult:
        """Stay put (no goal is active, so the robot holds its position)."""
        self._interrupt.clear()
        wait = min(seconds, timeout_s)
        try:
            await asyncio.wait_for(self._interrupt.wait(), wait)
        except TimeoutError:
            if seconds > timeout_s:
                return MotionResult(SkillOutcome.TIMEOUT, "dwell exceeded the skill timeout")
            return MotionResult(SkillOutcome.SUCCEEDED, f"held {seconds:.0f} s")
        return MotionResult(SkillOutcome.CANCELLED, "dwell interrupted")

    async def cancel(self) -> bool:
        self._interrupt.set()
        return bool(await self.client.request("cancel", timeout_s=10.0))

    async def stop(self) -> None:
        self._interrupt.set()
        await self.client.request("stop", timeout_s=10.0)

    async def capture_image(self, max_kb: int) -> str:
        png = await self.client.request("capture_image", timeout_s=10.0)
        if png is None:
            raise RuntimeError("no camera frame received yet")
        data = await asyncio.to_thread(_to_jpeg, Path(png), max_kb)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        path = self.image_dir / f"{self.robot_id}_{next(self._image_seq):05d}.jpg"
        await asyncio.to_thread(path.write_bytes, data)
        purge_expired_images(self.image_dir, self.image_retention_h)
        return path.resolve().as_uri()


def _to_jpeg(png: Path, max_kb: int) -> bytes:
    """Re-encode the adapter's PNG frame as a JPEG of at most ``max_kb`` KB (Rule 8)."""
    from PIL import Image

    with Image.open(png) as img:
        img = img.convert("RGB")
        for scale in (1.0, 0.75, 0.5, 0.35, 0.25):
            frame = (
                img
                if scale == 1.0
                else img.resize(
                    (max(1, math.floor(img.width * scale)), max(1, math.floor(img.height * scale)))
                )
            )
            for quality in (85, 70, 55, 40):
                buf = io.BytesIO()
                frame.save(buf, "JPEG", quality=quality)
                if buf.tell() <= max_kb * 1024:
                    return buf.getvalue()
    raise ValueError(f"cannot fit the frame in {max_kb} KB")


class Go2Backend(AdapterBackend):
    """Unitree Go2 (Foxy on the dock; Jazzy Nav2 around the Gazebo proxy).

    Velocity limits live in ``cmd_vel_to_sport`` and are never bypassed; stop is Nav2 cancel
    plus Sport API StopMove (api_id 1003), the only Sport API call the adapter makes.
    """

    platform = Platform.GO2


class SummitBackend(AdapterBackend):
    """Robotnik Summit-XL (Jazzy, ``/robot``). Reverses only short and slow (manifest
    ``reverse_limits``, enforced by Nav2 MPPI and the base-side reverse_governor); no BackUp
    in its Nav2 tree, and the adapter publishes no velocity except the all-zero stop Twist."""

    platform = Platform.SUMMIT


BACKENDS: dict[Platform, type[AdapterBackend]] = {
    Platform.GO2: Go2Backend,
    Platform.SUMMIT: SummitBackend,
}
