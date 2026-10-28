import math

import numpy as np
import pytest
from a6g_gz_go2_shim.logic import (
    STOP_MOVE,
    LegOdometry,
    ScanBinning,
    SportApiGuard,
    SportCommandFilter,
    matrix_from_tq,
    quat_from_yaw,
    transform_points,
)


def run(f: SportCommandFilter, t0: float, t1: float, cmd=None, dt=0.05):
    out, t = [], t0
    while t < t1 - 1e-9:
        if cmd is not None:
            f.command(*cmd, t)
        out.append((t, f.output(t)))
        t += dt
    return out


def test_clamps_and_vy_zero():
    f = SportCommandFilter()
    run(f, 0.0, 1.0, cmd=(1.0, 0.5, 2.0))
    vx, vy, wz = f.output(1.0)
    assert (vx, vy, wz) == (0.30, 0.0, 0.60)
    f.command(-1.0, 0.0, -2.0, 1.0)
    assert f.output(1.0) == (-0.30, 0.0, -0.60)


def test_settle_delay_from_standstill():
    f = SportCommandFilter()
    out = run(f, 0.0, 1.0, cmd=(0.2, 0.0, 0.0))
    moving = [t for t, (vx, _, _) in out if vx > 0]
    assert moving and moving[0] == pytest.approx(0.5, abs=0.051)


def test_timeout_stops_and_settle_again():
    f = SportCommandFilter()
    run(f, 0.0, 1.0, cmd=(0.2, 0.0, 0.0))
    assert f.output(1.4)[0] == 0.2  # within 0.5 s of the last command
    assert f.output(1.6) == (0.0, 0.0, 0.0)  # > 0.5 s without commands
    out = run(f, 2.0, 2.3, cmd=(0.2, 0.0, 0.0))
    assert all(v == (0.0, 0.0, 0.0) for _, v in out)  # settling again


def test_static_walk_yaw_limit():
    f = SportCommandFilter()
    run(f, 0.0, 1.0, cmd=(0.0, 0.0, 0.6))
    assert f.output(1.0)[2] == pytest.approx(0.105)
    run(f, 1.0, 2.0, cmd=(0.2, 0.0, 0.6))
    assert f.output(2.0)[2] == pytest.approx(0.6)
    # 180 deg in place takes about 30 s
    assert math.pi / 0.105 == pytest.approx(30, abs=0.5)


def test_stop_move_resets():
    f = SportCommandFilter()
    run(f, 0.0, 1.0, cmd=(0.2, 0.0, 0.0))
    f.stop()
    assert f.output(1.0) == (0.0, 0.0, 0.0)


def test_sport_guard_counts_violations():
    g = SportApiGuard()
    assert g.handle(STOP_MOVE)
    assert not g.handle(1008)  # Move
    assert not g.handle(1004)  # StandUp
    assert g.violations == 2 and g.last_violation == 1004


def test_leg_odometry_drifts_mildly():
    o = LegOdometry(seed=1)
    for _ in range(1000):  # 100 s straight at 0.3 m/s
        o.step(0.3, 0.0, 0.0, 0.1)
    assert 30.0 < o.x < 31.5
    assert abs(o.y) < 3.0
    still = LegOdometry(seed=1)
    for _ in range(100):
        still.step(0.0, 0.0, 0.0, 0.1)
    assert (still.x, still.y, still.yaw) == (0.0, 0.0, 0.0)


def test_scan_binning():
    b = ScanBinning()
    assert b.n_bins == 720
    pts = np.array(
        [
            [2.0, 0.0, 0.0],  # ahead, in band
            [3.0, 0.0, 0.0],  # same bin, farther
            [0.0, 1.5, 0.5],  # above the band
            [0.0, -1.0, -0.1],  # right, in band
            [0.2, 0.0, 0.0],  # self hit
            [np.nan, 0.0, 0.0],
        ]
    )
    r = b.ranges(pts)
    ahead = int((0.0 - b.angle_min) / b.angle_increment)
    right = int((-math.pi / 2 - b.angle_min) / b.angle_increment)
    left = int((math.pi / 2 - b.angle_min) / b.angle_increment)
    assert r[ahead] == pytest.approx(2.0)
    assert r[right] == pytest.approx(1.0)
    assert math.isinf(r[left])
    assert np.isfinite(r).sum() == 2


def test_transform_points():
    m = matrix_from_tq((1.0, 2.0, 0.0), quat_from_yaw(math.pi / 2))
    p = transform_points(m, np.array([[1.0, 0.0, 0.5]]))
    assert p[0] == pytest.approx([1.0, 3.0, 0.5])
