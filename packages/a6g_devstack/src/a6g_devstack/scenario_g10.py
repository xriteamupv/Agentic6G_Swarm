"""Scenario G10 (SPEC A3.11, stage E6): the D2.1 consolidated story in one run, on the 2D
simulator, with trigger times drawn from seeded distributions the agents cannot read.

1. a survey runs ("Patrol the campus");
2. a person asks a Go2 for the bus stop, declaring a constraint (wheelchair);
3. a weather alert arrives while the escort is open: the escort concludes at the nearest
   shelter;
4. the escorting robot walks into the coverage shadow (both shelters lie in it on the
   final layout, which has no exposed point inside a shadow): the link is lost by
   geometry, not injection, and the robot recovers (``hold_then_recover``) or is replaced;
5. the operator gives the all clear and the survey resumes.

The run directory keeps ``decisions.jsonl``, ``comms.jsonl``, ``telemetry.jsonl`` (pose,
battery and link per robot) and ``run.json`` (seed, triggers, timeline, checks), which
``a6g kpi report --run <dir>`` reads.
"""

import asyncio
import contextlib
import json
import math
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from a6g_common.config import A6GConfig
from a6g_common.schemas import DecisionKind
from a6g_observability.decision_log import verify

from a6g_devstack.stack import SimStack

MISSION = "m1"
REQUEST = "¿Me llevas a la parada del bus? Voy en silla de ruedas."
CANARY = "silla de ruedas"  # the declared constraint: never in a log (rule 28)
GOAL = "Patrol the campus"


def triggers(seed: int) -> dict[str, float]:
    """Simulated seconds, drawn per seed; the agents never see them."""
    rng = random.Random(seed)
    return {
        "request_after_s": round(rng.uniform(60, 300), 1),
        "alert_after_departure_s": round(rng.uniform(10, 60), 1),
        "all_clear_after_alert_s": round(rng.uniform(300, 900), 1),
        "speaker_pick": rng.random(),
    }


async def _sim_wait(stack: SimStack, seconds: float, timeout_s: float) -> None:
    start = stack.world.clock.now()
    async with asyncio.timeout(timeout_s):
        while (stack.world.clock.now() - start).total_seconds() < seconds:
            await asyncio.sleep(0.05)


async def _until(predicate: Any, timeout_s: float, what: str) -> None:
    try:
        async with asyncio.timeout(timeout_s):
            while not (
                await predicate() if asyncio.iscoroutinefunction(predicate) else predicate()
            ):
                await asyncio.sleep(0.2)
    except TimeoutError:
        raise TimeoutError(f"G10: {what} did not happen within {timeout_s} s") from None


async def _telemetry(stack: SimStack, path: Path, every_s: float = 5.0) -> None:
    """Pose, battery and link of every robot every ``every_s`` simulated seconds."""
    last = None
    with path.open("w", encoding="utf-8") as fh:
        while True:
            now = stack.world.clock.now()
            if last is None or (now - last).total_seconds() >= every_s:
                last = now
                for rid, robot in stack.world.robots.items():
                    fh.write(
                        json.dumps(
                            {
                                "stamp": now.isoformat(),
                                "robot_id": rid,
                                "x": round(robot.x, 2),
                                "y": round(robot.y, 2),
                                "battery_pct": round(robot.battery_pct, 1),
                                "link": stack.world.link_quality(rid).value,
                                "failed": bool(getattr(robot, "failed", False)),
                            }
                        )
                        + "\n"
                    )
                fh.flush()
            await asyncio.sleep(0.05)


def _decisions(stack: SimStack, kind: DecisionKind | None = None) -> list:
    return [d for d in stack.log.read() if kind is None or d.kind is kind]


#: Step 4's outage: the former placeholder shadow around the shelters (removed from the
#: campus on 2026-10-26, coverage is emulated), injected here as a stress case
OUTAGE_WGS84 = [
    (39.4782885, -0.3341788),
    (39.4780638, -0.3334241),
    (39.4788163, -0.3330652),
    (39.4790278, -0.3338341),
]


def outage_polygon(cfg: A6GConfig) -> list[tuple[float, float]]:
    from a6g_common.geo import Datum

    d = cfg.campus.datum or {}
    datum = Datum(d["lat_deg"], d["lon_deg"], d.get("alt_m", 0.0), d.get("heading_deg", 0.0))
    scale = cfg.campus.scale or 1.0
    return [
        (round(x * scale, 3), round(y * scale, 3))
        for x, y, _ in (datum.to_enu(lat, lon) for lat, lon in OUTAGE_WGS84)
    ]


async def run(
    cfg: A6GConfig, workdir: Path, seed: int = 0, timeout_s: float = 600, **stack_kw: Any
) -> dict[str, Any]:
    workdir.mkdir(parents=True, exist_ok=True)
    trig = triggers(seed)
    # Nobody answers "have we arrived?" in an unattended run (wall-clock deadline).
    icfg = cfg.interactions.model_copy(update={"arrival_confirm_s": 1.0})
    cfg = cfg.model_copy(update={"interactions": icfg})
    stack = SimStack(cfg, workdir, **stack_kw)
    timeline: list[dict[str, Any]] = []

    def mark(event: str, **kw: Any) -> None:
        timeline.append({"event": event, "sim": stack.world.clock.now().isoformat(), **kw})

    operator: list[dict[str, str]] = []
    await stack.start()
    stack.outage_zone(outage_polygon(cfg))
    sampler = asyncio.create_task(_telemetry(stack, workdir / "telemetry.jsonl"))
    iid = speaker = None
    error = None
    try:
        await stack.mission(GOAL, mission_id=MISSION)
        operator.append({"command": GOAL, "reserved": "mission goal"})
        mark("survey_started")
        await _sim_wait(stack, trig["request_after_s"], timeout_s)
        go2s = sorted(r for r in stack.robots if r.startswith("go2"))
        in_coverage = [r for r in go2s if stack.world.link_quality(r).value == "ok"] or go2s
        speaker = in_coverage[int(trig["speaker_pick"] * len(in_coverage))]
        await stack.say(speaker, REQUEST)
        await stack.say(speaker, "sí")
        local = stack.robots[speaker].agent.deps.local
        iid = next(iter(local))
        mark("request", robot=speaker)

        def departed() -> bool:
            entry = local.get(iid) or {}
            return bool((entry.get("content") or {}).get("t_departure"))

        await _until(departed, timeout_s, "the escort departing")
        mark("escort_departed", robot=speaker)
        await _sim_wait(stack, trig["alert_after_departure_s"], timeout_s)
        await stack.weather_alert()
        mark("weather_alert")

        def forgotten() -> bool:
            return any(
                d.actor == "coordinator" and d.output.get("interaction_id") == iid
                for d in _decisions(stack, DecisionKind.FORGET)
            )

        await _until(forgotten, timeout_s, "the escort ending")
        mark("escort_ended")
        await _sim_wait(stack, trig["all_clear_after_alert_s"], timeout_s)
        await stack.coordinator.chat(MISSION, "All clear")
        operator.append({"command": "All clear", "reserved": "closure"})
        mark("all_clear")

        async def resumed() -> bool:
            state = await stack.coordinator.state(MISSION)
            return state.get("posture") == "routine" and any(
                t["spec"]["kind"] == "survey_sector" and t["status"] == "in_progress"
                for t in state["tasks"].values()
            )

        await _until(resumed, timeout_s, "the survey resuming")
        mark("survey_resumed")
        await stack.coordinator.drain()
        interrupts = await stack.coordinator.interrupts(MISSION)
    except Exception as exc:  # a failed run is reported, not hidden
        error, interrupts = repr(exc), []
    finally:
        sampler.cancel()
        await stack.stop()  # first: a failed sampler must not leave the stack running
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sampler
    result = {
        "scenario": "G10",
        "seed": seed,
        "triggers": trig,
        "mission_id": MISSION,
        "speaker": speaker,
        "interaction_id": iid,
        "operator": operator,
        "pending_interrupts": len(interrupts),
        "timeline": timeline,
        "error": error,
        "checks": checks(stack, workdir, speaker, iid, operator, interrupts) if not error else {},
        "network": {
            "mode": cfg.network.mode,
            "coverage_source": cfg.network.coverage.source,
            # SPEC A4.6: the outage around the shelters is injected (the emulated coverage has
            # no outage there; the measured velodrome shows none): a stress case
            "link_loss": "injected outage zone (former placeholder shadow, a stress case)",
        },
        "stamp": datetime.now(UTC).isoformat(),
    }
    (workdir / "run.json").write_text(json.dumps(result, indent=2, default=str))
    return result


def checks(
    stack: SimStack,
    workdir: Path,
    speaker: str | None,
    iid: str | None,
    operator: list[dict[str, str]],
    interrupts: list,
) -> dict[str, Any]:
    decisions = _decisions(stack)
    forget = next(
        (
            d
            for d in decisions
            if d.kind is DecisionKind.FORGET
            and d.actor == "coordinator"
            and d.output.get("interaction_id") == iid
        ),
        None,
    )
    outcome = (forget.output if forget else {}) or {}
    links = [
        d.output.get("link_quality")
        for d in decisions
        if d.actor.startswith(f"{speaker}.") and d.output.get("link_quality")
    ]
    # back in coverage: "ok", or "degraded" in low coverage (radio DT map, 2026-10-26)
    lost_then_ok = "lost" in links and any(q != "lost" for q in links[links.index("lost") :])
    replaced = any(
        d.kind is DecisionKind.FALLBACK
        and d.actor == "coordinator"
        and d.output.get("robot_id") == speaker
        and d.policy.get("rule") in ("robot_failure", "robot_lost_long")
        for d in decisions
    )
    reconciles = [
        d for d in decisions if d.kind is DecisionKind.RECONCILE and "kpi" not in d.output
    ]
    inconsistent = [
        d.decision_id
        for d in reconciles
        if d.output.get("tasks_without_flow") or d.output.get("flows_without_task")
    ]
    logs = "".join(
        (workdir / name).read_text(encoding="utf-8", errors="replace")
        for name in ("decisions.jsonl", "comms.jsonl")
        if (workdir / name).exists()
    )
    chain = verify(workdir / "decisions.jsonl")
    return {
        "interventions": {
            "manual": [o for o in operator if o["reserved"] not in ("mission goal", "closure")],
            "pending_interrupts": len(interrupts),
            "ok": all(o["reserved"] in ("mission goal", "closure") for o in operator)
            and not interrupts,
        },
        "guidance": {
            "outcome": outcome.get("outcome"),
            "repeated_requests": outcome.get("repeated_requests"),
            "route_compliant": outcome.get("route_compliant"),
            "ok": outcome.get("outcome") in ("arrived", "concluded_at_shelter")
            and not outcome.get("repeated_requests"),
        },
        "link_loss": {
            "robot": speaker,
            "lost_and_recovered": lost_then_ok,
            "replaced": replaced,
            "ok": lost_then_ok or replaced,
        },
        "forget": {
            "store_entry_deleted": bool(forget and forget.output.get("deleted")),
            "constraint_in_logs": CANARY in logs,
            "ok": bool(forget and forget.output.get("deleted")) and CANARY not in logs,
        },
        "decision_chain": {**{k: chain[k] for k in ("ok", "records", "legacy")}},
        "invariants": {
            "publications": len(reconciles),
            "inconsistent": inconsistent,
            "ok": not inconsistent,
        },
        "separation": min_separation(workdir / "telemetry.jsonl"),
    }


NEAR_MISS_M = 0.5  # SPEC A3.9 near_miss_m


def min_separation(path: Path) -> dict[str, Any]:
    """Minimum pairwise distance between robots over the run, and the samples with a pair
    closer than ``NEAR_MISS_M`` (the 2D simulator has no contact model, SPEC A3.9)."""
    by_stamp: dict[str, list[tuple[str, float, float]]] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            r = json.loads(line)
            by_stamp.setdefault(r["stamp"], []).append((r["robot_id"], r["x"], r["y"]))
    best, pair, near = math.inf, None, 0
    for rows in by_stamp.values():
        close = False
        for i, (a, ax, ay) in enumerate(rows):
            for b, bx, by in rows[i + 1 :]:
                d = math.hypot(ax - bx, ay - by)
                close = close or d < NEAR_MISS_M
                if d < best:
                    best, pair = d, (a, b)
        near += close
    return {
        "min_m": round(best, 2) if pair else None,
        "pair": pair,
        "near_miss_samples": near,
        "near_miss_m": NEAR_MISS_M,
        "samples": len(by_stamp),
    }
