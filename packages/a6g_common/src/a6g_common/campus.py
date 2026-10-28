"""Campus configuration v2: geometry defined once, in WGS84.

``CampusV2`` is the file format. ``to_enu()`` derives the runtime ``CampusConfig``
(``campus_map`` metres about the datum), so code that uses ``x, y`` keeps working. Building
footprints and trees come from the cached OpenStreetMap extract named in ``world.osm_cache``
and become the 2D simulator's obstacles, so the 2D simulator, Gazebo and the Nav2 prior map
agree (geometry is defined once).
"""

import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from a6g_common.geo import Datum
from a6g_common.geometry import point_in_polygon

LatLon = tuple[float, float]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DatumV2(_M):
    lat_deg: float = Field(ge=-90, le=90)
    lon_deg: float = Field(ge=-180, le=180)
    alt_m: float = 0.0
    heading_deg: float = 0.0

    def geo(self) -> Datum:
        return Datum(self.lat_deg, self.lon_deg, self.alt_m, self.heading_deg)


class PilotSector(_M):
    """The pilot area: an axis-aligned ``bbox_wgs84`` or any simple ``polygon_wgs84``.

    With a polygon, ``bbox_wgs84`` is its envelope (derived when omitted); the envelope
    bounds the OSM extract and the generated world and maps, the polygon bounds geometry.
    """

    # lat_min, lon_min, lat_max, lon_max
    bbox_wgs84: tuple[float, float, float, float] | None = None
    polygon_wgs84: list[LatLon] | None = Field(None, min_length=3)

    @model_validator(mode="after")
    def _check(self) -> "PilotSector":
        if self.polygon_wgs84 is not None:
            problem = polygon_problem(self.polygon_wgs84)
            if problem:
                raise ValueError(f"polygon_wgs84: {problem}")
            lats = [p[0] for p in self.polygon_wgs84]
            lons = [p[1] for p in self.polygon_wgs84]
            envelope = (min(lats), min(lons), max(lats), max(lons))
            if self.bbox_wgs84 is None:
                self.bbox_wgs84 = envelope
            elif not (
                self.bbox_wgs84[0] <= envelope[0]
                and self.bbox_wgs84[1] <= envelope[1]
                and self.bbox_wgs84[2] >= envelope[2]
                and self.bbox_wgs84[3] >= envelope[3]
            ):
                raise ValueError("bbox_wgs84 must contain polygon_wgs84")
        if self.bbox_wgs84 is None:
            raise ValueError("pilot_sector needs bbox_wgs84 or polygon_wgs84")
        lat0, lon0, lat1, lon1 = self.bbox_wgs84
        if not (lat0 < lat1 and lon0 < lon1):
            raise ValueError("bbox_wgs84 must be [lat_min, lon_min, lat_max, lon_max]")
        return self

    def contains(self, lat: float, lon: float) -> bool:
        lat0, lon0, lat1, lon1 = self.bbox_wgs84
        if not (lat0 <= lat <= lat1 and lon0 <= lon <= lon1):
            return False
        if self.polygon_wgs84 is None:
            return True
        return point_in_polygon((lat, lon), [tuple(p) for p in self.polygon_wgs84])


#: Environment override of ``world.scale`` (e.g. ``A6G_WORLD_SCALE=0.1``).
SCALE_ENV = "A6G_WORLD_SCALE"


class WorldSettings(_M):
    #: Uniform scale of the whole campus geometry in ``campus_map`` (and so in the Gazebo
    #: world, the Nav2 maps and the 2D simulator). Robots, sensors and building heights keep
    #: their real size: a smaller campus means shorter drives at real speeds.
    scale: float = Field(1.0, gt=0.0, le=1.0)
    osm_cache: str | None = None
    output_dir: str = "../data/world"
    map_resolution_m: float = Field(0.05, gt=0)
    #: Wall thickness of the AMCL localization map (footprint outlines, unknown interiors).
    loc_outline_m: float = Field(0.30, gt=0)
    default_building_height_m: float = Field(10.0, gt=0)
    level_height_m: float = Field(3.0, gt=0)
    tree_radius_m: float = Field(0.4, gt=0)
    tree_height_m: float = Field(6.0, gt=0)

    #: Scaled campus only: minimum free space around POIs, waypoints, staging and recovery
    #: points (the robots keep their real size); closer points are nudged away from walls.
    scaled_clearance_m: float = Field(0.9, gt=0)

    @property
    def tree_radius_scaled_m(self) -> float:
        """Trunks shrink with the campus (else a 0.1 campus is a forest), but stay solid."""
        return max(0.05, round(self.tree_radius_m * self.scale, 3))


class SectorV2(_M):
    id: str
    name: str = ""
    aliases: list[str] = Field(default_factory=list)  # v3
    #: ``patrol_zones.mode: polygons``: the sector as drawn, and its survey route.
    polygon_wgs84: list[LatLon] | None = Field(None, min_length=3)
    survey_waypoints_wgs84: list[LatLon] = Field(default_factory=list)
    #: ``patrol_zones.mode: footpaths``: where the zone grows from (default: the polygon's
    #: centre), and path edges fixed to it (manual adjustments of the automatic split).
    seed_wgs84: LatLon | None = None
    edges: list[str] = Field(default_factory=list)

    def seed(self) -> LatLon:
        if self.seed_wgs84 is not None:
            return self.seed_wgs84
        assert self.polygon_wgs84 is not None
        n = len(self.polygon_wgs84)
        return (
            sum(p[0] for p in self.polygon_wgs84) / n,
            sum(p[1] for p in self.polygon_wgs84) / n,
        )


class PatrolZones(_M):
    """How patrol sectors are defined (footpaths only, or polygons as drawn)."""

    #: footpaths: zones derived from the route graph's paths (the robots walk nowhere
    #: else); polygons: sectors as drawn.
    mode: Literal["footpaths", "polygons"] = "polygons"
    margin_m: float = Field(3.0, ge=0, description="zone polygon around its paths")
    simplify_m: float = Field(1.0, gt=0, description="survey route within this of the paths")
    corridor_m: float = Field(2.0, gt=0, description="walkable half-width around a path")
    #: A robot moving between tasks (transit, recovery, return home) prefers the patrol
    #: paths: any other footway costs this many times its length.
    transit_off_zone_factor: float = Field(3.0, ge=1.0)
    #: Paths no zone patrols (still walkable), by path id (``a6g campus edit``).
    exclude_edges: list[str] = Field(default_factory=list)


class FloodParams(_M):
    """Linear-reservoir flood twin of an exposed POI."""

    gain_m_per_mm: float = Field(gt=0)
    drain_rate_per_min: float = Field(gt=0)
    risk_levels_m: dict[Literal["elevated", "high", "critical"], float]

    @model_validator(mode="after")
    def _ordered(self) -> "FloodParams":
        levels = self.risk_levels_m
        if set(levels) != {"elevated", "high", "critical"}:
            raise ValueError("risk_levels_m needs elevated, high and critical")
        if not 0 < levels["elevated"] < levels["high"] < levels["critical"]:
            raise ValueError("risk levels must increase: elevated < high < critical")
        return self


class PoiV2(_M):
    id: str
    name: str = ""
    lat: float
    lon: float
    yaw_deg: float = 0.0
    kind: list[Literal["inspect", "exposed", "escort_dest", "staging", "shelter", "access"]] = (
        Field(default_factory=lambda: ["inspect"])
    )
    exposed: bool = False
    #: v3: other names, room codes, the names in the deployment languages.
    aliases: list[str] = Field(default_factory=list)
    building: str | None = None
    #: v3: attach to this graph node instead of the nearest point of the network.
    node_id: str | None = None
    flood: FloodParams | None = None


class GraphNodeV3(_M):
    id: str
    lat: float
    lon: float
    kind: Literal["junction", "entrance", "access"] = "junction"


class GraphEdgeV3(_M):
    id: str
    from_: str = Field(alias="from")
    to: str
    length_m: float | None = Field(None, gt=0)
    step_free: bool = True
    stairs: bool = False
    slope_pct: float | None = None
    width_m: float | None = Field(None, gt=0)
    sheltered: bool = False

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)


class RouteGraphV3(_M):
    """Outdoor pedestrian graph: OSM footways + manual additions (defined once, here)."""

    source: Literal["osm", "manual", "osm+manual"] = "osm+manual"
    #: Footway cache (default: next to ``world.osm_cache``, ``<stem>.paths.json``).
    paths_cache: str | None = None
    nodes: list[GraphNodeV3] = Field(default_factory=list)
    edges: list[GraphEdgeV3] = Field(default_factory=list)
    bridge_max_m: float = Field(25.0, gt=0, description="join separate footway pieces")
    attach_max_m: float = Field(60.0, gt=0, description="POI to footway connector")
    exposed_radius_m: float = Field(10.0, gt=0, description="edges this close are exposed")
    #: Open walkable space (plazas): link each POI / staging to the nodes within this
    #: distance through free space (OSM maps only the edges of a plaza). 0 = off.
    open_space_max_m: float = Field(0.0, ge=0)


class DocumentV3(_M):
    """A notice, accessibility note or description, indexed for retrieval."""

    id: str
    kind: Literal["notice", "accessibility", "description"]
    text: str = Field(min_length=1)
    lang: str = "en"
    applies_to: list[str] = Field(default_factory=list)
    valid_from: str | None = None
    valid_until: str | None = None
    source: str = "operator"


class RoutingProfileV3(_M):
    exclude: list[str] = Field(default_factory=list)
    require: list[str] = Field(default_factory=list)
    max_slope_pct: float | None = Field(None, gt=0)


class StagingV2(_M):
    lat: float
    lon: float
    yaw_deg: float = 0.0
    half_size_m: float = Field(10.0, gt=0)


class ShadowV2(_M):
    id: str
    polygon_wgs84: list[LatLon] = Field(min_length=3)
    link_quality: Literal["lost", "degraded"] = "lost"


class NetworkSiteV2(_M):
    """A gNB site, WGS84; its radio parameters are in config/network.yaml."""

    id: str
    name: str = ""
    lat: float
    lon: float
    #: antenna height above ground; None: the height of the building it stands on (the
    #: nearest footprint within 5 m: the rooftop site) plus ``mast_m``
    height_m: float | None = None
    mast_m: float = 3.0  # mast above the roof: assumed, to be confirmed on site


class CalibrationLoopV2(_M):
    """A loop with measured coverage: the velodrome track of UPV's plots."""

    id: str
    name: str = ""
    polygon_wgs84: list[LatLon] = Field(min_length=3)
    source: str = ""


class VegetationAreaV2(_M):
    """Trees that attenuate the radio: a polygon, or the footpaths of a patrol
    zone widened by ``half_width_m`` (a tree-lined walk) until the tree line is drawn."""

    id: str
    name: str = ""
    sector: str | None = None
    half_width_m: float = 6.0
    polygon_wgs84: list[LatLon] | None = None
    canopy_height_m: float | None = None
    note: str = ""


class RecoveryPointV2(_M):
    id: str
    lat: float
    lon: float
    note: str = ""
    name: str = ""
    aliases: list[str] = Field(default_factory=list)  # v3


class CampusV2(_M):
    """Campus file v2 (WGS84) and v3 (+ knowledge base)."""

    schema_version: Literal[2, 3]
    campus_id: str
    datum: DatumV2
    pilot_sector: PilotSector
    world: WorldSettings = Field(default_factory=WorldSettings)
    staging_area: StagingV2
    sectors: list[SectorV2] = Field(min_length=1)
    patrol_zones: PatrolZones = Field(default_factory=PatrolZones)
    pois: list[PoiV2] = Field(default_factory=list)
    coverage_shadows: list[ShadowV2] = Field(default_factory=list)
    link_recovery_points: list[RecoveryPointV2] = Field(default_factory=list)
    #: gNB sites and coverage-calibration loops (geometry defined once)
    network_sites: list[NetworkSiteV2] = Field(default_factory=list)
    calibration_loops: list[CalibrationLoopV2] = Field(default_factory=list)
    vegetation_areas: list[VegetationAreaV2] = Field(default_factory=list)
    # v3 (optional: a v2 file is a v3 file without a knowledge base)
    route_graph: RouteGraphV3 | None = None
    documents: list[DocumentV3] = Field(default_factory=list)
    people_routing_profiles: dict[str, RoutingProfileV3] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> "CampusV2":
        problems: list[str] = []
        for label, items in (
            ("sector", self.sectors),
            ("poi", self.pois),
            ("coverage shadow", self.coverage_shadows),
            ("recovery point", self.link_recovery_points),
            ("document", self.documents),
        ):
            ids = [i.id for i in items]
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                problems.append(f"duplicate {label} id(s): {dupes}")
        for poi in self.pois:
            if not self.pilot_sector.contains(poi.lat, poi.lon):
                problems.append(f"poi {poi.id} lies outside the pilot sector")
        if not self.pilot_sector.contains(self.staging_area.lat, self.staging_area.lon):
            problems.append("staging area lies outside the pilot sector")
        for rp in self.link_recovery_points:
            if not self.pilot_sector.contains(rp.lat, rp.lon):
                problems.append(f"link recovery point {rp.id} lies outside the pilot sector")
        footpaths = self.patrol_zones.mode == "footpaths"
        if footpaths and self.route_graph is None:
            problems.append("patrol_zones.mode footpaths needs a route_graph (campus v3)")
        for s in self.sectors:
            if s.polygon_wgs84 is None and not (footpaths and s.seed_wgs84 is not None):
                problems.append(
                    f"sector {s.id}: give polygon_wgs84 (polygons mode) or seed_wgs84 (footpaths)"
                )
        polys = [(f"sector {s.id}", s.polygon_wgs84) for s in self.sectors if s.polygon_wgs84]
        polys += [(f"coverage shadow {c.id}", c.polygon_wgs84) for c in self.coverage_shadows]
        for label, poly in polys:
            problem = polygon_problem(poly)
            if problem:
                problems.append(f"{label}: {problem}")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    # ------------------------------------------------------------------ derivation

    def projection(self) -> "ScaledProjection":
        """WGS84 <-> campus_map, including ``world.scale``."""
        return ScaledProjection(self.datum.geo(), self.world.scale)

    @property
    def world_name(self) -> str:
        """Name of the generated world / maps: the campus id, suffixed when scaled."""
        return world_name(self.campus_id, self.world.scale)

    def with_scale_override(self, environ: dict[str, str] | None = None) -> "CampusV2":
        import os

        value = (os.environ if environ is None else environ).get(SCALE_ENV)
        if not value:
            return self
        world = self.world.model_copy(update={"scale": float(value)})
        return self.model_copy(update={"world": WorldSettings.model_validate(world.model_dump())})

    def to_enu(self, base_dir: Path | None = None) -> dict[str, Any]:
        """The runtime ``CampusConfig`` as a dict (validated by the caller)."""
        d = self.projection()
        obstacles: list[list[tuple[float, float]]] = []
        heights: list[float | None] = []
        if self.world.osm_cache:
            obstacles = osm_obstacles(self, _resolve(self.world.osm_cache, base_dir))
            heights = osm_obstacle_heights(self, _resolve(self.world.osm_cache, base_dir))

        def xy(p: LatLon) -> tuple[float, float]:
            return d.xy(p[0], p[1])

        # In a scaled campus the robots keep their real size: points without room or cut off
        # from the staging area move to the nearest roomy, reachable spot (none at scale 1).
        free = None
        if self.world.scale != 1.0 and obstacles:
            if self.pilot_sector.polygon_wgs84 is not None:
                ring = [xy(p) for p in self.pilot_sector.polygon_wgs84]
            else:
                b = self.pilot_sector.bbox_wgs84
                ring = [xy((b[0], b[1])), xy((b[2], b[3]))]
            xs, ys = [x for x, _ in ring], [y for _, y in ring]
            free = FreeSpace(
                obstacles,
                (min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2),
                self.world.scaled_clearance_m,
                xy((self.staging_area.lat, self.staging_area.lon)),
            )

        def spot(p: LatLon) -> tuple[float, float]:
            return free.snap(xy(p)) if free is not None else xy(p)

        sectors = []
        for s in self.sectors:
            if s.polygon_wgs84 is None:
                continue  # a footpath zone: derived from the route graph below
            poly = [xy(p) for p in s.polygon_wgs84]
            sectors.append(
                {
                    "sector_id": s.id,
                    "name": s.name,
                    "aliases": list(s.aliases),
                    "polygon": poly,
                    "survey_waypoints": [
                        {"x": x, "y": y} for x, y in (spot(p) for p in s.survey_waypoints_wgs84)
                    ],
                }
            )
        pois = []
        for p in self.pois:
            x, y = spot((p.lat, p.lon))
            inside = next(
                (s["sector_id"] for s in sectors if point_in_polygon((x, y), s["polygon"])), None
            )
            pois.append(
                {
                    "poi_id": p.id,
                    "name": p.name,
                    "sector_id": inside,
                    "pose": {
                        "x": x,
                        "y": y,
                        "yaw": round(_rad(p.yaw_deg - self.datum.heading_deg), 6),
                    },
                    "exposed": p.exposed or "exposed" in p.kind,
                    "tags": list(p.kind),
                    "aliases": list(p.aliases),
                    "building": p.building,
                    "node_id": f"poi:{p.id}" if self.route_graph else None,
                    "flood": p.flood.model_dump() if p.flood else None,
                }
            )
        sx, sy = spot((self.staging_area.lat, self.staging_area.lon))
        h = round(self.staging_area.half_size_m * self.world.scale, 3)
        if self.pilot_sector.polygon_wgs84 is not None:
            corners = [xy(p) for p in self.pilot_sector.polygon_wgs84]
        else:
            lat0, lon0, lat1, lon1 = self.pilot_sector.bbox_wgs84
            corners = [xy((lat0, lon0)), xy((lat0, lon1)), xy((lat1, lon1)), xy((lat1, lon0))]
        knowledge = self._knowledge(base_dir, xy, spot, pois, corners, obstacles)
        footpaths = self.patrol_zones.mode == "footpaths"
        if footpaths:
            sectors = self._footpath_zones(knowledge, xy, (sx, sy), corners)
            for p in pois:
                p["sector_id"] = next(
                    (
                        s["sector_id"]
                        for s in sectors
                        if point_in_polygon((p["pose"]["x"], p["pose"]["y"]), s["polygon"])
                    ),
                    None,
                )
        return {
            "schema_version": "1.0",
            "campus_id": self.campus_id,
            "frame": "campus_map",
            "footpaths_only": footpaths,
            # Robots keep their real size on a scaled campus: so does the corridor.
            "walk_corridor_m": self.patrol_zones.corridor_m,
            "transit_off_zone_factor": self.patrol_zones.transit_off_zone_factor,
            "map": {
                "map_version": f"osm-prior-{self.campus_id}",
                "tile_uri": f"file://{self.world.output_dir}/maps/{self.world_name}.yaml",
            },
            "staging": {
                "x": sx,
                "y": sy,
                "yaw": round(_rad(self.staging_area.yaw_deg - self.datum.heading_deg), 6),
            },
            "staging_area": [
                (sx - h, sy - h),
                (sx + h, sy - h),
                (sx + h, sy + h),
                (sx - h, sy + h),
            ],
            "sectors": sectors,
            "vegetation_areas": [
                {
                    "area_id": v.id,
                    "name": v.name,
                    "canopy_height_m": v.canopy_height_m,
                    "polygon": [xy(p) for p in v.polygon_wgs84] if v.polygon_wgs84 else [],
                    # a tree-lined walk: the zone's footpaths, widened
                    "paths": [
                        [tuple(p) for p in e["geometry"]]
                        for e in (knowledge.get("route_graph") or {}).get("edges", [])
                        if not v.polygon_wgs84
                        and e.get("id")
                        in next(
                            (
                                set(sec.get("path_edges") or [])
                                for sec in sectors
                                if sec["sector_id"] == v.sector
                            ),
                            set(),
                        )
                    ],
                    "half_width_m": v.half_width_m * self.world.scale,
                }
                for v in self.vegetation_areas
            ],
            "pois": pois,
            "link_shadows": [
                [xy(p) for p in c.polygon_wgs84]
                for c in self.coverage_shadows
                if c.link_quality == "lost"
            ],
            "degraded_shadows": [
                [xy(p) for p in c.polygon_wgs84]
                for c in self.coverage_shadows
                if c.link_quality == "degraded"
            ],
            "link_recovery_points": [
                {
                    "point_id": r.id,
                    "note": r.note,
                    "name": r.name,
                    "aliases": list(r.aliases),
                    "pose": dict(zip(("x", "y"), spot((r.lat, r.lon)), strict=True)),
                }
                for r in self.link_recovery_points
            ],
            "network_sites": [
                _site_entry(n, xy((n.lat, n.lon)), obstacles, heights, self.world.scale)
                for n in self.network_sites
            ],
            "calibration_loops": [
                {
                    "loop_id": c.id,
                    "name": c.name,
                    "source": c.source,
                    "polygon": [xy(p) for p in c.polygon_wgs84],
                }
                for c in self.calibration_loops
            ],
            "pilot_area": corners,
            "obstacles": obstacles,
            "obstacle_heights": heights,
            **knowledge,
            "datum": self.datum.model_dump(),
            "scale": self.world.scale,
            "world_name": self.world_name,
        }

    def _footpath_zones(
        self,
        knowledge: dict[str, Any],
        xy: Any,
        staging: tuple[float, float],
        area: list[tuple[float, float]],
    ) -> list[dict[str, Any]]:
        """Patrol zones from the footpaths, deterministic."""
        from a6g_common.footpaths import footpath_zones
        from a6g_common.routegraph import RouteGraph

        graph = RouteGraph.model_validate(knowledge["route_graph"])
        pz = self.patrol_zones
        zones = footpath_zones(
            graph,
            {s.id: xy(s.seed()) for s in self.sectors},
            fixed={s.id: s.edges for s in self.sectors if s.edges},
            start=staging,
            margin_m=pz.margin_m * self.world.scale,
            simplify_m=pz.simplify_m * self.world.scale,
            area=area,
            exclude=pz.exclude_edges,
        )
        return [
            {
                "sector_id": s.id,
                "name": s.name,
                "aliases": list(s.aliases),
                "polygon": zones[s.id].polygon,
                "survey_waypoints": [{"x": x, "y": y} for x, y in zones[s.id].waypoints],
                "path_edges": zones[s.id].edges,
            }
            for s in self.sectors
        ]

    def _knowledge(
        self,
        base_dir: Path | None,
        xy: Any,
        spot: Any,
        pois: list[dict[str, Any]],
        area: list[tuple[float, float]],
        obstacles: list[list[tuple[float, float]]],
    ) -> dict[str, Any]:
        """v3: the route graph, documents and routing profiles in campus_map."""
        if self.route_graph is None:
            return {}
        import json

        from a6g_common.routegraph import DEFAULT_PROFILES, build_route_graph

        g = self.route_graph
        ways: list[dict[str, Any]] = []
        if "osm" in g.source:
            cache = g.paths_cache
            if cache is None and self.world.osm_cache:
                osm = Path(self.world.osm_cache)
                cache = str(osm.with_name(osm.stem + ".paths.json"))
            if cache is None:
                raise ValueError("route_graph.source uses osm: set route_graph.paths_cache")
            path = _resolve(cache, base_dir)
            if not path.exists():
                raise ValueError(f"footway cache {path} missing: run `a6g campus build`")
            ways = json.loads(path.read_text())["ways"]
        attach = {
            f"poi:{p['poi_id']}": (
                (p["pose"]["x"], p["pose"]["y"]),
                "staging" if "staging" in p["tags"] else "poi",
            )
            for p in pois
        }
        attach["staging"] = (spot((self.staging_area.lat, self.staging_area.lon)), "staging")
        scale = self.world.scale
        graph, notes = build_route_graph(
            ways,
            lambda lat, lon: xy((lat, lon)),
            area=area,
            obstacles=obstacles,
            attach=attach,
            exposed={
                f"poi:{p['poi_id']}": (p["pose"]["x"], p["pose"]["y"]) for p in pois if p["exposed"]
            },
            manual_nodes=[
                {
                    "id": n.id,
                    "kind": n.kind,
                    **dict(zip(("x", "y"), xy((n.lat, n.lon)), strict=True)),
                }
                for n in g.nodes
            ],
            manual_edges=[e.model_dump(by_alias=True) for e in g.edges],
            bridge_max_m=g.bridge_max_m * scale,
            attach_max_m=g.attach_max_m * scale,
            exposed_radius_m=g.exposed_radius_m * scale,
            open_space_max_m=g.open_space_max_m * scale,
        )
        for edge in graph.edges:  # exposed_poi names the POI, not its graph node
            if edge.exposed_poi:
                edge.exposed_poi = edge.exposed_poi.removeprefix("poi:")
        profiles = {k: v.model_dump() for k, v in DEFAULT_PROFILES.items()}
        profiles.update({k: v.model_dump() for k, v in self.people_routing_profiles.items()})
        return {
            "route_graph": graph.model_dump(by_alias=True),
            "route_graph_notes": notes,
            "documents": [d.model_dump() for d in self.documents],
            "people_routing_profiles": profiles,
        }


def _rad(deg: float) -> float:
    import math

    return math.atan2(math.sin(math.radians(deg)), math.cos(math.radians(deg)))


def _resolve(path: str, base_dir: Path | None) -> Path:
    p = Path(path)
    return p if p.is_absolute() or base_dir is None else (base_dir / p)


def polygon_problem(poly: list[LatLon] | list[tuple[float, float]]) -> str | None:
    """None if ``poly`` is a valid simple polygon; else a description."""
    pts = [tuple(p) for p in poly]
    if len(pts) > 3 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return "needs at least 3 distinct vertices"
    if len(set(pts)) != len(pts):
        return "repeated vertices"
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for j in range(i + 1, n):
            if j in (i, (i + 1) % n) or (j + 1) % n == i:
                continue  # adjacent edges share a vertex
            c, e = pts[j], pts[(j + 1) % n]
            if _segments_cross(a, b, c, e):
                return "self-intersecting"
    area = sum(
        pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
        for i in range(len(pts))
    )
    if abs(area) < 1e-14:
        return "zero area"
    return None


def _segments_cross(a, b, c, d) -> bool:
    def orient(p, q, r):
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return (v > 0) - (v < 0)

    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return o1 != o2 and o3 != o4 and 0 not in (o1, o2, o3, o4)


def building_height(b: dict[str, Any], campus: "CampusV2") -> float:
    """OSM ``height``, else ``building:levels`` x ``level_height_m``, else the default
    (the rule of the Gazebo world generator)."""
    if b.get("height"):
        return float(b["height"])
    if b.get("levels"):
        return float(b["levels"]) * campus.world.level_height_m
    return campus.world.default_building_height_m


def osm_obstacle_heights(campus: "CampusV2", cache: Path) -> list[float | None]:
    """Heights (real metres) aligned with ``osm_obstacles``: buildings, then trees (None:
    foliage is not an obstruction for the line-of-sight test)."""
    if not cache.exists():
        return []
    data = json.loads(cache.read_text())
    d = campus.projection()
    out: list[float | None] = []
    for b in data.get("buildings", []):
        ring = [d.xy(lat, lon) for lat, lon in b["outer"]]
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        if len(ring) >= 3:
            out.append(building_height(b, campus))
    out += [None] * len(data.get("trees", []))
    return out


def osm_obstacles(campus: CampusV2, cache: Path) -> list[list[tuple[float, float]]]:
    """Building footprints (and trees as small squares) in campus_map, deterministic order."""
    if not cache.exists():
        raise ValueError(
            f"world.osm_cache {cache} not found: run `a6g campus build` (fetches the OSM extract)"
        )
    data = json.loads(cache.read_text())
    d = campus.projection()
    out = []
    for b in data.get("buildings", []):
        ring = [d.xy(lat, lon) for lat, lon in b["outer"]]
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        if len(ring) >= 3:
            out.append(ring)
    r = campus.world.tree_radius_scaled_m
    for t in data.get("trees", []):
        x, y = d.xy(t["lat"], t["lon"])
        out.append(
            [
                (round(x - r, 3), round(y - r, 3)),
                (round(x + r, 3), round(y - r, 3)),
                (round(x + r, 3), round(y + r, 3)),
                (round(x - r, 3), round(y + r, 3)),
            ]
        )
    return out


def _site_entry(
    n: "NetworkSiteV2",
    at: tuple[float, float],
    obstacles: list[list[tuple[float, float]]],
    heights: list[float | None],
    scale: float,
) -> dict[str, Any]:
    """A gNB site in campus_map; on a rooftop its height is the building's plus the mast."""
    from a6g_common.geometry import point_in_polygon

    def gap(poly: list[tuple[float, float]]) -> float:
        if point_in_polygon(at, poly):
            return 0.0
        best = math.inf
        for i, a in enumerate(poly):
            b = poly[(i + 1) % len(poly)]
            dx, dy = b[0] - a[0], b[1] - a[1]
            t = max(
                0.0,
                min(
                    1.0, ((at[0] - a[0]) * dx + (at[1] - a[1]) * dy) / ((dx * dx + dy * dy) or 1.0)
                ),
            )
            best = min(best, math.dist(at, (a[0] + t * dx, a[1] + t * dy)))
        return best

    host = None
    hosts = [
        (gap(o), i) for i, o in enumerate(obstacles) if i < len(heights) and heights[i] is not None
    ]
    if hosts:
        g, i = min(hosts)
        if g <= 5.0 * scale:
            host = i
    if n.height_m is not None:
        height, source = n.height_m, "campus file"
    elif host is not None:
        height = float(heights[host]) + n.mast_m
        source = f"rooftop: building {host} ({heights[host]:g} m) + mast {n.mast_m:g} m"
    else:
        height, source = 20.0 + n.mast_m, "no building found within 5 m: 20 m + mast"
    return {
        "site_id": n.id,
        "name": n.name,
        "x": at[0],
        "y": at[1],
        "height_m": round(height, 2),
        "height_source": source,
        "host_building": host,
        "lat": n.lat,
        "lon": n.lon,
    }


def world_name(campus_id: str, scale: float) -> str:
    """``upv_vera_cpi`` at scale 1, ``upv_vera_cpi_s0p1`` at 0.1."""
    return campus_id if scale == 1.0 else f"{campus_id}_s{scale:g}".replace(".", "p")


class ScaledProjection:
    """A datum projection whose planar coordinates are multiplied by ``scale``."""

    def __init__(self, datum: Datum, scale: float = 1.0) -> None:
        self.datum, self.scale = datum, scale

    def xy(self, lat_deg: float, lon_deg: float, ndigits: int = 3) -> tuple[float, float]:
        x, y, _ = self.datum.to_enu(lat_deg, lon_deg)
        return round(x * self.scale, ndigits), round(y * self.scale, ndigits)

    def to_wgs84(self, x: float, y: float, z: float = 0.0) -> tuple[float, float, float]:
        return self.datum.to_wgs84(x / self.scale, y / self.scale, z)


class FreeSpace:
    """Cells with ``clearance`` from every obstacle that connect to ``origin`` (scaled campus).

    A small campus with real-size robots closes some passages; points snapped into this set
    are both roomy and reachable from the staging area.
    """

    def __init__(
        self,
        obstacles,
        bounds: tuple[float, float, float, float],
        clearance: float,
        origin: tuple[float, float],
        res: float = 0.1,
    ) -> None:
        import numpy as np

        x0, y0, x1, y1 = bounds
        self.x0, self.y0, self.res = x0, y0, res
        xs = x0 + (np.arange(int((x1 - x0) / res) + 1) + 0.5) * res
        ys = y0 + (np.arange(int((y1 - y0) / res) + 1) + 0.5) * res
        gx, gy = np.meshgrid(xs, ys)
        free = np.ones(gx.shape, dtype=bool)
        for poly in obstacles:
            px = [x for x, _ in poly]
            py = [y for _, y in poly]
            # Only cells within the polygon's box + clearance can be inside or too close.
            c0, c1 = np.searchsorted(xs, [min(px) - clearance, max(px) + clearance])
            r0, r1 = np.searchsorted(ys, [min(py) - clearance, max(py) + clearance])
            if c0 >= c1 or r0 >= r1:
                continue
            wx, wy = gx[r0:r1, c0:c1], gy[r0:r1, c0:c1]
            free[r0:r1, c0:c1] &= ~(
                _np_inside(wx, wy, poly) | (_np_edge_distance(wx, wy, poly) < clearance)
            )
        self.gx, self.gy = gx, gy
        reach = np.zeros(free.shape, dtype=bool)
        start = self._cell(origin)
        if free[start]:
            reach[start] = True
            while True:  # 4-connected flood fill, a whole front per numpy step
                grown = reach.copy()
                grown[1:, :] |= reach[:-1, :]
                grown[:-1, :] |= reach[1:, :]
                grown[:, 1:] |= reach[:, :-1]
                grown[:, :-1] |= reach[:, 1:]
                grown &= free
                if np.array_equal(grown, reach):
                    break
                reach = grown
        self.gx, self.gy, self.ok = gx, gy, reach

    def _cell(self, p: tuple[float, float]) -> tuple[int, int]:
        r = int((p[1] - self.y0) / self.res)
        c = int((p[0] - self.x0) / self.res)
        return max(0, min(r, self.gx.shape[0] - 1)), max(0, min(c, self.gx.shape[1] - 1))

    def snap(self, p: tuple[float, float]) -> tuple[float, float]:
        import numpy as np

        if self.ok[self._cell(p)] or not self.ok.any():
            return p
        d = np.where(self.ok, (self.gx - p[0]) ** 2 + (self.gy - p[1]) ** 2, np.inf)
        r, c = np.unravel_index(int(np.argmin(d)), d.shape)
        return round(float(self.gx[r, c]), 3), round(float(self.gy[r, c]), 3)


def _np_inside(gx, gy, poly):
    import numpy as np

    inside = np.zeros(gx.shape, dtype=bool)
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if y1 == y2:
            continue
        cond = (y1 > gy) != (y2 > gy)
        inside ^= cond & (gx < x1 + (gy - y1) * (x2 - x1) / (y2 - y1))
    return inside


def _np_edge_distance(gx, gy, poly):
    import numpy as np

    best = np.full(gx.shape, np.inf)
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        t = np.clip(((gx - x1) * dx + (gy - y1) * dy) / (dx * dx + dy * dy or 1e-12), 0.0, 1.0)
        best = np.minimum(best, np.hypot(gx - x1 - t * dx, gy - y1 - t * dy))
    return best
