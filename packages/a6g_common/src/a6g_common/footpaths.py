"""Footpaths only (decision of 2026-10-18): robots move on the OSM footpaths and nowhere else.

The rest of the campus has low fences and obstacles that are not mapped as buildings, so
the walkable space is the route graph's path edges (OSM footways, manual paths, short
bridges and the connectors that attach POIs). This module gives every consumer the same
geometry (rule 16):

* :class:`Walker`: the shortest walk between two points over the paths, entering and
  leaving the network at the nearest path point (the 2D simulator's navigation, the
  allocator's travel costs, POI slots and spawn spots);
* :func:`footpath_zones`: the patrol zones derived from the paths (an automatic, balanced
  split from one seed point per zone), each with its survey route covering every path of
  the zone and a polygon drawn around them.

Deterministic for a given graph and seeds.
"""

import heapq
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from a6g_common.routegraph import GraphEdge, RouteGraph

XY = tuple[float, float]

#: Edge sources that are walkable paths ("open" links through free space are not).
WALKABLE = frozenset({"osm", "manual", "bridge", "connector"})
#: Edge sources a patrol covers (connectors only lead to POIs).
PATROLLED = frozenset({"osm", "manual", "bridge"})


def base_id(eid: str) -> str:
    """The path an edge belongs to: a POI attachment splits ``w123.0:1`` into ``w123.0:1a``
    and ``w123.0:1b``; zone assignments name the path, so they survive moved POIs."""
    return re.sub(r"[a-z]+$", "", eid)


def _seg(p: XY, a: XY, b: XY) -> tuple[float, XY, float]:
    dx, dy = b[0] - a[0], b[1] - a[1]
    den = dx * dx + dy * dy
    t = 0.0 if den == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / den))
    q = (a[0] + t * dx, a[1] + t * dy)
    return math.dist(p, q), q, t


@dataclass(frozen=True)
class Projection:
    edge: str
    point: XY
    distance: float  # from the query point to the path
    along: float  # metres from the edge's ``from`` node


class Walker:
    """Shortest walks over the walkable edges of a route graph."""

    def __init__(self, graph: RouteGraph, sources: frozenset[str] = WALKABLE) -> None:
        self.edges: dict[str, GraphEdge] = {
            e.id: e for e in sorted(graph.edges, key=lambda e: e.id) if e.source in sources
        }
        self.nodes = {n.id: (n.x, n.y) for n in graph.nodes}
        self.geom: dict[str, list[XY]] = {}
        self.length: dict[str, float] = {}
        self.adj: dict[str, list[tuple[str, str]]] = {}
        for eid, e in self.edges.items():
            line = [tuple(p) for p in e.geometry] or [self.nodes[e.from_], self.nodes[e.to]]
            self.geom[eid] = line  # type: ignore[assignment]
            self.length[eid] = sum(math.dist(a, b) for a, b in pairwise(line))
            self.adj.setdefault(e.from_, []).append((e.to, eid))
            self.adj.setdefault(e.to, []).append((e.from_, eid))

    # ------------------------------------------------------------------ geometry

    def project(self, p: XY) -> Projection | None:
        """The nearest path point to ``p``."""
        best: Projection | None = None
        for eid, line in self.geom.items():
            run = 0.0
            for a, b in pairwise(line):
                d, q, t = _seg(p, a, b)
                if best is None or d < best.distance - 1e-9:
                    best = Projection(eid, q, d, run + t * math.dist(a, b))
                run += math.dist(a, b)
        return best

    def distance(self, p: XY) -> float:
        proj = self.project(p)
        return math.inf if proj is None else proj.distance

    def cut(self, eid: str, s0: float, s1: float) -> list[XY]:
        """The edge's polyline between arc lengths ``s0`` and ``s1`` (either direction)."""
        if s0 > s1:
            return list(reversed(self.cut(eid, s1, s0)))
        line, out, run = self.geom[eid], [], 0.0
        for a, b in pairwise(line):
            seg = math.dist(a, b)
            lo, hi = run, run + seg
            if hi >= s0 - 1e-9 and lo <= s1 + 1e-9 and seg > 0:
                for s in (max(s0, lo), min(s1, hi)):
                    t = (s - lo) / seg
                    q = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
                    if not out or math.dist(out[-1], q) > 1e-6:
                        out.append(q)
            run = hi
        return out or [line[0]]

    def edge_line(self, eid: str, start_node: str) -> list[XY]:
        """The edge's polyline walked from ``start_node``."""
        line = self.geom[eid]
        return list(line) if self.edges[eid].from_ == start_node else list(reversed(line))

    # ------------------------------------------------------------------ walks

    def _dijkstra(self, sources: Mapping[str, float]) -> tuple[dict, dict]:
        best = dict(sources)
        prev: dict[str, tuple[str, str]] = {}
        heap = [(d, n) for n, d in sorted(sources.items())]
        heapq.heapify(heap)
        while heap:
            d, n = heapq.heappop(heap)
            if d > best.get(n, math.inf) + 1e-9:
                continue
            for m, eid in self.adj.get(n, []):
                nd = d + self.length[eid]
                if nd < best.get(m, math.inf) - 1e-9:
                    best[m] = nd
                    prev[m] = (n, eid)
                    heapq.heappush(heap, (nd, m))
        return best, prev

    def walk(self, a: XY, b: XY) -> tuple[float, list[XY]] | None:
        """(length, polyline) of the shortest walk from ``a`` to ``b`` on the paths: to the
        nearest path point, along the paths, then to ``b``."""
        pa, pb = self.project(a), self.project(b)
        if pa is None or pb is None:
            return None
        ea, eb = self.edges[pa.edge], self.edges[pb.edge]
        la, lb = self.length[pa.edge], self.length[pb.edge]
        options: list[tuple[float, list[XY]]] = []
        if pa.edge == pb.edge:  # along the same path
            options.append((abs(pb.along - pa.along), self.cut(pa.edge, pa.along, pb.along)))
        starts = {ea.from_: pa.along, ea.to: la - pa.along}
        best, prev = self._dijkstra(starts)
        for end, tail in ((eb.from_, pb.along), (eb.to, lb - pb.along)):
            if end not in best:
                continue
            nodes, eids, n = [end], [], end
            while n in prev:  # back to the start node it was reached from
                n, eid = prev[n]
                nodes.append(n)
                eids.append(eid)
            nodes.reverse()
            eids.reverse()
            first = nodes[0]
            line = self.cut(pa.edge, pa.along, 0.0 if first == ea.from_ else la)
            for node, eid in zip(nodes, eids, strict=False):
                line += self.edge_line(eid, node)[1:]
            line += self.cut(pb.edge, 0.0 if end == eb.from_ else lb, pb.along)[1:]
            options.append((best[end] + tail, line))
        if not options:
            return None
        length, line = min(options, key=lambda o: o[0])
        pts = [a, *line, b]
        out = [pts[0]]
        for q in pts[1:]:
            if math.dist(out[-1], q) > 1e-3:
                out.append(q)
        return round(length + pa.distance + pb.distance, 3), [
            (round(x, 3), round(y, 3)) for x, y in out
        ]

    def along(self, p: XY, offsets: Sequence[float]) -> list[XY]:
        """Points ``offsets`` metres along the paths from the nearest path point to ``p``
        (positive towards the edge's ``to`` node, negative towards ``from``), continuing
        through junctions (the lowest edge id) and turning back past a dead end."""
        proj = self.project(p)
        if proj is None:
            return [p for _ in offsets]
        return [self._walk_from(proj.edge, proj.along, off) for off in offsets]

    def _walk_from(self, eid: str, s: float, off: float) -> XY:
        direction = 1 if off >= 0 else -1
        left, came = abs(off), None
        for _ in range(64):
            length = self.length[eid]
            end_s = length if direction > 0 else 0.0
            room = abs(end_s - s)
            if left <= room:
                t = s + direction * left
                return self.cut(eid, t, t)[0]
            left -= room
            e = self.edges[eid]
            node = e.to if direction > 0 else e.from_
            nexts = sorted(x for _, x in self.adj.get(node, []) if x not in (eid, came))
            if not nexts:  # dead end: back along the same path
                came, direction, s = eid, -direction, end_s
                continue
            came, eid = eid, nexts[0]
            e = self.edges[eid]
            direction, s = (1, 0.0) if e.from_ == node else (-1, self.length[eid])
        return self.cut(eid, s, s)[0]


_WALKERS: dict[tuple[int, frozenset[str]], tuple[RouteGraph, Walker]] = {}


def walker_for(graph: RouteGraph, sources: frozenset[str] = WALKABLE) -> Walker:
    """A shared walker per graph object (graphs are immutable once loaded)."""
    key = (id(graph), sources)
    hit = _WALKERS.get(key)
    if hit is None or hit[0] is not graph:
        if len(_WALKERS) > 64:
            _WALKERS.clear()
        hit = _WALKERS[key] = (graph, Walker(graph, sources))
    return hit[1]


# --------------------------------------------------------------------------- zones


@dataclass
class Zone:
    zone_id: str
    seed: XY
    edges: list[str] = field(default_factory=list)
    length_m: float = 0.0
    waypoints: list[XY] = field(default_factory=list)
    polygon: list[XY] = field(default_factory=list)


def _simplify(line: Sequence[XY], tol: float) -> list[XY]:
    """Douglas-Peucker: the walk within ``tol`` metres of the path."""
    if len(line) < 3:
        return list(line)
    a, b = line[0], line[-1]
    worst, idx = -1.0, 0
    for i in range(1, len(line) - 1):
        d = _seg(line[i], a, b)[0]
        if d > worst:
            worst, idx = d, i
    if worst <= tol:
        return [a, b]
    left = _simplify(line[: idx + 1], tol)
    return left[:-1] + _simplify(line[idx:], tol)


def _hull(points: Sequence[XY]) -> list[XY]:
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def cross(o: XY, a: XY, b: XY) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[XY] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[XY] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _clip(poly: Sequence[XY], clip: Sequence[XY]) -> list[XY]:
    """Sutherland-Hodgman: ``poly`` inside the convex polygon ``clip``."""

    def area2(ring: Sequence[XY]) -> float:
        return sum(
            a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, [*ring[1:], ring[0]], strict=True)
        )

    ring = list(clip) if area2(clip) > 0 else list(reversed(clip))
    out = list(poly)
    for a, b in zip(ring, [*ring[1:], ring[0]], strict=True):
        if not out:
            break

        def inside(p: XY, a: XY = a, b: XY = b) -> bool:
            return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) >= 0

        def cut(p: XY, q: XY, a: XY = a, b: XY = b) -> XY:
            x1, y1, x2, y2 = *p, *q
            x3, y3, x4, y4 = *a, *b
            den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4) or 1e-12
            t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
            return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))

        src, out = out, []
        for i, q in enumerate(src):
            p = src[i - 1]
            if inside(q):
                if not inside(p):
                    out.append(cut(p, q))
                out.append(q)
            elif inside(p):
                out.append(cut(p, q))
    return out


def footpath_zones(
    graph: RouteGraph,
    seeds: Mapping[str, XY],
    *,
    fixed: Mapping[str, Sequence[str]] | None = None,
    start: XY | None = None,
    margin_m: float = 3.0,
    simplify_m: float = 1.0,
    area: Sequence[XY] | None = None,
    exclude: Sequence[str] = (),
) -> dict[str, Zone]:
    """Split the patrolled paths into one zone per seed, contiguous and balanced by length:
    each path goes to the zone whose seed is nearest along the network plus a per-zone
    offset, tuned until the zones' lengths are as even as whole paths allow (additively
    weighted network Voronoi: every zone is connected). ``fixed`` edges stay in their zone
    (manual adjustments). Each zone gets a survey route over all its paths, starting at its
    node nearest to ``start`` (e.g. the staging area), and a polygon around them (clipped
    to ``area``)."""
    walker = Walker(graph, PATROLLED)
    if not walker.edges:
        raise ValueError("no patrolled paths in the route graph")
    skip = {base_id(e) for e in exclude}  # paths nobody patrols (still walkable)
    keep = {
        e
        for e in walker.edges
        if base_id(e) not in skip and (area is None or _inside(_mid(walker, e), area))
    }
    if keep != set(walker.edges):  # the pilot area only, without the excluded paths
        walker = Walker(
            graph.model_copy(update={"edges": [e for e in graph.edges if e.id in keep]}),
            PATROLLED,
        )
    bases: dict[str, list[str]] = {}
    for eid in walker.edges:
        bases.setdefault(base_id(eid), []).append(eid)
    fixed_owner: dict[str, str] = {}
    for zid, eids in (fixed or {}).items():
        for name in eids:
            parts = bases.get(base_id(name))
            if not parts:
                raise ValueError(f"zone {zid}: unknown path edge {name!r}")
            for eid in parts:
                if eid in fixed_owner and fixed_owner[eid] != zid:
                    raise ValueError(f"path {name!r} is in zones {fixed_owner[eid]} and {zid}")
                fixed_owner[eid] = zid
    zids = sorted(seeds)
    dist: dict[str, dict[str, float]] = {}
    for zid in zids:  # network distance from each seed to each path's midpoint
        proj = walker.project(tuple(seeds[zid]))  # type: ignore[arg-type]
        assert proj is not None
        e = walker.edges[proj.edge]
        best, _ = walker._dijkstra(
            {e.from_: proj.along, e.to: walker.length[proj.edge] - proj.along}
        )
        dist[zid] = {}
        for eid, edge in walker.edges.items():
            half = walker.length[eid] / 2
            d = min(best.get(edge.from_, math.inf), best.get(edge.to, math.inf)) + half
            if eid == proj.edge:
                d = 0.0
            dist[zid][eid] = d
    total = sum(walker.length.values())
    offset = dict.fromkeys(zids, 0.0)

    def assign() -> dict[str, str]:
        return {
            eid: fixed_owner.get(eid) or min(zids, key=lambda z: (dist[z][eid] + offset[z], z))
            for eid in walker.edges
        }

    best_owner, best_spread = assign(), math.inf
    for step in range(200):  # tune the offsets (the largest zone gives way)
        owner = assign()
        lengths = {z: sum(walker.length[e] for e, o in owner.items() if o == z) for z in zids}
        spread = max(lengths.values()) - min(lengths.values())
        if spread < best_spread - 1e-6:
            best_owner, best_spread = owner, spread
        target = total / len(zids)
        rate = 0.5 * (0.98**step)
        for z in zids:
            offset[z] += rate * (lengths[z] - target)
    zones = {zid: Zone(zid, tuple(seeds[zid])) for zid in zids}  # type: ignore[arg-type]
    for eid in sorted(best_owner):
        z = zones[best_owner[eid]]
        z.edges.append(eid)
        z.length_m += walker.length[eid]
    for zone in zones.values():
        if not zone.edges:  # reported by validation ("zone ... has no paths")
            x, y = zone.seed
            zone.polygon = [(x - 1, y - 1), (x + 1, y - 1), (x + 1, y + 1), (x - 1, y + 1)]
            continue
        zone.waypoints = _cover(walker, zone.edges, start or zone.seed, simplify_m)
        # The polygon covers the zone's paths and its whole survey route (a zone painted in
        # separate pieces walks between them over other paths).
        pts = [p for e in zone.edges for p in walker.geom[e]] + list(zone.waypoints)
        corners = [
            (x + dx, y + dy)
            for x, y in pts
            for dx in (-margin_m, margin_m)
            for dy in (-margin_m, margin_m)
        ]
        hull = _hull(corners)
        if area is not None:
            hull = _clip(hull, area) or hull
        zone.polygon = [(round(x, 3), round(y, 3)) for x, y in hull]
        zone.length_m = round(zone.length_m, 1)
    return zones


def _mid(walker: Walker, eid: str) -> XY:
    half = walker.length[eid] / 2
    return walker.cut(eid, half, half)[0]


def _inside(p: XY, poly: Sequence[XY]) -> bool:
    from a6g_common.geometry import point_in_polygon

    return point_in_polygon(p, [tuple(q) for q in poly])


def _cover(walker: Walker, edges: Sequence[str], start: XY, simplify_m: float) -> list[XY]:
    """A short walk over every edge of the zone (an approximate Chinese postman route): the
    odd junctions are paired greedily by walking distance inside the zone, those connecting
    paths are walked twice, and an Euler trail runs from the zone's node nearest to
    ``start``. Simplified to within ``simplify_m`` of the paths."""
    mine = sorted(set(edges))
    zone = Walker.__new__(Walker)  # the zone's own sub-network
    zone.edges = {e: walker.edges[e] for e in mine}
    zone.nodes, zone.geom, zone.length = walker.nodes, walker.geom, walker.length
    zone.adj = {}
    for eid in mine:
        e = walker.edges[eid]
        zone.adj.setdefault(e.from_, []).append((e.to, eid))
        zone.adj.setdefault(e.to, []).append((e.from_, eid))
    degree = {n: len(v) for n, v in zone.adj.items()}
    first = min(zone.adj, key=lambda n: (math.dist(walker.nodes[n], start), n))
    odd = sorted(n for n, d in degree.items() if d % 2)
    extra: list[str] = []  # edges walked twice
    if odd:
        if first not in odd:  # an open trail starts at an odd node: the nearest to start
            first = min(odd, key=lambda n: (math.dist(walker.nodes[n], start), n))
        rest = [n for n in odd if n != first]
        paths: dict[tuple[str, str], tuple[float, list[str]]] = {}
        for a in rest:
            best, prev = zone._dijkstra({a: 0.0})
            for b in rest:
                if b <= a or b not in best:
                    continue
                eids, n = [], b
                while n in prev:
                    n, eid = prev[n]
                    eids.append(eid)
                paths[(a, b)] = (best[b], eids)
        unmatched = set(rest)
        for (a, b), (_, eids) in sorted(paths.items(), key=lambda kv: (kv[1][0], kv[0])):
            if len(unmatched) <= 1:
                break  # one odd node is left: the trail ends there
            if a in unmatched and b in unmatched:
                unmatched -= {a, b}
                extra += eids
    # Hierholzer over the multigraph (zone edges + doubled ones), deterministic.
    counts: dict[str, int] = {}
    for eid in [*mine, *extra]:
        counts[eid] = counts.get(eid, 0) + 1
    adj: dict[str, list[tuple[str, str]]] = {}
    for eid, k in sorted(counts.items()):
        e = walker.edges[eid]
        for _ in range(k):
            adj.setdefault(e.from_, []).append((e.to, eid))
            adj.setdefault(e.to, []).append((e.from_, eid))
    for n in adj:
        adj[n].sort(key=lambda x: x[1], reverse=True)
    used: dict[str, int] = {}
    stack: list[tuple[str, str | None]] = [(first, None)]
    trail: list[tuple[str, str | None]] = []
    while stack:
        node, _via = stack[-1]
        while adj.get(node) and used.get(adj[node][-1][1], 0) >= counts[adj[node][-1][1]]:
            adj[node].pop()
        if adj.get(node):
            nxt, eid = adj[node].pop()
            used[eid] = used.get(eid, 0) + 1
            # the twin entry at nxt is consumed lazily via the used counter
            stack.append((nxt, eid))
        else:
            trail.append(stack.pop())
    trail.reverse()
    line: list[XY] = []
    if len(trail) > 1 and sum(used.values()) == sum(counts.values()):
        prev_node = trail[0][0]
        for node, eid in trail[1:]:
            assert eid is not None
            seg = walker.edge_line(eid, prev_node)
            line += seg if not line else seg[1:]
            prev_node = node
    else:  # not one connected piece: fall back to walking each path in turn
        line = _cover_pieces(walker, zone, mine, first)
    pts = _simplify(line, simplify_m)
    return [(round(x, 3), round(y, 3)) for x, y in pts]


def _cover_pieces(walker: Walker, zone: Walker, mine: Sequence[str], first: str) -> list[XY]:
    line: list[XY] = []
    pending = set(mine)

    def add(seg: Sequence[XY]) -> None:
        line.extend(seg if not line else seg[1:])

    def visit(node: str) -> None:
        for nxt, eid in zone.adj[node]:
            if eid not in pending:
                continue
            pending.discard(eid)
            add(walker.edge_line(eid, node))
            visit(nxt)
            if pending:
                add(walker.edge_line(eid, nxt))

    visit(first)
    while pending:
        here = line[-1]
        node = min(
            {n for e in pending for n in (walker.edges[e].from_, walker.edges[e].to)},
            key=lambda n: (math.dist(walker.nodes[n], here), n),
        )
        route = walker.walk(here, walker.nodes[node])
        if route is not None:
            add(route[1])
        visit(node)
    return line
