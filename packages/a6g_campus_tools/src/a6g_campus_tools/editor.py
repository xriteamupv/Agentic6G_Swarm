"""``a6g campus edit``: an interactive editor for the patrol zones and the campus points.

A local web page (``http://127.0.0.1:8350``) over the campus file: paint footpaths into a
zone (or "not patrolled"), drag POIs, the staging area and link recovery points (they snap
to the nearest footpath), watch each zone's length, survey route and battery cost, and the
validation problems, update as you go; then save. Saving rewrites only the lines it changes
(coordinates, each sector's ``edges``, ``patrol_zones.exclude_edges``) so comments and
layout stay, after checking that the result loads; the previous file is kept as ``.bak``.

Edits are held in memory until saved. Zone assignments name paths (``w123.0:1``), so they
survive POIs that split a path (``w123.0:1a``/``1b``).
"""

import copy
import math
import re
import shutil
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml
from a6g_common.campus import CampusV2
from a6g_common.config import CampusConfig
from a6g_common.footpaths import PATROLLED, base_id, walker_for
from a6g_common.routegraph import RouteGraph
from pydantic import ValidationError

from a6g_campus_tools.editor_page import PAGE

#: Go2 drain (config/robots/go2_*.yaml) for the per-lap battery estimate in the panel.
GO2_DRAIN_PCT_PER_M = 0.05


class CampusEditor:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.revert()

    # ------------------------------------------------------------------ state

    def revert(self) -> None:
        self.text = self.path.read_text()
        self.data: dict[str, Any] = yaml.safe_load(self.text)
        self.saved = copy.deepcopy(self.data)

    @property
    def dirty(self) -> bool:
        return self.data != self.saved

    def _campus(self) -> tuple[CampusV2, dict[str, Any]]:
        campus = CampusV2.model_validate(self.data)
        return campus, campus.to_enu(base_dir=self.path.parent)

    def state(self) -> dict[str, Any]:
        campus, enu = self._campus()
        problems: list[str] = []
        try:
            CampusConfig.model_validate(enu)
        except ValidationError as exc:
            problems = [e["msg"].removeprefix("Value error, ") for e in exc.errors()]
        owner = {e: s["sector_id"] for s in enu["sectors"] for e in s.get("path_edges", [])}
        manual = {base_id(e): s["id"] for s in self.data["sectors"] for e in s.get("edges") or []}
        excluded = {base_id(e) for e in self._pz().get("exclude_edges") or []}
        edges = []
        for e in enu["route_graph"]["edges"]:
            b = base_id(e["id"])
            edges.append(
                {
                    "id": e["id"],
                    "base": b,
                    "source": e["source"],
                    "pts": e["geometry"],
                    "zone": owner.get(e["id"]),
                    "manual": manual.get(b) or ("none" if b in excluded else None),
                }
            )
        zones = []
        for s in enu["sectors"]:
            wps = s["survey_waypoints"]
            walk = sum(math.dist((a["x"], a["y"]), (b["x"], b["y"])) for a, b in pairwise(wps))
            length = sum(
                sum(math.dist(p, q) for p, q in zip(e["pts"], e["pts"][1:], strict=False))
                for e in edges
                if e["zone"] == s["sector_id"]
            )
            zones.append(
                {
                    "id": s["sector_id"],
                    "name": s["name"],
                    "length_m": round(length),
                    "survey_m": round(walk),
                    "waypoints": [(w["x"], w["y"]) for w in wps],
                    "battery_pct": round(walk * GO2_DRAIN_PCT_PER_M),
                }
            )
        walker = walker_for(RouteGraph.model_validate(enu["route_graph"]), PATROLLED)
        points = [
            {"id": p["poi_id"], "kind": "poi", "name": p["name"], **_xy(p["pose"])}
            for p in enu["pois"]
        ]
        points.append(
            {"id": "staging", "kind": "staging", "name": "Staging", **_xy(enu["staging"])}
        )
        points += [
            {
                "id": r["point_id"],
                "kind": "recovery",
                "name": r["name"] or r["point_id"],
                **_xy(r["pose"]),
            }
            for r in enu["link_recovery_points"]
        ]
        for p in points:
            p["off_m"] = round(walker.distance((p["x"], p["y"])), 1)
        return {
            "campus_id": campus.campus_id,
            "file": str(self.path),
            "dirty": self.dirty,
            "pilot_area": enu["pilot_area"],
            "buildings": enu["obstacles"],
            "shadows": [
                {
                    "id": sh["id"],
                    "link_quality": sh.get("link_quality", "lost"),
                    "polygon": [
                        list(campus.projection().xy(lat, lon)) for lat, lon in sh["polygon_wgs84"]
                    ],
                }
                for sh in self.data.get("coverage_shadows") or []
            ],
            "edges": edges,
            "zones": zones,
            "points": points,
            "corridor_m": enu["walk_corridor_m"],
            "problems": problems,
        }

    def _pz(self) -> dict[str, Any]:
        return self.data.setdefault("patrol_zones", {})

    # ------------------------------------------------------------------ edits

    def assign(self, edge: str, zone: str) -> None:
        """``zone``: a sector id, ``none`` (not patrolled) or ``auto`` (the automatic split)."""
        b = base_id(edge)
        for s in self.data["sectors"]:
            kept = [e for e in s.get("edges") or [] if base_id(e) != b]
            if kept:
                s["edges"] = kept
            else:
                s.pop("edges", None)
        pz = self._pz()
        exclude = [e for e in pz.get("exclude_edges") or [] if base_id(e) != b]
        if zone == "none":
            exclude.append(b)
        elif zone != "auto":
            sector = next(s for s in self.data["sectors"] if s["id"] == zone)
            sector.setdefault("edges", []).append(b)
            sector["edges"].sort()
        if exclude:
            pz["exclude_edges"] = sorted(exclude)
        else:
            pz.pop("exclude_edges", None)

    def auto(self) -> None:
        """Back to the automatic split: no fixed paths, nothing excluded."""
        for s in self.data["sectors"]:
            s.pop("edges", None)
        self._pz().pop("exclude_edges", None)

    def move(self, kind: str, ident: str, x: float, y: float) -> None:
        """Move a point to (x, y) in campus_map, snapped to the nearest footpath."""
        campus, enu = self._campus()
        walker = walker_for(RouteGraph.model_validate(enu["route_graph"]), PATROLLED)
        proj = walker.project((x, y))
        if proj is not None:
            x, y = proj.point
        lat, lon, _ = campus.projection().to_wgs84(x, y)
        lat, lon = round(lat, 7), round(lon, 7)
        if kind == "poi":
            item = next(p for p in self.data["pois"] if p["id"] == ident)
        elif kind == "recovery":
            item = next(r for r in self.data["link_recovery_points"] if r["id"] == ident)
        else:
            item = self.data["staging_area"]
        item["lat"], item["lon"] = lat, lon

    def shadow(self, ident: str, polygon: list[list[float]], quality: str | None = None) -> None:
        """Set a coverage shadow's polygon (campus_map x, y), creating it if new."""
        if len(polygon) < 3:
            raise ValueError("a shadow needs at least 3 corners")
        proj = CampusV2.model_validate(self.data).projection()
        ring = []
        for x, y in polygon:
            lat, lon, _ = proj.to_wgs84(float(x), float(y))
            ring.append([round(lat, 7), round(lon, 7)])
        shadows = self.data.setdefault("coverage_shadows", [])
        item = next((sh for sh in shadows if sh["id"] == ident), None)
        if item is None:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", ident):
                raise ValueError("shadow ids are lower case letters, digits and _")
            item = {"id": ident, "link_quality": "lost"}
            shadows.append(item)
        item["polygon_wgs84"] = ring
        if quality is not None:
            if quality not in ("lost", "degraded"):
                raise ValueError("link_quality is lost or degraded")
            item["link_quality"] = quality
        CampusV2.model_validate(self.data)  # e.g. a self-intersecting polygon: refused

    def shadow_quality(self, ident: str, quality: str) -> None:
        item = next(sh for sh in self.data["coverage_shadows"] if sh["id"] == ident)
        if quality not in ("lost", "degraded"):
            raise ValueError("link_quality is lost or degraded")
        item["link_quality"] = quality

    def delete_shadow(self, ident: str) -> None:
        self.data["coverage_shadows"] = [
            sh for sh in self.data.get("coverage_shadows") or [] if sh["id"] != ident
        ]

    # ------------------------------------------------------------------ save

    def save(self) -> Path:
        text = self.text
        for p in self.data["pois"]:
            text = _set_latlon(text, rf"id:\s*{re.escape(p['id'])}\b", p["lat"], p["lon"])
        for r in self.data["link_recovery_points"]:
            text = _set_latlon(text, rf"id:\s*{re.escape(r['id'])}\b", r["lat"], r["lon"])
        st = self.data["staging_area"]
        text = _set_latlon(text, r"^staging_area:", st["lat"], st["lon"])
        for s in self.data["sectors"]:
            text = _set_list(text, s["id"], "edges", s.get("edges") or [], indent=4)
        text = _set_exclude(text, self._pz().get("exclude_edges") or [])
        text = _set_shadows(text, self.data.get("coverage_shadows") or [])
        check = yaml.safe_load(text)  # the result must say exactly what the editor holds
        CampusV2.model_validate(check)
        if check != self.data:
            raise ValueError("the rewritten file does not match the edits: nothing saved")
        backup = self.path.with_suffix(self.path.suffix + ".bak")
        shutil.copyfile(self.path, backup)
        self.path.write_text(text)
        self.revert()
        return backup


def _xy(pose: dict[str, Any]) -> dict[str, float]:
    return {"x": pose["x"], "y": pose["y"]}


def _set_latlon(text: str, anchor: str, lat: float, lon: float) -> str:
    """Replace ``lat: ..., lon: ...`` on the line that matches ``anchor``."""
    lines = text.split("\n")
    rx = re.compile(anchor)
    for i, line in enumerate(lines):
        if rx.search(line) and "lat:" in line:
            line = re.sub(r"lat:\s*-?[0-9.]+", f"lat: {lat}", line, count=1)
            lines[i] = re.sub(r"lon:\s*-?[0-9.]+", f"lon: {lon}", line, count=1)
            return "\n".join(lines)
    raise ValueError(f"no line with lat/lon for {anchor!r}")


def _flow(items: list[str], indent: int, key: str) -> list[str]:
    """``key: [a, b, ...]`` wrapped at 100 columns."""
    pad = " " * indent
    if not items:
        return []
    out, line = [], f"{pad}{key}: ["
    cont = " " * len(line)
    for i, item in enumerate(items):
        piece = item + ("]" if i == len(items) - 1 else ",")
        if len(line) + len(piece) + 1 > 100 and not line.endswith("["):
            out.append(line)
            line = cont + piece
        else:
            line += ("" if line.endswith("[") else " ") + piece
    out.append(line)
    return out


def _drop_key(
    lines: list[str], start: int, end: int, key: str, indent: int
) -> tuple[list[str], int]:
    """Remove ``key: [...]`` (possibly wrapped) between ``start`` and ``end``."""
    prefix = " " * indent + f"{key}:"
    for i in range(start, end):
        if lines[i].startswith(prefix):
            j = i
            while "]" not in lines[j] and j + 1 < len(lines):
                j += 1
            del lines[i : j + 1]
            return lines, i
    return lines, -1


def _set_list(text: str, sector_id: str, key: str, items: list[str], indent: int) -> str:
    lines = text.split("\n")
    head = next(
        i for i, ln in enumerate(lines) if re.match(rf"\s*- id:\s*{re.escape(sector_id)}\s*$", ln)
    )
    end = next(
        (
            i
            for i in range(head + 1, len(lines))
            if not lines[i].startswith(" " * indent) or lines[i].lstrip().startswith("- ")
        ),
        len(lines),
    )
    lines, at = _drop_key(lines, head + 1, end, key, indent)
    if at < 0:
        end = next(
            (
                i
                for i in range(head + 1, len(lines))
                if not lines[i].startswith(" " * indent) or lines[i].lstrip().startswith("- ")
            ),
            len(lines),
        )
        at = end
    lines[at:at] = _flow(items, indent, key)
    return "\n".join(lines)


def _set_exclude(text: str, items: list[str]) -> str:
    lines = text.split("\n")
    head = next(i for i, ln in enumerate(lines) if ln.startswith("patrol_zones:"))
    end = next(
        (i for i in range(head + 1, len(lines)) if not lines[i].startswith("  ")), len(lines)
    )
    lines, at = _drop_key(lines, head + 1, end, "exclude_edges", 2)
    if at < 0:
        at = next(
            (i for i in range(head + 1, len(lines)) if not lines[i].startswith("  ")), len(lines)
        )
    lines[at:at] = _flow(items, 2, "exclude_edges")
    return "\n".join(lines)


def _shadow_line(sh: dict[str, Any]) -> str:
    ring = ", ".join(f"[{lat}, {lon}]" for lat, lon in sh["polygon_wgs84"])
    extra = "".join(
        f", {k}: {v}" for k, v in sh.items() if k not in ("id", "link_quality", "polygon_wgs84")
    )
    quality = sh.get("link_quality", "lost")
    return f"  - {{id: {sh['id']}, link_quality: {quality}{extra}, polygon_wgs84: [{ring}]}}"


def _set_shadows(text: str, shadows: list[dict[str, Any]]) -> str:
    """Rewrite the ``coverage_shadows`` items (one flow line each), keeping the key line
    and its comment."""
    lines = text.split("\n")
    head = next(i for i, ln in enumerate(lines) if ln.startswith("coverage_shadows:"))
    end = head + 1
    while end < len(lines) and lines[end].startswith("  "):  # its items
        end += 1
    body, _, comment = lines[head].partition("#")
    key = "coverage_shadows:" if shadows else "coverage_shadows: []"
    lines[head] = key.ljust(max(len(body), len(key) + 1)) + "#" + comment if comment else key
    lines[head + 1 : end] = [_shadow_line(sh) for sh in shadows]
    return "\n".join(lines)


# --------------------------------------------------------------------------- server


def build_app(editor: CampusEditor):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Route

    def guarded(fn):
        async def endpoint(request: Request):
            try:
                body = await request.json() if request.method == "POST" else {}
                extra = fn(body) or {}
                return JSONResponse({**editor.state(), **extra})
            except Exception as exc:  # shown in the page; the edits stay as they were
                return JSONResponse({"error": str(exc)}, status_code=400)

        return endpoint

    async def page(_request: Request) -> HTMLResponse:
        return HTMLResponse(PAGE)

    def assign(b: dict) -> None:
        editor.assign(b["edge"], b["zone"])

    def move(b: dict) -> None:
        editor.move(b["kind"], b["id"], float(b["x"]), float(b["y"]))

    def shadow(b: dict) -> None:
        editor.shadow(b["id"], b["polygon"], b.get("link_quality"))

    def shadow_quality(b: dict) -> None:
        editor.shadow_quality(b["id"], b["link_quality"])

    def delete_shadow(b: dict) -> None:
        editor.delete_shadow(b["id"])

    def save(_b: dict) -> dict:
        return {"saved": str(editor.save())}

    return Starlette(
        routes=[
            Route("/", page),
            Route("/api/state", guarded(lambda _b: None)),
            Route("/api/assign", guarded(assign), methods=["POST"]),
            Route("/api/move", guarded(move), methods=["POST"]),
            Route("/api/auto", guarded(lambda _b: editor.auto()), methods=["POST"]),
            Route("/api/shadow", guarded(shadow), methods=["POST"]),
            Route("/api/shadow/quality", guarded(shadow_quality), methods=["POST"]),
            Route("/api/shadow/delete", guarded(delete_shadow), methods=["POST"]),
            Route("/api/revert", guarded(lambda _b: editor.revert()), methods=["POST"]),
            Route("/api/save", guarded(save), methods=["POST"]),
        ]
    )
