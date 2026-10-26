"""``a6g kpi report --run <dir>`` (SPEC A3.8): the UC2 KPI table of one run.

Reads the run directory a scenario writes (``run.json``, ``decisions.jsonl``,
``telemetry.jsonl``) and the latest bench results (``bench/results``) for the KPIs measured
by benches (9, 10, 11 over seeds, 15). Writes ``kpi.json`` and ``kpi.md`` next to the run.

KPIs that need the baselines B0/B1 (2, 5, 6 versus B1, 8) or the real network or robots
are ``n/a`` with the reason; UC2-KPI-4 is a configuration fact. Results with cloud models
are ``functional`` (rule 25). Decisions after the run's last event (the stack shutting
down) are left out.
"""

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from a6g_common.config import A6GConfig
from a6g_common.schemas import Decision, DecisionKind

RECOVERY_DEADLINE_S = 300.0  # SPEC A3.8 recovery_deadline_s
NA = "n/a"


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def _latest(results: Path, prefix: str) -> tuple[Path, dict] | None:
    files = sorted(results.glob(f"{prefix}_*.json"))
    return (files[-1], json.loads(files[-1].read_text())) if files else None


def _row(kpi: str, value: Any, unit: str = "", source: str = "", note: str = "") -> dict:
    return {"kpi": kpi, "value": value, "unit": unit, "source": source, "note": note}


def link_events(decisions: list[Decision]) -> list[dict[str, Any]]:
    """Each link loss of a robot and when (if) it came back."""
    out: list[dict[str, Any]] = []
    open_: dict[str, dict[str, Any]] = {}
    for d in decisions:
        robot = d.actor.split(".")[0]
        q = d.output.get("link_quality")
        if q == "lost" and robot not in open_:
            open_[robot] = {"robot": robot, "lost": d.stamp, "recovered": None}
        elif q == "ok" and robot in open_:
            event = open_.pop(robot)
            event["recovered"] = d.stamp
            out.append(event)
    out += open_.values()
    for e in out:
        e["detected"] = any(
            d.actor == "coordinator"
            and d.policy.get("rule") == "robot_failure"
            and d.output.get("robot_id") == e["robot"]
            and d.stamp >= e["lost"]
            for d in decisions
        )
        e["replanned"] = any(
            d.actor == "coordinator"
            and d.kind in (DecisionKind.REALLOCATE, DecisionKind.FALLBACK)
            and d.stamp >= e["lost"]
            for d in decisions
        )
        e["recovery_s"] = (
            round((e["recovered"] - e["lost"]).total_seconds(), 1) if e["recovered"] else None
        )
    return out


def max_concurrent(decisions: list[Decision]) -> int:
    """The most subtasks allocated at once: per robot, the tasks of the envelope version
    it last accepted, summed over robots, at its maximum over the run."""
    held: dict[str, set[str]] = {}
    best = 0
    for d in decisions:
        if d.kind in (DecisionKind.ACCEPT, DecisionKind.RENEGOTIATE, DecisionKind.REFUSE):
            held[d.actor.split(".")[0]] = set(d.output.get("accepted") or [])
            best = max(best, sum(len(v) for v in held.values()))
    return best


def odometry_m(telemetry: Path) -> float:
    last: dict[str, tuple[float, float]] = {}
    total = 0.0
    if telemetry.exists():
        for line in telemetry.read_text().splitlines():
            r = json.loads(line)
            if r["robot_id"] in last:
                total += math.dist(last[r["robot_id"]], (r["x"], r["y"]))
            last[r["robot_id"]] = (r["x"], r["y"])
    return total


def _baseline_row(kpi: str, results: Path) -> dict:
    """KPI-2, -5, -6 (vs B1) and -8 from the latest ``a6g bench baselines`` result."""
    found = _latest(results, "n1_outage") or _latest(results, "e6_baselines")
    if not found:
        return _row(kpi, NA, note="run `a6g bench baselines` (B0-B2 against P)")
    t = found[1]["table"]
    if any("_o" in k for k in t):  # the outage sweep (SPEC A4 N1): its 60 s cells
        t = {k.rsplit("_o", 1)[0]: v for k, v in t.items() if k.endswith("_o60")}

    def pick(key: str, field: str) -> Any:
        return {arm: t[f"{arm}_f{3}"][field] for arm in ("P", key) if f"{arm}_f3" in t}

    value: Any
    if kpi == "UC2-KPI-2":
        value = pick("B0", "operator_person_hours")
        note = "operator person-hours per mission (the order and manual reassignments)"
    elif kpi == "UC2-KPI-8":
        value = {
            arm: {
                "completion_s_median": t[f"{arm}_f3"]["completion_s_median"],
                "completed": t[f"{arm}_f3"]["completed"],
                "runs": t[f"{arm}_f3"]["runs"],
            }
            for arm in ("P", "B0")
        }
        note = "with a coordinator crash and a robot failure (completion: all six done)"
    elif kpi == "UC2-KPI-6":
        value = pick("B1", "unrecovered_failures_per_mission")
        note = "unrecovered failures per mission"
    else:  # KPI-5
        value = {
            k: {"completion_s_median": v["completion_s_median"], "task_ratio": v["task_ratio_mean"]}
            for k, v in t.items()
            if k.split("_f")[0] in ("P", "B1")
        }
        note = "completion time and task ratio by fleet size, P against B1"
    return _row(kpi, value, source=found[0].name, note=note + " (fault-injection bench)")


def _kpi3_row(flows: list[Decision]) -> dict:
    """UC2-KPI-3. With ``network.mode: capacity`` (SPEC A4.7) from the grants: the share of
    the streaming flows requested that the network granted at the requested level (still
    emulated, rule 32). With the always-grant stand-in it is 1.0 by construction (proxy)."""
    capacity = any((d.policy or {}).get("network") == "capacity" for d in flows)
    answers = [
        a
        for d in flows
        for a in (d.output.get("answers") if isinstance(d.output.get("answers"), list) else [])
        if a.get("flow_class") != "telemetry"
    ]
    if capacity and answers:
        share = round(sum(1 for a in answers if a.get("state") == "granted") / len(answers), 4)
        return _row(
            "UC2-KPI-3",
            share,
            "share",
            "network answers in the reconcile decisions",
            "emulated network (capacity model on the coverage map, rule 32): streaming flows "
            "granted at the requested level, of those requested",
        )
    return _row(
        "UC2-KPI-3",
        1.0 if flows else None,
        "share",
        "flow records",
        "proxy: the network branch is a stand-in that grants every request until C5",
    )


def report(run_dir: Path, cfg: A6GConfig, results: Path = Path("bench/results")) -> dict:
    run = json.loads((run_dir / "run.json").read_text())
    end = _when(run["timeline"][-1]["sim"]) if run["timeline"] else None
    decisions = [
        Decision.model_validate_json(line)
        for line in (run_dir / "decisions.jsonl").read_text().splitlines()
        if line.strip()
    ]
    decisions = sorted(
        (d for d in decisions if end is None or d.stamp <= end), key=lambda d: d.stamp
    )
    start = _when(run["timeline"][0]["sim"]) if run["timeline"] else None
    hours = (end - start).total_seconds() / 3600 if start and end else 0.0
    manual = yaml.safe_load(Path("config/bench/manual_round.yaml").read_text())
    robots = sorted(
        {json.loads(x)["robot_id"] for x in (run_dir / "telemetry.jsonl").read_text().splitlines()}
    )
    links = link_events(decisions)
    operator = [
        o for o in run.get("operator", []) if o["reserved"] not in ("mission goal", "closure")
    ]
    reconciles = [d for d in decisions if d.kind is DecisionKind.RECONCILE]
    kpi11 = next((d.output for d in reconciles if d.output.get("kpi") == "UC2-KPI-11"), None)
    flows = [d for d in reconciles if "kpi" not in d.output]
    walked = odometry_m(run_dir / "telemetry.jsonl")
    inspected = sum(
        1 for d in decisions if d.kind is DecisionKind.SKILL and d.output.get("skill") == "inspect"
    )
    person_h = (
        walked / manual["walking_speed_mps"] / 3600 + inspected * manual["minutes_per_poi"] / 60
    )
    commands = len(run.get("operator", []))
    rows = [
        _row(
            "UC2-KPI-1",
            {
                "guard_person_hours": round(person_h, 3),
                "operator_person_hours": round(
                    commands * manual["operator_minutes_per_command"] / 60, 3
                ),
                "cost_saving_eur": round(
                    (person_h - commands * manual["operator_minutes_per_command"] / 60)
                    * manual["cost_per_hour_eur"],
                    2,
                ),
                "robot_hours": round(hours * len(robots), 3),
            },
            "h",
            "telemetry, decision log",
            "people's time for the same coverage (distance walked, points inspected): a "
            "guard's round versus the operator's commands; robot-hours apart. "
            "config/bench/manual_round.yaml is TODO(UPV)",
        ),
        _baseline_row("UC2-KPI-2", results),
        _kpi3_row(flows),
        _row(
            "UC2-KPI-4",
            sorted({r.platform.value for r in cfg.manifests.values()}),
            source="configuration",
            note="robot types in the fleet",
        ),
        _baseline_row("UC2-KPI-5", results),
        {
            **_baseline_row("UC2-KPI-6", results),
            "this_run": sum(e["recovered"] is None for e in links),
        },
        _row("UC2-KPI-7", max_concurrent(decisions), "subtasks", "decision log"),
        _baseline_row("UC2-KPI-8", results),
    ]
    for kpi, prefix, pick in (
        ("UC2-KPI-9", "e6_kpi9_fake", lambda r: r["exact"]),
        (
            "UC2-KPI-10",
            "e6_kpi10_fake",
            lambda r: {
                "places": r["places"]["accuracy"],
                "route_compliant": {k: v["compliant_rate"] for k, v in r["routes"].items()},
                "decomposition": r["decomposition"]["accuracy"],
            },
        ),
    ):
        found = _latest(results, prefix)
        rows.append(
            _row(
                kpi,
                pick(found[1]) if found else NA,
                source=found[0].name if found else "",
                note="bench (offline)",
            )
        )
    bench11 = _latest(results, "e5_kpi11_fake")
    rows.append(
        _row(
            "UC2-KPI-11",
            {
                "this_run_s": kpi11["posture_change_to_publication_s"] if kpi11 else None,
                "bench_p95_s": bench11[1]["p95_s"] if bench11 else None,
            },
            "s",
            "decision log; " + (bench11[0].name if bench11 else "no bench"),
            "posture change to reconciled envelopes (wall clock)",
        )
    )
    in_time = [
        e for e in links if e["recovery_s"] is not None and e["recovery_s"] <= RECOVERY_DEADLINE_S
    ]
    rows += [
        _row(
            "UC2-KPI-12",
            {"faults": len(links), "detected_and_recovered_in_time": len(in_time)},
            source="decision log",
            note=f"recovered without the operator within {RECOVERY_DEADLINE_S:.0f} s; "
            "recovery times: " + ", ".join(f"{e['robot']} {e['recovery_s']} s" for e in links),
        ),
        _row(
            "UC2-KPI-13",
            int(any(e["recovered"] is None for e in links)),
            "missions with an unrecovered failure",
            "decision log",
        ),
        _row(
            "UC2-KPI-14",
            {
                "disruptions": len(links),
                "replanned_and_resumed_without_operator": sum(
                    e["replanned"] and e["recovered"] is not None for e in links
                )
                if not operator
                else 0,
            },
            source="decision log",
        ),
    ]
    found = _latest(results, "e6_kpi15_fake")
    rows.append(
        _row(
            "UC2-KPI-15",
            {
                "observations_p_at_1": found[1]["observations"]["r_normalised_p_at_k"]["query"][
                    "1"
                ],
                "route_context": found[1]["route_context"]["router"],
                "handover_p_at_1": found[1]["handover"]["store_kept"]["precision_at_1"],
            }
            if found
            else NA,
            source=found[0].name if found else "",
            note="bench (offline)",
        )
    )
    checks = run.get("checks", {})
    guidance = checks.get("guidance", {})
    allocations = [
        d for d in decisions if d.kind in (DecisionKind.ALLOCATE, DecisionKind.REALLOCATE)
    ]
    unreconciled = [
        a.decision_id for a in allocations if not any(r.stamp >= a.stamp for r in reconciles)
    ]
    return {
        "run": str(run_dir),
        "scenario": run.get("scenario"),
        "seed": run.get("seed"),
        "fidelity": "functional (2D simulator)",
        "kpis": rows,
        "run_metrics": {
            "manual_interventions": len(operator),
            "audit_completeness": {
                "allocations": len(allocations),
                "without_reconcile": unreconciled,
                "decision_chain": checks.get("decision_chain"),
            },
            "exposed_coverage": NA + " (flood twin, stage E4)",
            "guidance": {
                k: guidance.get(k) for k in ("outcome", "repeated_requests", "route_compliant")
            },
            "separation": checks.get("separation"),
        },
    }


def markdown(rep: dict) -> str:
    lines = [
        f"# KPI report: {rep['scenario']} seed {rep['seed']} ({rep['fidelity']})",
        "",
        "| KPI | Value | Source | Note |",
        "|---|---|---|---|",
    ]
    for r in rep["kpis"]:
        value = r["value"] if isinstance(r["value"], str) else json.dumps(r["value"])
        unit = f" {r['unit']}" if r["unit"] and not isinstance(r["value"], dict) else ""
        lines.append(f"| {r['kpi']} | {value}{unit} | {r['source']} | {r['note']} |")
    lines += ["", "Run metrics: `" + json.dumps(rep["run_metrics"]) + "`"]
    return "\n".join(lines)


def write(rep: dict, run_dir: Path) -> tuple[Path, Path]:
    j, m = run_dir / "kpi.json", run_dir / "kpi.md"
    j.write_text(json.dumps(rep, indent=2, default=str))
    m.write_text(markdown(rep) + "\n")
    return j, m
