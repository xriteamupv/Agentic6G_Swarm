"""UC2-KPI-10 / KVI-07 bench (SPEC A3.8, stage E6): what the campus knowledge server adds,
by ablation (``--kb off``), on paired items (the same phrases, orders and seeded trips).

Three measures, each with the server (``on``) and without it (``off``):

* **Place resolution** (``bench/kb/places.yaml``): the place a destination phrase means.
  Off, two fallbacks exist and both are measured: the coordinator's word overlap with the
  place names (its closest name when none matches) and the robot's alias match.
* **Route compliance** (seeded trips): a person with a routing profile goes from a point
  on the footpaths to a POI while restrictive statuses (obstructed, closed) are in force,
  and, during a weather alert, the exposed POIs are at ``elevated`` risk. On: the server's
  route (profile, statuses and risk applied). Off: the shortest footpath route, what a
  robot drives without the server. Both are checked by the same code (``violations``).
  Run on the campus as mapped and, labelled, with the synthetic steps of scenario G11
  (the campus has no mapped steps yet, TODO(UPV)).
* **Decomposition** (``bench/kb/goals.yaml``): the tasks an order decomposes into, order
  ignored. With fake models the rule-based planner decomposes; ``--model`` uses the
  coordinator model.

Deterministic and offline by default (hashing embedder). KPI targets are orientative
(decision of 2026-10-16): the measured values are reported as they are.
"""

import json
import random
import statistics
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml
from a6g_common.clock import ManualClock
from a6g_common.config import A6GConfig, CampusConfig
from a6g_common.routegraph import GraphEdge
from a6g_common.schemas import Posture, StatusReport
from a6g_coordinator.decompose import _target_key, decompose_goal, overlap_place
from a6g_kb import KnowledgeService
from a6g_kb.index import HashingEmbedder
from a6g_kb.status import StatusLayer
from a6g_robot_agent import guidance

T0 = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
PROFILES = (("step_free", 0.5), ("default", 0.3), ("avoid_slopes", 0.2))
BENCH = Path(__file__).resolve().parents[4] / "bench" / "kb"


def load(name: str, directory: Path = BENCH) -> list[dict[str, Any]]:
    return yaml.safe_load((directory / name).read_text())


def rate(hits: int, n: int) -> float | None:
    return round(hits / n, 4) if n else None


def gain(on: float | None, off: float | None) -> float | None:
    """Relative improvement given by the server, (on - off) / off."""
    if on is None or off is None or off == 0:
        return None
    return round((on - off) / off, 4)


def service_for(campus: CampusConfig, cfg: A6GConfig, embedder: Any = None) -> KnowledgeService:
    return KnowledgeService(
        campus, cfg.knowledge, embedder=embedder or HashingEmbedder(), clock=ManualClock(T0)
    )


# --------------------------------------------------------------------------- places


async def places(cfg: A6GConfig, service: KnowledgeService, items: list[dict]) -> dict[str, Any]:
    campus = cfg.campus
    robot_deps = SimpleNamespace(kb=None, campus=campus)
    rows = []
    for item in items:
        found = service.resolve_place(item["text"])
        kb = found[0]["id"] if found else None
        overlap = overlap_place(item["text"], campus)
        local = await guidance.resolve(robot_deps, item["text"], None)
        rows.append(
            {
                **item,
                "kb": kb,
                "overlap": overlap[1] if overlap else None,
                "robot_local": local["id"] if local else None,
            }
        )
    arms = ("kb", "overlap", "robot_local")

    def accuracy(subset: list[dict]) -> dict[str, float | None]:
        return {a: rate(sum(r[a] == r["expect"] for r in subset), len(subset)) for a in arms}

    groups = sorted({r["group"] for r in rows})
    total = accuracy(rows)
    return {
        "items": len(rows),
        "accuracy": total,
        "gain_vs_overlap": gain(total["kb"], total["overlap"]),
        "gain_vs_robot_local": gain(total["kb"], total["robot_local"]),
        "by_group": {g: accuracy([r for r in rows if r["group"] == g]) for g in groups},
        "by_lang": {
            lang: accuracy([r for r in rows if r["lang"] == lang])
            for lang in sorted({r["lang"] for r in rows})
        },
        "errors": [r for r in rows if r["kb"] != r["expect"]],
        "rows": rows,
    }


# --------------------------------------------------------------------------- routes


def with_steps(campus: CampusConfig, goal: str = "poi:poi_shelter_9b") -> CampusConfig:
    """Scenario G11's synthetic steps: a stairs shortcut on every way into ``goal``, half
    the length of the step-free way (the campus has no mapped steps, TODO(UPV))."""
    graph = campus.route_graph
    nodes = {n.id: n for n in graph.nodes}
    hubs = {goal} | {
        (e.to if e.from_ == goal else e.from_)
        for e in graph.edges
        if goal in (e.from_, e.to) and e.source == "connector"
    }
    extra = []
    for e in graph.edges:
        hub = next((h for h in hubs if h in (e.from_, e.to)), None)
        if hub is not None and e.source != "connector":
            other = e.to if e.from_ == hub else e.from_
            a, b = nodes[other], nodes[goal]
            extra.append(
                GraphEdge.model_validate(
                    {
                        "id": f"0steps-{other}",
                        "from": other,
                        "to": goal,
                        "length_m": round(e.length_m * 0.5, 2),
                        "stairs": True,
                        "step_free": False,
                        "source": "manual",
                        "geometry": [(a.x, a.y), (b.x, b.y)],
                    }
                )
            )
    return campus.model_copy(
        update={"route_graph": graph.model_copy(update={"edges": [*graph.edges, *extra]})}
    )


def trip(rng: random.Random, campus: CampusConfig) -> dict[str, Any]:
    """A seeded trip: start on the footpaths, destination, profile, statuses, alert."""
    graph = campus.route_graph
    paths = [e for e in graph.edges if e.source == "osm" and len(e.geometry) >= 2]
    edge = rng.choice(paths)
    i = rng.randrange(len(edge.geometry) - 1)
    (ax, ay), (bx, by) = edge.geometry[i], edge.geometry[i + 1]
    t = rng.random()
    start = {"x": round(ax + t * (bx - ax), 3), "y": round(ay + t * (by - ay), 3)}
    alert = rng.random() < 0.5
    # In an alert people are taken to a shelter (the escort concludes there, SPEC A3.4).
    goals = [p for p in campus.pois if "shelter" in p.tags] if alert else list(campus.pois)
    goal = rng.choice(goals).poi_id
    names, weights = zip(*PROFILES, strict=True)
    profile = rng.choices(names, weights)[0]
    keep = {f"poi:{goal}", edge.id, edge.from_, edge.to}  # the trip itself stays possible
    keep |= {e.id for e in graph.edges if f"poi:{goal}" in (e.from_, e.to)}
    nodes = {n.id for n in graph.nodes if not n.id.startswith(("poi:", "x:"))}
    subjects = sorted((nodes | {e.id for e in paths}) - keep)
    statuses = [
        {"subject": s, "status": rng.choice(["obstructed", "closed"])}
        for s in rng.sample(subjects, k=rng.choice([0, 1, 2, 3]))
    ]
    return {"start": start, "goal": goal, "profile": profile, "statuses": statuses, "alert": alert}


def judge(service: KnowledgeService, item: dict[str, Any]) -> dict[str, Any]:
    router = service._router()
    service.statuses = StatusLayer(service.cfg.status_ttl_min)
    for i, s in enumerate(item["statuses"]):
        service.statuses.put(
            StatusReport(
                status_id=f"st_{i}",
                subject=s["subject"],
                status=s["status"],
                source="robot:go2_01",
                stamp=T0,
            ),
            T0,
        )
    exposed = [p.poi_id for p in service.campus.pois if p.exposed]
    service.risk = {p: "elevated" for p in exposed} if item["alert"] else {}
    on = router.plan(
        item["start"],
        item["goal"],
        item["profile"],
        statuses=service.statuses,
        now=T0,
        risk=service.risk,
    )
    off = router.plan(item["start"], item["goal"], "default", statuses=StatusLayer(), now=T0)

    def why(route: Any) -> list[str] | None:
        if not route.ok:
            return None
        return router.violations(
            route.edges,
            item["profile"],
            service.statuses,
            T0,
            start=route.start_node,
            goal=route.goal_node,
            risk=service.risk,
        )

    return {
        **item,
        "on_ok": on.ok,
        "on_violations": why(on),
        "on_length_m": round(on.length_m, 1) if on.ok else None,
        "off_ok": off.ok,
        "off_violations": why(off),
        "off_length_m": round(off.length_m, 1) if off.ok else None,
    }


def routes(cfg: A6GConfig, seeds: int, steps: bool) -> dict[str, Any]:
    campus = with_steps(cfg.campus) if steps else cfg.campus
    service = service_for(campus, cfg)
    rows = [judge(service, trip(random.Random(seed), campus)) for seed in range(seeds)]
    feasible = [r for r in rows if r["on_ok"]]  # a compliant route exists
    on = sum(r["on_violations"] == [] for r in feasible)
    off = sum(r["off_ok"] and r["off_violations"] == [] for r in feasible)
    differ = [r for r in feasible if r["off_violations"]]
    reasons = Counter(v.split(":")[0].split(" ")[0] for r in differ for v in r["off_violations"])
    detour = [
        (r["on_length_m"] - r["off_length_m"]) / r["off_length_m"]
        for r in differ
        if r["off_length_m"]
    ]
    return {
        "campus": "with G11 synthetic steps" if steps else "as mapped",
        "trips": len(rows),
        "no_compliant_route": len(rows) - len(feasible),
        "compliant_rate": {"kb": rate(on, len(feasible)), "off": rate(off, len(feasible))},
        "gain": gain(rate(on, len(feasible)), rate(off, len(feasible))),
        "trips_the_server_changed": len(differ),
        "off_violation_kinds": dict(reasons.most_common()),
        "detour_median_pct": round(100 * statistics.median(detour), 1) if detour else None,
        "rows": rows,
    }


# --------------------------------------------------------------------------- decompose


class _KB:
    """The service as the coordinator's client sees it (``resolve_place`` is async)."""

    def __init__(self, service: KnowledgeService) -> None:
        self.service = service

    async def resolve_place(self, phrase: str, mission_id: str | None = None) -> list[dict]:
        return self.service.resolve_place(phrase)


async def decomposition(
    cfg: A6GConfig, service: KnowledgeService, goals: list[dict], model: Any = None
) -> dict[str, Any]:
    rows = []
    for g in goals:
        out: dict[str, Any] = {**g}
        for arm, kb in (("kb", _KB(service)), ("off", None)):
            tasks, *_ = await decompose_goal(
                g["text"], Posture(g["posture"]), cfg.campus, model, kb=kb
            )
            got = sorted(
                [t.kind.value, (_target_key(t.target, cfg.campus) or ("", ""))[1]] for t in tasks
            )
            out[arm] = got
            out[f"{arm}_exact"] = got == sorted(g["expect"])
        rows.append(out)
    acc = {a: rate(sum(r[f"{a}_exact"] for r in rows), len(rows)) for a in ("kb", "off")}
    return {
        "goals": len(rows),
        "planner": "model" if model is not None else "rule-based (fake models)",
        "accuracy": acc,
        "gain": gain(acc["kb"], acc["off"]),
        "errors": [r for r in rows if not r["kb_exact"]],
        "rows": rows,
    }


# --------------------------------------------------------------------------- run


async def run(
    cfg: A6GConfig,
    seeds: int = 500,
    model: Any = None,
    embedder: Any = None,
    label: str = "fake",
    directory: Path = BENCH,
) -> dict[str, Any]:
    service = service_for(cfg.campus, cfg, embedder)
    place = await places(cfg, service, load("places.yaml", directory))
    mapped = routes(cfg, seeds, steps=False)
    steps = routes(cfg, seeds, steps=True)
    decomp = await decomposition(cfg, service, load("goals.yaml", directory), model)
    return {
        "kpi": "UC2-KPI-10, KVI-07",
        "definition": "ablation --kb off: place-resolution accuracy, route_compliant rate and "
        "decomposition accuracy, with the relative improvement (on - off) / off",
        "model": label,
        "embedder": type(service.embedder).__name__ if service.embedder else "none",
        # Rule 25: cloud models (the planner or the embedder) make the results functional.
        "fidelity": "functional"
        if model is not None or not isinstance(service.embedder, HashingEmbedder | None)
        else "offline, deterministic",
        "seeds": seeds,
        "places": place,
        "routes": {"as_mapped": mapped, "with_steps": steps},
        "decomposition": decomp,
        "stamp": datetime.now(UTC).isoformat(),
    }


def write(result: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"e6_kpi10_{result['model']}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(result, indent=2, default=str))
    return path


def summary(result: dict[str, Any]) -> str:
    p, d = result["places"], result["decomposition"]
    lines = [
        f"UC2-KPI-10 ({result['model']}, embedder {result['embedder']}, {result['fidelity']})",
        f"  places ({p['items']}): kb {p['accuracy']['kb']}, word overlap "
        f"{p['accuracy']['overlap']}, robot aliases {p['accuracy']['robot_local']} "
        f"(gain {p['gain_vs_overlap']} / {p['gain_vs_robot_local']})",
    ]
    for name, r in result["routes"].items():
        lines.append(
            f"  routes {name} ({r['trips']} trips, {r['no_compliant_route']} without a "
            f"compliant route): compliant kb {r['compliant_rate']['kb']}, off "
            f"{r['compliant_rate']['off']} (gain {r['gain']}; {r['trips_the_server_changed']} "
            f"trips changed, median detour {r['detour_median_pct']} %)"
        )
    lines.append(
        f"  decomposition ({d['goals']}, {d['planner']}): kb {d['accuracy']['kb']}, off "
        f"{d['accuracy']['off']} (gain {d['gain']})"
    )
    return "\n".join(lines)
