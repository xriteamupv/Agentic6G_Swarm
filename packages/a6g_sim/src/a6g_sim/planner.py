"""Grid path planner for the 2D simulator around campus building footprints.

The 2D simulator and Gazebo share the same footprints, so robots must
drive around buildings here too. A straight line is used when it is clear; otherwise A*
(8-connected) on an occupancy grid with footprints inflated by the robot's clearance,
followed by line-of-sight smoothing. Deterministic for a given input.
"""

import heapq
import math
from collections.abc import Sequence

import numpy as np

Point = tuple[float, float]


_CACHE: dict[tuple, "GridPlanner"] = {}


class FootpathPlanner:
    """Footpaths only: every move follows the campus footpaths, as
    Nav2 does with its keepout mask on the real robots. A goal farther than ``corridor_m``
    from a path is refused; a robot off the paths walks back to the nearest one first."""

    def __init__(
        self, graph: object, corridor_m: float, factors: dict[str, float] | None = None
    ) -> None:
        from a6g_common.footpaths import Walker

        self.walker = Walker(graph, factors=factors)  # type: ignore[arg-type]
        self.factors = factors or {}
        self.corridor = corridor_m
        self.reason = ""

    def plan(self, start: Point, goal: Point) -> list[Point] | None:
        if self.walker.distance(goal) > self.corridor:
            self.reason = "goal is off the footpaths"
            return None
        route = self.walker.walk(start, goal)
        if route is None:
            self.reason = "no footpath to the goal"
            return None
        pts = route[1][1:]
        if not pts or math.dist(pts[-1], goal) > 1e-6:
            pts.append(goal)
        return pts


_FOOT: dict[int, FootpathPlanner] = {}


def footpath_planner_for(
    graph: object, corridor_m: float, factors: dict[str, float] | None = None
) -> FootpathPlanner:
    key = id(graph)
    hit = _FOOT.get(key)
    if hit is None or hit.corridor != corridor_m or hit.factors != (factors or {}):
        _FOOT[key] = FootpathPlanner(graph, corridor_m, factors)
    return _FOOT[key]


def planner_for(obstacles: Sequence[Sequence[Point]], **kwargs: float) -> "GridPlanner":
    """Shared planner per obstacle set (worlds built from the same campus reuse the grid)."""
    key = (tuple(tuple(map(tuple, p)) for p in obstacles), tuple(sorted(kwargs.items())))
    if key not in _CACHE:
        _CACHE[key] = GridPlanner(obstacles, **kwargs)
    return _CACHE[key]


class GridPlanner:
    def __init__(
        self,
        obstacles: Sequence[Sequence[Point]],
        *,
        inflation_m: float = 0.8,
        resolution_m: float = 1.0,
        margin_m: float = 20.0,
    ) -> None:
        self.obstacles = [list(p) for p in obstacles if len(p) >= 3]
        self.res = resolution_m
        self.inflation = inflation_m
        if not self.obstacles:
            self.grid = None
            return
        xs = [x for p in self.obstacles for x, _ in p]
        ys = [y for p in self.obstacles for _, y in p]
        self.x0, self.y0 = min(xs) - margin_m, min(ys) - margin_m
        w = math.ceil((max(xs) + margin_m - self.x0) / self.res) + 1
        h = math.ceil((max(ys) + margin_m - self.y0) / self.res) + 1
        occ = np.zeros((h, w), dtype=bool)
        pad = self.inflation + self.res
        for poly in self.obstacles:  # rasterize each footprint only inside its own window
            px = [x for x, _ in poly]
            py = [y for _, y in poly]
            c0 = max(0, int((min(px) - pad - self.x0) // self.res))
            c1 = min(w, int((max(px) + pad - self.x0) // self.res) + 2)
            r0 = max(0, int((min(py) - pad - self.y0) // self.res))
            r1 = min(h, int((max(py) + pad - self.y0) // self.res) + 2)
            cx = self.x0 + (np.arange(c0, c1) + 0.5) * self.res
            cy = self.y0 + (np.arange(r0, r1) + 0.5) * self.res
            gx, gy = np.meshgrid(cx, cy)
            occ[r0:r1, c0:c1] |= _inside(gx, gy, poly) | (
                _edge_distance(gx, gy, poly) < self.inflation
            )
        self.grid = occ

    # ------------------------------------------------------------------ queries

    def _cell(self, p: Point) -> tuple[int, int]:
        return int((p[1] - self.y0) // self.res), int((p[0] - self.x0) // self.res)

    def _center(self, c: tuple[int, int]) -> Point:
        return (self.x0 + (c[1] + 0.5) * self.res, self.y0 + (c[0] + 0.5) * self.res)

    def _blocked_cell(self, c: tuple[int, int]) -> bool:
        assert self.grid is not None
        r, k = c
        if r < 0 or k < 0 or r >= self.grid.shape[0] or k >= self.grid.shape[1]:
            return False  # outside the mapped area: open ground
        return bool(self.grid[r, k])

    def inside_obstacle(self, p: Point) -> bool:
        return any(_point_in(p, poly) for poly in self.obstacles)

    def segment_clear(self, a: Point, b: Point, ignore: set[tuple[int, int]] = frozenset()) -> bool:
        if self.grid is None:
            return True
        n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / (self.res * 0.25)))
        for i in range(n + 1):
            t = i / n
            c = self._cell((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            if c not in ignore and self._blocked_cell(c):
                return False
        return True

    def plan(self, start: Point, goal: Point) -> list[Point] | None:
        """Waypoints after ``start`` ending exactly at ``goal``; None if unreachable."""
        if self.grid is None:
            return [goal]
        if self.inside_obstacle(goal):
            return None
        s, g = self._cell(start), self._cell(goal)
        # The robot may start (or a goal may sit) within the inflation band next to a wall.
        ignore = {c for c in (s, g) if self._blocked_cell(c)}
        if self.segment_clear(start, goal, ignore):
            return [goal]
        cells = self._astar(s, g, ignore)
        if cells is None:
            return None
        pts = [start] + [self._center(c) for c in cells[1:-1]] + [goal]
        # Line-of-sight smoothing.
        out, i = [], 0
        while i < len(pts) - 1:
            j = len(pts) - 1
            while j > i + 1 and not self.segment_clear(pts[i], pts[j], ignore):
                j -= 1
            out.append(pts[j])
            i = j
        return out

    def _astar(self, s, g, ignore):
        def h(c):
            return math.hypot(c[0] - g[0], c[1] - g[1])

        steps = [
            (-1, 0, 1.0),
            (1, 0, 1.0),
            (0, -1, 1.0),
            (0, 1, 1.0),
            (-1, -1, math.sqrt(2)),
            (-1, 1, math.sqrt(2)),
            (1, -1, math.sqrt(2)),
            (1, 1, math.sqrt(2)),
        ]
        frontier = [(h(s), 0.0, s)]
        came: dict = {s: None}
        cost = {s: 0.0}
        limit = 400_000
        while frontier and limit:
            limit -= 1
            _, gcost, c = heapq.heappop(frontier)
            if c == g:
                path = [c]
                while came[path[-1]] is not None:
                    path.append(came[path[-1]])
                return path[::-1]
            if gcost > cost.get(c, math.inf):
                continue
            for dr, dk, w in steps:
                n = (c[0] + dr, c[1] + dk)
                if n not in ignore and self._blocked_cell(n):
                    continue
                if (
                    dr
                    and dk
                    and (
                        self._blocked_cell((c[0] + dr, c[1]))
                        or self._blocked_cell((c[0], c[1] + dk))
                    )
                ):
                    continue  # no corner cutting
                ng = gcost + w
                if ng < cost.get(n, math.inf):
                    cost[n] = ng
                    came[n] = c
                    heapq.heappush(frontier, (ng + h(n), ng, n))
        return None


def _point_in(p: Point, poly: Sequence[Point]) -> bool:
    x, y = p
    inside = False
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _inside(gx: np.ndarray, gy: np.ndarray, poly: Sequence[Point]) -> np.ndarray:
    inside = np.zeros(gx.shape, dtype=bool)
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if y1 == y2:
            continue
        cond = (y1 > gy) != (y2 > gy)
        xint = x1 + (gy - y1) * (x2 - x1) / (y2 - y1)
        inside ^= cond & (gx < xint)
    return inside


def _edge_distance(gx: np.ndarray, gy: np.ndarray, poly: Sequence[Point]) -> np.ndarray:
    best = np.full(gx.shape, np.inf)
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy or 1e-12
        t = np.clip(((gx - x1) * dx + (gy - y1) * dy) / length2, 0.0, 1.0)
        best = np.minimum(best, np.hypot(gx - x1 - t * dx, gy - y1 - t * dy))
    return best
