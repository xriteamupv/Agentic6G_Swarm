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
The map holds a sample every ``sample_step_m`` along the footpaths (robots move only on
footpaths); it is deterministic (generator rule of SPEC A1.5).
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
MODEL_VERSION = "38901-uma-footprint-los/1"


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


def _segments_cross(
    p1: tuple[float, float],
    p2: tuple[float, float],
    q1: tuple[float, float],
    q2: tuple[float, float],
) -> bool:
    def orient(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1, d2 = orient(q1, q2, p1), orient(q1, q2, p2)
    d3, d4 = orient(p1, p2, q1), orient(p1, p2, q2)
    return (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0)


class Footprints:
    """Building footprints for the LOS test; the site's own building (rooftop antenna)
    never blocks."""

    def __init__(self, polygons: list[list[tuple[float, float]]], site: tuple[float, float]):
        self.polys = [p for p in polygons if len(p) >= 3 and not point_in_polygon(site, p)]
        self.boxes = [
            (min(x for x, _ in p), min(y for _, y in p), max(x for x, _ in p), max(y for _, y in p))
            for p in self.polys
        ]

    def blocked(self, a: tuple[float, float], b: tuple[float, float]) -> bool:
        lo_x, hi_x = min(a[0], b[0]), max(a[0], b[0])
        lo_y, hi_y = min(a[1], b[1]), max(a[1], b[1])
        for poly, (x0, y0, x1, y1) in zip(self.polys, self.boxes, strict=True):
            if x1 < lo_x or x0 > hi_x or y1 < lo_y or y0 > hi_y:
                continue
            if point_in_polygon(b, poly):
                return True
            for i in range(len(poly)):
                if _segments_cross(a, b, poly[i], poly[(i + 1) % len(poly)]):
                    return True
        return False


@dataclass
class RadioModel:
    """RSRP (dBm) at a campus_map point from the first cell of ``network.yaml``."""

    net: NetworkConfig
    site_xy: tuple[float, float]
    site_height_m: float
    footprints: Footprints
    scale: float = 1.0
    offset_db: float = 0.0

    def rsrp(self, x: float, y: float) -> tuple[float, bool]:
        cell = self.net.cells[0]
        d2d = math.dist(self.site_xy, (x, y)) / self.scale  # real metres on a scaled campus
        los = not (
            self.net.coverage.model.los_from_footprints
            and self.footprints.blocked(self.site_xy, (x, y))
        )
        pl = pathloss_uma(
            d2d, self.site_height_m, self.net.coverage.model.h_ut_m, cell.frequency_mhz / 1000, los
        )
        return cell.rs_epre_dbm + cell.antenna.gain_dbi + self.offset_db - pl, los


def sinr_from_rsrp(net: NetworkConfig, rsrp: float) -> float:
    s = net.coverage.sinr_from_rsrp
    return min(max(rsrp + s.offset_db, s.clip_db[0]), s.clip_db[1])


def level_of(net: NetworkConfig, sinr: float, rsrp: float | None = None) -> str:
    t = net.coverage.thresholds_sinr_db
    if rsrp is not None and rsrp < net.coverage.lost_rsrp_dbm:
        return "lost"
    if sinr >= t["good"]:
        return "good"
    if sinr >= t["cell_edge"]:
        return "cell_edge"
    if sinr >= t["degraded"]:
        return "degraded"
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


def radio_model(campus: Any, net: NetworkConfig, offset_db: float = 0.0) -> RadioModel:
    site = _site(campus, net)
    xy = (site["x"], site["y"])
    return RadioModel(
        net=net,
        site_xy=xy,
        site_height_m=float(site.get("height_m") or 20.0),
        footprints=Footprints([[tuple(p) for p in o] for o in campus.obstacles or []], xy),
        scale=campus.scale or 1.0,
        offset_db=offset_db,
    )


# --------------------------------------------------------------------------- calibration


def calibrate(campus: Any, net: NetworkConfig) -> dict[str, Any]:
    """One RSRP offset so that the model's median on the velodrome loop equals the plotted
    median (no measurement CSV: values read from UPV's plots)."""
    model = radio_model(campus, net)
    pts = loop_samples(campus, net.coverage.sample_step_m)
    if not pts:
        raise ValueError("no calibration_loops in the campus file")
    raw = [model.rsrp(x, y) for x, y in pts]
    target = net.coverage.calibration_targets
    offset = target.rsrp_dbm["median"] - statistics.median(r for r, _ in raw)
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

    def __post_init__(self) -> None:
        for i, s in enumerate(self.samples):
            self._grid.setdefault(self._key(s["x"], s["y"]), []).append(i)

    def _key(self, x: float, y: float) -> tuple[int, int]:
        return (math.floor(x / self.cell_m), math.floor(y / self.cell_m))

    def nearest(self, x: float, y: float, max_rings: int = 6) -> dict[str, Any] | None:
        kx, ky = self._key(x, y)
        best, best_d = None, math.inf
        for r in range(max_rings + 1):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    for i in self._grid.get((kx + dx, ky + dy), []):
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
        total = len(self.samples) or 1
        share = {
            lvl: round(sum(1 for s in self.samples if s["level"] == lvl) / total, 4)
            for lvl in LEVELS
        }
        # the model's own levels (without the injected placeholder shadows)
        model_share = {
            lvl: round(
                sum(1 for s in self.samples if s.get("model_level", s["level"]) == lvl) / total, 4
            )
            for lvl in LEVELS
        }
        sectors: dict[str, dict[str, float]] = {}
        for sec in getattr(campus, "sectors", []) or []:
            edges = set(getattr(sec, "path_edges", []) or [])
            mine = [s for s in self.samples if s["edge"] in edges]
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
            s for s in self.samples if any(point_in_polygon((s["x"], s["y"]), sh) for sh in shadows)
        ]
        model_lost = [s for s in self.samples if s.get("model_level") == "lost"]
        return {
            "footpath_m": round(len(self.samples) * step, 1),
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


def build_model_map(campus: Any, net: NetworkConfig, calibration: dict[str, Any]) -> CoverageMap:
    model = radio_model(campus, net, calibration["offset_db"])
    cell = net.cells[0].id
    samples = []
    for edge, x, y in footpath_samples(campus, net.coverage.sample_step_m):
        rsrp, los = model.rsrp(x, y)
        sinr = sinr_from_rsrp(net, rsrp)
        samples.append(
            {
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
            "label": "model (38.901 UMa, offset matched to the plotted velodrome RSRP; SINR from "
            "the plots); footpaths behind buildings (NLOS) extrapolated",
            "calibration": calibration,
            "sample_step_m": net.coverage.sample_step_m,
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
    for edge, x, y in footpath_samples(campus, net.coverage.sample_step_m):
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
    return {"lost": "lost", "degraded": "degraded"}.get(level, "ok")


def levels(samples: Iterable[dict[str, Any]]) -> dict[str, int]:
    out = dict.fromkeys(LEVELS, 0)
    for s in samples:
        out[s["level"]] += 1
    return out
