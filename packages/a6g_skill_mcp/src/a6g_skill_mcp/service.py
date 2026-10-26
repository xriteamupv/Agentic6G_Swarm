"""Skill semantics shared by every backend (SPEC 6.1, Rules 2, 7, 10).

For each skill the service: validates arguments against the manifest and campus, checks
preconditions (armed, clearance, localization covariance, battery floor), enforces the
manifest timeout, composes the backend primitives, returns a :class:`SkillResult` (a
summary only) and records a ``Decision(kind="skill")``.
"""

import contextvars
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from a6g_common.config import MAX_ROUTE_WAYPOINTS, CampusConfig
from a6g_common.ids import IdFactory, new_id
from a6g_common.schemas import (
    MOTION_SKILLS,
    DecisionKind,
    Pose2D,
    RobotState,
    SkillName,
    SkillOutcome,
    SkillResult,
)
from a6g_observability.decision_log import DecisionSink, make_decision

from a6g_skill_mcp.backend import MotionResult, SkillBackend

log = logging.getLogger(__name__)

#: Event images are capped at 300 KB over 5G (SPEC Rule 8).
MAX_EVENT_IMAGE_KB = 300
#: Per-leg navigation budget: LEG_BASE_S + LEG_DETOUR x straight-line time at max speed
#: (+ a half turn), times the profile's time scale. A robot stuck a few cm from a goal (or
#: lost) fails that leg in minutes instead of exhausting the whole skill's timeout.
LEG_BASE_S = 30.0
LEG_DETOUR = 3.0
#: Motion skills whose manifest ceiling shrinks with a scaled campus (dwells do not).
SCALED_SKILLS = (SkillName.NAVIGATE_TO, SkillName.FOLLOW_ROUTE, SkillName.RETURN_HOME)
#: MCP request ``_meta`` key carrying the caller's mission id (audit attribution only).
MISSION_META_KEY = "a6g/mission_id"
#: Mission of the request being served (set per MCP call by the server).
current_mission: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "a6g_mission", default=None
)


class SkillRefused(Exception):
    """Raised inside a skill to end it with a refusal outcome."""

    def __init__(self, outcome: SkillOutcome, detail: str) -> None:
        self.outcome = outcome
        self.detail = detail
        super().__init__(detail)


@dataclass
class ArmingState:
    """Motion arming. Default disarmed; set only through the operator channel."""

    armed: bool = False
    armed_by: str | None = None
    history: list[tuple[datetime, str, bool]] = field(default_factory=list)


class SkillService:
    def __init__(
        self,
        backend: SkillBackend,
        campus: CampusConfig,
        decisions: DecisionSink | None = None,
        *,
        battery_floor_pct: float = 20.0,
        mission_id: str | None = None,
        ids: IdFactory = new_id,
        time_scale: float = 1.0,
    ) -> None:
        """``time_scale`` stretches the per-leg budgets (Gazebo below real time); the
        manifest's skill timeouts are expected to be scaled already."""
        self.backend = backend
        self.manifest = backend.manifest
        self.time_scale = time_scale
        scale = getattr(campus, "scale", 1.0)
        if scale < 1.0:  # a 0.25 campus needs a quarter of the time to cross
            timeouts = dict(self.manifest.skill_timeouts)
            for skill in SCALED_SKILLS:
                timeouts[skill] = max(timeouts[skill] * scale, 120.0 * time_scale)
            self.manifest = self.manifest.model_copy(update={"skill_timeouts": timeouts})
        self.campus = campus
        self.decisions = decisions
        self.battery_floor_pct = battery_floor_pct
        self.mission_id = mission_id
        self.ids = ids
        self.arming = ArmingState()
        self.actor = f"{self.manifest.robot_id}.skill_mcp"
        self._active: SkillName | None = None
        self._cancel_requested = False

    # ------------------------------------------------------------------ operator channel

    @property
    def armed(self) -> bool:
        return self.arming.armed

    def arm(self, operator: str) -> None:
        self.arming.armed, self.arming.armed_by = True, operator
        self.arming.history.append((self.backend.now(), operator, True))
        log.warning("%s ARMED by %s", self.manifest.robot_id, operator)

    def disarm(self, operator: str) -> None:
        self.arming.armed, self.arming.armed_by = False, None
        self.arming.history.append((self.backend.now(), operator, False))
        log.warning("%s disarmed by %s", self.manifest.robot_id, operator)

    # ------------------------------------------------------------------ plumbing

    def _timeout(self, skill: SkillName) -> float:
        return self.manifest.skill_timeouts[skill]

    def _remaining(self, started: datetime, skill: SkillName) -> float:
        elapsed = (self.backend.now() - started).total_seconds()
        remaining = self._timeout(skill) - elapsed
        if remaining <= 0:
            raise SkillRefused(SkillOutcome.TIMEOUT, "skill timeout")
        return remaining

    def _check_cancel(self) -> None:
        if self._cancel_requested:
            raise SkillRefused(SkillOutcome.CANCELLED, "cancelled")

    def _record(self, skill: SkillName, args: dict[str, Any], result: SkillResult) -> None:
        if self.decisions is None:
            return
        self.decisions.record(
            make_decision(
                self.actor,
                DecisionKind.SKILL,
                mission_id=current_mission.get() or self.mission_id,
                inputs_ref=f"skill:{skill.value}",
                output={"args": args, "result": result.model_dump(mode="json")},
                policy={
                    "armed": self.armed,
                    "timeout_s": self._timeout(skill),
                    "clearance_m": self.manifest.clearance_m,
                    "localization_cov_max": self.manifest.localization_cov_max,
                    "battery_floor_pct": self.battery_floor_pct,
                },
                justification=result.detail,
                clock=self.backend,
                ids=self.ids,
            )
        )

    async def _run(
        self,
        skill: SkillName,
        args: dict[str, Any],
        body: Callable[[datetime], Awaitable[MotionResult | str | None]],
    ) -> SkillResult:
        """Run ``body`` with arming/precondition/timeout handling; always returns a result."""
        started = self.backend.now()
        artifact: str | None = None
        try:
            if skill in MOTION_SKILLS:
                await self._check_motion_preconditions()
                if self._active is not None:
                    raise SkillRefused(
                        SkillOutcome.REFUSED_PRECONDITION,
                        f"busy executing {self._active.value}; cancel it first",
                    )
                self._active, self._cancel_requested = skill, False
            try:
                outcome = await body(started)
            finally:
                if skill in MOTION_SKILLS and self._active is skill:
                    self._active = None
            if isinstance(outcome, MotionResult):
                result_outcome, detail, code = outcome.outcome, outcome.detail, outcome.native_code
            else:
                result_outcome, detail, code = SkillOutcome.SUCCEEDED, "", None
                artifact = outcome
        except SkillRefused as refusal:
            result_outcome, detail, code = refusal.outcome, refusal.detail, None
        except Exception as exc:  # backend fault: report, never crash the server
            log.exception("%s %s failed", self.manifest.robot_id, skill.value)
            result_outcome, detail, code = SkillOutcome.FAILED, f"backend error: {exc}", None
        duration = max(0.0, (self.backend.now() - started).total_seconds())
        result = SkillResult(
            skill=skill,
            outcome=result_outcome,
            detail=detail,
            native_code=code,
            duration_s=round(duration, 3),
            artifact_uri=artifact,
        )
        self._record(skill, args, result)
        return result

    async def _check_motion_preconditions(self) -> None:
        if not self.armed:
            raise SkillRefused(SkillOutcome.REFUSED_DISARMED, "robot is disarmed")
        state = await self.backend.get_state()
        cov = state.pose.cov_xy
        if cov is None or cov > self.manifest.localization_cov_max:
            raise SkillRefused(
                SkillOutcome.REFUSED_PRECONDITION,
                f"localization covariance {cov} exceeds {self.manifest.localization_cov_max}",
            )
        clearance = await self.backend.scan_min_range()
        if clearance < self.manifest.clearance_m:
            raise SkillRefused(
                SkillOutcome.REFUSED_PRECONDITION,
                f"clearance {clearance:.2f} m below {self.manifest.clearance_m} m",
            )
        battery = state.battery_pct if self.manifest.battery_reliable else None
        if battery is not None and battery < self.battery_floor_pct:
            raise SkillRefused(
                SkillOutcome.REFUSED_PRECONDITION,
                f"battery {battery:.1f}% below floor {self.battery_floor_pct}%",
            )

    def _validate_pose(self, pose: Pose2D) -> Pose2D:
        if pose.frame != self.campus.frame:
            raise SkillRefused(
                SkillOutcome.REFUSED_PRECONDITION,
                f"pose frame {pose.frame!r} is not {self.campus.frame!r}",
            )
        return pose

    def _poi_pose(self, poi_id: str) -> Pose2D:
        try:
            return self.campus.poi(poi_id).pose
        except StopIteration:
            raise SkillRefused(
                SkillOutcome.REFUSED_PRECONDITION, f"unknown poi {poi_id!r}"
            ) from None

    async def _leg_budget(self, goal: Pose2D) -> float:
        try:
            pose = (await self.backend.get_state()).pose
        except Exception:  # no pose: only the skill's own timeout applies
            return math.inf
        dist = math.hypot(goal.x - pose.x, goal.y - pose.y)
        if self.campus.footpaths_only and self.campus.route_graph is not None:
            # The robot walks the footpaths (2026-10-18): budget the walk, not the line.
            from a6g_common.footpaths import walker_for

            walk = walker_for(self.campus.route_graph).walk((pose.x, pose.y), (goal.x, goal.y))
            if walk is not None:
                dist = max(dist, walk[0])
        turn = math.pi * self.manifest.turn_in_place_s_per_rad
        return (LEG_BASE_S + LEG_DETOUR * dist / self.manifest.max_speed_mps + turn) * (
            self.time_scale
        )

    async def _navigate(self, goal: Pose2D, started: datetime, skill: SkillName) -> MotionResult:
        self._check_cancel()
        remaining = self._remaining(started, skill)
        budget = min(remaining, await self._leg_budget(goal))
        result = await self.backend.navigate(goal, budget)
        if result.outcome is SkillOutcome.TIMEOUT and budget < remaining:
            result = MotionResult(
                SkillOutcome.TIMEOUT,
                f"goal not reached within its {budget:.0f} s leg budget: {result.detail}",
                result.native_code,
            )
        if result.outcome is SkillOutcome.UNKNOWN and result.native_code is not None:
            mapped = self.manifest.error_code_map.get(result.native_code)
            if mapped is not None:
                result = MotionResult(mapped, result.detail, result.native_code)
        return result

    # ------------------------------------------------------------------ the nine skills

    async def get_state(self) -> RobotState:
        """Read-only; not recorded as a Decision (it is polled at 1 Hz)."""
        state = await self.backend.get_state()
        return state.model_copy(update={"armed": self.armed})

    async def navigate_to(
        self, pose: Pose2D | None = None, poi_id: str | None = None
    ) -> SkillResult:
        args = {"pose": pose.model_dump() if pose else None, "poi_id": poi_id}

        async def body(started: datetime) -> MotionResult:
            if (pose is None) == (poi_id is None):
                raise SkillRefused(
                    SkillOutcome.REFUSED_PRECONDITION, "give exactly one of pose or poi_id"
                )
            goal = self._validate_pose(pose) if pose else self._poi_pose(poi_id or "")
            return await self._navigate(goal, started, SkillName.NAVIGATE_TO)

        return await self._run(SkillName.NAVIGATE_TO, args, body)

    async def follow_route(self, waypoints: list[Pose2D]) -> SkillResult:
        args = {"waypoints": [w.model_dump() for w in waypoints]}

        async def body(started: datetime) -> MotionResult:
            if not 1 <= len(waypoints) <= MAX_ROUTE_WAYPOINTS:
                raise SkillRefused(
                    SkillOutcome.REFUSED_PRECONDITION,
                    f"follow_route takes 1..{MAX_ROUTE_WAYPOINTS} waypoints, got {len(waypoints)}",
                )
            for wp in waypoints:
                self._validate_pose(wp)
            for i, wp in enumerate(waypoints):
                result = await self._navigate(wp, started, SkillName.FOLLOW_ROUTE)
                if result.outcome is not SkillOutcome.SUCCEEDED:
                    return MotionResult(
                        result.outcome, f"waypoint {i}: {result.detail}", result.native_code
                    )
            return MotionResult(SkillOutcome.SUCCEEDED, f"{len(waypoints)} waypoints reached")

        return await self._run(SkillName.FOLLOW_ROUTE, args, body)

    async def inspect(
        self, poi_id: str, dwell_s: float, viewpoint: Pose2D | None = None
    ) -> SkillResult:
        """Navigate to the POI's viewpoint (its pose, facing its yaw, or ``viewpoint`` when
        several robots share the POI), dwell, capture."""
        args = {"poi_id": poi_id, "dwell_s": dwell_s}
        if viewpoint is not None:
            args["viewpoint"] = viewpoint.model_dump()

        async def body(started: datetime) -> MotionResult | str:
            if not 0 <= dwell_s < self._timeout(SkillName.INSPECT):
                raise SkillRefused(SkillOutcome.REFUSED_PRECONDITION, f"invalid dwell_s {dwell_s}")
            goal = self._poi_pose(poi_id)
            if viewpoint is not None:
                goal = self._validate_pose(viewpoint)
            result = await self._navigate(goal, started, SkillName.INSPECT)
            if result.outcome is not SkillOutcome.SUCCEEDED:
                return result
            self._check_cancel()
            held = await self.backend.dwell(dwell_s, self._remaining(started, SkillName.INSPECT))
            if held.outcome is not SkillOutcome.SUCCEEDED:
                return held
            self._check_cancel()
            return await self.backend.capture_image(MAX_EVENT_IMAGE_KB)

        return await self._run(SkillName.INSPECT, args, body)

    async def hold_post(self, pose: Pose2D, until: datetime) -> SkillResult:
        args = {"pose": pose.model_dump(), "until": until.isoformat()}

        async def body(started: datetime) -> MotionResult:
            self._validate_pose(pose)
            if until.tzinfo is None:
                raise SkillRefused(
                    SkillOutcome.REFUSED_PRECONDITION, "until must be timezone-aware"
                )
            if until <= self.backend.now():
                raise SkillRefused(SkillOutcome.REFUSED_PRECONDITION, "until is in the past")
            result = await self._navigate(pose, started, SkillName.HOLD_POST)
            if result.outcome is not SkillOutcome.SUCCEEDED:
                return result
            self._check_cancel()
            seconds = (until - self.backend.now()).total_seconds()
            held = await self.backend.dwell(seconds, self._remaining(started, SkillName.HOLD_POST))
            if held.outcome is SkillOutcome.SUCCEEDED:
                return MotionResult(SkillOutcome.SUCCEEDED, "held post until deadline")
            return held

        return await self._run(SkillName.HOLD_POST, args, body)

    async def return_home(self) -> SkillResult:
        async def body(started: datetime) -> MotionResult:
            return await self._navigate(self.campus.staging, started, SkillName.RETURN_HOME)

        return await self._run(SkillName.RETURN_HOME, {}, body)

    async def cancel(self) -> SkillResult:
        async def body(started: datetime) -> MotionResult:
            active = self._active
            self._cancel_requested = True
            cancelled = await self.backend.cancel()
            if active is None and not cancelled:
                return MotionResult(SkillOutcome.SUCCEEDED, "no active skill")
            return MotionResult(
                SkillOutcome.SUCCEEDED, f"cancelled {active.value if active else 'goal'}"
            )

        return await self._run(SkillName.CANCEL, {}, body)

    async def stop(self) -> SkillResult:
        """Immediate stop; allowed while disarmed."""

        async def body(started: datetime) -> MotionResult:
            self._cancel_requested = True
            await self.backend.stop()
            return MotionResult(SkillOutcome.SUCCEEDED, "stopped")

        return await self._run(SkillName.STOP, {}, body)

    async def capture_event_image(self, max_kb: int = MAX_EVENT_IMAGE_KB) -> SkillResult:
        async def body(started: datetime) -> str:
            if not 0 < max_kb <= MAX_EVENT_IMAGE_KB:
                raise SkillRefused(
                    SkillOutcome.REFUSED_PRECONDITION,
                    f"max_kb must be in 1..{MAX_EVENT_IMAGE_KB}, got {max_kb}",
                )
            return await self.backend.capture_image(max_kb)

        return await self._run(SkillName.CAPTURE_EVENT_IMAGE, {"max_kb": max_kb}, body)
