"""``a6g campus edit``: interactive zone and point editing (footpaths only, 2026-10-18)."""

from pathlib import Path

import yaml
from a6g_campus_tools.editor import CampusEditor, build_app
from a6g_common.config import load_config
from a6g_common.footpaths import base_id
from starlette.testclient import TestClient


def _editor(config_copy: Path) -> CampusEditor:
    return CampusEditor(config_copy / "campus" / "upv_vera_cpi.yaml")


def test_painting_paths_moving_points_and_saving_keeps_the_rest_of_the_file(
    config_copy: Path,
) -> None:
    ed = _editor(config_copy)
    before = ed.path.read_text()
    state = ed.state()
    assert not state["dirty"] and state["problems"] == []
    north = next(e for e in state["edges"] if e["zone"] == "sec_north" and e["source"] == "osm")
    east = next(e for e in state["edges"] if e["zone"] == "sec_east" and e["source"] == "osm")
    ed.assign(north["id"], "sec_south")  # a north path now belongs to the south zone
    ed.assign(east["id"], "none")  # an east path is no longer patrolled
    kiosk = next(p for p in state["points"] if p["id"] == "poi_kiosk")
    ed.move("poi", "poi_kiosk", kiosk["x"] + 30.0, kiosk["y"] + 3.0)  # snapped to a path
    ed.move("staging", "staging", 0.0, 0.0)
    after = ed.state()
    assert after["dirty"]
    owner = {e["base"]: e["zone"] for e in after["edges"]}
    assert owner[north["base"]] == "sec_south" and owner[east["base"]] is None
    moved = next(p for p in after["points"] if p["id"] == "poi_kiosk")
    assert moved["off_m"] < 0.05 and (moved["x"], moved["y"]) != (kiosk["x"], kiosk["y"])

    backup = ed.save()
    assert backup.read_text() == before
    text = ed.path.read_text()
    import difflib

    changed = [
        line[1:]
        for line in difflib.unified_diff(before.splitlines(), text.splitlines(), lineterm="")
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    # Only the moved points' coordinates and the edges / exclude_edges lists (they wrap).
    import re

    listed = re.compile(r"^\s+(edges:|exclude_edges:)?\s*\[?[\w.:, ]+\]?$")
    assert all("lat:" in line or listed.match(line) for line in changed), changed
    assert "# Footpaths only (decision of 2026-10-18)" in text  # comments survive
    data = yaml.safe_load(text)
    south = next(s for s in data["sectors"] if s["id"] == "sec_south")
    assert north["base"] in south["edges"]
    assert east["base"] in data["patrol_zones"]["exclude_edges"]
    cfg = load_config(config_copy, env={})  # the saved file loads and validates
    assert {base_id(e) for e in cfg.campus.sector("sec_south").path_edges} >= {north["base"]}
    assert not any(base_id(e) == east["base"] for s in cfg.campus.sectors for e in s.path_edges)

    ed.auto()  # back to the automatic split
    assert all(e["manual"] is None for e in ed.state()["edges"])


def test_the_editor_page_and_api(config_copy: Path) -> None:
    client = TestClient(build_app(_editor(config_copy)))
    assert "Campus zone editor" in client.get("/").text
    state = client.get("/api/state").json()
    edge = next(e for e in state["edges"] if e["source"] == "osm" and e["manual"] != "sec_east")
    out = client.post("/api/assign", json={"edge": edge["id"], "zone": "sec_east"}).json()
    assert out["dirty"] and next(e for e in out["edges"] if e["id"] == edge["id"])["manual"]
    assert client.post("/api/revert", json={}).json()["dirty"] is False
    bad = client.post("/api/assign", json={"edge": edge["id"], "zone": "nowhere"})
    assert bad.status_code == 400 and "error" in bad.json()


def test_shadows_are_moved_reshaped_added_and_deleted(config_copy: Path) -> None:
    ed = _editor(config_copy)
    before = ed.path.read_text()
    east = next(sh for sh in ed.state()["shadows"] if sh["id"] == "shadow_east")
    moved = [[x + 10.0, y] for x, y in east["polygon"]] + [[east["polygon"][0][0] + 5.0, 0.0]]
    moved = [[x + 10.0, y] for x, y in east["polygon"]]
    moved.insert(2, [(moved[1][0] + moved[2][0]) / 2 + 4.0, (moved[1][1] + moved[2][1]) / 2])
    ed.shadow("shadow_east", moved)  # moved 10 m east, with a fifth corner
    ed.shadow("shadow_plaza", [[-10, -40], [10, -40], [10, -30], [-10, -30]])
    ed.shadow_quality("shadow_plaza", "degraded")
    import pytest

    with pytest.raises(ValueError, match="self-intersecting"):  # a bow tie is refused
        ed.shadow("shadow_bad", [[0, 0], [10, 10], [10, 0], [0, 10]])
    ed.delete_shadow("shadow_bad")
    state = ed.state()
    assert [sh["id"] for sh in state["shadows"]] == ["shadow_east", "shadow_plaza"]
    ed.save()
    text = ed.path.read_text()
    assert "emulated 5G shadows (TODO(UPV): uplink survey)" in text  # the comment stays
    cfg = load_config(config_copy, env={})
    assert len(cfg.campus.link_shadows) == 1 and len(cfg.campus.link_shadows[0]) == 5
    assert len(cfg.campus.degraded_shadows) == 1
    x0 = min(x for x, _ in cfg.campus.link_shadows[0])
    assert x0 == pytest.approx(min(x for x, _ in east["polygon"]) + 10.0, abs=0.05)
    ed.delete_shadow("shadow_plaza")
    ed.delete_shadow("shadow_east")
    ed.save()
    assert load_config(config_copy, env={}).campus.link_shadows == []
    assert len(ed.path.read_text().splitlines()) == len(before.splitlines()) - 1


async def test_a_degraded_shadow_degrades_the_simulated_link(config_copy: Path) -> None:
    from a6g_common.schemas import LinkQuality
    from a6g_sim.world import SimWorld

    ed = _editor(config_copy)
    home = next(p for p in ed.state()["points"] if p["id"] == "staging")
    x, y = home["x"], home["y"]  # around the staging area, wherever it is
    ed.shadow(
        "shadow_staging",
        [[x - 30, y - 30], [x + 30, y - 30], [x + 30, y + 30], [x - 30, y + 30]],
        "degraded",
    )
    ed.save()
    cfg = load_config(config_copy, env={})
    world = SimWorld(cfg.campus)
    world.add_robot(cfg.manifests["go2_01"])
    assert world.link_quality("go2_01") is LinkQuality.DEGRADED
