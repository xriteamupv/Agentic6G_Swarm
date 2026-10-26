"""Baselines B0-B2 against the proposed system P (SPEC A3.8): UC2-KPI-2, -5, -6 and -8.

| | reallocation | contract-net | coordinator state survives a restart |
|---|---|---|---|
| P (``none``) | yes | yes | yes (checkpointed twin) |
| B2 ``central_twin`` | yes | no | yes |
| B1 ``central_memory`` | yes | no | no (memory) |
| B0 ``independent`` | no (allocated once) | no | yes |

Each run (2D simulator, fake models): the operator asks for the six points of interest to be
inspected; at a seeded moment the coordinator crashes, a robot holding open inspections
fails during the outage, and the coordinator restarts 60 s after the crash. Outcomes come
from the simulator's own skill records (which points were inspected and when), never from
the coordinator's state, which B1 loses:

* completion time: when the sixth point was inspected (None: not within the cap);
* task ratio: points inspected / 6;
* unrecovered failures: injected faults whose affected inspections were never done;
* operator person-hours: the order, plus one manual reassignment per point left undone.
"""

import asyncio
import json
import random
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from a6g_common.config import A6GConfig
from a6g_common.schemas import DecisionKind

from a6g_devstack.stack import SimStack

ARMS = ("none", "central_twin", "central_memory", "independent")
LABEL = {"none": "P", "central_twin": "B2", "central_memory": "B1", "independent": "B0"}
GOAL = (
    "Inspect the info kiosk, the building 9B door, the bike rack, the bus stop, the 9B east "
    "entrance canopy and the south plaza arcade"
)
EXTRA = ("go2_03", "go2_04")  # the fleet of 5


def schedule(seed: int) -> dict[str, float]:
    rng = random.Random(seed)
    kill = round(rng.uniform(30, 600), 1)  # any phase of the ~700-1500 s mission
    return {
        "coordinator_kill_s": kill,
        "robot_fail_s": round(kill + rng.uniform(5, 15), 1),
        "coordinator_restart_s": kill + 60.0,
    }


def _inspected(stack: SimStack) -> dict[str, datetime]:
    """POI -> first successful inspection (the skill servers' own records)."""
    out: dict[str, datetime] = {}
    for d in stack.log.read():
        if d.kind is not DecisionKind.SKILL or not d.actor.endswith("skill_mcp"):
            continue
        result, args = d.output.get("result") or {}, d.output.get("args") or {}
        if result.get("skill") == "inspect" and result.get("outcome") == "succeeded":
            out.setdefault(args.get("poi_id"), d.stamp)
    return out


async def _sim_until(stack: SimStack, t: float, start: datetime, wall_s: float) -> None:
    async with asyncio.timeout(wall_s):
        while (stack.world.clock.now() - start).total_seconds() < t:
            await asyncio.sleep(0.05)


async def run_one(
    cfg: A6GConfig,
    workdir: Path,
    arm: str,
    fleet: int,
    seed: int,
    cap_s: float = 1500,
    wall_s: float = 300,
) -> dict[str, Any]:
    sched = schedule(seed)
    # Fake models: the outcomes do not depend on the speed, so the runs go at 200x.
    stack = SimStack(cfg, workdir, baseline=arm, realtime_factor=200.0)
    pois = [p.poi_id for p in cfg.campus.pois]
    await stack.start()
    out: dict[str, Any] = {"arm": LABEL[arm], "fleet": fleet, "seed": seed, **sched}
    try:
        if fleet > 3:
            for rid in EXTRA[: fleet - 3]:
                await stack.add_robot(robot_id=rid)
        start = stack.world.clock.now()
        await stack.mission(GOAL)
        await _sim_until(stack, sched["coordinator_kill_s"], start, wall_s)
        tasks = (await stack.coordinator.state("m1"))["tasks"]
        holding: dict[str, list[str]] = {}
        for t in tasks.values():
            if t["status"] in ("assigned", "in_progress") and t.get("robot_id"):
                holding.setdefault(t["robot_id"], []).append(t["spec"]["target"].get("poi_id"))
        await stack.kill_coordinator()
        await _sim_until(stack, sched["robot_fail_s"], start, wall_s)
        done = _inspected(stack)
        victims = sorted(holding, key=lambda r: (-len([p for p in holding[r] if p not in done]), r))
        victim = victims[0] if victims else sorted(stack.robots)[0]
        affected = [p for p in holding.get(victim, []) if p not in done]
        await stack.fail_robot(victim)
        await _sim_until(stack, sched["coordinator_restart_s"], start, wall_s)
        await stack.start_coordinator()
        try:
            async with asyncio.timeout(wall_s):
                while len(_inspected(stack)) < len(pois):
                    if (stack.world.clock.now() - start).total_seconds() >= cap_s:
                        break
                    await asyncio.sleep(0.2)
        except TimeoutError:
            out["wall_timeout"] = True
        done = _inspected(stack)
        last = max(done.values()) if len(done) == len(pois) else None
        out.update(
            victim=victim,
            affected=affected,
            inspected=sorted(done),
            task_ratio=round(len(done) / len(pois), 4),
            completion_s=round((last - start).total_seconds(), 1) if last else None,
            unrecovered_failures=int(any(p not in done for p in affected)),
            manual_reassignments=len(pois) - len(done),
        )
    except Exception as exc:  # a failed run is reported, not hidden
        out["error"] = repr(exc)
    finally:
        await stack.stop()
    return out


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


async def run(
    cfg: A6GConfig,
    workdir: Path,
    seeds: int = 5,
    fleets: tuple[int, ...] = (3, 5),
    arms: tuple[str, ...] = ARMS,
) -> dict[str, Any]:
    rows = []
    for fleet in fleets:
        for arm in arms:
            for seed in range(seeds):
                rows.append(
                    await run_one(cfg, workdir / f"{LABEL[arm]}_f{fleet}_s{seed}", arm, fleet, seed)
                )
    manual = yaml.safe_load(Path("config/bench/manual_round.yaml").read_text())
    minutes = manual["operator_minutes_per_command"]

    def cell(arm: str, fleet: int) -> dict[str, Any]:
        rs = [
            r for r in rows if r["arm"] == LABEL[arm] and r["fleet"] == fleet and "error" not in r
        ]
        times = [r["completion_s"] for r in rs if r["completion_s"] is not None]
        return {
            "runs": len(rs),
            "errors": sum(
                1 for r in rows if r["arm"] == LABEL[arm] and r["fleet"] == fleet and "error" in r
            ),
            "completed": len(times),
            "completion_s_median": _median(times),
            "task_ratio_mean": round(statistics.fmean(r["task_ratio"] for r in rs), 4)
            if rs
            else None,
            "unrecovered_failures_per_mission": round(
                statistics.fmean(r["unrecovered_failures"] for r in rs), 4
            )
            if rs
            else None,
            "operator_person_hours": round(
                statistics.fmean((1 + r["manual_reassignments"]) * minutes / 60 for r in rs), 4
            )
            if rs
            else None,
        }

    table = {f"{LABEL[a]}_f{f}": cell(a, f) for f in fleets for a in arms}
    return {
        "kpis": "UC2-KPI-2, -5, -6, -8 (SPEC A3.8)",
        "design": "six inspections; coordinator crash, a robot failure during the outage, "
        "coordinator restart 60 s later; outcomes from the skill servers' records",
        "fidelity": "functional (2D simulator, fake models)",
        "seeds": seeds,
        "cap_s": 1500,
        "table": table,
        "rows": rows,
        "stamp": datetime.now(UTC).isoformat(),
    }


def write(result: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"e6_baselines_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(result, indent=2, default=str))
    return path


def summary(result: dict[str, Any]) -> str:
    lines = ["config  fleet  completed  median_s  task_ratio  unrecovered  operator_h"]
    for key, c in result["table"].items():
        arm, fleet = key.split("_f")
        lines.append(
            f"{arm:<7} {fleet:>5}  {c['completed']}/{c['runs']:<7} "
            f"{c['completion_s_median']!s:>8}  "
            f"{c['task_ratio_mean']!s:>10}  {c['unrecovered_failures_per_mission']!s:>11}  "
            f"{c['operator_person_hours']!s:>10}"
            + (f"  ({c['errors']} errors)" if c["errors"] else "")
        )
    return "\n".join(lines)
