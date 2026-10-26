"""Unseen orders: ``rules_first`` against ``model_first`` understanding (2026-10-22).

Each order of ``bench/orders/unseen.yaml`` runs on a fresh 2D-simulator stack (time
paused, so the robots stand still and the result is who-does-what, not where they got to),
after an optional setup mission planned by the rules without a model. The order goes
through the whole coordinator (intent, plan, validation, allocation and, model-first, the
assignment check) with the given model; the open tasks after allocation are checked against
the case's expectations, and the wall time of the order is its latency.
"""

import json
import statistics
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from a6g_common.config import A6GConfig

from a6g_devstack.stack import SimStack

CASES = Path(__file__).resolve().parents[4] / "bench" / "orders" / "unseen.yaml"
SETUPS = {
    "patrol": "Patrol the campus",
    "guard_patrol": "Send one robot to guard the east gate, the others to patrol",
}
OPEN = ("pending", "assigned", "in_progress")


def with_understanding(cfg: A6GConfig, mode: str) -> A6GConfig:
    d = cfg.fleet.defaults
    coord = d.coordinator.model_copy(update={"understanding": mode})
    defaults = d.model_copy(update={"coordinator": coord})
    return cfg.model_copy(update={"fleet": cfg.fleet.model_copy(update={"defaults": defaults})})


#: place display name -> place id (place targets carry the name), set by ``run``
PLACES: dict[str, str] = {}


def _target(spec: dict[str, Any]) -> str:
    t = spec["target"]
    name = t.get("name")
    return t.get("sector_id") or t.get("poi_id") or PLACES.get(name or "", name) or t["type"]


def _key(entry: dict[str, Any]) -> tuple[str, str]:
    return entry["spec"]["kind"], _target(entry["spec"])


def check(case: dict[str, Any], tasks: dict[str, Any], guard: str | None) -> list[str]:
    """The expectations that do not hold (empty: the order was understood)."""
    live = [t for t in tasks.values() if t["status"] in OPEN]
    failed = []

    def match(kind: str, target: str) -> list[dict[str, Any]]:
        return [
            t for t in live if t["spec"]["kind"] == kind and target in ("*", _target(t["spec"]))
        ]

    def robots(kind: str, target: str) -> set[str | None]:
        return {t.get("robot_id") or t.get("pinned_to") for t in match(kind, target)}

    for e in case["expect"]:
        if "total" in e:
            ok = len(live) == e["total"]
        elif "kinds_absent" in e:
            ok = not any(t["spec"]["kind"] in e["kinds_absent"] for t in live)
        elif "guard_patrols" in e:
            ok = guard is not None and guard in robots("survey_sector", "*")
        elif "same_robot" in e or "other_robot" in e:
            (k1, t1), (k2, t2) = e.get("same_robot") or e["other_robot"]
            a, b = robots(k1, t1), robots(k2, t2)
            single = len(a) == 1 and len(b) == 1 and None not in a | b
            ok = single and (a == b if "same_robot" in e else a != b)
        else:
            found = match(e["kind"], e["target"])
            ok = True
            if "count" in e:
                ok &= len(found) == e["count"]
            if "robot" in e:
                ok &= e["robot"] in robots(e["kind"], e["target"])
            if "robots_in" in e:
                ok &= bool(found) and robots(e["kind"], e["target"]) <= set(e["robots_in"])
            if "not_robots" in e:
                ok &= not robots(e["kind"], e["target"]) & set(e["not_robots"])
            if e.get("distinct"):
                rs = [t.get("robot_id") for t in found]
                ok &= None not in rs and len(set(rs)) == len(rs)
        if not ok:
            failed.append(json.dumps(e))
    return failed


async def run_case(cfg: A6GConfig, case: dict[str, Any], model: Any) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"orders-{case['id']}-") as tmp:
        stack = SimStack(cfg, Path(tmp), model=None)
        await stack.start()
        try:
            stack.world.pause()
            guard = None
            if case.get("setup"):
                state = await stack.coordinator.chat("m1", SETUPS[case["setup"]])
                guard = next(
                    (
                        t.get("robot_id")
                        for t in state["tasks"].values()
                        if t["spec"]["kind"] == "hold_post"
                    ),
                    None,
                )
            stack.coordinator.deps.model = model
            started = time.perf_counter()
            state = await stack.coordinator.chat("m1", case["text"])
            latency = time.perf_counter() - started  # the robots have their envelopes
            await stack.coordinator.drain(timeout=180.0)
            settled = time.perf_counter() - started  # and the assignment check is done
            state = await stack.coordinator.state("m1")
        finally:
            await stack.stop()
    failed = check(case, state["tasks"], guard)
    reply = next(
        (m.text for m in reversed(state.get("messages") or []) if getattr(m, "text", "")), ""
    )
    return {
        "id": case["id"],
        "ok": not failed,
        "failed": failed,
        "latency_s": round(latency, 2),
        "settled_s": round(settled, 2),
        "open": sorted(
            f"{t['spec']['kind']} {_target(t['spec'])} -> {t.get('robot_id') or t.get('pinned_to')}"
            for t in state["tasks"].values()
            if t["status"] in OPEN
        ),
        "reply": reply[:400],
    }


def _p(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 2)


async def run(
    cfg: A6GConfig, model: Any, label: str, modes: tuple[str, ...] = ("rules_first", "model_first")
) -> dict[str, Any]:
    cases = yaml.safe_load(CASES.read_text())
    PLACES.update({name: pid for pid, (name, _) in cfg.campus.places().items()})
    out: dict[str, Any] = {"model": label, "cases": len(cases), "modes": {}}
    for mode in modes:
        rows = []
        for case in cases:
            try:
                rows.append(await run_case(with_understanding(cfg, mode), case, model))
            except Exception as exc:  # a failed run is reported, not hidden
                rows.append({"id": case["id"], "ok": False, "error": repr(exc), "latency_s": None})
        lat = [r["latency_s"] for r in rows if r.get("latency_s") is not None]
        settled = [r["settled_s"] for r in rows if r.get("settled_s") is not None]
        out["modes"][mode] = {
            "understood": round(sum(r["ok"] for r in rows) / len(rows), 3),
            "latency_p50_s": _p(lat, 0.5),
            "latency_p95_s": _p(lat, 0.95),
            "latency_mean_s": round(statistics.fmean(lat), 2) if lat else None,
            "settled_p50_s": _p(settled, 0.5),
            "settled_p95_s": _p(settled, 0.95),
            "rows": rows,
        }
    out["stamp"] = datetime.now(UTC).isoformat()
    return out


def write(result: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"orders_unseen_{result['model']}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(result, indent=2, default=str))
    return path


def summary(result: dict[str, Any]) -> str:
    lines = [f"Unseen orders ({result['cases']}, model {result['model']})"]
    for mode, m in result["modes"].items():
        lines.append(
            f"  {mode:<12} understood {m['understood']:.0%}  envelopes sent p50 "
            f"{m['latency_p50_s']} s p95 {m['latency_p95_s']} s; settled p50 "
            f"{m['settled_p50_s']} s p95 {m['settled_p95_s']} s"
        )
    ids = [r["id"] for r in next(iter(result["modes"].values()))["rows"]]
    for i, cid in enumerate(ids):
        marks = "  ".join(
            ("ok  " if m["rows"][i]["ok"] else "MISS") for m in result["modes"].values()
        )
        lines.append(f"    {marks}  {cid}")
    return "\n".join(lines)
