"""Retrieval precision: the three retrievals and their bench.

- observations near a pose (``kb_observations_near``);
- statuses and notices a route reports: only those that shaped it;
- interaction context at a hand-over: a robot leaving restores a lost store entry first.
"""

import asyncio
import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from a6g_common.clock import ManualClock
from a6g_common.config import load_config
from a6g_common.schemas import StatusReport
from a6g_devstack import bench_retrieval as B
from a6g_interactions import InteractionClient, InteractionStore
from a6g_interactions.store import COORDINATOR
from a6g_kb import KnowledgeClient, KnowledgeService, build_kb_server
from a6g_kb.index import HashingEmbedder
from a6g_robot_agent import guidance

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def cfg():
    return load_config("config", env={}, fleet_file="sim/fleet.yaml")


def _service(cfg) -> KnowledgeService:
    return KnowledgeService(
        cfg.campus, cfg.knowledge, embedder=HashingEmbedder(), clock=ManualClock(T0)
    )


def _obs(oid: str, label: str, x: float, y: float, minutes_ago: float = 5, **kw) -> dict:
    stamp = (T0 - timedelta(minutes=minutes_ago)).isoformat()
    return {"obs_id": oid, "label": label, "pose": {"x": x, "y": y}, "stamp": stamp, **kw}


def test_observations_near_a_pose(cfg) -> None:
    kb = _service(cfg)
    assert not kb.add_observation(_obs("o0", "puddle", 0, 0), "go2_01")["accepted"]  # ACL
    refused = kb.add_observation(
        _obs("o1", "person sitting", 0, 0, person_present=True), "coordinator"
    )
    assert not refused["accepted"]  # nothing about a person is kept
    for oid, label, x in (
        ("near_hazard", "fallen tree blocking the path", 5),
        ("near_bin", "litter bin overflowing", 2),
        ("far_hazard", "fallen tree blocking the path", 80),
        ("old_hazard", "broken glass on the ground", 3),
    ):
        ago = cfg.knowledge.observation_ttl_min + 10 if oid == "old_hazard" else 5
        assert kb.add_observation(_obs(oid, label, x, 0, ago), "coordinator")["accepted"]
    here = {"x": 0.0, "y": 0.0}
    by_query = [o["obs_id"] for o in kb.observations_near(here, "obstacles blocking the path")]
    assert by_query[0] == "near_hazard"  # the relevant one first, the bin after
    assert "far_hazard" not in by_query and "old_hazard" not in by_query  # radius, retention
    closest = [o["obs_id"] for o in kb.observations_near(here)]
    assert closest == ["near_bin", "near_hazard"]  # no query: the closest first


async def test_the_observation_tools_over_mcp(cfg) -> None:
    server = build_kb_server(_service(cfg))
    writer = KnowledgeClient(server, agent_id="coordinator")
    robot = KnowledgeClient(server, agent_id="go2_01")
    assert (await writer.add_observation(_obs("o1", "puddle on the walkway", 1, 1)))["accepted"]
    assert not (await robot.add_observation(_obs("o2", "puddle", 1, 1)))["accepted"]
    found = await robot.observations_near({"x": 0, "y": 0}, "puddle")
    assert [o["obs_id"] for o in found] == ["o1"]


def test_a_route_reports_only_the_statuses_that_shaped_it(cfg) -> None:
    kb = _service(cfg)
    router = kb._router()
    goal = "poi_bus_stop"
    base = kb.plan_route(B.path_point(random.Random(3), cfg), goal)
    on_it = base["edges"][len(base["edges"]) // 2]
    used = B._touched(
        router, SimpleNamespace(**{k: base[k] for k in ("start_node", "goal_node", "edges")})
    )
    far = next(
        e.id
        for e in cfg.campus.route_graph.edges
        if e.source == "osm" and not (B._elements(router, e.id) & used)
    )
    for sid, subject in (("st_on", on_it), ("st_far", far)):
        kb.statuses.put(
            StatusReport(
                status_id=sid, subject=subject, status="closed", source="operator", stamp=T0
            ),
            T0,
        )
    route = kb.plan_route(base["legs"][0][0], goal)
    assert route["ok"], route["blocking"]  # a way around exists
    assert "st_on" in route["statuses_applied"]  # it went around this one
    assert "st_far" not in route["statuses_applied"]  # nowhere near: not reported


async def test_a_robot_leaving_restores_a_lost_entry_for_the_next_one() -> None:
    store = InteractionStore(60, ManualClock(T0))
    view = store.create("int_1", {"lang": "es", "profile": "step_free"}, ["go2_01"], "go2_01")
    b = SimpleNamespace(
        local={"int_1": {**view, "stage": "escorting"}},
        robot_id="go2_01",
        interactions=InteractionClient(store, agent_id="go2_01"),
    )
    store.clear()  # the store restarted during the escort
    left = await guidance.release(b, "int_1", meeting_pose={"x": 1.0, "y": 2.0})
    assert left is not None and "int_1" not in b.local  # B forgot its copy
    store.update("int_1", COORDINATOR, participants=["go2_02"])  # the hand-over
    c = SimpleNamespace(
        local={}, robot_id="go2_02", interactions=InteractionClient(store, agent_id="go2_02")
    )
    entry, how = await guidance.fetch(c, "int_1")
    assert how == "store" and entry["content"] == {
        "lang": "es",
        "profile": "step_free",
        "meeting_pose": {"x": 1.0, "y": 2.0},
    }
    spy = SimpleNamespace(
        local={}, robot_id="go2_03", interactions=InteractionClient(store, agent_id="go2_03")
    )
    assert (await guidance.fetch(spy, "int_1"))[0] is None  # not a participant


def test_the_bench_runs_unattended_and_is_deterministic(cfg) -> None:
    first = asyncio.run(B.run(cfg, seeds=8))
    again = asyncio.run(B.run(cfg, seeds=8))
    for r in (first, again):
        r.pop("stamp")
    assert first == again
    obs, route, hand = first["observations"], first["route_context"], first["handover"]
    p = obs["r_normalised_p_at_k"]
    assert p["query"]["1"] > p["distance_only"]["1"]  # the question helps
    assert route["router"]["precision"] == 1.0 and route["all_in_force"]["precision"] < 0.5
    assert hand["store_kept"]["precision_at_1"] == 1.0
    assert hand["store_lost_10pct_during_escort"]["precision_at_1"] == 1.0  # restored
    assert all(h["leaks"] == 0 for h in hand.values())
    assert "UC2-KPI-15" in B.summary(first)
