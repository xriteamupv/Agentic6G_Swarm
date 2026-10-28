"""Baselines B0-B2 against the proposed system P: mission outcomes under a coordinator
outage, and the outage sweep.

| | reallocation | contract-net | coordinator state survives a restart |
|---|---|---|---|
| P (``none``) | yes | yes | yes (checkpointed twin) |
| B2 ``central_twin`` | yes | no | yes |
| B1 ``central_memory`` | yes | no | no (memory) |
| B0 ``independent`` | no (allocated once) | no | yes |

Each run (2D simulator, fake models, 200x): the operator asks for the six points of interest
to be inspected; at a seeded moment (30-600 s) the coordinator crashes, a robot holding open
inspections fails during the outage, and the coordinator restarts ``outage_s`` after the
crash (``inf``: not before the mission horizon). Outcomes come from the simulator's own
skill records (which robot inspected which point, when it started and ended), never from
the coordinator's state, which B1 loses:

* completion time: when the sixth point was inspected (None: not within the horizon);
* task ratio: points inspected / 6;
* unrecovered failures: injected faults whose affected inspections were never done;
* operator person-hours: the order, plus one manual reassignment per point left undone;
* time to resume: from the robot failure to the first of its open inspections started by
  another robot; resumed during the outage: those started before the coordinator returned;
* duplicates: points inspected by two robots;
* restart to reconciled: from the restart to the first reconciled publication;
* messages and bytes per robot (comms log), coordinator wall time per graph run.

Outage kind: ``coordinator_crash``. The surviving robots learn of the
coordinator loss from its heartbeat and last-will, and of the robot failure from the
robot's last-will, both through the MQTT broker hosted at the edge, which stays up: this is
not an edge partition.
"""

import asyncio
import contextlib
import csv
import json
import math
import random
import shutil
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from a6g_common.config import A6GConfig
from a6g_common.schemas import DecisionKind

from a6g_devstack.stack import EXTRA_ROBOTS, SimStack

ARMS = ("none", "central_twin", "central_memory", "independent")
LABEL = {"none": "P", "central_twin": "B2", "central_memory": "B1", "independent": "B0"}
GOAL = (
    "Inspect the building 8K door, the building 9B door, the bike rack, the bus stop, the "
    "campus east access and the parking 8B"
)
OUTAGE_KIND = "coordinator_crash"
DETECTION = {
    "coordinator_loss": "MQTT heartbeat timeout and last-will, through the edge broker",
    "robot_failure": "MQTT last-will of the robot, through the edge broker",
}


def schedule(seed: int, outage_s: float = 60.0) -> dict[str, float | None]:
    rng = random.Random(seed)
    kill = round(rng.uniform(30, 600), 1)  # any phase of the ~700-1500 s mission
    return {
        "coordinator_kill_s": kill,
        "robot_fail_s": round(kill + rng.uniform(5, 15), 1),
        "coordinator_restart_s": None if math.isinf(outage_s) else kill + outage_s,
    }


def _skills(stack: SimStack) -> list[dict[str, Any]]:
    """Successful inspections from the skill servers' records: point, robot, start, end."""
    out = []
    for d in stack.log.read():
        if d.kind is not DecisionKind.SKILL or not d.actor.endswith("skill_mcp"):
            continue
        result, args = d.output.get("result") or {}, d.output.get("args") or {}
        if result.get("skill") == "inspect" and result.get("outcome") == "succeeded":
            dur = float(result.get("duration_s") or 0.0)
            out.append(
                {
                    "poi": args.get("poi_id"),
                    "robot": d.actor.split(".")[0],
                    "end": d.stamp,
                    "start": d.stamp - timedelta(seconds=dur),
                }
            )
    return out


async def _sim_until(stack: SimStack, t: float, start: datetime, wall_s: float) -> None:
    async with asyncio.timeout(wall_s):
        while (stack.world.clock.now() - start).total_seconds() < t:
            await asyncio.sleep(0.05)


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 4)


def _comms(path: Path, robots: list[str]) -> dict[str, Any]:
    """Messages and wire bytes sent per robot, and by the coordinator (comms log)."""
    sent: dict[str, list[int]] = {}
    if path.exists():
        with path.open() as fh:
            for line in fh:
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if m.get("direction") != "send":
                    continue
                agent = m.get("agent") or m.get("sender") or "?"
                c = sent.setdefault(agent, [0, 0])
                c[0] += 1
                c[1] += int(m.get("wire_bytes") or 0)
    per = [sent.get(r, [0, 0]) for r in robots]
    return {
        "messages_per_robot": round(statistics.fmean(p[0] for p in per), 1) if per else 0,
        "bytes_per_robot": round(statistics.fmean(p[1] for p in per), 1) if per else 0,
        "coordinator_messages": sent.get("coordinator", [0, 0])[0],
        "coordinator_bytes": sent.get("coordinator", [0, 0])[1],
    }


async def run_one(
    cfg: A6GConfig,
    workdir: Path,
    arm: str,
    fleet: int,
    seed: int,
    outage_s: float = 60.0,
    horizon_s: float = 3600.0,
    wall_s: float = 900,
) -> dict[str, Any]:
    sched = schedule(seed, outage_s)
    if workdir.exists():  # a run cut short earlier: start clean (logs are appended)
        shutil.rmtree(workdir)
    # Fake models: the outcomes do not depend on the speed, so the runs go at 200x.
    stack = SimStack(cfg, workdir, baseline=arm, realtime_factor=200.0)
    pois = [p.poi_id for p in cfg.campus.pois]
    await stack.start()
    out: dict[str, Any] = {
        "arm": LABEL[arm],
        "fleet": fleet,
        "seed": seed,
        "outage_s": outage_s,
        "outage_kind": OUTAGE_KIND,
        **sched,
    }
    coord_runs: list[float] = []
    try:
        if fleet > 3:
            for rid in EXTRA_ROBOTS[: fleet - 3]:
                await stack.add_robot(robot_id=rid)
        start = stack.world.clock.now()

        def at(t: datetime) -> float:
            return round((t - start).total_seconds(), 1)

        await stack.mission(GOAL)
        await _sim_until(stack, sched["coordinator_kill_s"], start, wall_s)
        tasks = (await stack.coordinator.state("m1"))["tasks"]
        holding: dict[str, list[str]] = {}
        for t in tasks.values():
            if t["status"] in ("assigned", "in_progress") and t.get("robot_id"):
                holding.setdefault(t["robot_id"], []).append(t["spec"]["target"].get("poi_id"))
        coord_runs += stack.coordinator.run_wall_s
        await stack.kill_coordinator()
        await _sim_until(stack, sched["robot_fail_s"], start, wall_s)
        done = {s["poi"] for s in _skills(stack)}
        victims = sorted(holding, key=lambda r: (-len([p for p in holding[r] if p not in done]), r))
        victim = victims[0] if victims else sorted(stack.robots)[0]
        affected = [p for p in holding.get(victim, []) if p not in done]
        await stack.fail_robot(victim)
        restart = sched["coordinator_restart_s"]
        if restart is not None and restart < horizon_s:
            await _sim_until(stack, restart, start, wall_s)
            await stack.start_coordinator()
        try:
            async with asyncio.timeout(wall_s):
                while len({s["poi"] for s in _skills(stack)}) < len(pois):
                    if (stack.world.clock.now() - start).total_seconds() >= horizon_s:
                        break
                    await asyncio.sleep(0.5)
        except TimeoutError:
            out["wall_timeout"] = True
        if stack.coordinator is not None:
            coord_runs += stack.coordinator.run_wall_s
        skills = _skills(stack)
        first: dict[str, dict[str, Any]] = {}
        for s in sorted(skills, key=lambda s: s["end"]):
            first.setdefault(s["poi"], s)
        last = max(s["end"] for s in first.values()) if len(first) == len(pois) else None
        fail_t = start + timedelta(seconds=sched["robot_fail_s"])
        taken = [
            s
            for s in skills
            if s["poi"] in affected and s["robot"] != victim and s["start"] >= fail_t
        ]
        resume = min((s["start"] for s in taken), default=None)
        back = start + timedelta(seconds=restart) if restart is not None else None
        robots_by_poi: dict[str, set[str]] = {}
        for s in skills:
            robots_by_poi.setdefault(s["poi"], set()).add(s["robot"])
        decisions = stack.log.read()
        kill_t = start + timedelta(seconds=sched["coordinator_kill_s"])
        noticed = [
            d.stamp
            for d in decisions
            if d.kind is DecisionKind.FALLBACK
            and d.actor.endswith(".agent")
            and (d.policy or {}).get("rule") == "coordinator_loss"
            and d.output.get("coordinator_reachable") is False
            and d.stamp >= kill_t
        ]
        reconciled = (
            [
                d.stamp
                for d in decisions
                if d.actor == "coordinator" and d.kind is DecisionKind.RECONCILE and d.stamp >= back
            ]
            if back is not None
            else []
        )
        out.update(
            victim=victim,
            affected=affected,
            inspected=sorted(first),
            completed=last is not None,
            task_ratio=round(len(first) / len(pois), 4),
            completion_s=at(last) if last else None,
            unrecovered_failures=int(any(p not in first for p in affected)),
            manual_reassignments=len(pois) - len(first),
            time_to_resume_s=round((resume - fail_t).total_seconds(), 1) if resume else None,
            resumed_during_outage=sum(1 for s in taken if back is None or s["start"] < back),
            duplicates=sum(1 for rs in robots_by_poi.values() if len(rs) > 1),
            restart_to_reconciled_s=round((min(reconciled) - back).total_seconds(), 1)
            if reconciled
            else None,
            coordinator_loss_noticed_s=round((min(noticed) - kill_t).total_seconds(), 1)
            if noticed
            else None,
            coordinator_runs=len(coord_runs),
            coordinator_run_p50_ms=round((_pct(coord_runs, 0.5) or 0) * 1000, 1),
            coordinator_run_p95_ms=round((_pct(coord_runs, 0.95) or 0) * 1000, 1),
            **_comms(workdir / "comms.jsonl", list(stack.robots)),
        )
    except Exception as exc:  # a failed run is reported, not hidden
        out["error"] = repr(exc)
    finally:
        await stack.stop()
    if "error" not in out:
        (workdir / "row.json").write_text(json.dumps(out, default=str))
    return out


def _one(job: tuple[str, str, str, int, int, float, float]) -> dict[str, Any]:
    """A run in its own process (the messaging layer is configured per process)."""
    from a6g_common.config import load_config

    config_dir, workdir, arm, fleet, seed, outage_s, horizon_s = job
    cfg = load_config(config_dir, fleet_file="sim/fleet.yaml")
    return asyncio.run(run_one(cfg, Path(workdir), arm, fleet, seed, outage_s, horizon_s))


async def _subprocess(
    job: tuple[str, str, str, int, int, float, float], wall_s: float
) -> dict[str, Any]:
    """One run in a child process: a crash or a hang costs that run only (a worker dying
    in a process pool would take the whole sweep with it). The row is saved
    next to the run (``row.json``), so a rerun of the sweep skips finished runs."""
    import sys

    row_path = Path(job[1]) / "row.json"
    if row_path.exists():
        with contextlib.suppress(ValueError):
            return json.loads(row_path.read_text())
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "a6g_devstack.bench_baselines",
        json.dumps(list(job)),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), wall_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
    if row_path.exists():
        with contextlib.suppress(ValueError):
            return json.loads(row_path.read_text())
    _, _, arm, fleet, seed, outage_s, _ = job
    return {
        "arm": LABEL[arm],
        "fleet": fleet,
        "seed": seed,
        "outage_s": outage_s,
        "error": f"child process exited with {proc.returncode} (no row)",
    }


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _tag(outage_s: float) -> str:
    return "inf" if math.isinf(outage_s) else f"{outage_s:g}"


async def run(
    cfg: A6GConfig,
    workdir: Path,
    seeds: int = 5,
    fleets: tuple[int, ...] = (3, 5),
    arms: tuple[str, ...] = ARMS,
    outages: tuple[float, ...] = (60.0,),
    horizon_s: float = 3600.0,
    jobs: int = 1,
) -> dict[str, Any]:
    plan = [
        (
            str(cfg.config_dir),
            str(workdir / f"{LABEL[arm]}_f{fleet}_o{_tag(o)}_s{seed}"),
            arm,
            fleet,
            seed,
            o,
            horizon_s,
        )
        for fleet in fleets
        for o in outages
        for arm in arms
        for seed in range(seeds)
    ]
    gate = asyncio.Semaphore(max(1, jobs))

    async def one(j: tuple[str, str, str, int, int, float, float]) -> dict[str, Any]:
        async with gate:
            return await _subprocess(j, wall_s=1800)

    rows = list(await asyncio.gather(*(one(j) for j in plan)))
    manual = yaml.safe_load((Path(cfg.config_dir) / "bench/manual_round.yaml").read_text())
    minutes = manual["operator_minutes_per_command"]
    for r in rows:
        if "error" not in r:
            r["operator_h"] = round((1 + r["manual_reassignments"]) * minutes / 60, 4)

    def cell(arm: str, fleet: int, o: float) -> dict[str, Any]:
        mine = [
            r for r in rows if r["arm"] == LABEL[arm] and r["fleet"] == fleet and r["outage_s"] == o
        ]
        rs = [r for r in mine if "error" not in r]
        times = [r["completion_s"] for r in rs if r["completion_s"] is not None]
        resumes = [r["time_to_resume_s"] for r in rs if r["time_to_resume_s"] is not None]

        def mean(key: str) -> float | None:
            vals = [r[key] for r in rs if r.get(key) is not None]
            return round(statistics.fmean(vals), 4) if vals else None

        return {
            "runs": len(rs),
            "errors": len(mine) - len(rs),
            "completed": len(times),
            "completion_s_median": _median(times),
            "task_ratio_mean": mean("task_ratio"),
            "unrecovered_failures_per_mission": mean("unrecovered_failures"),
            "operator_person_hours": mean("operator_h"),
            "time_to_resume_s_median": _median(resumes),
            "resumed_during_outage_mean": mean("resumed_during_outage"),
            "duplicates_mean": mean("duplicates"),
            "restart_to_reconciled_s_median": _median(
                [
                    r["restart_to_reconciled_s"]
                    for r in rs
                    if r.get("restart_to_reconciled_s") is not None
                ]
            ),
            "messages_per_robot_mean": mean("messages_per_robot"),
            "bytes_per_robot_mean": mean("bytes_per_robot"),
            "coordinator_run_p50_ms_median": _median(
                [r["coordinator_run_p50_ms"] for r in rs if r.get("coordinator_runs")]
            ),
            "coordinator_run_p95_ms_median": _median(
                [r["coordinator_run_p95_ms"] for r in rs if r.get("coordinator_runs")]
            ),
        }

    table = {
        f"{LABEL[a]}_f{f}_o{_tag(o)}": cell(a, f, o) for f in fleets for o in outages for a in arms
    }
    return {
        "kpis": "UC2-KPI-2, -5, -6, -8 (SPEC A3.8); outage sweep (SPEC A4 N1)",
        "design": "six inspections; coordinator crash at a seeded time (30-600 s), a robot "
        "failure during the outage, coordinator restart after outage_s (inf: never before the "
        "horizon); outcomes from the skill servers' records",
        "outage_kind": OUTAGE_KIND,
        "detection": DETECTION,
        "fidelity": "functional (2D simulator, fake models)",
        "time_factor": "200x",
        "seeds": seeds,
        "fleets": list(fleets),
        "outages_s": [_tag(o) for o in outages],
        "horizon_s": horizon_s,
        "table": table,
        "rows": rows,
        "stamp": datetime.now(UTC).isoformat(),
    }


FIG_COLUMNS = (
    "config",
    "fleet",
    "outage_s",
    "seed",
    "completed",
    "completion_s",
    "task_ratio",
    "time_to_resume_s",
    "resumed_during_outage",
    "duplicates",
    "operator_h",
)


def write(result: dict[str, Any], out_dir: Path, prefix: str = "n1_outage") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{prefix}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(result, indent=2, default=str))
    return path


def write_csv(result: dict[str, Any], path: Path) -> Path:
    """``paper/fig_outage.csv``: one row per run (the paper figure is drawn from it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FIG_COLUMNS)
        for r in result["rows"]:
            if "error" in r:
                continue
            w.writerow(
                [
                    r["arm"],
                    r["fleet"],
                    _tag(r["outage_s"]),
                    r["seed"],
                    int(r["completed"]),
                    r["completion_s"] if r["completion_s"] is not None else "",
                    r["task_ratio"],
                    r["time_to_resume_s"] if r["time_to_resume_s"] is not None else "",
                    r["resumed_during_outage"],
                    r["duplicates"],
                    r["operator_h"],
                ]
            )
    return path


def summary(result: dict[str, Any]) -> str:
    lines = [
        "config fleet outage  completed  median_s  task_ratio  unrecov  resume_s  dur_outage"
        "  dups  operator_h"
    ]
    for key, c in result["table"].items():
        arm, rest = key.split("_f")
        fleet, outage = rest.split("_o")
        lines.append(
            f"{arm:<6} {fleet:>5} {outage:>6}  {c['completed']}/{c['runs']:<7} "
            f"{c['completion_s_median']!s:>8}  {c['task_ratio_mean']!s:>10}  "
            f"{c['unrecovered_failures_per_mission']!s:>7}  {c['time_to_resume_s_median']!s:>8}  "
            f"{c['resumed_during_outage_mean']!s:>10}  {c['duplicates_mean']!s:>4}  "
            f"{c['operator_person_hours']!s:>10}"
            + (f"  ({c['errors']} errors)" if c["errors"] else "")
        )
    return "\n".join(lines)


if __name__ == "__main__":  # one run, in a child process of the sweep
    import sys

    _one(tuple(json.loads(sys.argv[1])))  # type: ignore[arg-type]
