"""People routes on the outdoor graph (``plan_route``, ``nearest_shelter``).

Shortest path, deterministic (ties by edge id), excluding: the edges the profile excludes,
elements with a restrictive status in force, exposed edges of a POI whose flood
risk is at least ``elevated``, and anything in ``avoid``. Legs hold at most 20
waypoints (``follow_route``). Code computes routes; models only explain them.
"""

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from typing import Any

from a6g_common.routegraph import GraphEdge, RouteGraph, RoutingProfile, crosses

from a6g_kb.status import StatusLayer

MAX_LEG_WAYPOINTS = 20
RISKY = frozenset({"elevated", "high", "critical"})


@dataclass
class Route:
    ok: bool
    profile: str
    route_id: str | None = None
    legs: list[list[dict[str, float]]] = field(default_factory=list)
    length_m: float = 0.0
    est_time_s: float = 0.0
    edges: list[str] = field(default_factory=list)
    statuses_applied: list[str] = field(default_factory=list)
    notices_applied: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)
    destination: str | None = None
    #: The graph edges each leg runs along (route compliance metrics).
    leg_edges: list[list[str]] = field(default_factory=list)
    start_node: str | None = None
    goal_node: str | None = None
    #: edge id -> its two end nodes (a status on a node touches the route there).
    edge_nodes: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


#: A position attaches to up to this many free nodes in reach (the best total route wins).
ATTACH_CANDIDATES = 40


class Router:
    def __init__(
        self,
        graph: RouteGraph,
        profiles: Mapping[str, RoutingProfile],
        obstacles: list[list[tuple[float, float]]] | None = None,
        status_radius_m: float = 3.0,
    ) -> None:
        self.graph = graph
        self.profiles = dict(profiles)
        self._nodes = {n.id: n for n in graph.nodes}
        self.obstacles = obstacles or []
        self._walker: Any = None
        #: A restriction at a point also closes the nodes this close to it (a fallen tree
        #: at a POI closes the junction beside it).
        self.status_radius_m = status_radius_m

    # ------------------------------------------------------------------ references

    def node_of(self, ref: Any) -> str:
        """A POI id, a node id, or a pose ``{"x", "y"}`` (attached to the nearest node)."""
        if isinstance(ref, dict):
            x, y = float(ref["x"]), float(ref["y"])
            return min(self._nodes.values(), key=lambda n: (math.hypot(n.x - x, n.y - y), n.id)).id
        ref = str(ref)
        if ref in self._nodes:
            return ref
        if f"poi:{ref}" in self._nodes:
            return f"poi:{ref}"
        raise KeyError(f"unknown place or node {ref!r}")

    # ------------------------------------------------------------------ exclusions

    def _blocked(
        self,
        statuses: StatusLayer,
        risk: Mapping[str, str],
        avoid: set[str],
        now: datetime,
    ) -> tuple[dict[str, str], set[str], dict[str, set[str]]]:
        """(element -> why) for edges and nodes that may not be used, the status ids, and
        the elements each status closes."""
        why: dict[str, str] = {}
        applied: set[str] = set()
        closes: dict[str, set[str]] = {}  # status id -> the elements it closes
        for subject, reports in statuses.restricted(now).items():
            node = subject if subject in self._nodes else f"poi:{subject}"
            closed = {subject, node}
            for target in closed:
                why[target] = f"{reports[0].status} ({reports[0].source})"
            applied |= {r.status_id for r in reports}
            if node in self._nodes:  # the same spot: nodes within the status radius
                c = self._nodes[node]
                for n in self._nodes.values():
                    if math.hypot(n.x - c.x, n.y - c.y) <= self.status_radius_m:
                        why.setdefault(n.id, f"{reports[0].status} at {subject}")
                        closed.add(n.id)
            for e in self.graph.edges:  # a flooded / closed POI closes its exposed edges
                if e.exposed_poi == subject:
                    why.setdefault(e.id, f"{reports[0].status} at {subject}")
                    closed.add(e.id)
            for r in reports:
                closes[r.status_id] = closed
        for e in self.graph.edges:
            if e.exposed_poi and risk.get(e.exposed_poi) in RISKY:
                why.setdefault(e.id, f"flood risk {risk[e.exposed_poi]} at {e.exposed_poi}")
        for a in avoid:
            node = a if a in self._nodes else f"poi:{a}"
            why.setdefault(a, "avoid")
            why.setdefault(node, "avoid")
        return why, applied, closes

    def _allowed(self, profile: RoutingProfile, why: Mapping[str, str], start: str, goal: str):
        def ok(e: GraphEdge) -> bool:
            if not profile.allows(e) or e.id in why:
                return False
            # A blocked node blocks its edges, except at the two ends of the trip.
            return all(n in (start, goal) or n not in why for n in (e.from_, e.to))

        return ok

    # ------------------------------------------------------------------ planning

    def plan(
        self,
        start: Any,
        goal: Any,
        profile_name: str,
        *,
        statuses: StatusLayer,
        now: datetime,
        risk: Mapping[str, str] | None = None,
        avoid: set[str] | None = None,
        speed_mps: float = 0.8,
        notices: Mapping[str, list[str]] | None = None,
    ) -> Route:
        profile = self.profiles.get(profile_name)
        if profile is None:
            raise KeyError(f"unknown routing profile {profile_name!r}")
        why, applied, closes = self._blocked(statuses, risk or {}, set(avoid or ()), now)
        found = self._attached(start, goal, profile, why)
        if found is None:
            a, b = self.node_of(start), self.node_of(goal)
            return Route(
                ok=False, profile=profile_name, blocking=self._why_blocked(a, b, profile, why)
            )
        length, edge_ids, a, b, lead_in, lead_out = found
        legs, leg_edges = self._legs(a, edge_ids, lead_in, lead_out)
        touched = self._touched(a, b, edge_ids)
        # Statuses and notices that shaped the route: on it, or on the way it went around
        # (the shortest route with nothing blocked). Not every status in force on the
        # campus: reporting all of them makes retrieval precision poor.
        base = self._attached(start, goal, profile, {}) if why else None
        shaped = touched | (self._touched(base[2], base[3], base[1]) if base else set())
        relevant = sorted(sid for sid in applied if closes.get(sid, set()) & shaped)
        used_notices = sorted(
            {doc for k in shaped for doc in (notices or {}).get(k.removeprefix("poi:"), [])}
        )
        digest = hashlib.sha1(
            f"{profile_name}|{'/'.join(edge_ids)}|{','.join(sorted(applied))}".encode()
        ).hexdigest()[:12]
        return Route(
            ok=True,
            profile=profile_name,
            route_id=f"route_{digest}",
            legs=legs,
            length_m=round(length, 1),
            est_time_s=round(length / speed_mps, 1),
            edges=edge_ids,
            statuses_applied=relevant,
            notices_applied=used_notices,
            destination=b.removeprefix("poi:"),
            leg_edges=leg_edges,
            start_node=a,
            goal_node=b,
            edge_nodes={eid: [self._edge(eid).from_, self._edge(eid).to] for eid in edge_ids},
        )

    def _candidates(
        self, ref: Any, why: Mapping[str, str], profile: RoutingProfile
    ) -> list[tuple[str, float, list[tuple[float, float]], str | None]]:
        """(node, distance, lead polyline from the reference to the node, edge walked) for
        each node a reference can start or end at. A POI or node id is itself. A position
        attaches to the nearest usable path and walks along it to either end (footpaths
        only, open_space_max_m 0); with open space, to nearby free nodes in a straight line
        clear of buildings."""
        if not isinstance(ref, dict):
            node = self.node_of(ref)
            return [(node, 0.0, [], None)]
        x, y = float(ref["x"]), float(ref["y"])
        if self.graph.open_space_max_m <= 0:
            return self._on_path((x, y), why, profile)
        reach = self.graph.open_space_max_m
        ranked = sorted(self._nodes.values(), key=lambda n: (math.hypot(n.x - x, n.y - y), n.id))
        out: list[tuple[str, float, list[tuple[float, float]], str | None]] = []
        for n in ranked:
            d = math.hypot(n.x - x, n.y - y)
            if out and d > reach:
                break
            if n.id in why or crosses((x, y), (n.x, n.y), self.obstacles):
                continue
            out.append((n.id, d, [(x, y), (n.x, n.y)], None))
            if len(out) >= ATTACH_CANDIDATES:
                break
        if not out:
            n = ranked[0]
            out = [(n.id, math.hypot(n.x - x, n.y - y), [(x, y), (n.x, n.y)], None)]
        return out

    def _on_path(
        self, p: tuple[float, float], why: Mapping[str, str], profile: RoutingProfile
    ) -> list[tuple[str, float, list[tuple[float, float]], str | None]]:
        """Attach to the nearest path the profile allows and no restriction closes."""
        from a6g_common.footpaths import WALKABLE, Walker

        if self._walker is None:
            self._walker = Walker(self.graph, WALKABLE)
        w = self._walker
        best = None
        for eid, line in w.geom.items():
            e = w.edges[eid]
            if eid in why or not profile.allows(e):
                continue
            run = 0.0
            for a, b in pairwise(line):
                seg = math.dist(a, b)
                dx, dy = b[0] - a[0], b[1] - a[1]
                t = (
                    0.0
                    if seg == 0
                    else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / seg**2))
                )
                q = (a[0] + t * dx, a[1] + t * dy)
                d = math.dist(p, q)
                if best is None or d < best[0] - 1e-9:
                    best = (d, eid, q, run + t * seg)
                run += seg
        if best is None:
            return []
        d, eid, q, along = best
        e, length = w.edges[eid], w.length[eid]
        out = []
        for node, s_end in ((e.from_, 0.0), (e.to, length)):
            if node in why:
                continue
            lead = [p, *w.cut(eid, along, s_end)]
            out.append((node, d + abs(s_end - along), lead, eid))
        return out

    def _touched(self, a: str, b: str, edge_ids: list[str]) -> set[str]:
        """The nodes and edges a route uses."""
        return {a, b, *edge_ids} | {
            n for eid in edge_ids for n in (self._edge(eid).from_, self._edge(eid).to)
        }

    def _attached(
        self, start: Any, goal: Any, profile: RoutingProfile, why: Mapping[str, str]
    ) -> tuple[float, list[str], str, str, Any, Any] | None:
        """The best (length, edges, start node, goal node, lead-in, lead-out) over the
        attachment candidates of both ends; a lead is (polyline, edge walked)."""
        best = None
        for a, da, lead_a, ea in self._candidates(start, why, profile):
            for b, db, lead_b, eb in self._candidates(goal, why, profile):
                found = self.graph.shortest_path(a, b, self._allowed(profile, why, a, b))
                if found is None:
                    continue
                total = found[0] + da + db
                if best is None or total < best[0] - 1e-9:
                    best = (total, found[1], a, b, (lead_a, ea), (list(reversed(lead_b)), eb))
        if best is None:
            return None
        total, edges, a, b, lead_in, lead_out = best
        return round(total, 3), edges, a, b, lead_in, lead_out

    def _why_blocked(
        self, a: str, b: str, profile: RoutingProfile, why: Mapping[str, str]
    ) -> list[str]:
        """What stands in the way: the restrictions on the profile's own best path, or the
        profile itself when even an unrestricted trip is impossible for it."""
        free = self.graph.shortest_path(a, b, profile.allows)
        if free is not None:
            on_path = set(free[1]) | {
                n for eid in free[1] for n in (self._edge(eid).from_, self._edge(eid).to)
            }
            return sorted(f"{k}: {v}" for k, v in why.items() if k in on_path) or [
                "every route is restricted"
            ]
        anyway = self.graph.shortest_path(a, b)
        if anyway is None:
            return ["not connected"]
        return sorted(
            f"{eid}: excluded by the profile"
            for eid in anyway[1]
            if not profile.allows(self._edge(eid))
        )

    def _edge(self, eid: str) -> GraphEdge:
        return next(e for e in self.graph.edges if e.id == eid)

    def _has_edge(self, eid: str) -> bool:
        return any(e.id == eid for e in self.graph.edges)

    def _legs(
        self, start: str, edge_ids: list[str], lead_in: Any = None, lead_out: Any = None
    ) -> tuple[list[list[dict[str, float]]], list[list[str]]]:
        """The route as waypoints (heading along the leg), split into ``follow_route`` legs,
        and the edges each leg runs along. The leads (a position at either end, walked to
        or from the graph) are waypoints too."""
        node = start
        pts: list[tuple[float, float]] = []
        owner: list[str | None] = []  # the edge that leads to each waypoint

        def put(p: tuple[float, float], eid: str | None) -> None:
            if not pts or math.dist(pts[-1], p) > 0.05:
                pts.append((float(p[0]), float(p[1])))
                owner.append(eid)

        lead_pts, lead_edge = lead_in or ([], None)
        for i, p in enumerate(lead_pts):
            put(p, lead_edge if i else None)
        put((self._nodes[start].x, self._nodes[start].y), lead_edge if lead_pts else None)
        for eid in edge_ids:
            e = self._edge(eid)
            geom = list(e.geometry) if e.from_ == node else list(reversed(e.geometry))
            node = e.to if e.from_ == node else e.from_
            for p in geom[1:]:
                put(p, eid)
        out_pts, out_edge = lead_out or ([], None)
        for p in out_pts:
            put(p, out_edge)
        poses = []
        for i, (x, y) in enumerate(pts):
            nx, ny = pts[i + 1] if i + 1 < len(pts) else pts[i]
            px, py = pts[i - 1] if i else (x, y)
            yaw = math.atan2(ny - y, nx - x) if (nx, ny) != (x, y) else math.atan2(y - py, x - px)
            poses.append({"x": round(x, 3), "y": round(y, 3), "yaw": round(yaw, 4)})
        legs, leg_edges = [], []
        i = 0
        while True:  # consecutive legs share their boundary waypoint
            leg = poses[i : i + MAX_LEG_WAYPOINTS]
            legs.append(leg)
            leg_edges.append(
                list(dict.fromkeys(e for e in owner[i + 1 : i + len(leg)] if e is not None))
            )
            if i + len(leg) >= len(poses):
                return legs, leg_edges
            i += len(leg) - 1

    # ------------------------------------------------------------------ compliance

    def violations(
        self,
        edge_ids: list[str],
        profile_name: str,
        statuses: StatusLayer,
        at: datetime,
        *,
        start: str | None = None,
        goal: str | None = None,
        risk: Mapping[str, str] | None = None,
    ) -> list[str]:
        """Why each of ``edge_ids`` was not allowed for the profile and the statuses in
        force at ``at`` (``statuses`` as they stood then); [] when all were."""
        profile = self.profiles[profile_name]
        why, _, _ = self._blocked(statuses, risk or {}, set(), at)
        out = []
        for eid in edge_ids:
            if not self._has_edge(eid):
                out.append(f"{eid}: unknown edge")
                continue
            e = self._edge(eid)
            if not profile.allows(e):
                out.append(f"{eid}: excluded by the profile")
            elif eid in why:
                out.append(f"{eid}: {why[eid]}")
            else:
                for n in (e.from_, e.to):
                    if n not in (start, goal) and n in why:
                        out.append(f"{eid}: node {n} {why[n]}")
                        break
        return out

    def nearest(
        self,
        start: Any,
        candidates: list[str],
        profile_name: str,
        **kw: Any,
    ) -> Route:
        best: Route | None = None
        for c in sorted(candidates):
            route = self.plan(start, c, profile_name, **kw)
            if route.ok and (best is None or route.length_m < best.length_m):
                best = route
        return best or Route(ok=False, profile=profile_name, blocking=["no reachable shelter"])
