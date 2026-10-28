"""Acceptance in Gazebo (``pytest --gazebo``): GNSS localization.

``gnss_ekf``: robot_localization dual EKF (local: odometry + IMU; global: + GNSS through
navsat_transform, datum = campus datum). The simulated receiver adds the profile's noise.

- G1 with ``rtk``: ``navigate_to`` the library door from each robot's spawn (across the open
  plaza, where AMCL fails for the Go2); final ground-truth error <= 0.30 m.
- G1 with ``standard``: error statistics only. The EKF's covariance then exceeds the
  manifests' ``localization_cov_max`` (0.25 m^2), so the skill server rightly refuses to
  move; this run raises the limit to collect the statistics.
- A 200 m route under ``rtk`` with no localization jump larger than 1 m.
- ``loc_eval`` CSVs are produced for every run.
"""

import asyncio
import csv
import itertools
import json
import math
import os
from pathlib import Path

import pytest
from a6g_common.config import load_config
from a6g_common.schemas import Pose2D, SkillOutcome
from gz_harness import available, gazebo_robot, localized, skill_service

pytestmark = pytest.mark.gazebo

POI = "poi_library_door"
DOMAINS = {"summit_01": 11, "go2_01": 21}
#: 205 m loop from the staging area, >= 2.5 m from every obstacle (campus_map).
ROUTE = [(20, -15), (20, 5), (-60, 5), (-60, -15), (0, -15)]


@pytest.fixture(scope="module", autouse=True)
def _gazebo_available() -> None:
    reason = available()
    if reason:
        pytest.skip(reason)
    if float(os.environ.get("A6G_WORLD_SCALE") or 1.0) != 1.0:
        pytest.skip("routes and tolerances are designed for the full-scale campus")


@pytest.fixture(scope="module")
def cfg():
    return load_config("config", env={}, profile="gazebo")


def loc_eval_rows(out: Path) -> list[dict[str, float]]:
    files = sorted((out / "loc_eval").glob("*_gnss_ekf_*.csv"))
    assert files, "loc_eval produced no CSV"
    with files[-1].open() as f:
        return [{k: float(v) for k, v in row.items()} for row in csv.DictReader(f)]


def stats(rows: list[dict[str, float]]) -> dict[str, float]:
    err = sorted(r["err_xy"] for r in rows)
    return {
        "samples": len(err),
        "mean_m": round(sum(err) / len(err), 3),
        "p95_m": round(err[int(0.95 * (len(err) - 1))], 3),
        "max_m": round(err[-1], 3),
    }


def max_jump(rows: list[dict[str, float]]) -> float:
    """Largest change of the estimate between samples not explained by the true motion."""
    jumps = [
        math.hypot(
            (b["est_x"] - a["est_x"]) - (b["gt_x"] - a["gt_x"]),
            (b["est_y"] - a["est_y"]) - (b["gt_y"] - a["gt_y"]),
        )
        for a, b in itertools.pairwise(rows)
    ]
    return max(jumps)


def run_to_poi(cfg, robot: str, out: Path, noise: str, cov_max: float | None = None):
    with gazebo_robot(
        robot, DOMAINS[robot], out, "localization:=gnss_ekf", f"gnss_noise:={noise}"
    ) as run:
        svc = skill_service(run, cfg, robot, out, localization="gnss_ekf")
        if cov_max is not None:
            svc.manifest = svc.manifest.model_copy(update={"localization_cov_max": cov_max})

        async def go():
            await localized(svc)
            result = await svc.navigate_to(poi_id=POI)
            await svc.backend.client.close()
            return result

        result = asyncio.run(go())
        x, y, _ = run.ground_truth()
    poi = cfg.campus.poi(POI).pose
    return result, math.hypot(x - poi.x, y - poi.y)


@pytest.mark.parametrize("robot", ["summit_01", "go2_01"])
def test_g1_rtk(cfg, robot: str, tmp_path: Path) -> None:
    result, error = run_to_poi(cfg, robot, tmp_path, "rtk")
    s = stats(loc_eval_rows(tmp_path))
    print(f"G1 rtk {robot}: final ground-truth error {error:.3f} m; localization {s}")
    assert result.outcome is SkillOutcome.SUCCEEDED, result
    assert error <= 0.30


@pytest.mark.parametrize("robot", ["summit_01", "go2_01"])
def test_g1_standard_statistics(cfg, robot: str, tmp_path: Path) -> None:
    result, error = run_to_poi(cfg, robot, tmp_path, "standard", cov_max=100.0)
    s = stats(loc_eval_rows(tmp_path))
    report = {"robot": robot, "outcome": result.outcome.value, "final_error_m": round(error, 3)}
    report.update({f"localization_{k}": v for k, v in s.items()})
    print("G1 standard:", json.dumps(report))
    out = Path("var/gz/a4")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"g1_standard_{robot}.json").write_text(json.dumps(report, indent=1))
    assert s["samples"] > 0  # statistics only


def test_200m_route_rtk_without_jumps(cfg, tmp_path: Path) -> None:
    length = sum(math.dist(a, b) for a, b in zip([(0, 0), *ROUTE], ROUTE, strict=False))
    assert length >= 200
    with gazebo_robot(
        "summit_01", 11, tmp_path, "localization:=gnss_ekf", "gnss_noise:=rtk"
    ) as run:
        svc = skill_service(run, cfg, "summit_01", tmp_path, localization="gnss_ekf")

        async def go():
            await localized(svc)
            # Each waypoint faces along the leg that ends there (no turn-in-place stops).
            legs = itertools.pairwise([(0, 0), *ROUTE])
            waypoints = [
                Pose2D(x=b[0], y=b[1], yaw=math.atan2(b[1] - a[1], b[0] - a[0])) for a, b in legs
            ]
            result = await svc.follow_route(waypoints)
            await svc.backend.client.close()
            return result

        result = asyncio.run(go())
        velocity = run.velocity_stats()
    rows = loc_eval_rows(tmp_path)
    jump = max_jump(rows)
    print(f"200 m route: {result.outcome}; max jump {jump:.3f} m; localization {stats(rows)}")
    assert result.outcome is SkillOutcome.SUCCEEDED, result
    assert jump <= 1.0
    # Reverses, if any, stay short and slow (reverse_limits).
    assert velocity["below_threshold"] == 0 and velocity["reverse_too_long"] == 0, velocity
