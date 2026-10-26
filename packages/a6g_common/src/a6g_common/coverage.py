"""Coverage levels from the network twin (SPEC A4.6): the map, its calibration and lookup.

Sources (``network.coverage.source``):

* ``model``: RSRP from the 3GPP TR 38.901 UMa path loss of the campus cell (the n40 gNB
  on Building 8G), LOS or NLOS decided deterministically by whether the line from the site
  to the point crosses a building footprint (no shadow fading), with one RSRP offset
  calibrated so that the median on the velodrome loop matches UPV's plots;
* ``radio_dt``: the RSRP map exported from UPV's ray-tracing radio twin (CSV), interpolated
  to the footpaths;
* ``shadows``: today's binary shadow polygons (regression).

SINR comes from RSRP with the offset read from the plots (or from the export); the level
from SINR thresholds, with ``lost`` below the degraded threshold or below a minimum RSRP.
The map holds a sample every ``sample_step_m`` along the footpaths (where the robots work)
and one every ``grid_step_m`` over the rest of the pilot area, outdoors (robots may be sent
anywhere later; 2026-10-26); a point near a footpath takes the footpath's level. It is
deterministic (generator rule of SPEC A1.5).
"""

import csv
import itertools
import json
import math
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from a6g_common.geometry import point_in_polygon
from a6g_common.netconfig import LEVELS, NetworkConfig

C = 299_792_458.0
MODEL_VERSION = "38901-uma-3d-los-sector/6"  # /4: sector pattern, setback, foliage


# --------------------------------------------------------------------------- radio model


def pathloss_uma(d2d_m: float, h_bs: float, h_ut: float, fc_ghz: float, los: bool) -> float:
    """3GPP TR 38.901 Table 7.4.1-1, UMa (d2D clamped to 10 m, no shadow fading)."""
    d2d = max(d2d_m, 10.0)
    d3d = math.hypot(d2d, h_bs - h_ut)
    h_e = 1.0
    d_bp = 4 * (h_bs - h_e) * (h_ut - h_e) * fc_ghz * 1e9 / C
    if d2d <= d_bp:
        pl_los = 28.0 + 22.0 * math.log10(d3d) + 20.0 * math.log10(fc_ghz)
    else:
        pl_los = (
            28.0
            + 40.0 * math.log10(d3d)
            + 20.0 * math.log10(fc_ghz)
            - 9.0 * math.log10(d_bp**2 + (h_bs - h_ut) ** 2)
        )
    if los:
        return pl_los
    pl_nlos = 13.54 + 39.08 * math.log10(d3d) + 20.0 * math.log10(fc_ghz) - 0.6 * (h_ut - 1.5)
    return max(pl_los, pl_nlos)


class Footprints:
    """Buildings in 3D for the line-of-sight test: footprints with heights (OSM ``height``,
    else levels x 3 m, else the default). The straight line from the antenna to the robot's
    antenna is blocked where it crosses a footprint below that building's roof. Trees are
    not obstructions. Heights are real metres; on a scaled campus only the plan shrinks."""

    def __init__(
        self,
        polygons: list[list[tuple[float, float]]],
        heights: list[float | None] | None = None,
        default_height_m: float = 10.0,
    ):
        heights = heights or []
        self.polys: list[list[tuple[float, float]]] = []
        self.heights: list[float] = []
        for i, p in enumerate(polygons):
            h = heights[i] if i < len(heights) else default_height_m
            if len(p) >= 3 and h is not None:  # None: a tree
                self.polys.append(p)
                self.heights.append(float(h))
        self.boxes = [
            (min(x for x, _ in p), min(y for _, y in p), max(x for x, _ in p), max(y for _, y in p))
            for p in self.polys
        ]

    def blocked(
        self, a: tuple[float, float], b: tuple[float, float], h_a: float = 1e9, h_b: float = 0.0
    ) -> bool:
        """Whether the 3D segment from ``a`` at ``h_a`` m to ``b`` at ``h_b`` m passes
        through a building."""
        lo_x, hi_x = min(a[0], b[0]), max(a[0], b[0])
        lo_y, hi_y = min(a[1], b[1]), max(a[1], b[1])
        for poly, height, (x0, y0, x1, y1) in zip(
            self.polys, self.heights, self.boxes, strict=True
        ):
            if x1 < lo_x or x0 > hi_x or y1 < lo_y or y0 > hi_y:
                continue
            if point_in_polygon(b, poly) and h_b < height:
                return True
            for i in range(len(poly)):
                t = _crossing(a, b, poly[i], poly[(i + 1) % len(poly)])
                if t is not None and h_a + (h_b - h_a) * t < height:
                    return True
        return False


def _crossing(
    p1: tuple[float, float],
    p2: tuple[float, float],
    q1: tuple[float, float],
    q2: tuple[float, float],
) -> float | None:
    """Where segment p1-p2 crosses segment q1-q2, as the fraction along p1-p2 (None: no)."""
    rx, ry = p2[0] - p1[0], p2[1] - p1[1]
    sx, sy = q2[0] - q1[0], q2[1] - q1[1]
    den = rx * sy - ry * sx
    if abs(den) < 1e-12:
        return None
    qpx, qpy = q1[0] - p1[0], q1[1] - p1[1]
    t = (qpx * sy - qpy * sx) / den
    u = (qpx * ry - qpy * rx) / den
    return t if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0 else None


@dataclass
class RadioModel:
    """RSRP (dBm) at a campus_map point from the first cell of ``network.yaml``: 38.901 UMa
    path loss with LOS/NLOS from 3D buildings, the sector's antenna pattern, and foliage."""

    net: NetworkConfig
    site_xy: tuple[float, float]  # the antenna (after the setback from the roof edge)
    site_height_m: float
    footprints: Footprints
    scale: float = 1.0
    offset_db: float = 0.0
    #: (polygon, tree-lined paths, half width, canopy height): see ``in_canopy``
    vegetation: list[
        tuple[list[tuple[float, float]], list[list[tuple[float, float]]], float, float]
    ] = field(default_factory=list)

    def geometry(self, x: float, y: float) -> tuple[float, float, float]:
        """(2D distance m, bearing deg from north, depression deg below the horizon)."""
        dx, dy = (x - self.site_xy[0]) / self.scale, (y - self.site_xy[1]) / self.scale
        d2d = math.hypot(dx, dy)
        bearing = math.degrees(math.atan2(dx, dy)) % 360.0
        h_ut = self.net.coverage.model.h_ut_m
        depression = math.degrees(math.atan2(self.site_height_m - h_ut, max(d2d, 1.0)))
        return d2d, bearing, depression

    def foliage_db(self, x: float, y: float) -> float:
        """Excess loss along the part of the ray inside a canopy (1 m steps)."""
        if not self.vegetation:
            return 0.0
        veg = self.net.coverage.vegetation
        h_ut = self.net.coverage.model.h_ut_m
        d = math.dist(self.site_xy, (x, y))
        n = max(1, int(d / max(self.scale, 1e-6)))
        inside = 0.0
        for k in range(n + 1):
            f = k / n
            px = self.site_xy[0] + (x - self.site_xy[0]) * f
            py = self.site_xy[1] + (y - self.site_xy[1]) * f
            h = self.site_height_m + (h_ut - self.site_height_m) * f
            for poly, paths, half_width, canopy in self.vegetation:
                if h <= canopy and in_canopy((px, py), poly, paths, half_width):
                    inside += d / self.scale / n
                    break
        under = 0.0
        for poly, paths, half_width, _ in self.vegetation:  # the robot under the trees
            if in_canopy((x, y), poly, paths, half_width):
                under = float(veg.get("under_canopy_db", 0.0))
                break
        return min(inside * veg["loss_db_per_m"] + under, veg["max_loss_db"])

    def rsrp(self, x: float, y: float) -> tuple[float, bool]:
        cell = self.net.cells[0]
        d2d, bearing, depression = self.geometry(x, y)
        h_ut = self.net.coverage.model.h_ut_m
        los = not (
            self.net.coverage.model.los_from_footprints
            and self.footprints.blocked(self.site_xy, (x, y), self.site_height_m, h_ut)
        )
        pl = pathloss_uma(d2d, self.site_height_m, h_ut, cell.frequency_mhz / 1000, los)
        gain = cell.antenna.gain_dbi + cell.antenna.pattern_db(bearing, depression)
        return cell.rs_epre_dbm + gain + self.offset_db - pl - self.foliage_db(x, y), los


def _to_segment(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / ((dx * dx + dy * dy) or 1.0)
    t = max(0.0, min(1.0, t))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def in_canopy(
    p: tuple[float, float],
    poly: list[tuple[float, float]],
    paths: list[list[tuple[float, float]]],
    half_width: float,
) -> bool:
    """Inside a vegetation polygon, or within ``half_width`` of a tree-lined path."""
    if len(poly) >= 3 and point_in_polygon(p, poly):
        return True
    return any(
        _to_segment(p, a, b) <= half_width for path in paths for a, b in itertools.pairwise(path)
    )


def sinr_from_rsrp(net: NetworkConfig, rsrp: float) -> float:
    s = net.coverage.sinr_from_rsrp
    return min(max(rsrp + s.offset_db, s.clip_db[0]), s.clip_db[1])


def level_of(net: NetworkConfig, sinr: float, rsrp: float | None = None) -> str:
    t = net.coverage.thresholds_sinr_db
    if rsrp is not None and rsrp < net.coverage.lost_rsrp_dbm:
        return "lost"
    if sinr >= t["good"]:
        return "good"
    if sinr >= t["medium"]:
        return "medium"
    if sinr >= t["low"]:
        return "low"
    return "lost"


# --------------------------------------------------------------------------- sampling


def sample_polyline(points: list[tuple[float, float]], step: float) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    carry = 0.0
    for a, b in itertools.pairwise(points):
        seg = math.dist(a, b)
        t = carry
        while t <= seg:
            f = t / seg if seg else 0.0
            out.append((round(a[0] + (b[0] - a[0]) * f, 3), round(a[1] + (b[1] - a[1]) * f, 3)))
            t += step
        carry = t - seg
    return out


def footpath_samples(campus: Any, step: float) -> list[tuple[str, float, float]]:
    """(edge id, x, y) every ``step`` metres along every footpath edge, deterministic."""
    out = []
    graph = getattr(campus, "route_graph", None)
    if graph is None:
        return out
    for e in sorted(graph.edges, key=lambda e: e.id):
        for x, y in sample_polyline([tuple(p) for p in e.geometry], step * campus.scale):
            out.append((e.id, x, y))
    return out


def loop_samples(campus: Any, step: float) -> list[tuple[float, float]]:
    loops = getattr(campus, "calibration_loops", None) or []
    out: list[tuple[float, float]] = []
    for loop in loops:
        poly = [tuple(p) for p in loop["polygon"]]
        out += sample_polyline([*poly, poly[0]], step * campus.scale)
    return out


def _site(campus: Any, net: NetworkConfig) -> dict[str, Any]:
    want = net.cells[0].site if net.cells else None
    sites = getattr(campus, "network_sites", None) or []
    for s in sites:
        if s["site_id"] == want:
            return s
    if sites:
        return sites[0]
    raise ValueError("no network_sites in the campus file (SPEC A4.6.1)")


def antenna_xy(campus: Any, net: NetworkConfig) -> tuple[float, float]:
    """The antenna: the given site moved ``setback_m`` back from the roof edge, away from
    where the sector points (into the building it stands on)."""
    site = _site(campus, net)
    ant = net.cells[0].antenna
    if ant.azimuth_deg is None or not ant.setback_m:
        return site["x"], site["y"]
    az = math.radians(ant.azimuth_deg)
    back = ant.setback_m * (campus.scale or 1.0)
    return (
        round(site["x"] - back * math.sin(az), 3),
        round(site["y"] - back * math.cos(az), 3),
    )


def radio_model(campus: Any, net: NetworkConfig, offset_db: float = 0.0) -> RadioModel:
    site = _site(campus, net)
    xy = antenna_xy(campus, net)
    canopy = net.coverage.vegetation["canopy_height_m"]
    return RadioModel(
        vegetation=[
            (
                [tuple(p) for p in v.get("polygon") or []],
                [[tuple(p) for p in path] for path in v.get("paths") or []],
                float(v.get("half_width_m") or 0.0),
                float(v.get("canopy_height_m") or canopy),
            )
            for v in getattr(campus, "vegetation_areas", None) or []
            if len(v.get("polygon") or []) >= 3 or v.get("paths")
        ],
        net=net,
        site_xy=xy,
        site_height_m=float(site.get("height_m") or 20.0),
        footprints=Footprints(
            [[tuple(p) for p in o] for o in campus.obstacles or []],
            list(getattr(campus, "obstacle_heights", None) or []),
        ),
        scale=campus.scale or 1.0,
        offset_db=offset_db,
    )


# --------------------------------------------------------------------------- calibration


def calibrate(campus: Any, net: NetworkConfig) -> dict[str, Any]:
    """One RSRP offset fitted (least squares) to the plotted minimum, median and maximum on
    the velodrome loop (no measurement CSV: values read from UPV's plots)."""
    model = radio_model(campus, net)
    pts = loop_samples(campus, net.coverage.sample_step_m)
    if not pts:
        raise ValueError("no calibration_loops in the campus file")
    raw = [model.rsrp(x, y) for x, y in pts]
    target = net.coverage.calibration_targets
    # least squares over the plotted min, median and max (the median alone left the line-
    # of-sight spots 14 dB above the plotted maximum, 2026-10-26)
    raw_r = [r for r, _ in raw]
    offset = statistics.fmean(
        [
            target.rsrp_dbm["min"] - min(raw_r),
            target.rsrp_dbm["median"] - statistics.median(raw_r),
            target.rsrp_dbm["max"] - max(raw_r),
        ]
    )
    rsrp = [r + offset for r, _ in raw]
    sinr = [sinr_from_rsrp(net, r) for r in rsrp]
    spread = max(rsrp) - min(rsrp)
    plotted = target.rsrp_dbm["max"] - target.rsrp_dbm["min"]
    return {
        "offset_db": round(offset, 2),
        "loop_samples": len(pts),
        "los": sum(1 for _, los in raw if los),
        "nlos": sum(1 for _, los in raw if not los),
        "rsrp_dbm": {
            "median": round(statistics.median(rsrp), 1),
            "min": round(min(rsrp), 1),
            "max": round(max(rsrp), 1),
            "plotted": target.rsrp_dbm,
        },
        "sinr_db": {
            "min": round(min(sinr), 1),
            "max": round(max(sinr), 1),
            "plotted": target.sinr_db,
        },
        "spread_db": round(spread, 1),
        "plotted_spread_db": plotted,
        "spread_ok": plotted / 2 <= spread <= plotted * 2,
    }


# --------------------------------------------------------------------------- the map


@dataclass
class CoverageMap:
    """Samples along the footpaths, with a grid index for nearest-sample lookup."""

    samples: list[dict[str, Any]]
    meta: dict[str, Any] = field(default_factory=dict)
    cell_m: float = 5.0
    _grid: dict[tuple[int, int], list[int]] = field(default_factory=dict, repr=False)

    _area: dict[tuple[int, int], list[int]] = field(default_factory=dict, repr=False)
    #: a point this close to a footpath sample takes the footpath's level (robots on paths
    #: see exactly what they saw before the area grid existed)
    path_snap_m: float = 3.0

    def __post_init__(self) -> None:
        for i, s in enumerate(self.samples):
            index = self._area if s.get("kind") == "area" else self._grid
            index.setdefault(self._key(s["x"], s["y"]), []).append(i)

    @property
    def path_samples(self) -> list[dict[str, Any]]:
        return [s for s in self.samples if s.get("kind") != "area"]

    @property
    def area_samples(self) -> list[dict[str, Any]]:
        return [s for s in self.samples if s.get("kind") == "area"]

    def _key(self, x: float, y: float) -> tuple[int, int]:
        return (math.floor(x / self.cell_m), math.floor(y / self.cell_m))

    def nearest(self, x: float, y: float, max_rings: int = 6) -> dict[str, Any] | None:
        """The footpath sample within ``path_snap_m``, else the nearest area sample (any
        point of the pilot area), else the nearest footpath sample."""
        path = self._nearest(self._grid, x, y, max_rings)
        if path is not None and math.dist((path["x"], path["y"]), (x, y)) <= self.path_snap_m:
            return path
        area = self._nearest(self._area, x, y, max_rings) if self._area else None
        if area is None:
            return path
        if path is None:
            return area
        return min((path, area), key=lambda s: math.dist((s["x"], s["y"]), (x, y)))

    def _nearest(
        self, index: dict[tuple[int, int], list[int]], x: float, y: float, max_rings: int
    ) -> dict[str, Any] | None:
        kx, ky = self._key(x, y)
        best, best_d = None, math.inf
        for r in range(max_rings + 1):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    for i in index.get((kx + dx, ky + dy), []):
                        s = self.samples[i]
                        d = (s["x"] - x) ** 2 + (s["y"] - y) ** 2
                        if d < best_d:
                            best, best_d = s, d
            if best is not None and math.sqrt(best_d) <= r * self.cell_m:
                return best
        return best

    def summary(self, campus: Any) -> dict[str, Any]:
        """Share of footpath length per level and per sector; the level at each POI,
        the staging area and every link-recovery point."""
        step = self.meta.get("sample_step_m", 2.0)
        paths = self.path_samples
        total = len(paths) or 1
        share = {
            lvl: round(sum(1 for s in paths if s["level"] == lvl) / total, 4) for lvl in LEVELS
        }
        # the model's own levels (without the injected placeholder shadows)
        model_share = {
            lvl: round(sum(1 for s in paths if s.get("model_level", s["level"]) == lvl) / total, 4)
            for lvl in LEVELS
        }
        sectors: dict[str, dict[str, float]] = {}
        for sec in getattr(campus, "sectors", []) or []:
            edges = set(getattr(sec, "path_edges", []) or [])
            mine = [s for s in paths if s["edge"] in edges]
            if mine:
                sectors[sec.sector_id] = {
                    lvl: round(sum(1 for s in mine if s["level"] == lvl) / len(mine), 4)
                    for lvl in LEVELS
                }

        def at(x: float, y: float) -> str | None:
            s = self.nearest(x, y)
            return s["level"] if s else None

        pois = {p.poi_id: at(p.pose.x, p.pose.y) for p in campus.pois}
        exposed = {p.poi_id: pois[p.poi_id] for p in campus.pois if getattr(p, "exposed", False)}
        lrps = {r.point_id: at(r.pose.x, r.pose.y) for r in campus.link_recovery_points}
        shadows = getattr(campus, "link_shadows", []) or []
        in_shadow = [
            s for s in paths if any(point_in_polygon((s["x"], s["y"]), sh) for sh in shadows)
        ]
        model_lost = [s for s in paths if s.get("model_level") == "lost"]
        return {
            "footpath_m": round(len(paths) * step, 1),
            "area": self._area_summary(),
            "share_by_level": share,
            "share_by_level_without_injected_shadows": model_share,
            "share_by_sector": sectors,
            "pois": pois,
            "exposed_pois": exposed,
            "staging": at(campus.staging.x, campus.staging.y),
            "link_recovery_points": lrps,
            "shadow_overlap": {
                "samples_in_old_shadows": len(in_shadow),
                "of_which_model_lost": sum(1 for s in in_shadow if s.get("model_level") == "lost"),
                "model_lost_samples": len(model_lost),
                "model_lost_outside_old_shadows": sum(
                    1
                    for s in model_lost
                    if not any(point_in_polygon((s["x"], s["y"]), sh) for sh in shadows)
                ),
            },
            "mostly_good": model_share["good"] > 0.8,
            "exposed_below_good": sorted(k for k, v in exposed.items() if v != "good"),
        }

    def _area_summary(self) -> dict[str, Any]:
        """Share of the pilot area's outdoor ground per level (grid samples)."""
        area = self.area_samples
        if not area:
            return {}
        n = len(area)
        step = float(self.meta.get("grid_step_m") or 0.0)
        return {
            "samples": n,
            "grid_step_m": step,
            "outdoor_m2": round(n * step * step, 0),
            "share_by_level": {
                lvl: round(sum(1 for s in area if s["level"] == lvl) / n, 4) for lvl in LEVELS
            },
        }

    def to_json(self) -> dict[str, Any]:
        return {"meta": self.meta, "samples": self.samples}

    @classmethod
    def load(cls, path: Path) -> "CoverageMap":
        data = json.loads(path.read_text())
        return cls(samples=data["samples"], meta=data.get("meta") or {})


def _classify(
    net: NetworkConfig, campus: Any, x: float, y: float, rsrp: float, sinr: float
) -> dict[str, Any]:
    level = level_of(net, sinr, rsrp)
    out = {"level": level, "model_level": level, "injected": False}
    if net.coverage.shadows_override and any(
        point_in_polygon((x, y), sh) for sh in getattr(campus, "link_shadows", []) or []
    ):
        out.update(level="lost", injected=level != "lost")  # placeholder shadows stay lost
    return out


def area_points(campus: Any, step: float) -> list[tuple[float, float]]:
    """A grid over the pilot area, outdoors only (UC2): points inside a building footprint
    are left out. Deterministic."""
    area = getattr(campus, "pilot_area", None)
    if not area or step <= 0:
        return []
    poly = [tuple(p) for p in area]
    x0, x1 = min(p[0] for p in poly), max(p[0] for p in poly)
    y0, y1 = min(p[1] for p in poly), max(p[1] for p in poly)
    buildings = [[tuple(p) for p in o] for o in campus.obstacles or [] if len(o) >= 3]
    boxes = [
        (min(x for x, _ in b), min(y for _, y in b), max(x for x, _ in b), max(y for _, y in b))
        for b in buildings
    ]
    out = []
    nx, ny = int((x1 - x0) / step), int((y1 - y0) / step)
    for i in range(nx + 1):
        for j in range(ny + 1):
            x, y = round(x0 + i * step, 3), round(y0 + j * step, 3)
            if not point_in_polygon((x, y), poly):
                continue
            if any(
                bx0 <= x <= bx1 and by0 <= y <= by1 and point_in_polygon((x, y), b)
                for b, (bx0, by0, bx1, by1) in zip(buildings, boxes, strict=True)
            ):
                continue
            out.append((x, y))
    return out


def build_model_map(campus: Any, net: NetworkConfig, calibration: dict[str, Any]) -> CoverageMap:
    model = radio_model(campus, net, calibration["offset_db"])
    cell = net.cells[0].id
    samples = []
    points = [("path", e, x, y) for e, x, y in footpath_samples(campus, net.coverage.sample_step_m)]
    step = net.coverage.grid_step_m * (campus.scale or 1.0)
    points += [("area", None, x, y) for x, y in area_points(campus, step)]
    for kind, edge, x, y in points:
        rsrp, los = model.rsrp(x, y)
        sinr = sinr_from_rsrp(net, rsrp)
        samples.append(
            {
                "kind": kind,
                "edge": edge,
                "x": x,
                "y": y,
                "cell": cell,
                "rsrp": round(rsrp, 1),
                "sinr": round(sinr, 1),
                "los": los,
                **_classify(net, campus, x, y, rsrp, sinr),
            }
        )
    return CoverageMap(
        samples=samples,
        meta={
            "source": "model",
            "model_version": MODEL_VERSION,
            "label": "model (38.901 UMa, 3D buildings, one sector, foliage; offset fitted to the "
            "plotted velodrome RSRP min/median/max; SINR from the plots); NLOS extrapolated",
            "calibration": calibration,
            "sample_step_m": net.coverage.sample_step_m,
            "grid_step_m": net.coverage.grid_step_m,
            "cell": cell,
            "assumption": "uplink quality follows the plotted (downlink) SINR",
            "shadows_override": net.coverage.shadows_override,
        },
    )


def build_radio_dt_map(campus: Any, net: NetworkConfig, csv_path: Path, geo: Any) -> CoverageMap:
    """Interpolate the radio twin's RSRP export (lat, lon, cell_id, rsrp_dbm[, sinr_db]) to
    the footpath samples: inverse-distance weighting of the 4 nearest points per cell; the
    serving cell is the strongest."""
    rows: dict[str, list[tuple[float, float, float, float | None]]] = {}
    with csv_path.open() as fh:
        for r in csv.DictReader(fh):
            x, y = geo(float(r["lat"]), float(r["lon"]))
            sinr = float(r["sinr_db"]) if r.get("sinr_db") not in (None, "") else None
            rows.setdefault(r.get("cell_id") or "cell", []).append(
                (x, y, float(r["rsrp_dbm"]), sinr)
            )
    samples = []
    points = [("path", e, x, y) for e, x, y in footpath_samples(campus, net.coverage.sample_step_m)]
    step = net.coverage.grid_step_m * (campus.scale or 1.0)
    points += [("area", None, x, y) for x, y in area_points(campus, step)]
    for kind, edge, x, y in points:
        best: tuple[str, float, float | None] | None = None
        for cell, pts in rows.items():
            near = sorted(pts, key=lambda p: (p[0] - x) ** 2 + (p[1] - y) ** 2)[:4]
            w = [1.0 / max((p[0] - x) ** 2 + (p[1] - y) ** 2, 1e-6) for p in near]
            rsrp = sum(wi * p[2] for wi, p in zip(w, near, strict=True)) / sum(w)
            sinrs = [(wi, p[3]) for wi, p in zip(w, near, strict=True) if p[3] is not None]
            sinr = sum(wi * s for wi, s in sinrs) / sum(wi for wi, _ in sinrs) if sinrs else None
            if best is None or rsrp > best[1]:
                best = (cell, rsrp, sinr)
        if best is None:
            continue
        cell, rsrp, sinr = best
        sinr = sinr if sinr is not None else sinr_from_rsrp(net, rsrp)
        samples.append(
            {
                "kind": kind,
                "edge": edge,
                "x": x,
                "y": y,
                "cell": cell,
                "rsrp": round(rsrp, 1),
                "sinr": round(sinr, 1),
                "los": None,
                **_classify(net, campus, x, y, rsrp, sinr),
            }
        )
    return CoverageMap(
        samples=samples,
        meta={
            "source": "radio_dt",
            "label": f"radio DT export {csv_path.name} (UPV ray tracing, calibrated by campaigns)",
            "sample_step_m": net.coverage.sample_step_m,
            "grid_step_m": net.coverage.grid_step_m,
            "cells": sorted(rows),
            "shadows_override": net.coverage.shadows_override,
        },
    )


def map_path(cfg: Any) -> Path:
    name = cfg.network.coverage.map_file.format(
        campus=cfg.campus.world_name or cfg.campus.campus_id
    )
    return (Path(cfg.config_dir) / name).resolve()


def load_or_build(cfg: Any) -> CoverageMap:
    """The coverage map of ``network.coverage.source`` (``model``: built and cached)."""
    path = map_path(cfg)
    source = cfg.network.coverage.source
    if path.exists():
        cmap = CoverageMap.load(path)
        if cmap.meta.get("source") == source and (
            source != "model" or cmap.meta.get("model_version") == MODEL_VERSION
        ):
            return cmap
    if source == "model":
        cmap = build_model_map(cfg.campus, cfg.network, calibrate(cfg.campus, cfg.network))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cmap.to_json(), separators=(",", ":")))
        return cmap
    raise FileNotFoundError(f"coverage map {path} missing: run `a6g network import-coverage`")


# --------------------------------------------------------------------------- per robot


@dataclass
class LevelTracker:
    """A robot's serving cell and level, with hysteresis on SINR and a minimum dwell (in
    simulated seconds) before a change takes effect."""

    net: NetworkConfig
    level: str | None = None
    cell: str | None = None
    _pending: str | None = None
    _since: datetime | None = None

    def update(self, sample: dict[str, Any], now: datetime) -> str:
        raw = sample["level"]
        if self.level is None:
            self.level, self.cell = raw, sample.get("cell")
            return raw
        if raw != self.level and not sample.get("injected") and sample["level"] != "lost":
            # hysteresis: a better level needs the SINR beyond its threshold by the margin
            better = LEVELS.index(raw) < LEVELS.index(self.level)
            t = self.net.coverage.thresholds_sinr_db
            edge = t.get(raw if better else self.level, -5.0)
            margin = self.net.coverage.hysteresis_db / 2
            if (better and sample["sinr"] < edge + margin) or (
                not better and sample["sinr"] > edge - margin
            ):
                raw = self.level
        if raw == self.level:
            self._pending, self._since = None, None
            return self.level
        if raw != self._pending:
            self._pending, self._since = raw, now
        if (
            self._since is not None
            and (now - self._since).total_seconds() >= self.net.coverage.min_dwell_s
        ):
            self.level, self.cell = raw, sample.get("cell")
            self._pending, self._since = None, None
        return self.level


def quality_of(level: str) -> str:
    """``link_quality`` derived from the level (the link-loss policy acts on ``lost``)."""
    return {"lost": "lost", "low": "degraded"}.get(level, "ok")


def levels(samples: Iterable[dict[str, Any]]) -> dict[str, int]:
    out = dict.fromkeys(LEVELS, 0)
    for s in samples:
        out[s["level"]] += 1
    return out
