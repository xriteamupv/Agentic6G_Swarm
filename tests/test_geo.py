"""WGS84 <-> campus_map."""

import math

import pytest
from a6g_common.geo import Datum

# Placeholder datum: Ciudad Politécnica de la Innovación entrance (assumed; not surveyed).
DATUM = Datum(lat_deg=39.4786757, lon_deg=-0.3335511, alt_m=10.0)

# WGS84 reference values (independent of pymap3d): meridional radius M and prime-vertical
# radius N at the datum latitude give metres per degree.
A, F = 6378137.0, 1 / 298.257223563
E2 = F * (2 - F)


def _radii(lat_deg: float) -> tuple[float, float]:
    s = math.sin(math.radians(lat_deg))
    m = A * (1 - E2) / (1 - E2 * s * s) ** 1.5
    n = A / math.sqrt(1 - E2 * s * s)
    return m, n


def test_datum_is_origin() -> None:
    assert DATUM.xy(DATUM.lat_deg, DATUM.lon_deg) == (0.0, 0.0)


@pytest.mark.parametrize("dlat,dlon", [(0.001, 0.0), (0.0, 0.001), (-0.0007, 0.0012)])
def test_small_offsets_match_reference_radii(dlat: float, dlon: float) -> None:
    m, n = _radii(DATUM.lat_deg)
    expected_y = math.radians(dlat) * m
    expected_x = math.radians(dlon) * n * math.cos(math.radians(DATUM.lat_deg))
    x, y, _ = DATUM.to_enu(DATUM.lat_deg + dlat, DATUM.lon_deg + dlon)
    assert x == pytest.approx(expected_x, abs=0.02)  # ~111 m offsets: centimetre agreement
    assert y == pytest.approx(expected_y, abs=0.02)


def test_round_trip_within_a_millimetre() -> None:
    for x, y in [(0, 0), (150, -120), (-140.5, 133.25), (0.001, 299.0)]:
        lat, lon, _ = DATUM.to_wgs84(x, y)
        bx, by, _ = DATUM.to_enu(lat, lon)
        assert (bx, by) == pytest.approx((x, y), abs=1e-3)


def test_heading_rotates_the_campus_axes() -> None:
    rotated = Datum(DATUM.lat_deg, DATUM.lon_deg, DATUM.alt_m, heading_deg=90.0)
    lat, lon, _ = DATUM.to_wgs84(0.0, 100.0)  # 100 m north
    x, y, _ = rotated.to_enu(lat, lon)
    assert (x, y) == pytest.approx((100.0, 0.0), abs=1e-3)  # campus x axis points north
    lat2, lon2, _ = rotated.to_wgs84(x, y)
    assert (lat2, lon2) == pytest.approx((lat, lon), abs=1e-9)
