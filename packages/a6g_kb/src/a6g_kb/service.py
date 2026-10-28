"""The knowledge service: one in-memory twin of the campus knowledge base.

Static content comes from the campus file: documents, POI names and aliases, the
outdoor route graph and the people routing profiles. The status layer is live. The MCP
server (:mod:`a6g_kb.server`) and in-process callers use this class directly.

ACL stand-in: ``put_status`` accepts only the coordinator and the operator (a deployment
makes identities real with the agent cards and mTLS).
"""

import contextlib
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from a6g_common.clock import Clock, SystemClock
from a6g_common.config import CampusConfig, KnowledgeConfig
from a6g_common.geometry import point_in_polygon
from a6g_common.routegraph import DEFAULT_PROFILES, RoutingProfile
from a6g_common.schemas import StatusReport

from a6g_kb.index import Embedder, Entry, HybridIndex, fold
from a6g_kb.places import PlaceResolver, campus_places
from a6g_kb.routing import Router
from a6g_kb.status import StatusLayer

STATUS_WRITERS = frozenset({"coordinator", "operator"})


class KnowledgeUnavailable(Exception):
    """The campus has no knowledge base (v2 file) or the server could not answer."""


class KnowledgeService:
    def __init__(
        self,
        campus: CampusConfig,
        cfg: KnowledgeConfig | None = None,
        *,
        embedder: Embedder | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.campus = campus
        self.cfg = cfg or KnowledgeConfig()
        self.clock = clock or SystemClock()
        self.embedder = embedder if self.cfg.use_embeddings else None
        self.statuses = StatusLayer(self.cfg.status_ttl_min)
        self.places = PlaceResolver(campus_places(campus), self.embedder)
        self._documents: list[dict[str, Any]] = [d.model_dump() for d in campus.documents]
        self._reindex()
        profiles = {**DEFAULT_PROFILES, **campus.people_routing_profiles}
        self.profiles: dict[str, RoutingProfile] = profiles
        self.router = (
            Router(
                campus.route_graph,
                profiles,
                obstacles=campus.obstacles,
                status_radius_m=self.cfg.status_match_radius_m,
            )
            if campus.route_graph
            else None
        )
        self.risk: dict[str, str] = {}  # poi_id -> flood risk (the flood twin)
        #: What the robots observed (label, pose, time), for "what was seen near here";
        #: its index is built when first searched after a change.
        self._observations: dict[str, dict[str, Any]] = {}
        self._obs_index: HybridIndex | None = None
        #: Called with each accepted status (the stacks publish it on ``a6g/kb/status`` so
        #: escorting robots replan at once).
        self.on_status: list[Callable[[StatusReport], Any]] = []

    # ------------------------------------------------------------------ index

    def _reindex(self) -> None:
        entries = [
            Entry(
                d["id"],
                d["text"],
                {
                    "type": "document",
                    "kind": d["kind"],
                    "lang": d["lang"],
                    "applies_to": d["applies_to"],
                    "valid_from": d.get("valid_from"),
                    "valid_until": d.get("valid_until"),
                    "source": d["source"],
                },
            )
            for d in self._documents
        ]
        for p in self.campus.pois:
            text = ". ".join(
                [p.name, *p.aliases] + ([f"building {p.building}"] if p.building else [])
            )
            entries.append(
                Entry(
                    f"poi:{p.poi_id}",
                    text,
                    {
                        "type": "description",
                        "kind": "description",
                        "lang": "multi",
                        "applies_to": [p.poi_id],
                        "building": p.building,
                        "source": "campus",
                    },
                )
            )
        self.index = HybridIndex(entries, self.embedder)

    def add_document(self, doc: dict[str, Any]) -> None:
        """An operator notice becomes a document (``a6g kb notice add``)."""
        self._documents = [d for d in self._documents if d["id"] != doc["id"]] + [doc]
        self._reindex()

    def now(self) -> datetime:
        return self.clock.now()

    # ------------------------------------------------------------------ tools

    def search(
        self, query: str, k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        k = max(1, min(10, k))
        filters = filters or {}
        valid_at = filters.get("valid_at")
        out = []
        for entry, score in self.index.search(query, k=len(self.index.entries)):
            meta = entry.meta
            if "kind" in filters and meta["kind"] != filters["kind"]:
                continue
            if "lang" in filters and meta["lang"] not in (filters["lang"], "multi"):
                continue
            if "building" in filters and meta.get("building") != filters["building"]:
                continue
            if valid_at and not _valid(meta, valid_at):
                continue
            out.append(
                {
                    "doc_id": entry.id,
                    "text": entry.text,
                    "applies_to": meta["applies_to"],
                    "valid_from": meta.get("valid_from"),
                    "valid_until": meta.get("valid_until"),
                    "source": meta["source"],
                    "score": score,
                }
            )
            if len(out) == k:
                break
        return out

    def resolve_place(
        self, text: str, near: dict[str, float] | None = None
    ) -> list[dict[str, Any]]:
        found = self.places.resolve(text, self.cfg.resolve_min_score)
        return [
            {
                "kind": c.kind,
                "id": c.id,
                "name": c.name,
                "score": c.score,
                "matched": c.matched,
            }
            for c in found
        ]

    def status(
        self, subjects: list[str] | None = None, area: list[list[float]] | None = None
    ) -> dict[str, dict[str, Any]]:
        now = self.now()
        ids = list(subjects or [])
        if area is not None and self.campus.route_graph is not None:
            poly = [tuple(p) for p in area]
            ids += [
                n.id for n in self.campus.route_graph.nodes if point_in_polygon((n.x, n.y), poly)
            ]
            ids += [
                p.poi_id for p in self.campus.pois if point_in_polygon((p.pose.x, p.pose.y), poly)
            ]
        out = {}
        for sid in dict.fromkeys(ids):
            status, reports = self.statuses.effective(sid, now)
            out[sid] = {
                "status": status,
                "restrictive": bool(reports),
                "sources": [r.source for r in reports],
                "status_ids": [r.status_id for r in reports],
            }
        return out

    def put_status(self, report: StatusReport, caller: str) -> dict[str, Any]:
        if caller not in STATUS_WRITERS:
            return {"accepted": False, "reason": f"{caller} may not write statuses (ACL)"}
        known = set(self.campus.route_graph.ids()) if self.campus.route_graph else set()
        known |= {p.poi_id for p in self.campus.pois}
        if report.subject not in known:
            return {"accepted": False, "reason": f"unknown subject {report.subject!r}"}
        accepted, reason = self.statuses.put(report, self.now())
        if accepted:
            self._notify(report)
        return {"accepted": accepted, "reason": reason}

    def _notify(self, report: StatusReport) -> None:
        for callback in self.on_status:
            with contextlib.suppress(Exception):  # a listener never blocks the status layer
                callback(report)

    def add_notice(self, doc: dict[str, Any], status: str | None, caller: str) -> dict[str, Any]:
        """An operator notice: a document, and a status for each subject it
        names when it carries one (source ``notice:<doc_id>``)."""
        if caller not in STATUS_WRITERS:
            return {"accepted": False, "reason": f"{caller} may not add notices (ACL)"}
        doc = {
            "kind": "notice",
            "lang": "en",
            "applies_to": [],
            "valid_from": None,
            "valid_until": None,
            "source": caller,
            **doc,
        }
        known = set(self.campus.route_graph.ids()) if self.campus.route_graph else set()
        known |= {p.poi_id for p in self.campus.pois} | {s.sector_id for s in self.campus.sectors}
        unknown = sorted(set(doc["applies_to"]) - known)
        if unknown:
            return {"accepted": False, "reason": f"unknown ids {unknown}"}
        self.add_document(doc)
        results = {}
        if status:
            for subject in doc["applies_to"]:
                report = StatusReport(
                    status_id=f"st_{doc['id']}_{subject}"[:120],
                    subject=subject,
                    status=status,  # type: ignore[arg-type]
                    source=f"notice:{doc['id']}",
                    evidence=doc["id"],
                    stamp=self.now(),
                    valid_until=(
                        datetime.fromisoformat(doc["valid_until"]) if doc["valid_until"] else None
                    ),
                )
                accepted, results[subject] = self.statuses.put(report, self.now())
                if accepted:
                    self._notify(report)
        return {"accepted": True, "document": doc["id"], "statuses": results}

    # ------------------------------------------------------------------ observations

    def add_observation(self, obs: dict[str, Any], caller: str) -> dict[str, Any]:
        """Keep an observation for retrieval near a pose (coordinator and operator only).
        One with a person in view is not kept: its label may describe the person."""
        if caller not in STATUS_WRITERS:
            return {"accepted": False, "reason": f"{caller} may not add observations (ACL)"}
        if obs.get("person_present"):
            return {"accepted": False, "reason": "a person is in view: not kept (rule 28)"}
        pose = obs.get("pose") or {}
        stamp = obs.get("stamp") or self.now().isoformat()
        self._observations[obs["obs_id"]] = {
            "obs_id": obs["obs_id"],
            "label": str(obs.get("label") or ""),
            "x": float(pose["x"]),
            "y": float(pose["y"]),
            "stamp": stamp if isinstance(stamp, str) else stamp.isoformat(),
            "subject": obs.get("subject"),
            "robot_id": obs.get("robot_id"),
        }
        self._obs_index = None
        return {"accepted": True, "obs_id": obs["obs_id"]}

    def _forget_old_observations(self) -> None:
        cutoff = self.now() - timedelta(minutes=self.cfg.observation_ttl_min)
        old = [i for i, o in self._observations.items() if _when(o["stamp"]) < cutoff]
        for i in old:
            del self._observations[i]
        if old:
            self._obs_index = None

    def observations_near(
        self,
        pose: dict[str, float],
        query: str = "",
        radius_m: float | None = None,
        k: int = 5,
    ) -> list[dict[str, Any]]:
        """Observations within ``radius_m`` of ``pose``: with a ``query``, the most relevant
        to it first (hybrid lexical + embeddings), then the closest; without, the closest
        first, then the most recent."""
        self._forget_old_observations()
        radius = self.cfg.observation_radius_m if radius_m is None else radius_m
        x, y = float(pose["x"]), float(pose["y"])
        near = {
            i: math.hypot(o["x"] - x, o["y"] - y)
            for i, o in self._observations.items()
            if math.hypot(o["x"] - x, o["y"] - y) <= radius
        }
        if not near:
            return []
        if query.strip():
            if self._obs_index is None:
                entries = [Entry(i, o["label"], {}) for i, o in self._observations.items()]
                self._obs_index = HybridIndex(entries, self.embedder)
            ranked = self._obs_index.search(query, k=len(self._obs_index.entries))
            score = {e.id: s for e, s in ranked}
            order = sorted(near, key=lambda i: (-score.get(i, 0.0), near[i], i))
        else:
            order = sorted(
                near, key=lambda i: (near[i], -_when(self._observations[i]["stamp"]).timestamp(), i)
            )
        return [
            {
                **self._observations[i],
                "distance_m": round(near[i], 1),
                **({"score": round(score.get(i, 0.0), 4)} if query.strip() else {}),
            }
            for i in order[: max(1, min(10, k))]
        ]

    def _router(self) -> Router:
        if self.router is None:
            raise KnowledgeUnavailable("this campus has no route graph (campus file v2)")
        return self.router

    def _notices(self) -> dict[str, list[str]]:
        now = self.now()
        out: dict[str, list[str]] = {}
        for d in self._documents:
            if d["kind"] == "notice" and _valid(d, now.isoformat()):
                for target in d["applies_to"]:
                    out.setdefault(target, []).append(d["id"])
        return out

    def plan_route(
        self,
        start: Any,
        goal: Any,
        profile: str = "default",
        avoid: list[str] | None = None,
    ) -> dict[str, Any]:
        route = self._router().plan(
            start,
            goal,
            profile,
            statuses=self.statuses,
            now=self.now(),
            risk=self.risk,
            avoid=set(avoid or ()),
            speed_mps=self.cfg.walking_speed_mps,
            notices=self._notices(),
        )
        return self._with_notices(route.as_dict())

    def _with_notices(self, route: dict[str, Any]) -> dict[str, Any]:
        """The texts of the notices applied, for the robot's spoken explanation."""
        texts = {d["id"]: d["text"] for d in self._documents}
        route["notices"] = [
            {"id": n, "text": texts[n]} for n in route.get("notices_applied") or [] if n in texts
        ]
        return route

    def route_compliance(
        self, segments: list[dict[str, Any]], profile: str = "default"
    ) -> dict[str, Any]:
        """Checked by code against the graph (route compliance): every edge followed was
        allowed by the profile and by the statuses in force when it was traversed.
        ``segments``: ``[{"edges": [...], "at": ISO time, "start": node, "goal": node}]``."""
        router = self._router()
        violations = []
        for seg in segments:
            at = datetime.fromisoformat(seg["at"])
            violations += router.violations(
                list(seg.get("edges") or []),
                profile,
                self.statuses.at(at),
                at,
                start=seg.get("start"),
                goal=seg.get("goal"),
                risk=self.risk,
            )
        return {"compliant": not violations, "violations": violations}

    def nearest_shelter(self, start: Any, profile: str = "default") -> dict[str, Any]:
        shelters = [p.poi_id for p in self.campus.pois if "shelter" in p.tags]
        route = self._router().nearest(
            start,
            shelters,
            profile,
            statuses=self.statuses,
            now=self.now(),
            risk=self.risk,
            speed_mps=self.cfg.walking_speed_mps,
            notices=self._notices(),
        )
        return self._with_notices(route.as_dict())


def _valid(meta: dict[str, Any], at: str) -> bool:
    when = datetime.fromisoformat(at)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    for key, cmp in (("valid_from", lambda t: when >= t), ("valid_until", lambda t: when < t)):
        value = meta.get(key)
        if value:
            t = datetime.fromisoformat(value)
            if not cmp(t if t.tzinfo else t.replace(tzinfo=UTC)):
                return False
    return True


__all__ = ["KnowledgeService", "KnowledgeUnavailable", "fold"]


def _when(stamp: str) -> datetime:
    when = datetime.fromisoformat(stamp)
    return when if when.tzinfo else when.replace(tzinfo=UTC)
