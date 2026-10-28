"""Agent traffic and the overhead of the twin as memory: measurement only.

* **Traffic** from the message logs (``comms.jsonl``) of G10 runs (fleet 3; phases routine,
  interactive, emergency from the run's timeline) and of the outage-sweep runs of P at the 60 s
  outage (fleets 3, 5, 8; routine inspections): per robot, uplink and downlink bytes per
  simulated second, payload and wire. Wire bytes are ``accounted`` by the binding (the
  in-process stacks use the in-memory transport). Per pattern (P1-P11): count, payload mean
  and p95, wire mean, and the share of bytes in ``nl.*`` fields.
* **Model calls** from the cloud results (emergency reaction time and unseen orders): per
  role and function, calls, tokens and latency (``fidelity: functional``, cloud endpoint,
  latency includes the Internet round trip).
* **Checkpoints** from ``checkpoints.jsonl`` (``--checkpoint-meter`` G10 runs): bytes and
  write latency per agent and event type, the size of the latest checkpoint and the number
  of versions per thread.
"""

import csv
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

PATTERNS = tuple(f"P{i}" for i in range(1, 12))


def _when(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 3)


def _stats(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "median": _pct(values, 0.5),
        "p95": _pct(values, 0.95),
        "mean": round(statistics.fmean(values), 3) if values else None,
    }


def _messages(path: Path) -> list[dict[str, Any]]:
    out = []
    if path.exists():
        with path.open() as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


# --------------------------------------------------------------------------- traffic


def g10_phases(run_dir: Path) -> list[tuple[str, datetime, datetime]]:
    """routine (survey) -> interactive (escort) -> emergency (alert) -> routine (resumed)."""
    run = json.loads((run_dir / "run.json").read_text())
    t = {e["event"]: _when(e["sim"]) for e in run["timeline"]}
    end = _when(run["timeline"][-1]["sim"])
    marks = [
        ("routine", t.get("survey_started")),
        ("interactive", t.get("request")),
        ("emergency", t.get("weather_alert")),
        ("routine", t.get("all_clear")),
    ]
    marks = [(p, w) for p, w in marks if w is not None]
    out = []
    for i, (phase, start) in enumerate(marks):
        stop = marks[i + 1][1] if i + 1 < len(marks) else end
        if stop > start:
            out.append((phase, start, stop))
    return out


def _robot_bytes(msgs: list[dict[str, Any]], robots: list[str]) -> dict[str, dict[str, float]]:
    """Per robot: uplink and downlink payload and wire bytes (unique messages)."""
    out = {
        r: {"up_payload": 0.0, "up_wire": 0.0, "down_payload": 0.0, "down_wire": 0.0}
        for r in robots
    }
    for m in msgs:
        d, sender = m.get("direction"), m.get("sender")
        receivers = m.get("receivers") or []
        pay, wire = float(m.get("payload_bytes") or 0), float(m.get("wire_bytes") or 0)
        if d in ("send", "reply") and sender in out:
            if d == "send" and m.get("agent") != sender:
                continue  # logged by the receiving side: counted below as its downlink
            out[sender]["up_payload"] += pay
            out[sender]["up_wire"] += wire
        if d == "recv" and m.get("agent") in out:
            out[m["agent"]]["down_payload"] += pay
            out[m["agent"]]["down_wire"] += wire
        elif d in ("send", "reply") and m.get("binding") != "mqtt":
            for r in receivers:
                if r in out and r != sender:
                    out[r]["down_payload"] += pay
                    out[r]["down_wire"] += wire
    return out


def traffic_rows(
    msgs: list[dict[str, Any]],
    robots: list[str],
    phases: list[tuple[str, datetime, datetime]],
    fleet: int,
    source: str,
) -> list[dict[str, Any]]:
    rows = []
    for phase, start, stop in phases:
        span = (stop - start).total_seconds()
        if span <= 0:
            continue
        inside = [m for m in msgs if m.get("sim_ts") and start <= _when(m["sim_ts"]) < stop]
        per = _robot_bytes(inside, robots)
        for r, b in per.items():
            rows.append(
                {
                    "source": source,
                    "fleet": fleet,
                    "phase": phase,
                    "robot": r,
                    "sim_s": round(span, 1),
                    **{f"{k}_Bps": round(v / span, 2) for k, v in b.items()},
                }
            )
    return rows


def pattern_table(msgs: list[dict[str, Any]]) -> dict[str, Any]:
    """Per pattern: messages (each counted once, at its sender), payload, wire, nl share."""
    by: dict[str, list[dict[str, Any]]] = {}
    for m in msgs:
        d = m.get("direction")
        if d not in ("send", "reply") or m.get("pattern") not in PATTERNS:
            continue
        if d == "send" and m.get("agent") != m.get("sender"):
            continue
        by.setdefault(m["pattern"], []).append(m)
    out = {}
    for p in PATTERNS:
        ms = by.get(p, [])
        if not ms:
            continue
        pay = [float(m.get("payload_bytes") or 0) for m in ms]
        wire = [float(m.get("wire_bytes") or 0) for m in ms]
        nl = sum(float(m.get("nl_bytes") or 0) for m in ms)
        out[p] = {
            "messages": len(ms),
            "payload_mean": round(statistics.fmean(pay), 1),
            "payload_p95": _pct(pay, 0.95),
            "wire_mean": round(statistics.fmean(wire), 1),
            "wire_accounted": True,
            "nl_share": round(nl / sum(pay), 4) if sum(pay) else 0.0,
            "schemas": sorted({str(m.get("schema")) for m in ms}),
        }
    return out


# --------------------------------------------------------------------------- model calls


def model_call_table(results: list[Path]) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    sources = []
    for path in results:
        data = json.loads(path.read_text())
        rows = list(data.get("runs") or [])
        for mode in (data.get("modes") or {}).values():
            rows += mode.get("rows") or []
        found = [c for r in rows for c in r.get("model_calls") or []]
        if found:
            sources.append(path.name)
            calls += found
    table: dict[str, Any] = {}
    for c in calls:
        if c.get("outcome") not in ("ok", "accepted", "rejected", None):
            continue
        key = f"{c.get('role')}/{c.get('function')}"
        table.setdefault(key, []).append(c)
    out = {
        k: {
            "calls": len(v),
            "input_tokens_mean": round(statistics.fmean(float(c.get("in") or 0) for c in v), 1),
            "output_tokens_mean": round(statistics.fmean(float(c.get("out") or 0) for c in v), 1),
            "latency_ms_p50": _pct([float(c["ms"]) for c in v if c.get("ms") is not None], 0.5),
            "latency_ms_p95": _pct([float(c["ms"]) for c in v if c.get("ms") is not None], 0.95),
        }
        for k, v in sorted(table.items())
    }
    return {
        "fidelity": "functional (cloud endpoint; latency includes the Internet round trip)",
        "sources": sources,
        "by_role_function": out,
    }


# --------------------------------------------------------------------------- checkpoints


def checkpoint_table(run_dirs: list[Path], persistence: str) -> dict[str, Any]:
    recs = [r for d in run_dirs for r in _messages(d / "checkpoints.jsonl")]
    if not recs:
        return {"runs": 0}

    def group(agent: str) -> str:
        return "coordinator" if agent == "coordinator" else "robot"

    by: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for r in recs:
        by.setdefault(group(r["agent"]), {}).setdefault(f"{r['op']}:{r['event']}", []).append(r)
    table = {
        g: {
            k: {
                "bytes": _stats([float(x["bytes"]) for x in v if x["bytes"] >= 0]),
                "ms": _stats([float(x["ms"]) for x in v]),
            }
            for k, v in sorted(events.items())
        }
        for g, events in by.items()
    }
    latest: dict[str, list[float]] = {"coordinator": [], "robot": []}
    versions: dict[str, list[float]] = {"coordinator": [], "robot": []}
    for d in run_dirs:
        last: dict[tuple[str, str], int] = {}
        count: dict[tuple[str, str], int] = {}
        for r in _messages(d / "checkpoints.jsonl"):
            if r["op"] != "put":
                continue
            key = (group(r["agent"]), str(r.get("thread")))
            last[key] = r["bytes"]
            count[key] = count.get(key, 0) + 1
        for (g, _), b in last.items():
            latest[g].append(float(b))
        for (g, _), n in count.items():
            versions[g].append(float(n))
    totals = {
        g: {
            "writes": sum(len(v) for v in events.values()),
            "bytes": _stats(
                [float(x["bytes"]) for v in events.values() for x in v if x["bytes"] >= 0]
            ),
            "ms": _stats([float(x["ms"]) for v in events.values() for x in v]),
        }
        for g, events in by.items()
    }
    return {
        "runs": len(run_dirs),
        "coordinator_checkpointer": persistence,
        "robot_checkpointer": "sqlite (local)",
        "totals": totals,
        "by_event": table,
        "latest_checkpoint_bytes": {g: _stats(v) for g, v in latest.items()},
        "versions_per_thread": {g: _stats(v) for g, v in versions.items()},
    }


# --------------------------------------------------------------------------- report


def report(
    g10_dirs: list[Path],
    n1_dirs: list[Path],
    model_results: list[Path],
    persistence: str,
) -> dict[str, Any]:
    traffic: list[dict[str, Any]] = []
    patterns_g10: list[dict[str, Any]] = []
    for d in g10_dirs:
        msgs = _messages(d / "comms.jsonl")
        patterns_g10 += msgs
        robots = sorted(
            {
                m["agent"]
                for m in msgs
                if m.get("agent") not in (None, "coordinator")
                and not str(m.get("agent")).startswith("kb")
            }
        )
        robots = [r for r in robots if r.startswith(("go2", "summit"))]
        traffic += traffic_rows(msgs, robots, g10_phases(d), len(robots), f"g10/{d.name}")
    for d in n1_dirs:
        msgs = _messages(d / "comms.jsonl")
        stamps = [_when(m["sim_ts"]) for m in msgs if m.get("sim_ts")]
        if not stamps:
            continue
        robots = sorted(
            {m["agent"] for m in msgs if str(m.get("agent")).startswith(("go2", "summit"))}
        )
        fleet = int(d.name.split("_f")[1].split("_")[0])
        traffic += traffic_rows(
            msgs, robots, [("routine", min(stamps), max(stamps))], fleet, f"n1/{d.name}"
        )
    cells: dict[str, list[dict[str, Any]]] = {}
    for r in traffic:
        cells.setdefault(f"{r['source'].split('/')[0]}|{r['phase']}|f{r['fleet']}", []).append(r)
    summary = {
        k: {
            m: _stats([float(r[m]) for r in v])
            for m in ("up_payload_Bps", "up_wire_Bps", "down_payload_Bps", "down_wire_Bps")
        }
        for k, v in sorted(cells.items())
    }
    return {
        "spec": "SPEC A4 N2",
        "traffic": {
            "unit": "bytes per simulated second per robot; wire bytes accounted by the binding "
            "(in-process transport)",
            "by_source_phase_fleet": summary,
            "rows": traffic,
        },
        "patterns_g10": pattern_table(patterns_g10),
        "model_calls": model_call_table(model_results),
        "checkpoints": checkpoint_table(g10_dirs, persistence),
        "inputs": {
            "g10": [str(d) for d in g10_dirs],
            "n1": [str(d) for d in n1_dirs],
            "model_results": [p.name for p in model_results],
        },
    }


def write(result: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"n2_overhead_{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps(result, indent=2, default=str))
    return path


def write_csv(result: dict[str, Any], path: Path) -> Path:
    """``paper/table_overhead.csv``: section, group, item, metric, n, median, p95, unit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["section", "group", "item", "metric", "n", "median", "p95", "mean", "unit"])
        for key, metrics in result["traffic"]["by_source_phase_fleet"].items():
            for metric, s in metrics.items():
                w.writerow(
                    [
                        "traffic",
                        key,
                        "per robot",
                        metric,
                        s["n"],
                        s["median"],
                        s["p95"],
                        s["mean"],
                        "B/s (sim)",
                    ]
                )
        for p, row in result["patterns_g10"].items():
            for metric in ("messages", "payload_mean", "payload_p95", "wire_mean", "nl_share"):
                w.writerow(
                    [
                        "pattern",
                        "g10",
                        p,
                        metric,
                        row["messages"],
                        row[metric],
                        "",
                        "",
                        "count"
                        if metric == "messages"
                        else ("share" if metric == "nl_share" else "B"),
                    ]
                )
        for k, row in result["model_calls"]["by_role_function"].items():
            w.writerow(
                [
                    "model_call",
                    k,
                    "latency",
                    "ms",
                    row["calls"],
                    row["latency_ms_p50"],
                    row["latency_ms_p95"],
                    "",
                    "ms",
                ]
            )
            w.writerow(
                [
                    "model_call",
                    k,
                    "tokens",
                    "input_mean",
                    row["calls"],
                    "",
                    "",
                    row["input_tokens_mean"],
                    "tokens",
                ]
            )
            w.writerow(
                [
                    "model_call",
                    k,
                    "tokens",
                    "output_mean",
                    row["calls"],
                    "",
                    "",
                    row["output_tokens_mean"],
                    "tokens",
                ]
            )
        ck = result["checkpoints"]
        for g, events in (ck.get("by_event") or {}).items():
            for ev, s in events.items():
                w.writerow(
                    [
                        "checkpoint",
                        g,
                        ev,
                        "bytes",
                        s["bytes"]["n"],
                        s["bytes"]["median"],
                        s["bytes"]["p95"],
                        s["bytes"]["mean"],
                        "B",
                    ]
                )
                w.writerow(
                    [
                        "checkpoint",
                        g,
                        ev,
                        "write_ms",
                        s["ms"]["n"],
                        s["ms"]["median"],
                        s["ms"]["p95"],
                        s["ms"]["mean"],
                        "ms",
                    ]
                )
        for g, s in (ck.get("latest_checkpoint_bytes") or {}).items():
            w.writerow(
                [
                    "checkpoint",
                    g,
                    "latest checkpoint",
                    "bytes",
                    s["n"],
                    s["median"],
                    s["p95"],
                    s["mean"],
                    "B",
                ]
            )
        for g, s in (ck.get("versions_per_thread") or {}).items():
            w.writerow(
                [
                    "checkpoint",
                    g,
                    "versions per thread",
                    "count",
                    s["n"],
                    s["median"],
                    s["p95"],
                    s["mean"],
                    "count",
                ]
            )
    return path
