# ruff: noqa: E501  (table rows are kept on one line)
"""``a6g paper export``: the latest valid results for the paper.

Writes ``paper/RESULTS.md`` (one table per result: n, median and IQR, or p95 for
latencies; units; the manifest summary; results whose commit is not ``HEAD`` are flagged)
and the CSV files the paper figures are drawn from: ``fig_outage.csv`` (outage sweep),
``fig_scarcity.csv`` (scarcity bench), ``table_overhead.csv`` (overhead bench),
``table_protocols.csv`` (protocol bench) and ``table_cost.csv`` (emergency reaction time
offline, cloud and under scarcity; the unseen orders; place resolution and route
compliance; retrieval and leak probes; the G10 checks and link recovery).
"""

import csv
import json
import statistics
from collections.abc import Callable
from pathlib import Path
from typing import Any

from a6g_devstack import manifest as mf

RESULTS = Path("bench/results")


def _latest(prefix: str) -> Path | None:
    files = sorted(RESULTS.glob(f"{prefix}_*.json"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _all(prefix: str) -> list[Path]:
    return sorted(RESULTS.glob(f"{prefix}_*.json"), key=lambda p: p.stat().st_mtime)


def _load(path: Path | None) -> dict[str, Any] | None:
    return json.loads(path.read_text()) if path else None


def _q(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def med_iqr(values: list[float], digits: int = 1) -> str:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return "n/a"
    m, a, b = _q(vals, 0.5), _q(vals, 0.25), _q(vals, 0.75)
    return f"{m:.{digits}f} [{a:.{digits}f}, {b:.{digits}f}]"


def med_p95(values: list[float], digits: int = 1) -> str:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return "n/a"
    return f"{_q(vals, 0.5):.{digits}f} (p95 {_q(vals, 0.95):.{digits}f})"


def manifest_line(
    name: str, path: Path | None, data: dict[str, Any] | None, head: str | None
) -> str:
    if path is None or data is None:
        return f"| {name} | missing | | | | | |"
    m = data.get("manifest") or {}
    commit = (m.get("commit") or "")[:7] or "none"
    stale = " **possibly stale**" if head and m.get("commit") and m["commit"] != head else ""
    if not m:
        stale = " **no manifest**"
    models = ", ".join(
        f"{r}: {v.get('model')} ({v.get('fidelity')})" for r, v in (m.get("models") or {}).items()
    )
    net = f"{m.get('network', '?')} / {m.get('mode', '?')} / {m.get('coverage_source', '?')}"
    return (
        f"| {name} | `{path.name}` | {commit}{stale}{' dirty' if m.get('dirty') else ''} | "
        f"{models} | {net} | {m.get('seeds')} | {m.get('time_factor')} |"
    )


def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join("" if c is None else str(c) for c in r) + " |" for r in rows]
    return out


# --------------------------------------------------------------------------- sections


def outage_section(data: dict[str, Any]) -> list[str]:
    rows = [r for r in data["rows"] if "error" not in r]
    cells: dict[tuple, list[dict[str, Any]]] = {}
    for r in rows:
        cells.setdefault((r["arm"], r["fleet"], r["outage_s"]), []).append(r)
    out = []
    for (arm, fleet, o), rs in sorted(cells.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0])):
        out.append(
            [
                arm,
                fleet,
                "inf" if o == float("inf") else int(o),
                len(rs),
                f"{sum(1 for r in rs if r['completed'])}/{len(rs)}",
                med_iqr([r["completion_s"] for r in rs if r["completion_s"] is not None], 0),
                f"{statistics.fmean(r['task_ratio'] for r in rs):.3f}",
                med_iqr(
                    [r["time_to_resume_s"] for r in rs if r.get("time_to_resume_s") is not None], 0
                ),
                f"{statistics.fmean(r['resumed_during_outage'] for r in rs):.2f}",
                f"{statistics.fmean(r['duplicates'] for r in rs):.2f}",
                f"{statistics.fmean(r['operator_h'] for r in rs):.4f}",
                med_iqr([r.get("messages_per_robot") for r in rs], 0),
                med_iqr([r.get("coordinator_run_p95_ms") for r in rs], 0),
            ]
        )
    errors = len(data["rows"]) - len(rows)
    lines = _table(
        [
            "config",
            "fleet",
            "outage s",
            "n",
            "completed",
            "completion s, median [IQR]",
            "task ratio",
            "time to resume s",
            "resumed in outage",
            "duplicates",
            "operator h",
            "msgs/robot",
            "coord. run p95 ms",
        ],
        out,
    )
    if errors:
        lines.append(f"\n{errors} runs failed (in the JSON, `error`).")
    return lines


def scarcity_section(data: dict[str, Any]) -> list[str]:
    rows = [r for r in data["rows"] if "error" not in r]
    cells: dict[tuple, list[dict[str, Any]]] = {}
    for r in rows:
        cells.setdefault((r["variant"], r["capacity_mbps"], r["fleet"]), []).append(r)
    out = []
    for (v, c, f), rs in sorted(cells.items(), key=lambda kv: (kv[0][2], kv[0][1], kv[0][0])):
        out.append(
            [
                v,
                c,
                f,
                len(rs),
                med_iqr([r["posts_held"] for r in rs], 0),
                med_iqr([r["posts_video_usable"] for r in rs], 0),
                med_iqr([r["posts_sensing_usable"] for r in rs], 0),
                med_iqr(
                    [r["oversubscription"] for r in rs if r.get("oversubscription") is not None], 2
                ),
                f"{sum(r['i1_violations'] for r in rs)}/{sum(r['i2_violations'] for r in rs)}/{sum(r['i3_violations'] for r in rs)}",
                med_p95(
                    [
                        r["posture_change_to_reconciled_s"]
                        for r in rs
                        if r.get("posture_change_to_reconciled_s") is not None
                    ],
                    3,
                ),
                sum(r["escalations"] for r in rs),
                med_iqr([r["kpi3_share"] for r in rs if r.get("kpi3_share") is not None], 3),
            ]
        )
    lines = _table(
        [
            "variant",
            "UL capacity Mbps",
            "fleet",
            "n",
            "posts held",
            "video usable",
            "sensing usable",
            "oversubscription",
            "I1/I2/I3 violations",
            "posture -> reconciled s (wall)",
            "escalations",
            "KPI-3 share",
        ],
        out,
    )
    for what, why in (data.get("skipped") or {}).items():
        lines.append(f"\nSkipped {what}: {why}.")
    return lines


def overhead_section(data: dict[str, Any]) -> list[str]:
    lines = [
        "**Traffic per robot** (bytes per simulated second; wire bytes accounted by the binding, in-process transport)",
        "",
    ]
    rows = []
    for key, m in data["traffic"]["by_source_phase_fleet"].items():
        src, phase, fleet = key.split("|")
        rows.append(
            [
                src,
                phase,
                fleet[1:],
                m["up_wire_Bps"]["n"],
                f"{m['up_wire_Bps']['median']} (p95 {m['up_wire_Bps']['p95']})",
                f"{m['down_wire_Bps']['median']} (p95 {m['down_wire_Bps']['p95']})",
                f"{m['up_payload_Bps']['median']}",
                f"{m['down_payload_Bps']['median']}",
            ]
        )
    lines += _table(
        [
            "runs",
            "phase",
            "fleet",
            "n",
            "up wire B/s",
            "down wire B/s",
            "up payload B/s",
            "down payload B/s",
        ],
        rows,
    )
    lines += ["", "**Per pattern** (G10)", ""]
    lines += _table(
        ["pattern", "messages", "payload mean B", "payload p95 B", "wire mean B", "NL share"],
        [
            [
                p,
                r["messages"],
                r["payload_mean"],
                r["payload_p95"],
                r["wire_mean"],
                f"{r['nl_share']:.1%}",
            ]
            for p, r in data["patterns_g10"].items()
        ],
    )
    mc = data["model_calls"]
    lines += [
        "",
        f"**Model calls** ({mc['fidelity']}; sources: {', '.join(mc['sources']) or 'none'})",
        "",
    ]
    lines += _table(
        [
            "role/function",
            "calls",
            "input tokens mean",
            "output tokens mean",
            "latency p50 ms",
            "latency p95 ms",
        ],
        [
            [
                k,
                v["calls"],
                v["input_tokens_mean"],
                v["output_tokens_mean"],
                v["latency_ms_p50"],
                v["latency_ms_p95"],
            ]
            for k, v in mc["by_role_function"].items()
        ],
    )
    ck = data["checkpoints"]
    if ck.get("runs"):
        lines += [
            "",
            f"**Checkpoints** ({ck['runs']} G10 runs; coordinator: {ck['coordinator_checkpointer']}; robots: {ck['robot_checkpointer']})",
            "",
        ]
        rows = []
        for g, t in ck["totals"].items():
            rows.append(
                [
                    g,
                    "all writes",
                    t["writes"],
                    f"{t['bytes']['median']} (p95 {t['bytes']['p95']})",
                    f"{t['ms']['median']} (p95 {t['ms']['p95']})",
                ]
            )
        for g, s in ck["latest_checkpoint_bytes"].items():
            rows.append([g, "latest checkpoint", s["n"], f"{s['median']} (p95 {s['p95']})", ""])
        for g, s in ck["versions_per_thread"].items():
            rows.append([g, "versions per thread", s["n"], f"{s['median']} (p95 {s['p95']})", ""])
        lines += _table(["agent", "item", "n", "bytes", "write ms"], rows)
        lines += ["", "Per event type (bytes / write ms, median):", ""]
        ev_rows = []
        for g, events in ck["by_event"].items():
            for ev, s in events.items():
                ev_rows.append(
                    [
                        g,
                        ev,
                        s["bytes"]["n"],
                        s["bytes"]["median"],
                        s["ms"]["median"],
                        s["ms"]["p95"],
                    ]
                )
        lines += _table(["agent", "op:event", "n", "bytes median", "ms median", "ms p95"], ev_rows)
    return lines


def protocols_section(data: dict[str, Any]) -> list[str]:
    rows = []
    for key, c in data["table"].items():
        b, m, e, prof, pat, nl, n, o = key.split("|")
        if pat == "outage":
            continue
        rows.append(
            [
                f"{b}/{m}/{e}",
                prof,
                pat,
                nl,
                n,
                c["repeats"],
                c["units"],
                c["wire_bytes_per_unit"],
                c["latency_p50_ms"],
                c["latency_p95_ms"],
                c["latency_p99_ms"],
                c["deadline_misses"],
                c["timeouts"],
            ]
        )
    lines = [
        f"RTT checks: {[(r['rtt_check'], r['measured_ms'], 'ok' if r['ok'] else 'OUT OF 10 %') for r in data.get('rtt_checks', [])]}",
        "",
    ]
    lines += _table(
        [
            "binding",
            "profile",
            "pattern",
            "nl",
            "robots",
            "repeats",
            "units",
            "wire B/unit",
            "p50 ms",
            "p95 ms",
            "p99 ms",
            "misses",
            "timeouts",
        ],
        rows,
    )
    out_rows = []
    for key, c in data["table"].items():
        b, m, e, prof, pat, nl, n, o = key.split("|")
        if pat != "outage" or not c.get("outage"):
            continue
        oo = c["outage"]
        out_rows.append(
            [
                f"{b}/{m}/{e}",
                o,
                oo.get("restore_to_first_delivery_s"),
                oo.get("lost"),
                oo.get("duplicated"),
                oo.get("late"),
                oo.get("attempts_max"),
            ]
        )
    if out_rows:
        lines += ["", "**Outages** (robot link down during a P2 stream, one every 2 s)", ""]
        lines += _table(
            [
                "binding",
                "outage s",
                "restore -> first delivery s",
                "lost",
                "duplicated",
                "late",
                "max attempts",
            ],
            out_rows,
        )
    checks = [
        (k.split("|")[0], c["pcap_check"]) for k, c in data["table"].items() if c.get("pcap_check")
    ]
    if checks:
        lines += [
            "",
            "Capture check (frame bytes captured / interface counters): "
            + ", ".join(f"{b} {c['ratio']}" for b, c in checks),
        ]
    return lines


def cost_rows() -> list[dict[str, Any]]:
    """``table_cost.csv``: what the system costs and buys, one row per measure."""
    rows: list[dict[str, Any]] = []
    for p in _all("e5_kpi11"):
        d = json.loads(p.read_text())
        if not d.get("manifest"):
            continue  # no manifest: superseded
        rows.append(
            {
                "measure": "UC2-KPI-11 posture change -> reconciled envelopes",
                "variant": d["model"],
                "n": d["seeds"],
                "median": d["p50_s"],
                "p95": d["p95_s"],
                "unit": "s (wall)",
                "source": p.name,
            }
        )
        rows.append(
            {
                "measure": "UC2-KPI-11 all robots answered",
                "variant": d["model"],
                "n": d["seeds"],
                "median": d["acknowledged_p50_s"],
                "p95": d["acknowledged_p95_s"],
                "unit": "s (wall)",
                "source": p.name,
            }
        )
    for p in _all("orders_unseen"):
        d = json.loads(p.read_text())
        if not d.get("manifest"):
            continue
        for mode, m in d["modes"].items():
            rows.append(
                {
                    "measure": "unseen orders understood",
                    "variant": mode,
                    "n": d["cases"],
                    "median": m["understood"],
                    "p95": None,
                    "unit": "share",
                    "source": p.name,
                }
            )
            rows.append(
                {
                    "measure": "unseen orders: envelopes sent",
                    "variant": mode,
                    "n": d["cases"],
                    "median": m["latency_p50_s"],
                    "p95": m["latency_p95_s"],
                    "unit": "s (wall)",
                    "source": p.name,
                }
            )
    kb = _latest("e6_kpi10_gpt-5-nano+embedder") or _latest("e6_kpi10")
    if kb:
        d = json.loads(kb.read_text())
        for k, v in d["places"]["accuracy"].items():
            rows.append(
                {
                    "measure": "place resolution accuracy",
                    "variant": k,
                    "n": d["places"]["items"],
                    "median": v,
                    "p95": None,
                    "unit": "share",
                    "source": kb.name,
                }
            )
        for case, r in d["routes"].items():
            for k, val in (r.get("compliant_rate") or {}).items():
                rows.append(
                    {
                        "measure": f"route compliance ({case})",
                        "variant": k,
                        "n": r.get("trips"),
                        "median": val,
                        "p95": None,
                        "unit": "share",
                        "source": kb.name,
                    }
                )
    rt = _latest("e6_kpi15_embedder") or _latest("e6_kpi15")
    if rt:
        d = json.loads(rt.read_text())
        for k, v in d["observations"]["r_normalised_p_at_k"]["query"].items():
            rows.append(
                {
                    "measure": f"observation retrieval R-normalised P@{k}",
                    "variant": "query",
                    "n": d["observations"]["queries"],
                    "median": v,
                    "p95": None,
                    "unit": "share",
                    "source": rt.name,
                }
            )
        for case, h in d["handover"].items():
            rows.append(
                {
                    "measure": "hand-over context precision@1",
                    "variant": case,
                    "n": h["handovers"],
                    "median": h["precision_at_1"],
                    "p95": None,
                    "unit": "share",
                    "source": rt.name,
                }
            )
            rows.append(
                {
                    "measure": "hand-over leaks",
                    "variant": case,
                    "n": h["probes"],
                    "median": h["leaks"],
                    "p95": None,
                    "unit": "count",
                    "source": rt.name,
                }
            )
    for base in (Path("var/runs/g10_n2"), Path("var/runs/g10")):
        runs = sorted(base.glob("seed*/run.json"))
        if not runs:
            continue
        ok = 0
        recov: list[float] = []
        for r in runs:
            d = json.loads(r.read_text())
            checks = d.get("checks") or {}
            ok += all(
                v.get("ok", True) for v in checks.values() if isinstance(v, dict) and "ok" in v
            )
            k = r.parent / "kpi.json"
            if k.exists():
                kp = json.loads(k.read_text())
                for x in kp.get("kpis") or []:
                    if x.get("kpi") == "UC2-KPI-12":
                        import re

                        times = (x.get("note") or "").split("recovery times:", 1)
                        if len(times) == 2:
                            recov += [float(v) for v in re.findall(r"([\d.]+) s", times[1])]
        rows.append(
            {
                "measure": "G10 runs with every check passing",
                "variant": str(base),
                "n": len(runs),
                "median": ok,
                "p95": None,
                "unit": "count",
                "source": str(base),
            }
        )
        rows.append(
            {
                "measure": "G10 link recovery time (UC2-KPI-12)",
                "variant": str(base),
                "n": len(recov),
                "median": _q(recov, 0.5),
                "p95": _q(recov, 0.95),
                "unit": "s (simulated)",
                "source": str(base),
            }
        )
        break
    return rows


# --------------------------------------------------------------------------- export


def export(out: Path, cfg: Any) -> dict[str, Any]:
    from a6g_devstack import bench_baselines, bench_overhead, bench_protocols, bench_scarcity

    out.mkdir(parents=True, exist_ok=True)
    head = mf.current_git_state().get("commit")
    sources: dict[str, tuple[Path | None, dict[str, Any] | None]] = {}
    for name, prefix in (
        ("N1 outage sweep", "n1_outage"),
        ("N4 scarcity (G13)", "n4_scarcity"),
        ("N2 overhead", "n2_overhead"),
        ("N5 protocols", "n5_protocols"),
    ):
        p = _latest(prefix)
        sources[name] = (p, _load(p))
    written: dict[str, str] = {}
    writers: dict[str, tuple[str, Callable[[dict[str, Any], Path], Path]]] = {
        "N1 outage sweep": ("fig_outage.csv", bench_baselines.write_csv),
        "N4 scarcity (G13)": ("fig_scarcity.csv", bench_scarcity.write_csv),
        "N2 overhead": ("table_overhead.csv", bench_overhead.write_csv),
        "N5 protocols": ("table_protocols.csv", bench_protocols.write_csv),
    }
    for name, (fname, writer) in writers.items():
        data = sources[name][1]
        if data is not None:
            written[fname] = str(writer(data, out / fname))
    cost = cost_rows()
    with (out / "table_cost.csv").open("w", newline="") as fh:
        w = csv.DictWriter(
            fh, fieldnames=["measure", "variant", "n", "median", "p95", "unit", "source"]
        )
        w.writeheader()
        w.writerows(cost)
    written["table_cost.csv"] = str(out / "table_cost.csv")

    md = [
        "# Results for the IEEE Network submission",
        "",
        f"*Exported by `a6g paper export` at commit `{(head or 'none')[:7]}`. Every number below is "
        "from a result file with a manifest (rule 31); results whose commit is not this one are "
        "flagged **possibly stale** (rule 31). Network results are emulated, never measured on the campus network "
        "(rule 32). Medians with the interquartile range [Q1, Q3]; latencies with p95.*",
        "",
        "## Manifests",
        "",
        "| result | file | commit | models (fidelity) | network / mode / coverage | seeds | time |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, (p, d) in sources.items():
        md.append(manifest_line(name, p, d, head))
    for p in _all("e5_kpi11") + _all("orders_unseen"):
        d = json.loads(p.read_text())
        if d.get("manifest"):
            md.append(manifest_line(p.name.split("_2026")[0], p, d, head))
    sections = (
        (
            "N1: coordinator outages and fleet sizes (UC2-KPI-2, -5, -6, -8)",
            "N1 outage sweep",
            outage_section,
        ),
        (
            "N4: emergency under network scarcity, G13 (UC2-KPI-3, -11)",
            "N4 scarcity (G13)",
            scarcity_section,
        ),
        (
            "N2: agent traffic and the overhead of the twin as memory",
            "N2 overhead",
            overhead_section,
        ),
        ("N5: agent protocols over emulated radio links", "N5 protocols", protocols_section),
    )
    for title, key, fn in sections:
        md += ["", f"## {title}", ""]
        data = sources[key][1]
        md += fn(data) if data is not None else ["*No result yet.*"]
    md += ["", "## Costs and other measures (`table_cost.csv`)", ""]
    md += _table(
        ["measure", "variant", "n", "median", "p95", "unit", "source"],
        [
            [r["measure"], r["variant"], r["n"], r["median"], r["p95"], r["unit"], r["source"]]
            for r in cost
        ],
    )
    (out / "RESULTS.md").write_text("\n".join(md) + "\n")
    written["RESULTS.md"] = str(out / "RESULTS.md")
    return {"written": written, "head": head}
