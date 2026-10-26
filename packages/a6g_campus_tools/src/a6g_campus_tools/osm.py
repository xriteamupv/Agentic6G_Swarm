"""OpenStreetMap extract for the pilot sector, cached in the repository (ODbL).

Fetched once through the Overpass API (several public instances) with a fallback to the
OSM API ``map`` call (a single small bounding box), normalized to a deterministic JSON
cache. Later builds use the cache unless ``refresh`` is requested.
"""

import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "a6g-swarm/0.1 (Agentic6G UPV research; campus world generator)"
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OSM_API_MAP = "https://api.openstreetmap.org/api/0.6/map"

ATTRIBUTION = """# OpenStreetMap data

Files in this directory contain data from OpenStreetMap (https://www.openstreetmap.org),
© OpenStreetMap contributors, available under the Open Database License (ODbL) 1.0
(https://opendatacommons.org/licenses/odbl/). Derived worlds and maps are produced works
under the ODbL; keep this attribution with them.
"""


def _levels(tags: dict[str, str]) -> float | None:
    for key in ("building:levels",):
        try:
            return float(tags[key])
        except (KeyError, ValueError):
            continue
    return None


def _height(tags: dict[str, str]) -> float | None:
    try:
        return float(tags.get("height", "").replace("m", "").strip())
    except ValueError:
        return None


def _join_rings(segments: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Join way segments into closed rings (multipolygon outers)."""
    rings, pending = [], [list(s) for s in segments if len(s) >= 2]
    while pending:
        ring = pending.pop(0)
        changed = True
        while ring[0] != ring[-1] and changed:
            changed = False
            for i, seg in enumerate(pending):
                if seg[0] == ring[-1]:
                    ring += seg[1:]
                elif seg[-1] == ring[-1]:
                    ring += seg[::-1][1:]
                elif seg[-1] == ring[0]:
                    ring = seg[:-1] + ring
                elif seg[0] == ring[0]:
                    ring = seg[::-1][:-1] + ring
                else:
                    continue
                pending.pop(i)
                changed = True
                break
        if len(ring) >= 4 and ring[0] == ring[-1]:
            rings.append(ring)
    return rings


def parse_osm_xml(text: str) -> dict[str, Any]:
    root = ET.fromstring(text)
    nodes = {n.get("id"): (float(n.get("lat")), float(n.get("lon"))) for n in root.iter("node")}
    node_tags = {
        n.get("id"): {t.get("k"): t.get("v") for t in n.iter("tag")} for n in root.iter("node")
    }
    ways = {}
    for w in root.iter("way"):
        refs = [nd.get("ref") for nd in w.iter("nd")]
        ways[w.get("id")] = (
            [nodes[r] for r in refs if r in nodes],
            {t.get("k"): t.get("v") for t in w.iter("tag")},
        )
    buildings, trees, barriers = [], [], []
    for wid, (pts, tags) in ways.items():
        if "building" in tags and len(pts) >= 4 and pts[0] == pts[-1]:
            buildings.append(
                {
                    "id": f"w{wid}",
                    "levels": _levels(tags),
                    "height": _height(tags),
                    "name": tags.get("name") or tags.get("ref"),
                    "outer": pts,
                }
            )
        elif "barrier" in tags and len(pts) >= 2:
            barriers.append({"id": f"w{wid}", "kind": tags["barrier"], "line": pts})
    for r in root.iter("relation"):
        tags = {t.get("k"): t.get("v") for t in r.iter("tag")}
        if "building" not in tags or tags.get("type") != "multipolygon":
            continue
        outers = [
            ways[m.get("ref")][0]
            for m in r.iter("member")
            if m.get("type") == "way" and m.get("role") in ("outer", "") and m.get("ref") in ways
        ]
        for i, ring in enumerate(_join_rings(outers)):
            buildings.append(
                {
                    "id": f"r{r.get('id')}_{i}",
                    "levels": _levels(tags),
                    "height": _height(tags),
                    "name": tags.get("name") or tags.get("ref"),
                    "outer": ring,
                }
            )
    for nid, tags in node_tags.items():
        if tags.get("natural") == "tree":
            trees.append({"id": f"n{nid}", "lat": nodes[nid][0], "lon": nodes[nid][1]})
    return {"buildings": buildings, "trees": trees, "barriers": barriers}


def parse_overpass_json(data: dict[str, Any]) -> dict[str, Any]:
    buildings, trees, barriers = [], [], []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        if el["type"] == "node" and tags.get("natural") == "tree":
            trees.append({"id": f"n{el['id']}", "lat": el["lat"], "lon": el["lon"]})
        elif el["type"] == "way" and "geometry" in el:
            pts = [(g["lat"], g["lon"]) for g in el["geometry"]]
            if "building" in tags and len(pts) >= 4 and pts[0] == pts[-1]:
                buildings.append(
                    {
                        "id": f"w{el['id']}",
                        "levels": _levels(tags),
                        "height": _height(tags),
                        "name": tags.get("name") or tags.get("ref"),
                        "outer": pts,
                    }
                )
            elif "barrier" in tags:
                barriers.append({"id": f"w{el['id']}", "kind": tags["barrier"], "line": pts})
        elif el["type"] == "relation" and "building" in tags:
            outers = [
                [(g["lat"], g["lon"]) for g in m.get("geometry", [])]
                for m in el.get("members", [])
                if m.get("role") in ("outer", "")
            ]
            for i, ring in enumerate(_join_rings(outers)):
                buildings.append(
                    {
                        "id": f"r{el['id']}_{i}",
                        "levels": _levels(tags),
                        "height": _height(tags),
                        "name": tags.get("name") or tags.get("ref"),
                        "outer": ring,
                    }
                )
    return {"buildings": buildings, "trees": trees, "barriers": barriers}


def normalize(extract: dict[str, Any], bbox: list[float], source: str) -> dict[str, Any]:
    """Deterministic form: sorted by id, coordinates rounded to 1e-7 deg (~1 cm)."""

    def r(p):
        return [round(p[0], 7), round(p[1], 7)]

    return {
        "attribution": "© OpenStreetMap contributors, ODbL 1.0",
        "bbox_wgs84": bbox,
        "source": source,
        "buildings": sorted(
            ({**b, "outer": [r(p) for p in b["outer"]]} for b in extract["buildings"]),
            key=lambda b: b["id"],
        ),
        "trees": sorted(
            ({**t, "lat": round(t["lat"], 7), "lon": round(t["lon"], 7)} for t in extract["trees"]),
            key=lambda t: t["id"],
        ),
        "barriers": sorted(
            ({**b, "line": [r(p) for p in b["line"]]} for b in extract["barriers"]),
            key=lambda b: b["id"],
        ),
    }


def fetch(bbox: list[float], timeout_s: float = 90.0) -> tuple[dict[str, Any], str]:
    lat_min, lon_min, lat_max, lon_max = bbox
    query = (
        f"[out:json][timeout:60];("
        f'way["building"]({lat_min},{lon_min},{lat_max},{lon_max});'
        f'relation["building"]({lat_min},{lon_min},{lat_max},{lon_max});'
        f'node["natural"="tree"]({lat_min},{lon_min},{lat_max},{lon_max});'
        f'way["barrier"]({lat_min},{lon_min},{lat_max},{lon_max});'
        f");out geom;"
    )
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(timeout=timeout_s, headers=headers) as client:
        for url in OVERPASS:
            try:
                response = client.post(url, data={"data": query})
                if response.status_code == 200 and response.headers.get(
                    "content-type", ""
                ).startswith("application/json"):
                    return parse_overpass_json(response.json()), url
                log.warning("overpass %s: HTTP %s", url, response.status_code)
            except httpx.HTTPError as exc:
                log.warning("overpass %s: %s", url, exc)
        response = client.get(
            OSM_API_MAP, params={"bbox": f"{lon_min},{lat_min},{lon_max},{lat_max}"}
        )
        response.raise_for_status()
        return parse_osm_xml(response.text), OSM_API_MAP


def load_or_fetch(cache: Path, bbox: list[float], refresh: bool = False) -> dict[str, Any]:
    if cache.exists() and not refresh:
        data = json.loads(cache.read_text())
        if data.get("bbox_wgs84") == bbox:
            return data
        log.warning("cached extract is for another bounding box: refetching")
    extract, source = fetch(bbox)
    data = normalize(extract, bbox, source)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    (cache.parent / "ATTRIBUTION.md").write_text(ATTRIBUTION)
    return data


# --------------------------------------------------------------------------- footways (A3.3.1)

#: Pedestrian ways for the outdoor route graph (SPEC A3.3.1; UC2 runs outdoors only).
PATH_HIGHWAYS = (
    "footway",
    "path",
    "pedestrian",
    "steps",
    "living_street",
    "cycleway",
    "service",
    "track",
    "corridor",
)
PATH_TAGS = ("highway", "wheelchair", "incline", "ramp", "covered", "width", "surface", "name")


def parse_overpass_paths(data: dict[str, Any]) -> list[dict[str, Any]]:
    ways = []
    for el in data.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        tags = el.get("tags", {})
        if tags.get("highway") not in PATH_HIGHWAYS or tags.get("indoor") == "yes":
            continue
        ways.append(
            {
                "id": f"w{el['id']}",
                "nodes": [f"n{n}" for n in el.get("nodes", [])],
                "line": [(g["lat"], g["lon"]) for g in el["geometry"]],
                "tags": {k: tags[k] for k in PATH_TAGS if k in tags},
            }
        )
    return ways


def fetch_paths(bbox: list[float], timeout_s: float = 90.0) -> tuple[list[dict[str, Any]], str]:
    lat_min, lon_min, lat_max, lon_max = bbox
    kinds = "|".join(PATH_HIGHWAYS)
    query = (
        f"[out:json][timeout:60];"
        f'way["highway"~"^({kinds})$"]({lat_min},{lon_min},{lat_max},{lon_max});'
        f"out body geom;"
    )
    with httpx.Client(timeout=timeout_s, headers={"User-Agent": USER_AGENT}) as client:
        for url in OVERPASS:
            try:
                response = client.post(url, data={"data": query})
                if response.status_code == 200 and response.headers.get(
                    "content-type", ""
                ).startswith("application/json"):
                    return parse_overpass_paths(response.json()), url
                log.warning("overpass %s: HTTP %s", url, response.status_code)
            except httpx.HTTPError as exc:
                log.warning("overpass %s: %s", url, exc)
        response = client.get(
            OSM_API_MAP, params={"bbox": f"{lon_min},{lat_min},{lon_max},{lat_max}"}
        )
        response.raise_for_status()
        return parse_osm_xml_paths(response.text), OSM_API_MAP


def parse_osm_xml_paths(text: str) -> list[dict[str, Any]]:
    """Footways from an OSM API ``map`` answer (fallback when Overpass is down)."""
    root = ET.fromstring(text)
    nodes = {n.get("id"): (float(n.get("lat")), float(n.get("lon"))) for n in root.iter("node")}
    ways = []
    for w in root.iter("way"):
        tags = {t.get("k"): t.get("v") for t in w.iter("tag")}
        if tags.get("highway") not in PATH_HIGHWAYS or tags.get("indoor") == "yes":
            continue
        refs = [nd.get("ref") for nd in w.iter("nd") if nd.get("ref") in nodes]
        if len(refs) < 2:
            continue
        ways.append(
            {
                "id": f"w{w.get('id')}",
                "nodes": [f"n{r}" for r in refs],
                "line": [nodes[r] for r in refs],
                "tags": {k: tags[k] for k in PATH_TAGS if k in tags},
            }
        )
    return ways


def load_or_fetch_paths(cache: Path, bbox: list[float], refresh: bool = False) -> dict[str, Any]:
    """Footways of the pilot box (``<osm cache>.paths.json``), fetched once and cached."""
    if cache.exists() and not refresh:
        data = json.loads(cache.read_text())
        if data.get("bbox_wgs84") == bbox:
            return data
        log.warning("cached footways are for another bounding box: refetching")
    ways, source = fetch_paths(bbox)
    data = {
        "attribution": "© OpenStreetMap contributors, ODbL 1.0",
        "bbox_wgs84": bbox,
        "source": source,
        "ways": sorted(
            ({**w, "line": [[round(a, 7), round(b, 7)] for a, b in w["line"]]} for w in ways),
            key=lambda w: w["id"],
        ),
    }
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    return data
