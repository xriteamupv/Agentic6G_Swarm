"""UC2-KPI-15 bench (SPEC A3.8, stage E6): precision of three retrievals against simulator
ground truth, on seeded scenarios (``a6g bench retrieval``).

1. **Observations near a pose** (``kb_observations_near``): robots have reported
   observations of three kinds (hazards on the path, security, maintenance) in en/es/ca,
   at seeded places and times; a robot asks about one kind near where it stands. Relevant:
   the observations of that kind within the radius and not older than the retention.
   P@k = relevant among the k returned / k, and R-normalised P@k = relevant among the k
   returned / min(k, relevant that exist). Compared with distance-only ranking (no query).
2. **Statuses and notices relevant to a route** (``plan_route``): restrictive statuses and
   operator notices (some expired) at seeded subjects. Relevant to a route: those on the
   route, or on the shortest route it went around (the same profile, nothing blocked).
   Precision and recall of what the route reports (``statuses_applied``,
   ``notices_applied``).
3. **Interaction context at a hand-over** (InteractionStore, ``guidance.fetch``): many
   interactions at once; each is handed from robot B to robot C as the coordinator does.
   Precision@1: C retrieves its own interaction with every field B recorded, nothing from
   another one; C asking for an interaction it is not part of must be refused (leaks).
   Optionally the store loses a share of entries before the hand-over (a restart).

Deterministic and offline by default (hashing embedder).
"""

import json
import math
import random
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from a6g_common.clock import ManualClock
from a6g_common.config import A6GConfig
from a6g_common.schemas import StatusReport
from a6g_interactions import InteractionClient, InteractionStore
from a6g_interactions.store import COORDINATOR
from a6g_kb import KnowledgeService
from a6g_kb.index import HashingEmbedder
from a6g_kb.status import StatusLayer
from a6g_robot_agent import guidance

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
KS = (1, 3, 5)

#: Observation labels per kind, as robots word them (en, es, ca).
LABELS: dict[str, list[str]] = {
    "hazard": [
        "fallen tree blocking the path",
        "large puddle covering the walkway",
        "broken glass on the ground",
        "loose paving stones on the footpath",
        "rama caída bloqueando el camino",
        "charco grande en la acera",
        "branca caiguda al camí",
    ],
    "security": [
        "unattended backpack next to the bench",
        "door left open with nobody around",
        "bicycle with a cut lock",
        "mochila abandonada junto al banco",
        "puerta abierta sin vigilancia",
        "motxilla abandonada al costat del banc",
    ],
    "maintenance": [
        "litter bin overflowing",
        "streetlight switched off",
        "damaged bench seat",
        "papelera llena",
        "farola apagada",
        "paperera plena",
    ],
}
#: What a robot asks for each kind.
QUERIES: dict[str, list[str]] = {
    "hazard": [
        "anything blocking the way or dangerous on the path",
        "obstacles on the footpath",
        "¿hay algún obstáculo en el camino?",
    ],
    "security": [
        "suspicious objects or security issues",
        "unattended bags or open doors",
        "objetos abandonados o puertas abiertas",
    ],
    "maintenance": [
        "maintenance problems such as bins or lights",
        "broken street furniture",
        "papeleras o farolas",
    ],
}


def rate(hits: float, n: int) -> float | None:
    return round(hits / n, 4) if n else None


def service_for(cfg: A6GConfig, embedder: Any = None) -> KnowledgeService:
    return KnowledgeService(
        cfg.campus, cfg.knowledge, embedder=embedder or HashingEmbedder(), clock=ManualClock(T0)
    )


def path_point(rng: random.Random, cfg: A6GConfig) -> dict[str, float]:
    edges = [e for e in cfg.campus.route_graph.edges if e.source == "osm" and len(e.geometry) > 1]
    e = rng.choice(edges)
    i = rng.randrange(len(e.geometry) - 1)
    (ax, ay), (bx, by) = e.geometry[i], e.geometry[i + 1]
    t = rng.random()
    return {"x": round(ax + t * (bx - ax), 2), "y": round(ay + t * (by - ay), 2)}


# --------------------------------------------------------------------------- 1. observations


def observations(
    cfg: A6GConfig, seeds: int, embedder: Any = None, n_obs: int = 80, n_queries: int = 20
) -> dict[str, Any]:
    radius = cfg.knowledge.observation_radius_m
    ttl = cfg.knowledge.observation_ttl_min
    rows = []
    for seed in range(seeds):
        rng = random.Random(seed)
        service = service_for(cfg, embedder)
        truth: dict[str, dict[str, Any]] = {}
        for i in range(n_obs):
            kind = rng.choice(list(LABELS))
            pose = path_point(rng, cfg)
            age = rng.uniform(0, ttl * 1.25)  # some are past the retention
            oid = f"obs_{seed}_{i}"
            service.add_observation(
                {
                    "obs_id": oid,
                    "label": rng.choice(LABELS[kind]),
                    "pose": pose,
                    "stamp": (T0 - timedelta(minutes=age)).isoformat(),
                    "robot_id": "go2_01",
                },
                COORDINATOR,
            )
            truth[oid] = {"kind": kind, "pose": pose, "age": age}
        for _ in range(n_queries):
            kind = rng.choice(list(QUERIES))
            here = path_point(rng, cfg)
            relevant = {
                oid
                for oid, t in truth.items()
                if t["kind"] == kind
                and t["age"] <= ttl
                and math.hypot(t["pose"]["x"] - here["x"], t["pose"]["y"] - here["y"]) <= radius
            }
            if not relevant:
                continue  # nothing to find: precision is undefined
            query = rng.choice(QUERIES[kind])
            got = {
                arm: [o["obs_id"] for o in service.observations_near(here, q, radius, max(KS))]
                for arm, q in (("query", query), ("distance_only", ""))
            }
            rows.append(
                {
                    "kind": kind,
                    "query": query,
                    "relevant": len(relevant),
                    **{
                        f"{arm}_hits@{k}": sum(o in relevant for o in got[arm][:k])
                        for arm in got
                        for k in KS
                    },
                    **{f"{arm}_returned@{k}": len(got[arm][:k]) for arm in got for k in KS},
                }
            )

    def precision(arm: str, k: int, normalised: bool) -> float | None:
        vals = [r[f"{arm}_hits@{k}"] / (min(k, r["relevant"]) if normalised else k) for r in rows]
        return round(statistics.fmean(vals), 4) if vals else None

    return {
        "queries": len(rows),
        "observations_per_seed": n_obs,
        "radius_m": radius,
        "retention_min": ttl,
        "p_at_k": {
            arm: {str(k): precision(arm, k, False) for k in KS}
            for arm in ("query", "distance_only")
        },
        "r_normalised_p_at_k": {
            arm: {str(k): precision(arm, k, True) for k in KS} for arm in ("query", "distance_only")
        },
        "by_kind": {
            kind: {
                str(k): round(
                    statistics.fmean(
                        r[f"query_hits@{k}"] / min(k, r["relevant"])
                        for r in rows
                        if r["kind"] == kind
                    ),
                    4,
                )
                for k in KS
            }
            for kind in QUERIES
            if any(r["kind"] == kind for r in rows)
        },
    }


# --------------------------------------------------------------------------- 2. route context


def _touched(router: Any, route: Any) -> set[str]:
    out = {route.start_node, route.goal_node, *route.edges}
    for eid in route.edges:
        e = router._edge(eid)
        out |= {e.from_, e.to}
    return {x for x in out if x}


def _elements(router: Any, subject: str) -> set[str]:
    """What a status on ``subject`` closes (as the router applies it, SPEC A3.3)."""
    node = subject if subject in router._nodes else f"poi:{subject}"
    out = {subject, node}
    if node in router._nodes:
        c = router._nodes[node]
        out |= {
            n.id
            for n in router._nodes.values()
            if math.hypot(n.x - c.x, n.y - c.y) <= router.status_radius_m
        }
    out |= {e.id for e in router.graph.edges if e.exposed_poi == subject}
    return out


def route_context(cfg: A6GConfig, seeds: int) -> dict[str, Any]:
    graph = cfg.campus.route_graph
    subjects = sorted(
        {n.id for n in graph.nodes if not n.id.startswith(("poi:", "x:"))}
        | {e.id for e in graph.edges if e.source == "osm"}
        | {p.poi_id for p in cfg.campus.pois}
    )
    rows = []
    for seed in range(seeds):
        rng = random.Random(seed)
        service = service_for(cfg)
        router = service._router()
        service.statuses = StatusLayer(cfg.knowledge.status_ttl_min)
        start = path_point(rng, cfg)
        goal = rng.choice(cfg.campus.pois).poi_id
        status_subjects = rng.sample(subjects, k=rng.randint(1, 6))
        for i, s in enumerate(status_subjects):
            service.statuses.put(
                StatusReport(
                    status_id=f"st_{i}",
                    subject=s,
                    status=rng.choice(["obstructed", "closed"]),
                    source="robot:go2_01",
                    stamp=T0,
                ),
                T0,
            )
        notices = {}
        for i, s in enumerate(rng.sample(subjects, k=rng.randint(1, 6))):
            expired = rng.random() < 0.25
            doc = {
                "id": f"notice_{i}",
                "kind": "notice",
                "lang": "en",
                "text": f"Works at {s}",
                "applies_to": [s],
                "source": "operator",
                "valid_from": (T0 - timedelta(days=2)).isoformat(),
                "valid_until": (T0 + timedelta(days=-1 if expired else 1)).isoformat(),
            }
            service.add_document(doc)
            notices[doc["id"]] = (s, expired)
        route = router.plan(
            start,
            goal,
            "default",
            statuses=service.statuses,
            now=T0,
            notices=service._notices(),
        )
        if not route.ok:
            continue
        base = router.plan(start, goal, "default", statuses=StatusLayer(), now=T0)
        shaped = _touched(router, route) | (_touched(router, base) if base.ok else set())
        rel_status = {
            f"st_{i}" for i, s in enumerate(status_subjects) if _elements(router, s) & shaped
        }
        rel_notice = {  # a notice applies to the ids it names (no radius)
            d for d, (s, expired) in notices.items() if not expired and {s, f"poi:{s}"} & shaped
        }
        relevant = rel_status | rel_notice
        reported = set(route.statuses_applied) | set(route.notices_applied)
        # Baseline (the router before 2026-10-20): every status in force, and the notices
        # on the route or on anything blocked anywhere.
        blocked = set().union(*(_elements(router, s) for s in status_subjects))
        everything = {f"st_{i}" for i in range(len(status_subjects))} | {
            d
            for d, (s, expired) in notices.items()
            if not expired and {s, f"poi:{s}"} & (_touched(router, route) | blocked)
        }
        rows.append(
            {
                "seed": seed,
                "relevant": sorted(relevant),
                "reported": sorted(reported),
                "hits": len(relevant & reported),
                "all_in_force": sorted(everything),
                "all_hits": len(relevant & everything),
            }
        )

    def scores(reported: str, hits: str) -> dict[str, float | None]:
        rep = [r for r in rows if r[reported]]
        rel = [r for r in rows if r["relevant"]]
        return {
            "precision": round(statistics.fmean(r[hits] / len(r[reported]) for r in rep), 4)
            if rep
            else None,
            "recall": round(statistics.fmean(r[hits] / len(r["relevant"]) for r in rel), 4)
            if rel
            else None,
            "reported_mean": round(statistics.fmean(len(r[reported]) for r in rows), 2)
            if rows
            else None,
        }

    return {
        "routes": len(rows),
        "definition": "relevant: on the route, or on the shortest route with nothing "
        "blocked (what it went around); notices only on the ids they name",
        "relevant_mean": round(statistics.fmean(len(r["relevant"]) for r in rows), 2)
        if rows
        else None,
        "router": scores("reported", "hits"),
        "all_in_force": scores("all_in_force", "all_hits"),
    }


# --------------------------------------------------------------------------- 3. hand-over


FIELDS = ("objective_kind", "destination", "place_said", "declared_constraint", "profile", "lang")


async def handover(
    cfg: A6GConfig, seeds: int, n: int = 40, loss: float = 0.0, when: str = "during"
) -> dict[str, Any]:
    """``loss``: share of entries the store loses (a restart), ``during`` the escort (before
    robot B leaves) or ``after`` B left (before the coordinator hands over)."""
    robots = ["go2_01", "go2_02", "go2_03", "go2_04"]
    pois = [p.poi_id for p in cfg.campus.pois]
    exact = fields_ok = fields_all = lost = leaks = probes = 0
    for seed in range(seeds):
        rng = random.Random(seed)
        store = InteractionStore(60, ManualClock(T0))
        truth: dict[str, dict[str, Any]] = {}
        copies: dict[str, dict[str, Any]] = {}  # robot -> its copies (deps.local)
        for i in range(n):  # n people talking to robots at once
            b = rng.choice(robots)
            iid = f"int_{seed}_{i}"
            dest = rng.choice(pois)
            content = {
                "objective_kind": "escort",
                "destination": {"kind": "poi", "id": dest},
                "place_said": dest.removeprefix("poi_").replace("_", " "),
                "declared_constraint": rng.choice([None, "uses a wheelchair", "va amb crosses"]),
                "profile": rng.choice(["default", "step_free", "avoid_slopes"]),
                "lang": rng.choice(["en", "es", "ca"]),
                "meeting_pose": None,
            }
            view = store.create(iid, content, [b], b)
            copies.setdefault(b, {})[iid] = {**view, "stage": "escorting"}
            truth[iid] = {"from": b, "to": rng.choice([r for r in robots if r != b]), **content}
        lose = rng.sample(sorted(truth), k=round(loss * n))

        def deps_of(
            robot: str, local: dict | None = None, copies=copies, store=store
        ) -> SimpleNamespace:
            return SimpleNamespace(
                local=copies.setdefault(robot, {}) if local is None else local,
                robot_id=robot,
                interactions=InteractionClient(store, agent_id=robot),
            )

        if when == "during":
            for iid in lose:
                store.delete(iid, COORDINATOR)
        for iid in rng.sample(sorted(truth), k=n):  # B leaves (low battery), in any order
            t = truth[iid]
            t["meeting_pose"] = {
                "x": round(rng.uniform(-60, 60), 2),
                "y": round(rng.uniform(-60, 60), 2),
            }
            await guidance.release(deps_of(t["from"]), iid, meeting_pose=t["meeting_pose"])
        if when == "after":
            for iid in lose:
                store.delete(iid, COORDINATOR)
        for iid in rng.sample(sorted(truth), k=n):  # the coordinator hands over; C reads
            t = truth[iid]
            try:  # what the coordinator's _handover does
                current = store.get(iid, COORDINATOR)["content"]
                store.update(
                    iid,
                    COORDINATOR,
                    content={"handovers": int(current.get("handovers") or 0) + 1},
                    participants=[t["to"]],
                )
            except Exception:
                pass
            entry, _ = await guidance.fetch(deps_of(t["to"]), iid)
            if entry is None:
                lost += 1
                continue
            got = entry.get("content") or {}
            want = {f: t[f] for f in (*FIELDS, "meeting_pose")}
            ok = sum(got.get(f) == v for f, v in want.items())
            fields_ok += ok
            fields_all += len(want)
            exact += ok == len(want) and entry.get("interaction_id") == iid
            for other in rng.sample(sorted(set(truth) - {iid}), k=3):  # not its own
                if t["to"] in (truth[other]["to"], truth[other]["from"]):
                    continue  # it does take part in that one
                probes += 1
                peek, _ = await guidance.fetch(deps_of(t["to"], local={}), other)
                leaks += peek is not None
    total = seeds * n
    return {
        "handovers": total,
        "store_loss": loss,
        "loss_when": when if loss else None,
        "precision_at_1": rate(exact, total),
        "field_precision": rate(fields_ok, fields_all),
        "lost": rate(lost, total),
        "leaks": leaks,
        "probes": probes,
    }


# --------------------------------------------------------------------------- run


async def run(
    cfg: A6GConfig, seeds: int = 50, embedder: Any = None, label: str = "fake"
) -> dict[str, Any]:
    emb = embedder or HashingEmbedder()
    return {
        "kpi": "UC2-KPI-15",
        "definition": "precision of three retrievals against simulator ground truth: "
        "observations near a pose (P@k), statuses and notices relevant to a route "
        "(precision, recall), interaction context at a hand-over (precision@1, leaks)",
        "model": label,
        "embedder": type(emb).__name__,
        "fidelity": "offline, deterministic" if isinstance(emb, HashingEmbedder) else "functional",
        "seeds": seeds,
        "observations": observations(cfg, seeds, emb),
        "route_context": route_context(cfg, seeds * 10),
        "handover": {
            "store_kept": await handover(cfg, seeds),
            "store_lost_10pct_during_escort": await handover(cfg, seeds, loss=0.1),
            "store_lost_10pct_after_leaving": await handover(cfg, seeds, loss=0.1, when="after"),
        },
        "stamp": datetime.now(UTC).isoformat(),
    }


def write(result: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"e6_kpi15_{result['model']}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(result, indent=2))
    return path


def summary(result: dict[str, Any]) -> str:
    o, r, h = result["observations"], result["route_context"], result["handover"]
    q, d = o["r_normalised_p_at_k"]["query"], o["r_normalised_p_at_k"]["distance_only"]
    lines = [
        f"UC2-KPI-15 ({result['model']}, embedder {result['embedder']}, {result['fidelity']})",
        f"  observations near a pose ({o['queries']} queries): R-normalised P@1/3/5 "
        f"{q['1']}/{q['3']}/{q['5']} (distance only {d['1']}/{d['3']}/{d['5']})",
        f"  statuses and notices for a route ({r['routes']} routes, {r['relevant_mean']} "
        f"relevant each): precision {r['router']['precision']}, recall {r['router']['recall']} "
        f"(every status in force: {r['all_in_force']['precision']} / "
        f"{r['all_in_force']['recall']})",
    ]
    for name, x in h.items():
        lines.append(
            f"  hand-over context, {name} ({x['handovers']}): precision@1 {x['precision_at_1']}, "
            f"fields {x['field_precision']}, lost {x['lost']}, leaks {x['leaks']}/{x['probes']}"
        )
    return "\n".join(lines)
