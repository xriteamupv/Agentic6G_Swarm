# ruff: noqa: E501  (table columns are kept on one line)
"""``a6g bench protocols``: agent protocols over emulated radio links.

Builds the block matrix from ``config/bench/protocols.yaml``, samples payload sizes from the
recorded G10 message logs (so sizes are those of real ``AgentMessage``s), runs the blocks in
isolated rootless labs (``protolab.lab``, in parallel), and aggregates. Output:
``n5_protocols_*.json`` and ``paper/table_protocols.csv``.
"""

import asyncio
import csv
import json
import random
import shutil
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from a6g_common.config import A6GConfig

SCHEMA_KEYS = {
    ("P1", "envelope"): "P1",
    ("P2", "report"): "P2",
    ("P3", "cfp"): "P3_cfp",
    ("P4", "robot_state"): "P4",
    ("P7", "report"): "P7",
}
DEFAULTS = {"P1": 760, "P2": 1000, "P3_cfp": 700, "P4": 380, "P6": 1200, "P7": 470}


def payload_sizes(logs: list[Path], seed: int, n: int = 500) -> dict[str, list[int]]:
    """Payload (and NL) sizes per pattern from recorded message logs, sampled with ``seed``."""
    found: dict[str, list[int]] = {}
    for path in logs:
        with path.open() as fh:
            for line in fh:
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if m.get("direction") != "send":
                    continue
                key = SCHEMA_KEYS.get((m.get("pattern"), m.get("schema")))
                if key is None:
                    continue
                found.setdefault(key, []).append(int(m.get("payload_bytes") or 0))
                found.setdefault(f"{key}_nl", []).append(int(m.get("nl_bytes") or 0))
    rng = random.Random(seed)
    out: dict[str, list[int]] = {}
    for key, default in DEFAULTS.items():
        pool = found.get(key) or [default]
        out[key] = [rng.choice(pool) for _ in range(n)]
        nl = found.get(f"{key}_nl") or [0]
        out[f"{key}_nl"] = [rng.choice(nl) for _ in range(n)]
    out["P6"] = out.get("P6") or [DEFAULTS["P6"]]  # + the 300 KB JPEG (attachment)
    out["_sources"] = [str(p) for p in logs]  # type: ignore[list-item]
    out["_counts"] = {k: len(v) for k, v in found.items()}  # type: ignore[assignment]
    return out


def matrix(spec: dict[str, Any], reduced: bool) -> list[dict[str, Any]]:
    bindings = spec["reduced"]["bindings"] if reduced else spec["bindings"]
    patterns = spec["patterns"]
    names = spec["reduced"]["patterns"] if reduced else list(patterns)
    outages = spec["reduced"]["outages"] if reduced else spec["outages"]
    blocks = []
    for b in bindings:
        tag = f"{b['binding']}-{b['mode']}-{b['encoding']}"
        for prof in spec["profiles"]:
            for rep in range(spec["repeats"]):
                for p in names:
                    cfg = patterns[p]
                    for nl in cfg.get("nl", ["full"]):
                        blocks.append(
                            {
                                **b,
                                "profile": prof,
                                "pattern": p,
                                "nl": nl,
                                "rep": rep,
                                "count": cfg.get("count", 1),
                                "duration_s": cfg.get("duration_s", 300),
                                "seed": rep,
                                "id": f"{tag}-{prof}-{p}-{nl}-r{rep}",
                                # one block per binding is checked against a packet capture
                                "capture": prof == "good"
                                and p == "P2"
                                and nl == "full"
                                and rep == 0,
                            }
                        )
        for o in outages:
            blocks.append(
                {
                    **b,
                    "profile": "good",
                    "pattern": "outage",
                    "outage_s": o,
                    "duration_s": spec.get("outage_stream_s", 150),
                    "rep": 0,
                    "id": f"{tag}-good-outage{o}",
                }
            )
    return blocks


async def _lab(job: dict[str, Any], log: Path) -> int:
    path = Path(job["workdir"]) / "job.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job))
    with log.open("w") as fh:
        proc = await asyncio.create_subprocess_exec(
            "unshare",
            "--user",
            "--map-root-user",
            "--net",
            "--mount",
            sys.executable,
            "-m",
            "a6g_devstack.protolab.lab",
            str(path),
            stdout=fh,
            stderr=fh,
        )
        return await proc.wait()


async def run(cfg: A6GConfig, spec: dict[str, Any], workdir: Path, reduced: bool) -> dict[str, Any]:
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    logs = sorted(Path("var/runs/g10_n2").glob("seed*/comms.jsonl")) or sorted(
        Path("var/runs/g10").glob("seed*/comms.jsonl")
    )
    logs += sorted(Path("var/runs/n1_outage").glob("P_f*_o60_s*/comms.jsonl"))[:10]
    sizes = payload_sizes(logs, spec["seed"])
    payloads = workdir / "payloads.json"
    payloads.write_text(json.dumps(sizes))
    # every level (the outage tests run on `good` whatever profiles are selected)
    profiles = {p: cfg.network.level_profile(p) for p in ("good", "medium", "low")}
    blocks = matrix(spec, reduced)
    robots = spec["reduced"]["robots"] if reduced else spec["robots"]
    jobs = []
    lab_count = int(spec.get("labs", 1))
    for n in robots:
        mine = [b for b in blocks if b["pattern"] == "P3" or n == robots[0]]
        for i in range(lab_count):
            part = mine[i::lab_count]
            if not part:
                continue
            wd = workdir / f"lab_n{n}_{i}"
            jobs.append(
                {
                    "workdir": str(wd),
                    "robots": n,
                    "seed": spec["seed"] + i,
                    "blocks": [{**b, "robots": n} for b in part],
                    "profiles": profiles,
                    "payloads": str(payloads.resolve()),
                    "results": str(wd / "results.jsonl"),
                }
            )
    codes = await asyncio.gather(*(_lab(j, Path(j["workdir"] + ".log")) for j in jobs))
    rows: list[dict[str, Any]] = []
    rtt: list[dict[str, Any]] = []
    for j in jobs:
        p = Path(j["results"])
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            r = json.loads(line)
            if "rtt_check" in r:
                rtt.append({**r, "robots": j["robots"]})
            elif "warning" not in r:
                r["robots"] = j["robots"]
                rows.append(r)
    return {
        "spec": "SPEC A4.8 (N5)",
        "network": "emulated radio links: rootless Linux namespaces + tc netem (rule 32)",
        "time": "real time (rule 33)",
        "tls": "TLS 1.3, server authentication (mutual TLS not available in the stacks)",
        "reduced": reduced,
        "profiles": profiles,
        "payload_sources": sizes.get("_sources"),
        "payload_counts": sizes.get("_counts"),
        "lab_exit_codes": codes,
        "rtt_checks": rtt,
        "rows": rows,
        "table": table(rows),
        "stamp": datetime.now(UTC).isoformat(),
    }


def table(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per (binding, mode, encoding, profile, pattern, nl, robots): across repeats."""
    cells: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if "error" in r:
            continue
        key = "|".join(
            str(r.get(k))
            for k in (
                "binding",
                "mode",
                "encoding",
                "profile",
                "pattern",
                "nl",
                "robots",
                "outage_s",
            )
        )
        cells.setdefault(key, []).append(r)
    out = {}
    for key, rs in sorted(cells.items()):

        def med(get: Any, rs: list[dict[str, Any]] = rs) -> float | None:
            vals = [v for v in (get(r) for r in rs) if v is not None]
            return round(statistics.median(vals), 2) if vals else None

        def spread(get: Any, rs: list[dict[str, Any]] = rs) -> float | None:
            vals = [v for v in (get(r) for r in rs) if v is not None]
            return round(max(vals) - min(vals), 2) if len(vals) > 1 else 0.0

        out[key] = {
            "repeats": len(rs),
            "wire_bytes_per_unit": med(lambda r: r["wire_bytes_per_unit"]),
            "wire_up_bytes_per_unit": med(lambda r: r["wire_up_bytes_per_unit"]),
            "wire_down_bytes_per_unit": med(lambda r: r["wire_down_bytes_per_unit"]),
            "latency_p50_ms": med(lambda r: r["latency_ms"]["p50"]),
            "latency_p95_ms": med(lambda r: r["latency_ms"]["p95"]),
            "latency_p99_ms": med(lambda r: r["latency_ms"]["p99"]),
            "latency_p50_spread_ms": spread(lambda r: r["latency_ms"]["p50"]),
            "deadline_misses": sum(r.get("deadline_misses", 0) for r in rs),
            "timeouts": sum(r.get("timeouts", 0) for r in rs),
            "units": sum(r.get("n", 0) for r in rs),
            "outage": rs[0].get("outage"),
            "pcap_check": next((r["pcap_check"] for r in rs if r.get("pcap_check")), None),
        }
    return out


COLUMNS = (
    "binding",
    "mode",
    "encoding",
    "profile",
    "pattern",
    "nl",
    "robots",
    "outage_s",
    "repeats",
    "units",
    "wire_bytes_per_unit",
    "wire_up_bytes_per_unit",
    "wire_down_bytes_per_unit",
    "latency_p50_ms",
    "latency_p95_ms",
    "latency_p99_ms",
    "latency_p50_spread_ms",
    "deadline_misses",
    "timeouts",
    "restore_to_first_delivery_s",
    "lost",
    "duplicated",
    "late",
)


def write(result: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"n5_protocols_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(result, indent=2, default=str))
    return path


def write_csv(result: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for key, c in result["table"].items():
            parts = dict(
                zip(
                    (
                        "binding",
                        "mode",
                        "encoding",
                        "profile",
                        "pattern",
                        "nl",
                        "robots",
                        "outage_s",
                    ),
                    key.split("|"),
                    strict=True,
                )
            )
            o = c.get("outage") or {}
            row = {
                **parts,
                **c,
                **{
                    k: o.get(k)
                    for k in ("restore_to_first_delivery_s", "lost", "duplicated", "late")
                },
            }
            w.writerow(["" if row.get(k) in (None, "None") else row.get(k) for k in COLUMNS])
    return path


def summary(result: dict[str, Any]) -> str:
    lines = [
        f"rtt checks: {[(r['rtt_check'], r['measured_ms'], r['ok']) for r in result['rtt_checks']]}"
    ]
    lines.append(
        "binding/mode/enc            profile    pattern nl   N  wire B/unit  p50 ms  p95 ms  miss"
    )
    for key, c in result["table"].items():
        b, m, e, prof, pat, nl, n, o = key.split("|")
        lines.append(
            f"{b + '/' + m + '/' + e:<27} {prof:<10} {pat:<7} {nl:<4} {n:>2} {c['wire_bytes_per_unit']!s:>11} "
            f"{c['latency_p50_ms']!s:>7} {c['latency_p95_ms']!s:>7} {c['deadline_misses']:>5}"
            + (f"  outage {o}s: {c['outage']}" if c.get("outage") else "")
        )
    errors = [r for r in result["rows"] if "error" in r]
    if errors:
        lines.append(f"{len(errors)} blocks failed, e.g. {errors[0]}")
    return "\n".join(lines)


def load_spec(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())
