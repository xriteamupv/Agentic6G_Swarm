"""ROS-free logic of the Go2 shims, unit-tested without ROS or Gazebo."""

import math
from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------- cmd_vel_to_sport


@dataclass
class SportLimits:
    """The real ``cmd_vel_to_sport`` contract (SPEC A1 7.2); every value is a ROS parameter."""

    vx_max: float = 0.30
    wz_max: float = 0.60
    static_walk_wz_max: float = 0.105  # in-place yaw: ~30 s for 180 deg
    static_walk_vx_threshold: float = 0.05  # below this |vx| the robot turns in static walk
    timeout_s: float = 0.5
    settle_s: float = 0.5
    deadband: float = 1e-3


@dataclass
class SportCommandFilter:
    """Turns ``cmd_vel`` into the velocity the proxy actually executes.

    Clamps (vy is always 0), stops after ``timeout_s`` without commands, holds still for
    ``settle_s`` when starting from standstill and limits in-place yaw to static-walk speed.
    """

    limits: SportLimits = field(default_factory=SportLimits)
    _last_cmd: tuple[float, float] = (0.0, 0.0)
    _last_cmd_t: float | None = None
    _moving: bool = False
    _start_request_t: float | None = None

    def command(self, vx: float, vy: float, wz: float, now: float) -> None:
        del vy  # cmd_vel_to_sport sends vy = 0
        lim = self.limits
        vx = max(-lim.vx_max, min(lim.vx_max, vx))
        wz = max(-lim.wz_max, min(lim.wz_max, wz))
        if abs(vx) < lim.static_walk_vx_threshold:
            wz = max(-lim.static_walk_wz_max, min(lim.static_walk_wz_max, wz))
        self._last_cmd = (vx, wz)
        self._last_cmd_t = now

    def stop(self) -> None:
        """StopMove: back to standstill (the next motion pays the settle delay again)."""
        self._last_cmd = (0.0, 0.0)
        self._last_cmd_t = None
        self._moving = False
        self._start_request_t = None

    def output(self, now: float) -> tuple[float, float, float]:
        """(vx, vy, wz) to execute at time ``now``."""
        lim = self.limits
        if self._last_cmd_t is None or now - self._last_cmd_t > lim.timeout_s:
            self.stop()
            return 0.0, 0.0, 0.0
        vx, wz = self._last_cmd
        if abs(vx) < lim.deadband and abs(wz) < lim.deadband:
            self._moving = False
            self._start_request_t = None
            return 0.0, 0.0, 0.0
        if not self._moving:
            if self._start_request_t is None:
                self._start_request_t = now
            if now - self._start_request_t < lim.settle_s:
                return 0.0, 0.0, 0.0
            self._moving = True
        return vx, 0.0, wz


# ---------------------------------------------------------------- sport_api_sim

STOP_MOVE = 1003


@dataclass
class SportApiGuard:
    """Accepts only StopMove; everything else is a violation (counted, never executed)."""

    allowed: frozenset[int] = frozenset({STOP_MOVE})
    violations: int = 0
    last_violation: int | None = None

    def handle(self, api_id: int) -> bool:
        if api_id in self.allowed:
            return True
        self.violations += 1
        self.last_violation = api_id
        return False


# ---------------------------------------------------------------- leg_odom_sim


@dataclass
class LegOdometry:
    """Integrates the true body twist with leg-odometry errors (mild drift by default).

    Starts at the origin of ``odom`` like the real robot after power-up.
    """

    linear_scale_error: float = 0.02  # 2 % over-reported distance
    yaw_rate_bias: float = 0.0005  # rad/s while moving (~1.7 deg per minute)
    linear_noise: float = 0.01  # m/s (1 sigma)
    angular_noise: float = 0.005  # rad/s (1 sigma)
    seed: int = 0
    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0
    _rng: np.random.Generator = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = np.random.default_rng(self.seed)

    def step(self, vx: float, vy: float, wz: float, dt: float) -> tuple[float, float, float]:
        """Advance by ``dt`` with the true body twist; return the reported (vx, vy, wz)."""
        moving = abs(vx) > 1e-3 or abs(vy) > 1e-3 or abs(wz) > 1e-3
        if moving:
            vx = vx * (1 + self.linear_scale_error) + self._rng.normal(0, self.linear_noise)
            vy = vy * (1 + self.linear_scale_error) + self._rng.normal(0, self.linear_noise)
            wz = wz + self.yaw_rate_bias + self._rng.normal(0, self.angular_noise)
        else:
            vx = vy = wz = 0.0
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        self.x += (c * vx - s * vy) * dt
        self.y += (s * vx + c * vy) * dt
        self.yaw = math.atan2(math.sin(self.yaw + wz * dt), math.cos(self.yaw + wz * dt))
        return vx, vy, wz


# ---------------------------------------------------------------- cloud_to_scan


@dataclass
class ScanBinning:
    """3D points in ``base_link`` -> planar ranges (go2_nav's cloud_to_scan parameters)."""

    min_height: float = -0.15
    max_height: float = 0.30
    angle_increment: float = math.radians(0.5)
    range_min: float = 0.10
    range_max: float = 30.0
    # Points inside the body (plus margin) are the robot itself.
    self_box: tuple[float, float, float, float] = (-0.40, 0.40, -0.20, 0.20)

    @property
    def n_bins(self) -> int:
        return round(2 * math.pi / self.angle_increment)

    @property
    def angle_min(self) -> float:
        return -math.pi

    def ranges(self, xyz: np.ndarray) -> np.ndarray:
        """``xyz``: (N, 3) in base_link. Returns ``n_bins`` ranges (inf where nothing)."""
        out = np.full(self.n_bins, np.inf, dtype=np.float32)
        if xyz.size == 0:
            return out
        x, y, z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
        x0, x1, y0, y1 = self.self_box
        keep = (
            np.isfinite(x)
            & np.isfinite(y)
            & np.isfinite(z)
            & (z >= self.min_height)
            & (z <= self.max_height)
            & ~((x > x0) & (x < x1) & (y > y0) & (y < y1))
        )
        x, y = x[keep], y[keep]
        r = np.hypot(x, y)
        ok = (r >= self.range_min) & (r <= self.range_max)
        r, a = r[ok], np.arctan2(y[ok], x[ok])
        idx = np.floor((a - self.angle_min) / self.angle_increment).astype(int) % self.n_bins
        np.minimum.at(out, idx, r.astype(np.float32))
        return out


# ---------------------------------------------------------------- transforms


def yaw_from_quat(x: float, y: float, z: float, w: float) -> float:
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def quat_from_yaw(yaw: float) -> tuple[float, float, float, float]:
    return 0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)


def matrix_from_tq(t, q) -> np.ndarray:
    """4x4 homogeneous matrix from translation (x, y, z) and quaternion (x, y, z, w)."""
    x, y, z, w = q
    m = np.eye(4)
    m[:3, :3] = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    m[:3, 3] = t
    return m


def transform_points(m: np.ndarray, xyz: np.ndarray) -> np.ndarray:
    return xyz @ m[:3, :3].T + m[:3, 3]
