"""Campus world/map generator (no network: uses a copy of the cache)."""

import json
import re
from pathlib import Path

import numpy as np
import pytest
import yaml
from a6g_campus_tools.build import build
from a6g_common.config import load_config
from a6g_common.geo import Datum

REPO = Path(__file__).resolve().parents[1]
CACHE = REPO / "sim_gazebo" / "data" / "osm" / "upv_vera_cpi.json"
DATUM = Datum(39.4785856, -0.3330281, 10.0)


def small_campus(tmp_path: Path) -> Path:
    """A 120 m x 120 m test campus over the cached extract, with a POI at the datum."""
    lat0, lon0, _ = DATUM.to_wgs84(-60, -60)
    lat1, lon1, _ = DATUM.to_wgs84(60, 60)
    bbox = [round(lat0, 6), round(lon0, 6), round(lat1, 6), round(lon1, 6)]
    cache = json.loads(CACHE.read_text())
    cache["bbox_wgs84"] = bbox  # same footprints, smaller window: no re-fetch
    (tmp_path / "osm.json").write_text(json.dumps(cache))

    def ll(x, y):
        lat, lon, _ = DATUM.to_wgs84(x, y)
        return [lat, lon]

    campus = {
        "schema_version": 2,
        "campus_id": "test_campus",
        "datum": {
            "lat_deg": DATUM.lat_deg,
            "lon_deg": DATUM.lon_deg,
            "alt_m": DATUM.alt_m,
            "heading_deg": 0.0,
        },
        "pilot_sector": {"bbox_wgs84": bbox},
        "world": {"osm_cache": "osm.json", "output_dir": "out"},
        "staging_area": {"lat": DATUM.lat_deg, "lon": DATUM.lon_deg},
        "sectors": [
            {
                "id": "s1",
                "name": "S1",
                "polygon_wgs84": [ll(5, 5), ll(40, 5), ll(40, 30), ll(5, 30)],
            }
        ],
        "pois": [{"id": "at_datum", "name": "Datum", "lat": DATUM.lat_deg, "lon": DATUM.lon_deg}],
    }
    path = tmp_path / "campus.yaml"
    path.write_text(yaml.safe_dump(campus))
    return path


def test_generator_is_deterministic(tmp_path: Path) -> None:
    campus = small_campus(tmp_path)
    a = build(campus, out_dir=tmp_path / "a")
    b = build(campus, out_dir=tmp_path / "b")
    assert [p.relative_to(tmp_path / "a") for p in a.files] == [
        p.relative_to(tmp_path / "b") for p in b.files
    ]
    for pa, pb in zip(a.files, b.files, strict=True):
        assert pa.read_bytes() == pb.read_bytes(), pa.name
    names = {p.name for p in a.files}
    assert names == {
        "test_campus.sdf",
        "test_campus.markers.json",
        "test_campus.pgm",
        "test_campus.yaml",
        "test_campus_loc.pgm",
        "test_campus_loc.yaml",
        "test_campus_obstacles.yaml",
    }


def test_poi_at_datum_is_world_origin_and_navsat_round_trips(tmp_path: Path) -> None:
    result = build(small_campus(tmp_path), out_dir=tmp_path / "o")
    files = {p.name: p for p in result.files}
    markers = json.loads(files["test_campus.markers.json"].read_text())
    poi = next(p for p in markers["pois"] if p["id"] == "at_datum")
    assert abs(poi["x"]) < 1e-6 and abs(poi["y"]) < 1e-6  # the GUI overlay's frame = world
    world = files["test_campus.sdf"].read_text()
    lat = float(re.search(r"<latitude_deg>([-\d.]+)</latitude_deg>", world).group(1))
    lon = float(re.search(r"<longitude_deg>([-\d.]+)</longitude_deg>", world).group(1))
    assert "<world_frame_orientation>ENU</world_frame_orientation>" in world
    # A NavSat reading at world (0, 0) reports the spherical-coordinates origin: it must
    # convert back to the campus datum within 0.05 m (and so must other world points).
    for wx, wy in [(0.0, 0.0), (45.0, -30.0)]:
        reading = Datum(lat, lon, DATUM.alt_m).to_wgs84(wx, wy)
        x, y, _ = DATUM.to_enu(reading[0], reading[1])
        assert abs(x - wx) <= 0.05 and abs(y - wy) <= 0.05


def test_map_and_world_share_the_footprints(tmp_path: Path) -> None:
    result = build(small_campus(tmp_path), out_dir=tmp_path / "o")
    files = {p.name: p for p in result.files}
    meta = yaml.safe_load(files["test_campus.yaml"].read_text())
    assert meta["resolution"] == 0.05 and meta["mode"] == "trinary"
    raw = files["test_campus.pgm"].read_bytes()
    header, _, rest = raw.partition(b"\n255\n")
    w, h = map(int, header.split(b"\n")[-1].split())
    assert len(rest) == w * h and set(rest) <= {0, 254}
    assert 0 in rest  # buildings are occupied
    obstacles = yaml.safe_load(files["test_campus_obstacles.yaml"].read_text())["obstacles"]
    world = files["test_campus.sdf"].read_text()
    building_collisions = re.findall(r'<collision name="[wr][^"]*_c"><geometry><polyline>', world)
    assert len(building_collisions) == result.buildings  # one extruded footprint per building
    assert world.count("<polyline>") == 2 * result.buildings  # collision + visual
    assert len(obstacles) == result.buildings + result.trees


def _pgm(path: Path) -> np.ndarray:
    header, _, rest = path.read_bytes().partition(b"\n255\n")
    w, h = map(int, header.split(b"\n")[-1].split())
    return np.frombuffer(rest, dtype=np.uint8).reshape(h, w)


def test_localization_map_has_outlines_and_unknown_interiors(tmp_path: Path) -> None:
    result = build(small_campus(tmp_path), out_dir=tmp_path / "o")
    files = {p.name: p for p in result.files}
    filled, loc = _pgm(files["test_campus.pgm"]), _pgm(files["test_campus_loc.pgm"])
    assert filled.shape == loc.shape
    assert (
        yaml.safe_load(files["test_campus_loc.yaml"].read_text())["image"] == "test_campus_loc.pgm"
    )
    # Free space is identical; every building cell is either outline (occupied) or interior
    # (unknown, which trinary maps read as neither free nor occupied).
    assert np.array_equal(filled == 254, loc == 254)
    inside = filled == 0
    assert set(np.unique(loc[inside])) == {0, 128}
    assert (loc == 128).sum() > (loc == 0).sum()  # buildings are larger than their walls
    # Interior cells are at least the outline thickness away from free space.
    rows, cols = np.nonzero(loc == 128)
    r, c = rows[len(rows) // 2], cols[len(cols) // 2]
    k = round(0.30 / 0.05)
    assert (filled[r - k : r + k + 1, c - k : c + k + 1] == 0).all()


def test_runtime_campus_obstacles_match_the_generated_file(config_dir: Path) -> None:
    campus = load_config(config_dir, env={}).campus
    generated = (
        REPO / "sim_gazebo" / "src" / "a6g_gz_worlds" / "sim" / "upv_vera_cpi_obstacles.yaml"
    )
    if not generated.exists():
        pytest.skip("run `a6g campus build` first")
    data = yaml.safe_load(generated.read_text())["obstacles"]
    assert [[tuple(p) for p in o] for o in data] == [
        [tuple(p) for p in o] for o in campus.obstacles
    ]
