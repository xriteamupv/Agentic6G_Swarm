"""``a6g bench scarcity`` (SPEC A4.7): scenario G13 swept over the uplink capacity of the
campus cell, the reconciliation variants and the fleet sizes; fake models, 200x.

Each run is a child process (a crash or hang costs that run only); its row is saved next
to the run (``row.json``) so a rerun skips finished runs. Output: ``n4_scarcity_*.json``
and ``paper/fig_scarcity.csv`` (one row per run).
"""

import asyncio
import contextlib
import csv
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from a6g_common.config import A6GConfig

FIG_COLUMNS = (
    "variant",
    "capacity_mbps",
    "fleet",
    "seed",
    "posts_planned",
    "posts_held",
    "posts_video_usable",
    "posts_sensing_usable",
    "oversubscription",
    "i1_violations",
    "i2_violations",
    "i3_violations",
    "posture_change_to_reconciled_s",
    "reconcile_iterations_max",
    "switched_to_telemetry",
    "escalations",
    "kpi3_share",
)


async def _child(job: list[Any], wall_s: float) -> dict[str, Any]:
    row = Path(job[1]) / "row.json"
    if row.exists():
        with contextlib.suppress(ValueError):
            return json.loads(row.read_text())
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "a6g_devstack.scenario_g13",
        json.dumps(job),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), wall_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
    if row.exists():
        with contextlib.suppress(ValueError):
            return json.loads(row.read_text())
    _, _, capacity, variant, fleet, seed = job
    return {
        "variant": variant,
        "capacity_mbps": capacity,
        "fleet": fleet,
        "seed": seed,
        "error": f"child process exited with {proc.returncode} (no row)",
    }


async def run(
    cfg: A6GConfig,
    workdir: Path,
    capacities: tuple[float, ...],
    variants: tuple[str, ...],
    fleets: tuple[int, ...],
    seeds: int,
    jobs: int,
    coverage_summary: dict[str, Any],
) -> dict[str, Any]:
    skipped: dict[str, str] = {}
    run_variants = []
    for v in variants:
        if v == "reconcile_relocate" and not coverage_summary.get("exposed_below_good"):
            skipped[v] = (
                "no exposed POI below `good` on the coverage map: relocate_for_coverage has "
                "nothing to relocate (SPEC A4.7)"
            )
            continue
        run_variants.append(v)
    exposed = sum(1 for p in cfg.campus.pois if p.exposed)
    run_fleets = []
    for f in fleets:
        if f >= 8 and exposed < 8:
            skipped[f"fleet {f}"] = f"the campus has {exposed} exposed POIs (< 8)"
            continue
        run_fleets.append(f)
    plan = [
        [
            str(cfg.config_dir),
            str(workdir / f"{v}_c{c:g}_f{f}_s{s}"),
            c,
            v,
            f,
            s,
        ]
        for c in capacities
        for v in run_variants
        for f in run_fleets
        for s in range(seeds)
    ]
    gate = asyncio.Semaphore(max(1, jobs))

    async def one(job: list[Any]) -> dict[str, Any]:
        async with gate:
            return await _child(job, wall_s=900)

    rows = list(await asyncio.gather(*(one(j) for j in plan)))

    def med(values: list[float]) -> float | None:
        return round(statistics.median(values), 3) if values else None

    table = {}
    for c in capacities:
        for v in run_variants:
            for f in run_fleets:
                rs = [
                    r
                    for r in rows
                    if r["capacity_mbps"] == c
                    and r["variant"] == v
                    and r["fleet"] == f
                    and "error" not in r
                ]
                table[f"{v}_c{c:g}_f{f}"] = {
                    "runs": len(rs),
                    "errors": sum(
                        1
                        for r in rows
                        if r["capacity_mbps"] == c
                        and r["variant"] == v
                        and r["fleet"] == f
                        and "error" in r
                    ),
                    **{
                        f"{k}_median": med([float(r[k]) for r in rs if r.get(k) is not None])
                        for k in (
                            "posts_held",
                            "posts_video_usable",
                            "posts_sensing_usable",
                            "oversubscription",
                            "posture_change_to_reconciled_s",
                            "kpi3_share",
                        )
                    },
                    "i1_violations": sum(r["i1_violations"] for r in rs),
                    "i2_violations": sum(r["i2_violations"] for r in rs),
                    "i3_violations": sum(r["i3_violations"] for r in rs),
                    "escalations": sum(r["escalations"] for r in rs),
                }
    return {
        "scenario": "G13 (SPEC A4.7)",
        "network": "emulated (capacity model of the noa_stub; rule 32)",
        "coverage": coverage_summary.get("label"),
        "fidelity": "functional (2D simulator, fake models)",
        "time_factor": "200x",
        "capacities_mbps": list(capacities),
        "variants": run_variants,
        "fleets": run_fleets,
        "seeds": seeds,
        "skipped": skipped,
        "table": table,
        "rows": rows,
        "stamp": datetime.now(UTC).isoformat(),
    }


def write(result: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"n4_scarcity_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(result, indent=2, default=str))
    return path


def write_csv(result: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FIG_COLUMNS)
        for r in result["rows"]:
            if "error" in r:
                continue
            w.writerow(["" if r.get(k) is None else r.get(k) for k in FIG_COLUMNS])
    return path


def summary(result: dict[str, Any]) -> str:
    lines = ["variant         cap  fleet  held  video  sensing  oversub  I1 I2 I3  esc  kpi3"]
    for key, c in result["table"].items():
        v, rest = key.rsplit("_c", 1)
        cap, fleet = rest.split("_f")
        lines.append(
            f"{v:<15} {cap:>4} {fleet:>5}  {c['posts_held_median']!s:>4}  "
            f"{c['posts_video_usable_median']!s:>5}  {c['posts_sensing_usable_median']!s:>7}  "
            f"{c['oversubscription_median']!s:>7}  {c['i1_violations']:>2} {c['i2_violations']:>2} "
            f"{c['i3_violations']:>2}  {c['escalations']:>3}  {c['kpi3_share_median']!s:>4}"
            + (f"  ({c['errors']} errors)" if c["errors"] else "")
        )
    for what, why in result["skipped"].items():
        lines.append(f"skipped {what}: {why}")
    return "\n".join(lines)
