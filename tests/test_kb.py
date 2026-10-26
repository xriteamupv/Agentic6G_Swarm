"""SPEC Part A3, stage E2: knowledge server and status layer (outdoors)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from a6g_allocator.cost import route_distance
from a6g_common.config import load_config
from a6g_common.routegraph import DEFAULT_PROFILES, build_route_graph
from a6g_common.schemas import DecisionKind, Pose2D, StatusReport
from a6g_devstack import SimStack
from a6g_kb import KnowledgeClient, KnowledgeService, build_kb_server
from a6g_kb.index import HashingEmbedder
from a6g_kb.routing import MAX_LEG_WAYPOINTS, Router
from a6g_kb.status import StatusLayer
from conftest import wait_for

T0 = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def cfg():
    return load_config("config", env={}, fleet_file="sim/fleet.yaml")


@pytest.fixture
def service(cfg):
    return KnowledgeService(cfg.campus, cfg.knowledge, embedder=HashingEmbedder())


def _status(subject: str, status: str, source: str, at: datetime = T0) -> StatusReport:
    return StatusReport(
        status_id=f"st_{subject}_{source.replace(':', '_')}_{status}",
        subject=subject,
        status=status,
        source=source,
        stamp=at,
    )


# --------------------------------------------------------------------------- routes


def _toy_graph():
    """A square A-B-C-D with stairs as a shortcut A-C (metres directly)."""
    ways = [
        {
            "id": "w1",
            "nodes": ["A", "B", "C"],
            "line": [(0, 0), (0, 20), (20, 20)],
            "tags": {"highway": "footway"},
        },
        {
            "id": "w2",
            "nodes": ["C", "D", "A"],
            "line": [(20, 20), (20, 0), (0, 0)],
            "tags": {"highway": "footway"},
        },
        {"id": "w3", "nodes": ["A", "C"], "line": [(0, 0), (20, 20)], "tags": {"highway": "steps"}},
    ]
    graph, _ = build_route_graph(
        ways, lambda lat, lon: (lon, lat), area=None, obstacles=[], attach={}, exposed={}
    )
    return graph


def test_step_free_routes_never_use_stairs() -> None:
    router = Router(_toy_graph(), DEFAULT_PROFILES)
    layer = StatusLayer()
    stairs = {e.id for e in router.graph.edges if e.stairs}
    default = router.plan("A", "C", "default", statuses=layer, now=T0)
    step_free = router.plan("A", "C", "step_free", statuses=layer, now=T0)
    assert default.ok and set(default.edges) <= stairs  # the stairs are the short way
    assert step_free.ok and not set(step_free.edges) & stairs
    assert step_free.length_m == 40.0 and default.length_m == pytest.approx(28.3, abs=0.1)
    # Without step-free alternatives there is no route, and the profile is the reason.
    only_stairs = Router(_toy_graph(), DEFAULT_PROFILES)
    only_stairs.graph.edges = [e for e in only_stairs.graph.edges if e.stairs]
    blocked = only_stairs.plan("A", "C", "step_free", statuses=layer, now=T0)
    assert not blocked.ok and "excluded by the profile" in blocked.blocking[0]


def test_pilot_routes_come_in_follow_route_legs(service) -> None:
    route = service.plan_route("poi_bus_stop", "poi_library_door", "step_free")
    assert route["ok"] and route["route_id"].startswith("route_")
    assert all(2 <= len(leg) <= MAX_LEG_WAYPOINTS for leg in route["legs"])
    first, last = route["legs"][0][0], route["legs"][-1][-1]
    bus, door = service.campus.poi("poi_bus_stop").pose, service.campus.poi("poi_library_door").pose
    assert (first["x"], first["y"]) == pytest.approx((bus.x, bus.y), abs=0.01)
    assert (last["x"], last["y"]) == pytest.approx((door.x, door.y), abs=0.01)
    assert route["est_time_s"] == pytest.approx(route["length_m"] / 0.8, rel=0.01)
    shelter = service.nearest_shelter("poi_bike_rack", "step_free")
    assert shelter["ok"] and shelter["destination"] in ("poi_shelter_9b", "poi_shelter_south")


# --------------------------------------------------------------------------- rule 27


def test_rule_27_restrictions_apply_at_once_and_only_their_source_or_the_operator_lifts_them(
    service,
) -> None:
    edge = next(e for e in service.campus.route_graph.edges if e.source == "osm")
    service.clock = type("C", (), {"now": staticmethod(lambda: T0)})()
    before = service.plan_route(edge.from_, edge.to, "default")
    assert before["ok"] and edge.id in before["edges"]

    put = service.put_status(_status(edge.id, "obstructed", "robot:go2_01"), "coordinator")
    assert put["accepted"]
    detour = service.plan_route(edge.from_, edge.to, "default")
    assert edge.id not in detour.get("edges", [])  # avoided at once
    assert service.status([edge.id])[edge.id]["status"] == "obstructed"

    other = service.put_status(_status(edge.id, "ok", "robot:go2_02"), "coordinator")
    assert not other["accepted"] and "rule 27" in other["reason"]  # another robot cannot lift it
    assert service.status([edge.id])[edge.id]["restrictive"]

    operator = service.put_status(_status(edge.id, "ok", "operator"), "operator")
    assert operator["accepted"]
    assert edge.id in service.plan_route(edge.from_, edge.to, "default")["edges"]  # open again


def test_status_writes_need_the_coordinator_or_the_operator_and_robot_reports_expire(
    service,
) -> None:
    poi = "poi_bike_rack"
    assert not service.put_status(_status(poi, "flooded", "robot:go2_01"), "go2_01")["accepted"]
    layer = StatusLayer(ttl_min=60)
    layer.put(_status(poi, "flooded", "robot:go2_01"), T0)
    assert layer.effective(poi, T0 + timedelta(minutes=59))[0] == "flooded"
    assert layer.effective(poi, T0 + timedelta(minutes=61))[0] == "ok"  # TTL


def test_an_operator_notice_closes_what_it_names(service) -> None:
    edge = next(e for e in service.campus.route_graph.edges if e.source == "osm")
    answer = service.add_notice(
        {"id": "notice_works", "text": "Path closed for works", "applies_to": [edge.id]},
        "closed",
        "operator",
    )
    assert answer["accepted"] and service.status([edge.id])[edge.id]["status"] == "closed"
    hits = service.search("works closed path", 3)
    assert hits[0]["doc_id"] == "notice_works"
    assert not service.add_notice(
        {"id": "x", "text": "x", "applies_to": ["nope"]}, None, "operator"
    )["accepted"]


# --------------------------------------------------------------------------- places

PLACES = [
    # English
    ("bus stop", "poi_bus_stop"),
    ("Take me to the bus stop please", "poi_bus_stop"),
    ("the info kiosk", "poi_kiosk"),
    ("information point", "poi_kiosk"),
    ("building 9B door", "poi_library_door"),
    ("9B", "poi_library_door"),
    ("bike parking", "poi_bike_rack"),
    ("the bike rack", "poi_bike_rack"),
    ("the east gate", "lrp_east_gate"),
    ("north-west walk", "sec_north"),
    ("south plaza", "sec_south"),
    ("east corridor", "sec_east"),
    ("9B entrance canopy", "poi_shelter_9b"),
    ("south arcade", "poi_shelter_south"),
    # Spanish
    ("parada de autobús", "poi_bus_stop"),
    ("Llévame a la parada del bus", "poi_bus_stop"),
    ("punto de información", "poi_kiosk"),
    ("puerta del edificio 9B", "poi_library_door"),
    ("aparcabicis", "poi_bike_rack"),
    ("marquesina del 9B", "poi_shelter_9b"),
    ("soportales de la plaza sur", "poi_shelter_south"),
    ("paseo noroeste", "sec_north"),
    ("puerta este", "lrp_east_gate"),
    # Valencian
    ("parada d'autobús", "poi_bus_stop"),
    ("porta'm al punt d'informació", "poi_kiosk"),
    ("porta de l'edifici 9B", "poi_library_door"),
    ("aparcament de bicicletes", "poi_bike_rack"),
    ("porxo del 9B", "poi_shelter_9b"),
    ("plaça sud", "sec_south"),
    ("corredor est", "sec_east"),
    # Typos
    ("bus stp", "poi_bus_stop"),
    ("aparcabicis del campus", "poi_bike_rack"),
    ("informacion", "poi_kiosk"),
]


def test_places_resolve_in_three_languages(service) -> None:
    assert len(PLACES) >= 30
    wrong = []
    for text, expected in PLACES:
        found = service.resolve_place(text)
        if not found or found[0]["id"] != expected:
            wrong.append((text, expected, found[:1]))
    assert not wrong, wrong
    assert service.resolve_place("cafeteria") == []  # below resolve_min_score: none


async def test_the_coordinator_falls_back_to_word_overlap_without_the_server(cfg) -> None:
    from a6g_common.models import fake_model
    from a6g_common.schemas import Posture
    from a6g_coordinator.decompose import decompose_goal

    class Down:
        async def resolve_place(self, *_a, **_k):
            raise ConnectionError("knowledge server down")

    robots = {"go2_01": "go2", "go2_02": "go2", "summit_01": "summit-xl"}
    plan = '{"tasks": [{"kind": "escort", "target_type": "poi", "target_id": "poi_kiosk"}]}'
    for kb, expect in (
        (Down(), "fallback_no_kb"),
        (
            KnowledgeClient(
                build_kb_server(KnowledgeService(cfg.campus, cfg.knowledge)), agent_id="coordinator"
            ),
            "kb",
        ),
    ):
        log: list[str] = []
        tasks, _, _, _, _, notes = await decompose_goal(
            "Escort the visitor to the bus stop",
            Posture.INTERACTIVE,
            cfg.campus,
            fake_model([plan]),
            (),
            robots,
            kb=kb,
            kb_log=log,
        )
        assert log == [expect]
        assert [t.target.poi_id for t in tasks] == ["poi_bus_stop"] and not notes


# --------------------------------------------------------------------------- cost function


def test_the_cost_function_travels_on_the_route_graph(cfg) -> None:
    """Hand-computed: A(0,0)-B(0,20)-C(20,20); from (0,-1) to (21,20): 1 + 40 + 1 = 42 m on
    the graph (straight: 29.7 m). Open space: straight, unless a building is in the way."""
    graph = _toy_graph()
    graph.edges = [e for e in graph.edges if e.id.startswith("w1")]  # A-B-C only
    campus = cfg.campus.model_copy(
        update={"route_graph": graph, "obstacles": [], "footpaths_only": False}
    )
    a, b = Pose2D(x=0, y=-1), Pose2D(x=21, y=20)
    assert route_distance(campus, a, b) == pytest.approx(42.0)
    open_campus = campus.model_copy(
        update={"route_graph": graph.model_copy(update={"open_space_max_m": 50.0})}
    )
    assert route_distance(open_campus, a, b) == pytest.approx(29.7, abs=0.05)
    wall = [[(5.0, 5.0), (15.0, 5.0), (15.0, 15.0), (5.0, 15.0)]]
    walled = open_campus.model_copy(update={"obstacles": wall})
    assert route_distance(walled, a, b) == pytest.approx(42.0)
    assert route_distance(cfg.campus.model_copy(update={"route_graph": None}), a, b) is None


# --------------------------------------------------------------------------- end to end


async def test_a_robot_observation_becomes_a_validated_status_and_p10_is_measured(
    tmp_path: Path,
) -> None:
    """A robot sees "fallen tree blocking the path" at the bike rack: it proposes the
    status, the coordinator re-validates and writes it (status_update, validated_by), and
    the knowledge-server calls appear in ``a6g comms report`` as P10."""
    from a6g_cli.comms_report import render

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.inject_anomaly("poi_bike_rack", "fallen tree blocking the path")
        await stack.mission("Check the bike rack", posture="interactive")

        async def written() -> bool:
            return stack.kb_service.status(["poi_bike_rack"])["poi_bike_rack"]["restrictive"]

        await wait_for(written, 60, "status written")
        await stack.coordinator.goal("m1", "Escort the visitor to the bus stop", "interactive")
        state = await stack.coordinator.state("m1")
    finally:
        await stack.stop()
    status = stack.kb_service.status(["poi_bike_rack"])["poi_bike_rack"]
    assert status["status"] == "obstructed" and status["sources"][0].startswith("robot:")
    update = next(d for d in stack.log.read("m1") if d.kind is DecisionKind.STATUS_UPDATE)
    assert update.validated_by == "coordinator" and update.output["accepted"]
    assert any("people routes avoid it" in str(m.content) for m in state["messages"])
    records = [json.loads(line) for line in (tmp_path / "s" / "comms.jsonl").open()]
    p10 = [r for r in records if r["pattern"] == "P10" and r["binding"] == "mcp"]
    assert {r["schema"] for r in p10} >= {"kb_put_status", "kb_resolve_place"}
    assert all(r["payload_bytes"] > 0 for r in p10)
    assert all(r["e2e_ms"] > 0 for r in p10 if r["direction"] == "send")
    text, _ = render("m1", [tmp_path / "s" / "comms.jsonl"], messages=False)
    assert "P10" in text and "mcp" in text
