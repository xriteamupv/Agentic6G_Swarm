"""SPEC Part A3, stage E1: campus schema v3 (outdoor knowledge base) and its validation."""

import copy
import json
from pathlib import Path

import pytest
import yaml
from a6g_campus_tools.build import load_campus_v2 as _load_v2
from a6g_campus_tools.build import route_graph_artifact
from a6g_campus_tools.osm import parse_overpass_paths
from a6g_common.config import ConfigError, load_campus
from a6g_common.routegraph import DEFAULT_PROFILES, build_route_graph

ROOT = Path(__file__).resolve().parents[1]
CAMPUS = ROOT / "config" / "campus" / "upv_vera_cpi.yaml"


def _pilot() -> dict:
    data = yaml.safe_load(CAMPUS.read_text())
    data["world"]["osm_cache"] = str((CAMPUS.parent / data["world"]["osm_cache"]).resolve())
    return data


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "campus.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return path


def _poi(data: dict, poi_id: str) -> dict:
    return next(p for p in data["pois"] if p["id"] == poi_id)


def test_the_pilot_campus_v3_loads_with_a_connected_outdoor_graph() -> None:
    campus = load_campus(CAMPUS)
    graph = campus.route_graph
    assert graph is not None and len(graph.components()) == 1
    node_ids = {n.id for n in graph.nodes}
    assert all(p.node_id in node_ids for p in campus.pois)  # every POI is on the graph
    assert "staging" in node_ids
    assert {e.exposed_poi for e in graph.edges if e.exposed} == {
        p.poi_id for p in campus.pois if p.exposed
    }
    step_free = campus.people_routing_profiles["step_free"]
    shelters = [p.node_id for p in campus.pois if "shelter" in p.tags]
    for poi in campus.pois:
        if poi.exposed or "escort_dest" in poi.tags:
            assert any(graph.shortest_path(poi.node_id, s, step_free.allows) for s in shelters)
    bus = campus.poi("poi_bus_stop")
    assert "parada de autobús" in bus.aliases and "parada d'autobús" in bus.aliases
    assert bus.flood and bus.flood["risk_levels_m"]["high"] > bus.flood["risk_levels_m"]["elevated"]


def test_a_v2_campus_file_still_loads(tmp_path: Path) -> None:
    data = _pilot()
    data["schema_version"] = 2
    for key in ("route_graph", "documents", "people_routing_profiles", "patrol_zones"):
        data.pop(key)
    for sector in data["sectors"]:  # v2 sectors are drawn: a square around each seed
        lat, lon = sector.pop("seed_wgs84")
        d = 0.0001
        sector["polygon_wgs84"] = [
            [lat - d, lon - d],
            [lat - d, lon + d],
            [lat + d, lon + d],
            [lat + d, lon - d],
        ]
    for poi in data["pois"]:
        for key in ("aliases", "building", "flood"):
            poi.pop(key, None)
    campus = load_campus(_write(tmp_path, data))
    assert campus.route_graph is None and campus.documents == []
    assert campus.poi("poi_bus_stop").exposed  # the v2 content is unchanged


def _manual_graph(data: dict, nodes: list[tuple[str, str]], edges: list[dict]) -> dict:
    """A manual-only graph whose nodes sit on the given POIs (the OSM paths the zones name
    are gone: the zones go back to the automatic split)."""
    for sector in data["sectors"]:
        sector.pop("edges", None)
    data.get("patrol_zones", {}).pop("exclude_edges", None)
    data["route_graph"] = {
        "source": "manual",
        "bridge_max_m": 1.0,  # nothing gets bridged: what is drawn is what exists
        "attach_max_m": 2.0,  # only the POIs sitting on a node attach
        "nodes": [
            {"id": nid, "lat": _poi(data, pid)["lat"], "lon": _poi(data, pid)["lon"]}
            for nid, pid in nodes
        ],
        "edges": edges,
    }
    return data


def test_validation_rejects_a_disconnected_graph(tmp_path: Path) -> None:
    data = _manual_graph(
        _pilot(),
        [
            ("a", "poi_bus_stop"),
            ("b", "poi_shelter_south"),
            ("c", "poi_library_door"),
            ("d", "poi_shelter_9b"),
        ],
        [{"id": "ab", "from": "a", "to": "b"}, {"id": "cd", "from": "c", "to": "d"}],
    )
    with pytest.raises(ConfigError, match="route graph is not connected"):
        load_campus(_write(tmp_path, data))


def test_validation_rejects_an_exposed_poi_with_no_step_free_route_to_a_shelter(
    tmp_path: Path,
) -> None:
    data = _manual_graph(
        _pilot(),
        [("a", "poi_bus_stop"), ("b", "poi_shelter_south")],
        [{"id": "steps", "from": "a", "to": "b", "stairs": True, "step_free": False}],
    )
    with pytest.raises(ConfigError, match="poi_bus_stop: no shelter reachable on a step-free"):
        load_campus(_write(tmp_path, data))


def test_validation_rejects_a_document_about_an_unknown_id(tmp_path: Path) -> None:
    data = _pilot()
    data["documents"].append(
        {"id": "doc_x", "kind": "notice", "text": "Gate closed", "applies_to": ["gate_99"]}
    )
    with pytest.raises(
        ConfigError, match=r"document doc_x applies to unknown id\(s\) \['gate_99'\]"
    ):
        load_campus(_write(tmp_path, data))


def test_validation_rejects_an_exposed_poi_without_flood_parameters(tmp_path: Path) -> None:
    data = _pilot()
    _poi(data, "poi_bike_rack").pop("flood")
    with pytest.raises(ConfigError, match="exposed poi poi_bike_rack has no flood parameters"):
        load_campus(_write(tmp_path, data))


def test_flood_risk_levels_must_increase(tmp_path: Path) -> None:
    data = _pilot()
    _poi(data, "poi_bus_stop")["flood"]["risk_levels_m"]["high"] = 0.01
    with pytest.raises(ConfigError, match="risk levels must increase"):
        load_campus(_write(tmp_path, data))


def test_the_route_graph_is_deterministic() -> None:
    """Generator determinism (SPEC A1.5) also holds for the graph."""
    campus = _load_v2(CAMPUS)
    first = route_graph_artifact(campus, CAMPUS.parent)
    second = route_graph_artifact(_load_v2(CAMPUS), CAMPUS.parent)
    assert first is not None and first == second
    built = ROOT / "sim_gazebo/src/a6g_gz_worlds/sim/upv_vera_cpi_route_graph.json"
    assert json.loads(built.read_text()) == json.loads(first)  # the committed build is current
    a = load_campus(CAMPUS).route_graph.model_dump(by_alias=True)
    b = load_campus(CAMPUS).route_graph.model_dump(by_alias=True)
    assert a == b


def test_osm_footways_map_to_edges_with_their_accessibility() -> None:
    overpass = {
        "elements": [
            {
                "type": "way",
                "id": 1,
                "nodes": [10, 11, 12],
                "geometry": [
                    {"lat": 0.0, "lon": 0.0},
                    {"lat": 0.0, "lon": 0.0001},
                    {"lat": 0.0, "lon": 0.0002},
                ],
                "tags": {"highway": "footway", "incline": "8%", "covered": "yes"},
            },
            {
                "type": "way",
                "id": 2,
                "nodes": [12, 13],
                "geometry": [{"lat": 0.0, "lon": 0.0002}, {"lat": 0.0001, "lon": 0.0002}],
                "tags": {"highway": "steps"},
            },
            {"type": "way", "id": 3, "nodes": [], "geometry": [], "tags": {"highway": "motorway"}},
        ]
    }
    ways = parse_overpass_paths(overpass)
    assert [w["id"] for w in ways] == ["w1", "w2"]  # no motorways
    graph, _notes = build_route_graph(
        ways,
        lambda lat, lon: (lon * 100000.0, lat * 100000.0),
        area=None,
        obstacles=[],
        attach={},
        exposed={},
    )
    walk = next(e for e in graph.edges if e.id.startswith("w1"))
    steps = next(e for e in graph.edges if e.id.startswith("w2"))
    assert walk.sheltered and walk.slope_pct == 8.0 and walk.step_free and not walk.stairs
    assert steps.stairs and not steps.step_free
    assert {n.id for n in graph.nodes} == {"n10", "n12", "n13"}  # junctions and ends only
    assert not DEFAULT_PROFILES["step_free"].allows(steps)
    assert not DEFAULT_PROFILES["avoid_slopes"].allows(walk)  # 8 % > 6 %
    assert graph.shortest_path("n10", "n13") is not None
    assert graph.shortest_path("n10", "n13", DEFAULT_PROFILES["step_free"].allows) is None


def test_pieces_are_bridged_through_free_space_but_never_through_a_building() -> None:
    xy = lambda lat, lon: (lon, lat)  # noqa: E731 - metres directly
    ways = [
        {"id": "w1", "nodes": ["a", "b"], "line": [(0, 0), (0, 10)], "tags": {"highway": "path"}},
        {"id": "w2", "nodes": ["c", "d"], "line": [(0, 15), (0, 25)], "tags": {"highway": "path"}},
    ]
    attach = {"p1": ((0.0, 1.0), "poi"), "p2": ((25.0, 1.0), "poi")}  # map x, y
    graph, _notes = build_route_graph(
        ways, xy, area=None, obstacles=[], attach=attach, exposed={}, bridge_max_m=6
    )
    assert len(graph.components()) == 1 and any(e.source == "bridge" for e in graph.edges)
    wall = [[(11, -5), (14, -5), (14, 5), (11, 5)]]  # a building between the two pieces
    graph, _notes = build_route_graph(
        copy.deepcopy(ways),
        xy,
        area=None,
        obstacles=wall,
        attach=attach,
        exposed={},
        bridge_max_m=6,
    )
    assert len(graph.components()) == 2 and not any(e.source == "bridge" for e in graph.edges)
