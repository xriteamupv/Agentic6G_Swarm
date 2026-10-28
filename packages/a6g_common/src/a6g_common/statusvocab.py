"""Closed vocabulary from observation labels to statuses, checked by code.

Robots propose a status when on-board triage labels a known element close to a node, edge or
POI; the coordinator re-validates with the same vocabulary and distance (external
validation) before writing it. Outdoors only: gates, paths, flooding, obstructions.
"""

import math
import re

from a6g_common.config import CampusConfig

_VOCABULARY: tuple[tuple[str, str], ...] = (
    (
        "flooded",
        r"\b(flood\w*|standing water|water(logged)?|puddle|inundad\w*|inundat\w*|agua|aigua)\b",
    ),
    ("closed", r"\b(closed|locked|shut|cerrad[oa]|tancad[ao]|gate closed|barrier down)\b"),
    (
        "obstructed",
        r"\b(obstruct\w*|block\w*|fallen tree|debris|rubble|works?|roadworks|scaffold\w*|"
        r"fence|cones?|obras|obres|bloquead[oa]|bloquejat)\b",
    ),
    ("out_of_service", r"\b(out of service|broken|fuera de servicio|fora de servei|averiad[oa])\b"),
)


def status_from_label(label: str) -> str | None:
    """The status a triage label stands for, or None (most labels are not statuses)."""
    text = label.lower()
    for status, pattern in _VOCABULARY:
        if re.search(pattern, text):
            return status
    return None


def match_subject(campus: CampusConfig, x: float, y: float, radius_m: float) -> str | None:
    """The POI, graph node or edge within ``radius_m`` of (x, y) (closest; POIs first)."""
    best: tuple[float, int, str] | None = None
    for p in campus.pois:
        d = math.hypot(p.pose.x - x, p.pose.y - y)
        if d <= radius_m and (best is None or (d, 0, p.poi_id) < best):
            best = (d, 0, p.poi_id)
    graph = campus.route_graph
    if graph is not None:
        for n in graph.nodes:
            if n.kind != "junction":
                continue
            d = math.hypot(n.x - x, n.y - y)
            if d <= radius_m and (best is None or (d, 1, n.id) < best):
                best = (d, 1, n.id)
        for e in graph.edges:
            if e.source in ("connector", "open"):
                continue
            for (ax, ay), (bx, by) in zip(e.geometry, e.geometry[1:], strict=False):
                dx, dy = bx - ax, by - ay
                den = dx * dx + dy * dy
                t = 0.0 if den == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / den))
                d = math.hypot(x - ax - t * dx, y - ay - t * dy)
                if d <= radius_m and (best is None or (d, 2, e.id) < best):
                    best = (d, 2, e.id)
    return best[2] if best else None
