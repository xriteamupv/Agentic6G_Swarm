"""Live campus view served by the simulated stacks (``/view``)."""

from a6g_common.config import load_config
from a6g_devstack.view import PAGE, campus_geometry, view_routes
from starlette.applications import Starlette
from starlette.testclient import TestClient


def test_view_routes_serve_page_campus_and_state() -> None:
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")

    async def state():
        return {
            "sim_time_label": "08:00:05",
            "robots": {"go2_01": {"x": 1.0, "y": 2.0, "yaw": 0.3}},
        }

    client = TestClient(Starlette(routes=view_routes(cfg, state, "a6g test")))
    page = client.get("/view")
    assert page.status_code == 200 and "<title>a6g test</title>" in page.text
    geo = client.get("/sim/view/campus").json()
    assert {s["id"] for s in geo["sectors"]} == {"sec_north", "sec_south", "sec_east"}
    # no shadow polygons: the emulated coverage is drawn instead
    assert len(geo["buildings"]) > 20 and geo["coverage"] and geo["recovery"] and geo["sites"]
    assert geo["robots"]["summit_01"]["platform"] == "summit"
    assert client.get("/sim/view/state").json()["robots"]["go2_01"]["x"] == 1.0


def test_campus_geometry_follows_the_scale() -> None:
    small = load_config("config", env={"A6G_WORLD_SCALE": "0.1"}, fleet_file="sim/fleet.yaml")
    geo = campus_geometry(small)
    assert geo["scale"] == 0.1
    xs = [x for x, _ in geo["pilot_area"]]
    assert max(xs) - min(xs) < 40  # ~34 m across instead of ~345 m


def test_page_is_self_contained() -> None:
    assert "http://" not in PAGE and "https://" not in PAGE  # no external assets
