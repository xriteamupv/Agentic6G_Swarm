"""Scenario G13, an emergency under network scarcity (SPEC A4.7), 2D simulator.

A weather alert (emergency posture) asks for every exposed POI of the campus to be held;
posts stream video by default (``post_sensing_mode: video``). The network stand-in is the
capacity model (``network.mode: capacity``) on the coverage map; the campus cell's uplink
capacity is the swept variable. Variants: ``reconcile`` (admission and the degradation
loop), ``reconcile_off`` (the experiment baseline: no network-aware planning; every stream
starts at its nominal rate and the cell shares capacity by weighted fair share),
``reconcile_relocate`` (also viewpoints with better coverage; skipped when no exposed POI
is below ``good``, and the reason recorded).

Measured at steady state (every reachable post held, or 300 s simulated after the alert):
posts held, posts with usable video, posts with usable sensing (video usable or
telemetry_plus granted), oversubscription (``reconcile_off``), tasks without a flow and
flows without a task after every publication (I1, I2), streams opened above telemetry
without a grant (I3), posture change to reconciled envelopes (UC2-KPI-11 under scarcity),
reconciliation iterations and escalations.
"""

import asyncio
import json
import random
import shutil
from pathlib import Path
from typing import Any

from a6g_common.config import A6GConfig, load_config
from a6g_common.schemas import DecisionKind

from a6g_devstack.stack import EXTRA_ROBOTS, SimStack

VARIANTS = {"reconcile": "on", "reconcile_off": "off", "reconcile_relocate": "relocate"}


def g13_config(config_dir: str, capacity_mbps: float, variant: str) -> A6GConfig:
    import os

    env = dict(os.environ)
    env.update(
        A6G_NETWORK_MODE="capacity",
        A6G_COVERAGE_SOURCE=env.get("A6G_COVERAGE_SOURCE_G13", "radio_dt"),  # definitive map
        A6G_POST_SENSING="video",
        A6G_UL_CAPACITY_MBPS=str(capacity_mbps),
        A6G_RECONCILE_MODE=VARIANTS[variant],
    )
    return load_config(config_dir, env=env, fleet_file="sim/fleet.yaml")


def _comms(path: Path) -> list[dict[str, Any]]:
    out = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


async def _sim_wait(stack: SimStack, seconds: float, wall_s: float = 300) -> None:
    start = stack.world.clock.now()
    async with asyncio.timeout(wall_s):
        while (stack.world.clock.now() - start).total_seconds() < seconds:
            await asyncio.sleep(0.05)


async def run(
    cfg: A6GConfig,
    workdir: Path,
    *,
    fleet: int = 3,
    seed: int = 0,
    variant: str = "reconcile",
    steady_s: float = 300.0,
    wall_s: float = 600.0,
) -> dict[str, Any]:
    if workdir.exists():
        shutil.rmtree(workdir)
    rng = random.Random(seed)
    stack = SimStack(cfg, workdir, realtime_factor=200.0)
    await stack.start()
    exposed = [p.poi_id for p in cfg.campus.pois if p.exposed]
    out: dict[str, Any] = {
        "scenario": "G13",
        "variant": variant,
        "fleet": fleet,
        "seed": seed,
        "capacity_mbps": next(iter(cfg.network.capacity.cells.values()))["ul_capacity_mbps"],
        "exposed_pois": len(exposed),
    }
    try:
        for rid in EXTRA_ROBOTS[: max(0, fleet - 3)]:
            await stack.add_robot(robot_id=rid)
        await stack.mission("Patrol the campus")
        await _sim_wait(stack, rng.uniform(30, 120))
        alert_at = stack.world.clock.now()
        await stack.weather_alert()
        # steady state: every exposed POI held (its post in progress), or the time cap
        async with asyncio.timeout(wall_s):
            while (stack.world.clock.now() - alert_at).total_seconds() < steady_s:
                tasks = (await stack.coordinator.state("m1")).get("tasks") or {}
                held = [
                    t
                    for t in tasks.values()
                    if t["spec"]["kind"] == "hold_post"
                    and t["status"] == "in_progress"
                    and t["spec"]["target"].get("poi_id") in exposed
                ]
                if len(held) >= len(exposed):
                    break
                await asyncio.sleep(0.2)
        settled_s = round((stack.world.clock.now() - alert_at).total_seconds(), 1)
        state = await stack.coordinator.state("m1")
        tasks = state.get("tasks") or {}
        flows = state.get("flows") or {}
        posts = {
            tid: t
            for tid, t in tasks.items()
            if t["spec"]["kind"] == "hold_post"
            and t["spec"]["target"].get("poi_id") in exposed
            and t["status"] in ("assigned", "in_progress")
        }
        held = {tid: t for tid, t in posts.items() if t["status"] == "in_progress"}
        flow_of = {
            f["task_id"]: f
            for f in flows.values()
            if f["state"] != "released" and f["task_id"] in held
        }
        share = stack.network.fair_share() if hasattr(stack.network, "fair_share") else {}
        video_usable = sensing_usable = 0
        for tid in held:
            f = flow_of.get(tid)
            if f is None:
                continue
            if variant == "reconcile_off":
                usable = (share.get(f["flow_id"]) or {}).get("usable", False)
                video_usable += f["flow_class"] == "video_control" and usable
                sensing_usable += usable
            else:
                ok = f["state"] == "granted"
                video_usable += f["flow_class"] == "video_control" and ok
                sensing_usable += ok and f["flow_class"] in ("video_control", "telemetry_plus")
        over = max((v["oversubscription"] for v in share.values()), default=0.0)
        decisions = [d for d in stack.log.read() if d.stamp >= alert_at]
        recs = [
            d for d in decisions if d.actor == "coordinator" and d.kind is DecisionKind.RECONCILE
        ]
        moves = [d for d in recs if d.policy.get("rule") == "relocate_for_coverage"]
        flow_recs = [d for d in recs if "kpi" not in d.output and d not in moves]
        kpi = next((d.output for d in recs if d.output.get("kpi") == "UC2-KPI-11"), {})
        budget = [m for m in _comms(workdir / "comms.jsonl") if m.get("direction") == "budget"]
        i3 = [
            m
            for m in budget
            if m.get("opened") not in (None, "telemetry")
            and m.get("grant_state") not in ("granted", "degraded")
        ]
        out.update(
            settled_s=settled_s,
            posts_planned=len(posts),
            posts_held=len(held),
            posts_video_usable=int(video_usable),
            posts_sensing_usable=int(sensing_usable),
            oversubscription=round(over, 3) if variant == "reconcile_off" else None,
            i1_violations=sum(1 for d in flow_recs if d.output.get("tasks_without_flow")),
            i2_violations=sum(1 for d in flow_recs if d.output.get("flows_without_task")),
            i3_violations=len(i3),
            streams_opened=len(budget),
            posture_change_to_reconciled_s=kpi.get("posture_change_to_publication_s"),
            reconcile_iterations_max=max(
                (d.output.get("iterations", 0) for d in flow_recs), default=0
            ),
            switched_to_telemetry=sum(
                len(d.output.get("switched_to_telemetry") or []) for d in flow_recs
            ),
            escalations=sum(len(d.output.get("escalated_without_flow") or []) for d in flow_recs),
            rejected=sum(
                1
                for d in flow_recs
                for a in d.output.get("answers") or []
                if a.get("state") == "rejected"
            ),
            relocations=len(moves),
            # distance from the POI to the viewpoint it is held from (SPEC A4.7)
            relocation_distance_m=round(sum(d.output["distance_m"] for d in moves), 1),
            kpi3_share=_kpi3(stack.log.read()),
        )
    except Exception as exc:  # reported, not hidden
        out["error"] = repr(exc)
    finally:
        await stack.stop()
    if "error" not in out:
        (workdir / "row.json").write_text(json.dumps(out, default=str))
    return out


def _kpi3(decisions: list[Any]) -> float | None:
    """UC2-KPI-3 from the grants (SPEC A4.7): share of the streaming flows requested in
    the run that the network granted at the requested level (not degraded or rejected)."""
    answers = [
        a
        for d in decisions
        if d.actor == "coordinator" and d.kind is DecisionKind.RECONCILE
        for a in d.output.get("answers") or []
        if a.get("flow_class") != "telemetry"
    ]
    if not answers:
        return None
    return round(sum(1 for a in answers if a.get("state") == "granted") / len(answers), 4)


def _one(job: list[Any]) -> dict[str, Any]:
    config_dir, workdir, capacity, variant, fleet, seed = job
    cfg = g13_config(config_dir, capacity, variant)
    return asyncio.run(run(cfg, Path(workdir), fleet=fleet, seed=seed, variant=variant))


if __name__ == "__main__":  # one run, in a child process of the sweep
    import sys

    _one(json.loads(sys.argv[1]))
