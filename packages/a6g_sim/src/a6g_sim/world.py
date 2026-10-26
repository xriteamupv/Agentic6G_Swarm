"""Lightweight 2D fleet simulator (SPEC Section 12).

Robots are points moving at their manifest speed, turning in place at the manifest turn
rate and draining battery per metre. Motion is expressed as *primitives* (turn, drive,
dwell); every primitive passes the :func:`check_primitive` motion guard, so a manifest's
``forbidden_motions`` (e.g. Go2: ``strafe``) can never be executed; ``reverse_limits``
(Summit) caps reverse hops in distance and speed.

The world is synchronous and driven by :meth:`SimWorld.step`; asynchronous callers (the
skill backend) wait on futures that ``step`` resolves. :class:`a6g_sim.runner.SimRunner`
ticks the world from an asyncio task.
"""

import asyncio
import contextlib
import math
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from a6g_common.clock import _OUTSIDE, ManualClock
from a6g_common.config import CampusConfig, Motion, RobotManifest
from a6g_common.geometry import point_in_polygon
from a6g_common.schemas import (
    LinkQuality,
    Pose2D,
    PoseWithCovariance,
    RobotState,
    SkillOutcome,
)

from a6g_sim.planner import footpath_planner_for, planner_for

#: The freezes (world, token) the current computation and its callers took (``frozen``).
_HELD: ContextVar[tuple[tuple[object, object], ...]] = ContextVar("a6g_frozen_held", default=())

#: A robot that may reverse does so only for short hops to goals behind it.
REVERSE_MAX_M = 1.0
#: Heading error below which no turn-in-place is planned before driving.
HEADING_EPS_RAD = 0.05
#: Default goal tolerance when a manifest does not declare one.
DEFAULT_XY_TOL_M = 0.25
DEFAULT_YAW_TOL_RAD = 0.25
#: Range reported by the simulated scan when nothing is in view.
SCAN_MAX_RANGE_M = 30.0


def wrap_angle(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


class PrimitiveKind(StrEnum):
    TURN = "turn"
    DRIVE = "drive"
    DWELL = "dwell"


@dataclass
class Primitive:
    kind: PrimitiveKind
    yaw: float = 0.0  # TURN target
    x: float = 0.0  # DRIVE target
    y: float = 0.0
    reverse: bool = False  # DRIVE backwards
    seconds: float = 0.0  # DWELL duration
    elapsed: float = 0.0

    @property
    def motions(self) -> set[Motion]:
        if self.kind is PrimitiveKind.TURN:
            return {Motion.TURN_IN_PLACE}
        if self.kind is PrimitiveKind.DRIVE and self.reverse:
            return {Motion.REVERSE}
        return set()


class ForbiddenMotionError(Exception):
    def __init__(self, robot_id: str, motion: Motion) -> None:
        self.motion = motion
        super().__init__(f"{robot_id}: {motion.value} motion is forbidden by the manifest")


def check_primitive(manifest: RobotManifest, primitive: Primitive) -> None:
    """Motion guard: refuse any primitive whose motion the manifest forbids."""
    for motion in primitive.motions:
        if motion in manifest.forbidden_motions:
            raise ForbiddenMotionError(manifest.robot_id, motion)


def plan_motion(pose: Pose2D, goal: Pose2D, manifest: RobotManifest) -> list[Primitive]:
    """Straight-line plan honouring the manifest's forbidden motions."""
    tol = manifest.goal_tolerance
    xy_tol = tol.xy_m if tol else DEFAULT_XY_TOL_M
    yaw_tol = tol.yaw_rad if tol else DEFAULT_YAW_TOL_RAD
    can_reverse = Motion.REVERSE not in manifest.forbidden_motions
    can_turn = Motion.TURN_IN_PLACE not in manifest.forbidden_motions

    plan: list[Primitive] = []
    heading = pose.yaw
    dx, dy = goal.x - pose.x, goal.y - pose.y
    dist = math.hypot(dx, dy)
    if dist > xy_tol:
        bearing = math.atan2(dy, dx)
        error = wrap_angle(bearing - pose.yaw)
        limits = manifest.reverse_limits
        reverse_max = min(REVERSE_MAX_M, limits.max_distance_m) if limits else REVERSE_MAX_M
        if can_reverse and abs(error) > 3 * math.pi / 4 and dist <= reverse_max:
            plan.append(Primitive(PrimitiveKind.DRIVE, x=goal.x, y=goal.y, reverse=True))
        else:
            if abs(error) > HEADING_EPS_RAD and can_turn:
                plan.append(Primitive(PrimitiveKind.TURN, yaw=bearing))
                heading = bearing
            plan.append(Primitive(PrimitiveKind.DRIVE, x=goal.x, y=goal.y))
    if can_turn and abs(wrap_angle(goal.yaw - heading)) > yaw_tol:
        plan.append(Primitive(PrimitiveKind.TURN, yaw=goal.yaw))
    for primitive in plan:
        check_primitive(manifest, primitive)
    return plan


@dataclass
class JobResult:
    outcome: SkillOutcome
    detail: str = ""
    native_code: int | None = None


@dataclass
class Job:
    kind: str  # "navigate" | "hold"
    primitives: deque[Primitive]
    deadline: datetime
    future: asyncio.Future[JobResult]


@dataclass
class Obstacle:
    x: float
    y: float
    radius: float


@dataclass
class SimRobot:
    manifest: RobotManifest
    x: float
    y: float
    yaw: float
    battery_pct: float = 100.0
    cov_xy: float = 0.01
    cov_yaw: float = 0.005
    failed: bool = False
    job: Job | None = None
    odometer_m: float = 0.0
    motion_log: list[Primitive] = field(default_factory=list)
    link_lost_until: datetime | None = None
    stop_count: int = 0

    @property
    def robot_id(self) -> str:
        return self.manifest.robot_id

    @property
    def pose(self) -> Pose2D:
        return Pose2D(x=self.x, y=self.y, yaw=self.yaw)


class SimWorld:
    def __init__(
        self,
        campus: CampusConfig,
        clock: ManualClock | None = None,
        charge_pct_per_s: float = 0.5,
    ) -> None:
        self.campus = campus
        self.clock = clock or ManualClock()
        #: Charging rate of an idle robot inside the staging area (the dock).
        self.charge_pct_per_s = charge_pct_per_s
        # Building footprints from the campus (SPEC A1 Rule 16): robots drive around them.
        self.planner = (
            footpath_planner_for(campus.route_graph, campus.walk_corridor_m)
            if campus.footpaths_only and campus.route_graph is not None
            else planner_for(campus.obstacles)
        )
        self.robots: dict[str, SimRobot] = {}
        self.obstacles: list[Obstacle] = []
        self.blocked_areas: list[list[tuple[float, float]]] = []
        #: Injected anomalies by POI id (label); the perception stub reports them.
        self.anomalies: dict[str, str] = {}
        #: Set while any robot has a job; lets the runner idle without burning sim time.
        self.activity = asyncio.Event()
        #: Called after every step with the world; used by scenarios and fault injection.
        self.step_hooks: list[Callable[[SimWorld], None]] = []
        #: Cleared while paused; the runner does not advance time until it is set again.
        self.unpaused = asyncio.Event()
        self.unpaused.set()
        self._manual_pause = False
        self._holds: set[object] = set()  # freezes held by agents computing
        self._open: set[object] = set()  # freezes whose computation has not ended
        #: True: model calls take no simulated time (the world waits for them). False
        #: (default): the world runs on at full speed while a model thinks; the thinking
        #: then lasts wall latency x the speed factor in simulated time.
        self.models_freeze_time = False
        self._sleepers: list[tuple[datetime, asyncio.Future[None]]] = []

    # ------------------------------------------------------------------ fleet

    def add_robot(
        self, manifest: RobotManifest, pose: Pose2D | None = None, battery_pct: float = 100.0
    ) -> SimRobot:
        if manifest.robot_id in self.robots:
            raise ValueError(f"robot {manifest.robot_id} already in the simulation")
        if pose is None:
            s = self.campus.staging
            if hasattr(self.planner, "walker"):  # footpaths only: line up along the path
                x, y = self.planner.walker.along((s.x, s.y), [1.5 * len(self.robots)])[0]
                pose = Pose2D(x=x, y=y, yaw=s.yaw)
            else:  # 1.5 m apart along y from the staging pose
                pose = Pose2D(x=s.x, y=s.y + 1.5 * len(self.robots), yaw=s.yaw)
        robot = SimRobot(manifest, pose.x, pose.y, pose.yaw, battery_pct=battery_pct)
        self.robots[manifest.robot_id] = robot
        return robot

    def robot(self, robot_id: str) -> SimRobot:
        try:
            return self.robots[robot_id]
        except KeyError:
            raise KeyError(f"unknown robot {robot_id!r}") from None

    # ------------------------------------------------------------------ sensing

    def link_quality(self, robot_id: str) -> LinkQuality:
        robot = self.robot(robot_id)
        if robot.link_lost_until and self.clock.now() < robot.link_lost_until:
            return LinkQuality.LOST
        if any(point_in_polygon((robot.x, robot.y), s) for s in self.campus.link_shadows):
            return LinkQuality.LOST
        if any(point_in_polygon((robot.x, robot.y), s) for s in self.campus.degraded_shadows):
            return LinkQuality.DEGRADED  # as in the Gazebo stack's link emulator
        return LinkQuality.OK

    def scan_min_range(self, robot_id: str) -> float:
        """Closest obstacle surface in the simulated scan (m)."""
        robot = self.robot(robot_id)
        ranges = [math.hypot(o.x - robot.x, o.y - robot.y) - o.radius for o in self.obstacles]
        return max(0.0, min(ranges, default=SCAN_MAX_RANGE_M))

    def state(self, robot_id: str, *, armed: bool = False) -> RobotState:
        robot = self.robot(robot_id)
        m = robot.manifest
        return RobotState(
            robot_id=robot.robot_id,
            platform=m.platform,
            pose=PoseWithCovariance(
                x=robot.x, y=robot.y, yaw=robot.yaw, cov_xy=robot.cov_xy, cov_yaw=robot.cov_yaw
            ),
            battery_pct=round(robot.battery_pct, 2) if m.battery_reliable else None,
            capabilities=[c.value for c in m.capabilities],
            link_quality=self.link_quality(robot_id),
            armed=armed,
            stamp=self.clock.now(),
        )

    # ------------------------------------------------------------------ jobs

    def _start(
        self,
        robot: SimRobot,
        kind: str,
        plan: list[Primitive],
        timeout_s: float,
        immediate: JobResult | None = None,
    ) -> Job:
        if robot.job is not None:
            raise RuntimeError(f"{robot.robot_id} already executing a {robot.job.kind} job")
        job = Job(
            kind=kind,
            primitives=deque(plan),
            deadline=self.clock.now() + timedelta(seconds=timeout_s),
            future=asyncio.get_running_loop().create_future(),
        )
        robot.job = job
        if robot.failed:
            self._finish(robot, JobResult(SkillOutcome.FAILED, "robot failure"))
        elif immediate is not None:
            self._finish(robot, immediate)
        elif not plan:
            self._finish(robot, JobResult(SkillOutcome.SUCCEEDED, "already at goal"))
        self.activity.set()
        return job

    def start_navigation(self, robot_id: str, goal: Pose2D, timeout_s: float) -> Job:
        """Plan and start a navigation; raises ForbiddenMotionError if the plan is unsafe."""
        robot = self.robot(robot_id)
        if any(point_in_polygon((goal.x, goal.y), a) for a in self.blocked_areas) or any(
            math.hypot(goal.x - o.x, goal.y - o.y) <= o.radius for o in self.obstacles
        ):
            no_path = JobResult(SkillOutcome.NO_PATH, "goal is not reachable")
            return self._start(robot, "navigate", [], timeout_s, immediate=no_path)
        path = self.planner.plan((robot.x, robot.y), (goal.x, goal.y))
        if path is None:
            why = getattr(self.planner, "reason", "") or "no path around the buildings"
            no_path = JobResult(SkillOutcome.NO_PATH, why)
            return self._start(robot, "navigate", [], timeout_s, immediate=no_path)
        plan: list[Primitive] = []
        here = robot.pose
        for x, y in path[:-1]:  # detour legs: turn towards the waypoint, drive forward
            heading = math.atan2(y - here.y, x - here.x)
            leg = Pose2D(x=x, y=y, yaw=heading)
            plan += plan_motion(here, leg, robot.manifest)
            here = leg
        plan += plan_motion(here, goal, robot.manifest)
        return self._start(robot, "navigate", plan, timeout_s)

    def start_primitives(self, robot_id: str, plan: list[Primitive], timeout_s: float) -> Job:
        """Execute explicit primitives, each checked by the motion guard first."""
        robot = self.robot(robot_id)
        for primitive in plan:
            check_primitive(robot.manifest, primitive)
        return self._start(robot, "navigate", plan, timeout_s)

    def start_hold(self, robot_id: str, seconds: float, timeout_s: float) -> Job:
        plan = [Primitive(PrimitiveKind.DWELL, seconds=max(0.0, seconds))]
        return self._start(self.robot(robot_id), "hold", plan, timeout_s)

    def cancel(self, robot_id: str) -> bool:
        robot = self.robot(robot_id)
        if robot.job is None:
            return False
        self._finish(robot, JobResult(SkillOutcome.CANCELLED, "cancelled"))
        return True

    def stop(self, robot_id: str) -> None:
        """Platform stop: cancel any job and hold still."""
        robot = self.robot(robot_id)
        robot.stop_count += 1
        if robot.job is not None:
            self._finish(robot, JobResult(SkillOutcome.CANCELLED, "stopped"))

    def _finish(self, robot: SimRobot, result: JobResult) -> None:
        job = robot.job
        if job is None:
            return
        robot.job = None
        if not job.future.done():
            job.future.set_result(result)

    def busy(self) -> bool:
        """Simulated time needs to flow: a robot is moving or charging, or someone sleeps."""
        return any(r.job is not None or self.charging(r) for r in self.robots.values()) or any(
            not f.done() for _, f in self._sleepers
        )

    def at_dock(self, robot: SimRobot) -> bool:
        """Inside the staging area (or within 2 m of the staging pose without one)."""
        area = self.campus.staging_area
        if area:
            return point_in_polygon((robot.x, robot.y), area)
        st = self.campus.staging
        return math.hypot(robot.x - st.x, robot.y - st.y) <= 2.0

    def charging(self, robot: SimRobot) -> bool:
        return (
            robot.job is None
            and not robot.failed
            and robot.battery_pct < 100.0
            and self.charge_pct_per_s > 0
            and self.at_dock(robot)
        )

    # ------------------------------------------------------------------ faults

    def fail_robot(self, robot_id: str) -> None:
        robot = self.robot(robot_id)
        robot.failed = True
        self._finish(robot, JobResult(SkillOutcome.FAILED, "robot failure"))

    def inject_link_loss(self, robot_id: str, seconds: float) -> None:
        self.robot(robot_id).link_lost_until = self.clock.now() + timedelta(seconds=seconds)

    def inject_anomaly(self, poi_id: str, label: str) -> None:
        self.campus.poi(poi_id)  # raises StopIteration for unknown POIs
        self.anomalies[poi_id] = label

    def add_obstacle(self, x: float, y: float, radius: float) -> Obstacle:
        obstacle = Obstacle(x, y, radius)
        self.obstacles.append(obstacle)
        return obstacle

    # ------------------------------------------------------------------ time

    @property
    def paused(self) -> bool:
        return not self.unpaused.is_set()

    def _update_pause(self) -> None:
        if self._manual_pause or self._holds:
            self.unpaused.clear()
        else:
            self.unpaused.set()

    def pause(self) -> None:
        """Freeze simulated time (scripted scenarios inject faults at exact moments)."""
        self._manual_pause = True
        self._update_pause()

    def resume(self) -> None:
        self._manual_pause = False
        self._update_pause()

    @contextlib.asynccontextmanager
    async def frozen(self) -> AsyncIterator[None]:
        """Freeze simulated time while agents compute (re-entrant).

        Agents and the coordinator take wall-clock time to think; in a simulator running
        faster than real time that would show up as minutes of simulated time. Wrapping
        each graph invocation makes computation instantaneous in simulated time, so
        latency KPIs and heartbeats are measured on the robots' time base.
        """
        token = object()
        lineage = _HELD.get()
        reset_held = _HELD.set((*lineage, (self, token)))
        reset_thaw = _OUTSIDE.set(self._thaw)
        self._holds.add(token)
        self._open.add(token)
        self._update_pause()
        try:
            yield
        finally:
            self._open.discard(token)
            self._holds.discard(token)
            self._update_pause()
            _OUTSIDE.reset(reset_thaw)
            _HELD.reset(reset_held)

    @contextlib.asynccontextmanager
    async def _thaw(self) -> AsyncIterator[None]:
        """Release the freezes of this computation (and of the computations it was called
        from) while it waits on a model, a service or another agent: the world keeps
        running at full speed. Kept when ``models_freeze_time``."""
        mine = [t for w, t in _HELD.get() if w is self and t in self._holds]
        if not mine or self.models_freeze_time:
            yield
            return
        self._holds.difference_update(mine)
        self._update_pause()
        try:
            yield
        finally:
            # Only freezes still open: a task that outlived the computation that froze
            # time (a killed coordinator's send) must not bring its freeze back for good.
            self._holds.update(t for t in mine if t in self._open)
            self._update_pause()

    async def sleep(self, seconds: float) -> None:
        """Sleep in simulated time (heartbeats, monitors). Idle or paused worlds do not wake."""
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.clock.now() + timedelta(seconds=seconds), future))
        self.activity.set()  # time must flow for a sleeper to wake
        try:
            await future
        finally:
            self._sleepers = [(t, f) for t, f in self._sleepers if f is not future]

    def pause_when(self, predicate: Callable[["SimWorld"], bool]) -> asyncio.Event:
        """Pause the first time ``predicate(world)`` holds after a step; returns an event."""
        reached = asyncio.Event()

        def hook(world: SimWorld) -> None:
            if not reached.is_set() and predicate(world):
                world.pause()
                reached.set()
                world.step_hooks.remove(hook)

        self.step_hooks.append(hook)
        return reached

    def step(self, dt: float) -> None:
        self.clock.advance(dt)
        now = self.clock.now()
        for robot in self.robots.values():
            job = robot.job
            if job is None:
                if self.charging(robot):
                    robot.battery_pct = min(100.0, robot.battery_pct + self.charge_pct_per_s * dt)
                continue
            if robot.failed:
                self._finish(robot, JobResult(SkillOutcome.FAILED, "robot failure"))
                continue
            if now >= job.deadline:
                self._finish(robot, JobResult(SkillOutcome.TIMEOUT, "skill timeout"))
                continue
            self._advance(robot, dt)
        for hook in list(self.step_hooks):
            hook(self)
        for wake_at, future in list(self._sleepers):
            if now >= wake_at and not future.done():
                future.set_result(None)
        if not self.busy():
            self.activity.clear()

    def _advance(self, robot: SimRobot, dt: float) -> None:
        m = robot.manifest
        budget = dt
        while budget > 1e-9 and robot.job is not None and robot.job.primitives:
            prim = robot.job.primitives[0]
            if prim.kind is PrimitiveKind.DWELL:
                use = min(budget, prim.seconds - prim.elapsed)
                prim.elapsed += use
                budget -= use
                done = prim.elapsed >= prim.seconds - 1e-9
            elif prim.kind is PrimitiveKind.TURN:
                error = wrap_angle(prim.yaw - robot.yaw)
                need = abs(error) * m.turn_in_place_s_per_rad
                done = budget >= need
                if done:
                    robot.yaw = prim.yaw
                    budget -= need
                else:
                    turned = budget / m.turn_in_place_s_per_rad
                    robot.yaw = wrap_angle(robot.yaw + math.copysign(turned, error))
                    budget = 0.0
            else:  # DRIVE
                dx, dy = prim.x - robot.x, prim.y - robot.y
                remaining = math.hypot(dx, dy)
                speed = (
                    m.reverse_limits.max_speed_mps
                    if prim.reverse and m.reverse_limits
                    else m.max_speed_mps
                )
                step_m = min(remaining, speed * budget)
                if remaining > 1e-9:
                    nx = robot.x + dx / remaining * step_m
                    ny = robot.y + dy / remaining * step_m
                    if self._closing_in(robot, nx, ny, m.clearance_m):
                        self._finish(
                            robot, JobResult(SkillOutcome.BLOCKED, "obstacle within clearance")
                        )
                        return
                    robot.x, robot.y = nx, ny
                    robot.odometer_m += step_m
                    robot.battery_pct = max(
                        0.0, robot.battery_pct - m.battery_drain_pct_per_m * step_m
                    )
                budget -= step_m / speed
                done = step_m >= remaining - 1e-9
                if robot.battery_pct <= 0.0:
                    robot.failed = True
                    self._finish(robot, JobResult(SkillOutcome.FAILED, "battery depleted"))
                    return
            if done:
                robot.motion_log.append(robot.job.primitives.popleft())
        if robot.job is not None and not robot.job.primitives:
            self._finish(robot, JobResult(SkillOutcome.SUCCEEDED, "goal reached"))

    def _closing_in(self, robot: SimRobot, x: float, y: float, clearance: float) -> bool:
        """True if moving to (x, y) brings an obstacle inside the clearance radius."""
        for o in self.obstacles:
            after = math.hypot(o.x - x, o.y - y) - o.radius
            before = math.hypot(o.x - robot.x, o.y - robot.y) - o.radius
            if after < clearance and after < before:
                return True
        return False
