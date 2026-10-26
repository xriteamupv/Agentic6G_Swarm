"""Outdoor pedestrian route graph (SPEC A3.3.1; UC2 runs outdoors only).

Built deterministically from the campus file (rule 16): OpenStreetMap footways of the pilot
box (``<osm cache>.paths.json``) plus manual nodes and edges, every POI and the staging
area attached by a connector edge, and separate pieces bridged through free space. Ids are
stable (OSM node and way ids, POI ids), coordinates rounded to the millimetre.

Routing proper (profiles, statuses, A*) belongs to the knowledge server (E2); this module
keeps the shortest-path search that validation needs.
"""

import heapq
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from itertools import pairwise
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from a6g_common.geometry import point_in_polygon

XY = tuple[float, float]


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    x: float
    y: float
    kind: str = "junction"  # junction | poi | staging | entrance | access


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    from_: str = Field(alias="from")
    to: str
    length_m: float = Field(ge=0.0)
    step_free: bool = True
    stairs: bool = False
    slope_pct: float | None = None
    width_m: float | None = None
    sheltered: bool = False
    exposed: bool = False
    exposed_poi: str | None = None
    source: str = "osm"  # osm | manual | connector | bridge | open
    geometry: list[XY] = Field(default_factory=list, description="polyline, from -> to")


class RoutingProfile(BaseModel):
    """A people routing profile (non-identifying; SPEC A3.3.1)."""

    model_config = ConfigDict(extra="forbid")

    exclude: list[str] = Field(default_factory=list, description="edge flags to avoid")
    require: list[str] = Field(default_factory=list, description="edge flags required")
    max_slope_pct: float | None = None

    def allows(self, edge: GraphEdge) -> bool:
        if any(getattr(edge, flag, False) for flag in self.exclude):
            return False
        if not all(getattr(edge, flag, False) for flag in self.require):
            return False
        return not (
            self.max_slope_pct is not None
            and edge.slope_pct is not None
            and abs(edge.slope_pct) > self.max_slope_pct
        )


DEFAULT_PROFILES = {
    "step_free": RoutingProfile(exclude=["stairs"], require=["step_free"]),
    "avoid_slopes": RoutingProfile(max_slope_pct=6.0),
    "default": RoutingProfile(),
}


class RouteGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    #: Open walkable space (plazas): straight moves up to this length through free space.
    open_space_max_m: float = 0.0

    def node(self, node_id: str) -> GraphNode:
        return next(n for n in self.nodes if n.id == node_id)

    def ids(self) -> set[str]:
        return {n.id for n in self.nodes} | {e.id for e in self.edges}

    def components(self) -> list[list[str]]:
        """Connected components (undirected), each sorted, largest first."""
        return _components([n.id for n in self.nodes], self.edges)

    def shortest_path(
        self,
        start: str,
        goal: str,
        allowed: Callable[[GraphEdge], bool] | None = None,
    ) -> tuple[float, list[str]] | None:
        """Dijkstra over the allowed edges, ties broken by edge id: (length, edge ids)."""
        adj: dict[str, list[tuple[str, GraphEdge]]] = {}
        for e in sorted(self.edges, key=lambda e: e.id):
            if allowed is not None and not allowed(e):
                continue
            adj.setdefault(e.from_, []).append((e.to, e))
            adj.setdefault(e.to, []).append((e.from_, e))
        best = {start: 0.0}
        heap: list[tuple[float, str, str, list[str]]] = [(0.0, "", start, [])]
        while heap:
            dist, _, node, path = heapq.heappop(heap)
            if node == goal:
                return round(dist, 3), path
            if dist > best.get(node, math.inf):
                continue
            for nxt, e in adj.get(node, []):
                nd = dist + e.length_m
                if nd < best.get(nxt, math.inf) - 1e-9:
                    best[nxt] = nd
                    heapq.heappush(heap, (nd, e.id, nxt, [*path, e.id]))
        return None


# --------------------------------------------------------------------------- building


def _components(node_ids: Iterable[str], edges: Iterable[GraphEdge]) -> list[list[str]]:
    parent = {n: n for n in node_ids}

    def find(a: str) -> str:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for e in edges:
        if e.from_ in parent and e.to in parent:
            ra, rb = find(e.from_), find(e.to)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    groups: dict[str, list[str]] = {}
    for n in parent:
        groups.setdefault(find(n), []).append(n)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: (-len(g), g[0]))


def _length(line: Sequence[XY]) -> float:
    return sum(math.dist(a, b) for a, b in pairwise(line))


def _segment_point(p: XY, a: XY, b: XY) -> tuple[float, XY, float]:
    """(distance, closest point, parameter t) from ``p`` to segment ``a``-``b``."""
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    den = dx * dx + dy * dy
    t = 0.0 if den == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / den))
    q = (ax + t * dx, ay + t * dy)
    return math.dist(p, q), q, t


def crosses(a: XY, b: XY, obstacles: Sequence[Sequence[XY]]) -> bool:
    """Does segment a-b pass through an obstacle (sampled every 0.5 m)?"""
    lo_x, hi_x = min(a[0], b[0]), max(a[0], b[0])
    lo_y, hi_y = min(a[1], b[1]), max(a[1], b[1])
    near = [
        o
        for o in obstacles
        if not (
            max(p[0] for p in o) < lo_x
            or min(p[0] for p in o) > hi_x
            or max(p[1] for p in o) < lo_y
            or min(p[1] for p in o) > hi_y
        )
    ]
    if not near:
        return False
    n = max(2, int(math.dist(a, b) / 0.5) + 1)
    for i in range(1, n - 1):
        t = i / (n - 1)
        p = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
        if any(point_in_polygon(p, o) for o in near):
            return True
    return False


def _r(p: XY) -> XY:
    return (round(p[0], 3), round(p[1], 3))


def _float(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text.replace("%", "").replace("m", "").strip())
    except ValueError:
        return None


def build_route_graph(
    ways: Sequence[Mapping[str, Any]],
    xy: Callable[[float, float], XY],
    *,
    area: Sequence[XY] | None,
    obstacles: Sequence[Sequence[XY]],
    attach: Mapping[str, tuple[XY, str]],
    exposed: Mapping[str, XY],
    manual_nodes: Sequence[Mapping[str, Any]] = (),
    manual_edges: Sequence[Mapping[str, Any]] = (),
    bridge_max_m: float = 25.0,
    attach_max_m: float = 60.0,
    exposed_radius_m: float = 10.0,
    open_space_max_m: float = 0.0,
) -> tuple[RouteGraph, list[str]]:
    """The graph and notes on what was bridged or dropped.

    ``ways``: OSM footways (``id``, ``nodes``, ``line`` in WGS84, ``tags``). ``attach``:
    node id -> (position, kind) for POIs, staging and other places to connect. ``exposed``:
    exposed POI id -> position (edges near it are ``exposed``).
    """
    notes: list[str] = []
    inside = (lambda p: point_in_polygon(p, area)) if area else (lambda p: True)
    # 1. OSM ways -> vertex chains inside the pilot area.
    chains: list[tuple[str, list[str], list[XY], dict[str, str]]] = []
    pos: dict[str, XY] = {}
    for way in sorted(ways, key=lambda w: w["id"]):
        ids, pts = list(way.get("nodes") or []), [_r(xy(lat, lon)) for lat, lon in way["line"]]
        if len(ids) != len(pts):  # geometry without node ids: synthesize stable ones
            ids = [f"{way['id']}#{i}" for i in range(len(pts))]
        run_ids: list[str] = []
        run_pts: list[XY] = []
        part = 0
        for nid, p in zip(ids, pts, strict=True):
            if inside(p):
                run_ids.append(nid)
                run_pts.append(p)
                pos[nid] = p
                continue
            if len(run_ids) >= 2:
                chains.append(
                    (f"{way['id']}.{part}", run_ids, run_pts, dict(way.get("tags") or {}))
                )
                part += 1
            run_ids, run_pts = [], []
        if len(run_ids) >= 2:
            chains.append((f"{way['id']}.{part}", run_ids, run_pts, dict(way.get("tags") or {})))
    # 2. Junctions: chain ends and vertices shared by several chains.
    uses: dict[str, int] = {}
    for _, ids, _, _ in chains:
        for nid in set(ids):
            uses[nid] = uses.get(nid, 0) + 1
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    for cid, ids, pts, tags in chains:
        cut = [i for i, nid in enumerate(ids) if i in (0, len(ids) - 1) or uses[nid] > 1]
        for k, (i, j) in enumerate(pairwise(cut)):
            a, b = ids[i], ids[j]
            if a == b:
                continue
            for nid in (a, b):
                nodes.setdefault(nid, GraphNode(id=nid, x=pos[nid][0], y=pos[nid][1]))
            line = pts[i : j + 1]
            highway = tags.get("highway", "")
            stairs = highway == "steps"
            edges.append(
                GraphEdge(
                    id=f"{cid}:{k}",
                    **{"from": a},
                    to=b,
                    length_m=round(_length(line), 3),
                    stairs=stairs,
                    step_free=not stairs and tags.get("wheelchair") != "no",
                    slope_pct=_float(tags.get("incline")),
                    width_m=_float(tags.get("width")),
                    sheltered=tags.get("covered") in ("yes", "arcade", "colonnade"),
                    source="osm",
                    geometry=line,
                )
            )
    # 3. Manual nodes and edges (outdoor additions; TODO(UPV) survey).
    for m in manual_nodes:
        nodes[m["id"]] = GraphNode(id=m["id"], x=m["x"], y=m["y"], kind=m.get("kind", "junction"))
    for m in manual_edges:
        a, b = nodes[m["from"]], nodes[m["to"]]
        line = [(a.x, a.y), (b.x, b.y)]
        edges.append(
            GraphEdge(
                id=m["id"],
                **{"from": a.id},
                to=b.id,
                length_m=round(m.get("length_m") or _length(line), 3),
                step_free=m.get("step_free", True),
                stairs=m.get("stairs", False),
                slope_pct=m.get("slope_pct"),
                width_m=m.get("width_m"),
                sheltered=m.get("sheltered", False),
                source="manual",
                geometry=line,
            )
        )
    # 4. Attach POIs, staging, ... to the nearest point of the network (splitting an edge).
    for aid, (p, kind) in sorted(attach.items()):
        nodes[aid] = GraphNode(id=aid, x=_r(p)[0], y=_r(p)[1], kind=kind)
        target = _nearest_on_network(p, edges, nodes, obstacles, attach_max_m)
        if target is None:
            notes.append(f"{aid}: no footway within {attach_max_m:.0f} m in line of sight")
            continue
        node_id = _ensure_node(target, edges, nodes, f"x:{aid}")
        line = [_r(p), (nodes[node_id].x, nodes[node_id].y)]
        edges.append(
            GraphEdge(
                id=f"c:{aid}",
                **{"from": aid},
                to=node_id,
                length_m=round(_length(line), 3),
                source="connector",
                geometry=line,
            )
        )
    # 4b. Open space (plazas): people and robots cross it, OSM only maps its edges. Straight
    # links from each attached place to the nodes in reach, through free space only.
    if open_space_max_m > 0:
        linked = {(e.from_, e.to) for e in edges} | {(e.to, e.from_) for e in edges}
        for aid in sorted(attach):
            if aid not in nodes:
                continue
            pa = (nodes[aid].x, nodes[aid].y)
            for nid in sorted(nodes):
                if nid == aid or (aid, nid) in linked:
                    continue
                pb = (nodes[nid].x, nodes[nid].y)
                d = math.dist(pa, pb)
                if 0 < d <= open_space_max_m and not crosses(pa, pb, obstacles):
                    a_, b_ = sorted((aid, nid))
                    edges.append(
                        GraphEdge(
                            id=f"o:{a_}:{b_}",
                            **{"from": a_},
                            to=b_,
                            length_m=round(d, 3),
                            source="open",
                            geometry=[(nodes[a_].x, nodes[a_].y), (nodes[b_].x, nodes[b_].y)],
                        )
                    )
                    linked |= {(aid, nid), (nid, aid)}
    # 5. Bridge separate pieces through free space (closest pair first), then drop pieces
    # that hold no attached place.
    while True:
        comps = _components(nodes, edges)
        if len(comps) < 2:
            break
        main = set(comps[0])
        best = None
        for comp in comps[1:]:
            for a in comp:
                pa = (nodes[a].x, nodes[a].y)
                for b in sorted(main):
                    pb = (nodes[b].x, nodes[b].y)
                    d = math.dist(pa, pb)
                    better = best is None or (d, a, b) < best[:3]
                    if d <= bridge_max_m and better and not crosses(pa, pb, obstacles):
                        best = (d, a, b)
        if best is None:
            break
        d, a, b = best
        edges.append(
            GraphEdge(
                id=f"b:{a}:{b}",
                **{"from": a},
                to=b,
                length_m=round(d, 3),
                source="bridge",
                geometry=[(nodes[a].x, nodes[a].y), (nodes[b].x, nodes[b].y)],
            )
        )
        notes.append(f"bridged {a} - {b} ({d:.1f} m)")
    comps = _components(nodes, edges)
    keep: set[str] = set()
    for comp in comps:
        if comp is comps[0] or any(n in attach for n in comp):
            keep |= set(comp)
        else:
            notes.append(f"dropped a footway piece of {len(comp)} nodes with no place on it")
    nodes = {k: v for k, v in nodes.items() if k in keep}
    edges = [e for e in edges if e.from_ in keep and e.to in keep]
    # 6. Exposed edges: near an exposed POI (the flood twin closes them, SPEC A3.6).
    for e in edges:
        hits = sorted(
            (
                min(_segment_point(p, a, b)[0] for a, b in pairwise(e.geometry)),
                pid,
            )
            for pid, p in exposed.items()
            if len(e.geometry) >= 2
        )
        if hits and hits[0][0] <= exposed_radius_m:
            e.exposed, e.exposed_poi = True, hits[0][1]
    graph = RouteGraph(
        nodes=sorted(nodes.values(), key=lambda n: n.id),
        edges=sorted(edges, key=lambda e: e.id),
        open_space_max_m=open_space_max_m,
    )
    return graph, notes


def _nearest_on_network(
    p: XY,
    edges: Sequence[GraphEdge],
    nodes: Mapping[str, GraphNode],
    obstacles: Sequence[Sequence[XY]],
    max_m: float,
) -> tuple[str, int, XY] | None:
    """(edge id, segment index, point) of the closest reachable network point."""
    found = []
    for e in edges:
        if e.source == "connector":
            continue
        for i, (a, b) in enumerate(pairwise(e.geometry)):
            d, q, _ = _segment_point(p, a, b)
            if d <= max_m:
                found.append((round(d, 3), e.id, i, _r(q)))
    for _d, eid, i, q in sorted(found):
        if not crosses(p, q, obstacles):
            return eid, i, q
    return None


def _ensure_node(
    target: tuple[str, int, XY],
    edges: list[GraphEdge],
    nodes: dict[str, GraphNode],
    new_id: str,
    snap_m: float = 3.0,
) -> str:
    """A node at ``target``: an existing end within ``snap_m``, or the edge split there."""
    eid, i, q = target
    e = next(e for e in edges if e.id == eid)
    for end in (e.from_, e.to):
        if math.dist((nodes[end].x, nodes[end].y), q) <= snap_m:
            return end
    nodes[new_id] = GraphNode(id=new_id, x=q[0], y=q[1])
    first, second = [*e.geometry[: i + 1], q], [q, *e.geometry[i + 1 :]]
    edges.remove(e)
    for suffix, a, b, line in (("a", e.from_, new_id, first), ("b", new_id, e.to, second)):
        edges.append(
            e.model_copy(
                update={
                    "id": f"{e.id}{suffix}",
                    "from_": a,
                    "to": b,
                    "length_m": round(_length(line), 3),
                    "geometry": line,
                }
            )
        )
    return new_id
