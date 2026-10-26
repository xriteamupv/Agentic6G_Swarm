"""UC2-KPI-11 bench (SPEC A3.8, stage E5): time from a change to emergency written to the
twin until reconciled envelopes are published, p50 and p95 over seeded runs.

Each seed runs a fresh 2D-simulator stack: the patrol starts, a weather alert arrives at a
seeded moment of the patrol, and the coordinator's ``reconcile`` decision for the KPI gives
the latency (wall clock: computation, not simulated time). With a model, its decomposition
runs after the template's publication (``decompose_mode: template_then_model``); the bench
also reports when that follow-up was published. KPI targets are orientative (decision of
2026-10-16): the measured value is reported against the target.
"""

import asyncio
import json
import random
import statistics
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from a6g_common.config import A6GConfig
from a6g_common.schemas import DecisionKind

from a6g_devstack.stack import SimStack

TARGET_S = 3.0


def _kpi(stack: SimStack) -> list[dict[str, Any]]:
    return [
        d.output
        for d in stack.log.read()
        if d.kind is DecisionKind.RECONCILE and d.output.get("kpi") == "UC2-KPI-11"
    ]


async def run_seed(
    cfg: A6GConfig, seed: int, model: Any = None, robot_model: Any = None, timeout_s: float = 120
) -> dict[str, Any]:
    rng = random.Random(seed)
    with tempfile.TemporaryDirectory(prefix=f"kpi11-{seed}-") as tmp:
        stack = SimStack(cfg, Path(tmp), model=model, robot_model=robot_model)
        await stack.start()
        try:
            await stack.mission("Patrol the campus")
            start = stack.world.clock.now()
            wait = rng.uniform(30.0, 600.0)  # the alert comes at a seeded moment of the patrol
            async with asyncio.timeout(timeout_s):
                while (stack.world.clock.now() - start).total_seconds() < wait:
                    await asyncio.sleep(0.05)
            alerted = time.time()
            await stack.weather_alert()
            async with asyncio.timeout(timeout_s):
                while not _kpi(stack):
                    await asyncio.sleep(0.02)
            first = _kpi(stack)[0]
            followup_s = None
            if model is not None:  # the model's own plan, published afterwards
                try:
                    async with asyncio.timeout(timeout_s):
                        while not any(
                            d.kind is DecisionKind.DECOMPOSE and d.policy.get("phase") == "model"
                            for d in stack.log.read()
                        ):
                            await asyncio.sleep(0.05)
                    followup_s = round(time.time() - alerted, 3)
                except TimeoutError:
                    followup_s = None  # reported as missing, the KPI stands
        finally:
            await stack.stop()
    return {
        "seed": seed,
        "alert_after_s_sim": round(wait, 1),
        "kpi11_s": first["posture_change_to_publication_s"],
        "acknowledged_s": first["posture_change_to_acknowledged_s"],
        "robots": first["robots"],
        "model_plan_published_after_s": followup_s,
    }


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 3)


async def run(
    cfg: A6GConfig, seeds: int, model: Any = None, robot_model: Any = None, label: str = "fake"
) -> dict[str, Any]:
    rows = []
    for seed in range(seeds):
        try:
            rows.append(await run_seed(cfg, seed, model, robot_model))
        except Exception as exc:  # a failed run is reported, not hidden
            rows.append({"seed": seed, "error": repr(exc)})
    ok = [r for r in rows if "error" not in r]
    if not ok:
        raise RuntimeError(f"every run failed: {rows[0]['error']}")
    values = [r["kpi11_s"] for r in ok]
    acks = [r["acknowledged_s"] for r in ok]
    return {
        "kpi": "UC2-KPI-11",
        "definition": "posture change to emergency written to the twin -> reconciled "
        "envelopes published (wall clock); acknowledged_*: until every robot has answered",
        "simulator": "2D",
        "model": label,
        "decompose_mode": cfg.fleet.defaults.coordinator.decompose_mode,
        "seeds": seeds,
        "failed_runs": len(rows) - len(ok),
        "p50_s": percentile(values, 0.5),
        "p95_s": percentile(values, 0.95),
        "max_s": max(values),
        "mean_s": round(statistics.fmean(values), 3),
        "acknowledged_p50_s": percentile(acks, 0.5),
        "acknowledged_p95_s": percentile(acks, 0.95),
        "target_p95_s": TARGET_S,
        "meets_target": percentile(values, 0.95) <= TARGET_S,
        "runs": rows,
        "stamp": datetime.now(UTC).isoformat(),
    }


def write(result: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"e5_kpi11_{result['model']}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(result, indent=2))
    return path
