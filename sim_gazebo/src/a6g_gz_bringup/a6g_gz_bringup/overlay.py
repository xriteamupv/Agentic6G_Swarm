"""Campus overlay for the Gazebo GUI (ROS-free, unit-tested): the 2D view's features as
marker specs. ``campus_markers`` turns them into ``gz.msgs.Marker``.

Colours follow the 2D view: sectors light blue with their survey route, POIs blue (exposed
orange), 5G shadows red, recovery points green, staging a green square. Labels are drawn
with a stroke font as flat triangle strokes on the ground: Gazebo's default ogre2 renderer
has no TEXT markers ("Invalid Marker type 7").
"""

import itertools
import math
from dataclasses import dataclass, field

BLUE = (0.25, 0.47, 0.75, 1.0)
SECTOR_FILL = (0.55, 0.70, 0.88, 0.30)
POI = (0.15, 0.40, 0.80, 1.0)
EXPOSED = (0.90, 0.45, 0.10, 1.0)
SHADOW = (0.90, 0.20, 0.20, 0.30)
SHADOW_EDGE = (0.85, 0.25, 0.25, 1.0)
GREEN = (0.15, 0.65, 0.35, 1.0)
TEXT = (0.10, 0.10, 0.10, 1.0)


@dataclass
class Spec:
    ns: str
    id: int
    type: str  # gz.msgs.Marker.Type name
    rgba: tuple[float, float, float, float]
    pose: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    points: list[tuple[float, float, float]] = field(default_factory=list)
    text: str = ""


def _ring(polygon, z: float) -> list[tuple[float, float, float]]:
    pts = [(float(x), float(y), z) for x, y in polygon]
    return pts + pts[:1]


def _centroid(polygon) -> tuple[float, float]:
    xs, ys = [p[0] for p in polygon], [p[1] for p in polygon]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def _fan(polygon, z: float) -> list[tuple[float, float, float]]:
    """Triangle fan from the centroid (campus sectors and shadows are convex)."""
    cx, cy = _centroid(polygon)
    return [(cx, cy, z), *_ring(polygon, z)]


# Stroke font on a 4 x 6 grid (uppercase, digits, '-'): polylines per glyph, advance 6.
_O = [[(0, 0), (0, 6), (4, 6), (4, 0), (0, 0)]]
_S = [[(4, 6), (0, 6), (0, 3), (4, 3), (4, 0), (0, 0)]]
FONT: dict[str, list[list[tuple[int, int]]]] = {
    "A": [[(0, 0), (0, 4), (2, 6), (4, 4), (4, 0)], [(0, 3), (4, 3)]],
    "B": [
        [(0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)],
        [(3, 3), (4, 2), (4, 1), (3, 0), (0, 0)],
    ],
    "C": [[(4, 6), (0, 6), (0, 0), (4, 0)]],
    "D": [[(0, 0), (0, 6), (3, 6), (4, 5), (4, 1), (3, 0), (0, 0)]],
    "E": [[(4, 6), (0, 6), (0, 0), (4, 0)], [(0, 3), (3, 3)]],
    "F": [[(4, 6), (0, 6), (0, 0)], [(0, 3), (3, 3)]],
    "G": [[(4, 6), (0, 6), (0, 0), (4, 0), (4, 3), (2, 3)]],
    "H": [[(0, 0), (0, 6)], [(4, 0), (4, 6)], [(0, 3), (4, 3)]],
    "I": [[(1, 6), (3, 6)], [(2, 6), (2, 0)], [(1, 0), (3, 0)]],
    "J": [[(4, 6), (4, 0), (0, 0), (0, 2)]],
    "K": [[(0, 0), (0, 6)], [(4, 6), (0, 3), (4, 0)]],
    "L": [[(0, 6), (0, 0), (4, 0)]],
    "M": [[(0, 0), (0, 6), (2, 3), (4, 6), (4, 0)]],
    "N": [[(0, 0), (0, 6), (4, 0), (4, 6)]],
    "O": _O,
    "P": [[(0, 0), (0, 6), (4, 6), (4, 3), (0, 3)]],
    "Q": [*_O, [(2, 2), (4, 0)]],
    "R": [[(0, 0), (0, 6), (4, 6), (4, 3), (0, 3), (4, 0)]],
    "S": _S,
    "T": [[(0, 6), (4, 6)], [(2, 6), (2, 0)]],
    "U": [[(0, 6), (0, 0), (4, 0), (4, 6)]],
    "V": [[(0, 6), (2, 0), (4, 6)]],
    "W": [[(0, 6), (1, 0), (2, 3), (3, 0), (4, 6)]],
    "X": [[(0, 0), (4, 6)], [(0, 6), (4, 0)]],
    "Y": [[(0, 6), (2, 3), (4, 6)], [(2, 3), (2, 0)]],
    "Z": [[(0, 6), (4, 6), (0, 0), (4, 0)]],
    "0": [*_O, [(0, 0), (4, 6)]],
    "1": [[(1, 5), (2, 6), (2, 0)], [(1, 0), (3, 0)]],
    "2": [[(0, 6), (4, 6), (4, 3), (0, 3), (0, 0), (4, 0)]],
    "3": [[(0, 6), (4, 6), (4, 0), (0, 0)], [(1, 3), (4, 3)]],
    "4": [[(0, 6), (0, 3), (4, 3)], [(4, 6), (4, 0)]],
    "5": _S,
    "6": [[(4, 6), (0, 6), (0, 0), (4, 0), (4, 3), (0, 3)]],
    "7": [[(0, 6), (4, 6), (2, 0)]],
    "8": [*_O, [(0, 3), (4, 3)]],
    "9": [[(4, 3), (0, 3), (0, 6), (4, 6), (4, 0), (0, 0)]],
    "-": [[(1, 3), (3, 3)]],
}
ADVANCE = 6


def text_triangles(
    text: str, x: float, y: float, z: float, height: float
) -> list[tuple[float, float, float]]:
    """``text`` centred on (x, y), flat at ``z``, reading along +x: a TRIANGLE_LIST."""
    u = height / 6.0
    half = 0.35 * u  # stroke half-width
    chars = text.upper()
    x0 = x - (len(chars) * ADVANCE - 2) * u / 2
    y0 = y - height / 2
    tris: list[tuple[float, float, float]] = []
    for i, ch in enumerate(chars):
        for line in FONT.get(ch, []):
            for (ax, ay), (bx, by) in itertools.pairwise(line):
                p = (x0 + (i * ADVANCE + ax) * u, y0 + ay * u)
                q = (x0 + (i * ADVANCE + bx) * u, y0 + by * u)
                dx, dy = q[0] - p[0], q[1] - p[1]
                n = math.hypot(dx, dy) or 1.0
                ex, ey = dx / n * half, dy / n * half  # extend the ends: joints overlap
                nx, ny = -ey, ex
                a = (p[0] - ex + nx, p[1] - ey + ny, z)
                b = (p[0] - ex - nx, p[1] - ey - ny, z)
                c = (q[0] + ex - nx, q[1] + ey - ny, z)
                d = (q[0] + ex + nx, q[1] + ey + ny, z)
                tris += [a, b, c, a, c, d]
    return tris


def _disc(x: float, y: float, r: float, z: float, n: int = 20) -> list[tuple[float, float, float]]:
    ring = [
        (x + r * math.cos(2 * math.pi * k / n), y + r * math.sin(2 * math.pi * k / n), z)
        for k in range(n + 1)
    ]
    return [(x, y, z), *ring]


def overlay_specs(campus: dict) -> list[Spec]:
    scale = float(campus.get("scale") or 1.0)
    k = max(0.4, scale**0.5)  # sizes shrink with the campus, but stay visible
    label = 1.6 * k  # text height (m)
    post = 2.0 * k  # POI post height (m)
    out: list[Spec] = []

    def add(ns: str, **kw) -> None:
        out.append(Spec(ns=ns, id=sum(s.ns == ns for s in out), **kw))

    def text(t: str, x: float, y: float, rgba, size: float = label) -> None:
        add("labels", type="TRIANGLE_LIST", rgba=rgba, points=text_triangles(t, x, y, 0.08, size))

    for s in campus.get("sectors", []):
        poly = s["polygon"]
        add("sectors", type="TRIANGLE_FAN", rgba=SECTOR_FILL, points=_fan(poly, 0.02))
        add("sector_edges", type="LINE_STRIP", rgba=BLUE, points=_ring(poly, 0.03))
        wps = [(float(x), float(y), 0.05) for x, y in s.get("waypoints", [])]
        if wps:
            add("routes", type="LINE_STRIP", rgba=BLUE, points=wps)
            for x, y, _ in wps:
                add("waypoints", type="TRIANGLE_FAN", rgba=BLUE, points=_disc(x, y, 0.3 * k, 0.06))
        cx, cy = _centroid(poly)
        text(s.get("name") or s["id"], cx, cy, BLUE)
    for shadow in campus.get("shadows", []):
        add("shadows", type="TRIANGLE_FAN", rgba=SHADOW, points=_fan(shadow, 0.025))
        add("shadow_edges", type="LINE_STRIP", rgba=SHADOW_EDGE, points=_ring(shadow, 0.035))
    for p in campus.get("pois", []):
        rgba = EXPOSED if p.get("exposed") else POI
        add("pois", type="TRIANGLE_FAN", rgba=rgba, points=_disc(p["x"], p["y"], 0.8 * k, 0.07))
        add(
            "posts",
            type="CYLINDER",
            rgba=rgba,
            pose=(p["x"], p["y"], post / 2),
            scale=(0.4 * k, 0.4 * k, post),
        )
        text(p.get("name") or p["id"], p["x"], p["y"] + 0.8 * k + 0.7 * label, rgba)
    for r in campus.get("recovery", []):
        add(
            "recovery", type="TRIANGLE_FAN", rgba=GREEN, points=_disc(r["x"], r["y"], 0.6 * k, 0.07)
        )
        text(r.get("name") or r["id"], r["x"], r["y"] - 0.6 * k - 0.6 * label, GREEN, 0.8 * label)
    staging = campus.get("staging") or {}
    if staging.get("area"):
        add("staging", type="LINE_STRIP", rgba=GREEN, points=_ring(staging["area"], 0.04))
        ys = [p[1] for p in staging["area"]]
        text("staging", staging["x"], min(ys) - 0.7 * label, GREEN, 0.8 * label)
    return out


# --------------------------------------------------------------------------- robot trails

#: The 2D view's robot palette, assigned in sorted robot-id order.
ROBOT_COLORS = [
    (0.18, 0.44, 0.69, 0.55),  # #2f6fb0
    (0.85, 0.44, 0.11, 0.55),  # #d9711c
    (0.54, 0.31, 0.75, 0.55),  # #8a4fbf
    (0.18, 0.60, 0.36, 0.55),  # #2f9a5b
    (0.76, 0.25, 0.25, 0.55),  # #c24141
    (0.11, 0.60, 0.63, 0.55),  # #1b9aa0
]


@dataclass
class Trails:
    """Ground-truth trails as the 2D view keeps them: a point every ``step`` metres (scaled
    with the campus), the last ``cap`` points per robot."""

    scale: float = 1.0
    cap: int = 3000
    paths: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    changed: bool = False

    @property
    def step(self) -> float:
        return 0.1 * self.scale

    def add(self, robot_id: str, x: float, y: float) -> None:
        path = self.paths.setdefault(robot_id, [])
        if path and math.hypot(path[-1][0] - x, path[-1][1] - y) < self.step:
            return
        path.append((x, y))
        if len(path) > self.cap:
            del path[0]
        self.changed = True

    def specs(self) -> list[Spec]:
        """One ribbon (TRIANGLE_LIST) per robot, flat just above the ground."""
        width = 0.12 * max(0.4, self.scale**0.5)
        out = []
        for i, rid in enumerate(sorted(self.paths)):
            path = self.paths[rid]
            if len(path) < 2:
                continue
            out.append(
                Spec(
                    ns="trails",
                    id=i,
                    type="TRIANGLE_LIST",
                    rgba=ROBOT_COLORS[i % len(ROBOT_COLORS)],
                    points=ribbon(path, width, 0.09),
                )
            )
        self.changed = False
        return out


def ribbon(
    path: list[tuple[float, float]], width: float, z: float
) -> list[tuple[float, float, float]]:
    """A polyline as flat quads (2 triangles per segment)."""
    half = width / 2
    tris: list[tuple[float, float, float]] = []
    for (ax, ay), (bx, by) in itertools.pairwise(path):
        dx, dy = bx - ax, by - ay
        n = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / n * half, dx / n * half
        a, b = (ax + nx, ay + ny, z), (ax - nx, ay - ny, z)
        c, d = (bx - nx, by - ny, z), (bx + nx, by + ny, z)
        tris += [a, b, c, a, c, d]
    return tris
