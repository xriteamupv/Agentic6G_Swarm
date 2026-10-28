"""WGS84 <-> ``campus_map`` (local ENU about the campus datum).

``campus_map`` is East-North-Up at the datum, optionally rotated by ``heading_deg`` (the
angle of the campus x axis, counter-clockwise from east). Conversions use pymap3d's
WGS84 ellipsoid implementation.
"""

import math
from dataclasses import dataclass

import pymap3d


@dataclass(frozen=True)
class Datum:
    lat_deg: float
    lon_deg: float
    alt_m: float = 0.0
    heading_deg: float = 0.0

    def to_enu(
        self, lat_deg: float, lon_deg: float, alt_m: float | None = None
    ) -> tuple[float, float, float]:
        """WGS84 -> campus_map (x, y, z) in metres."""
        e, n, u = pymap3d.geodetic2enu(
            lat_deg,
            lon_deg,
            self.alt_m if alt_m is None else alt_m,
            self.lat_deg,
            self.lon_deg,
            self.alt_m,
        )
        if self.heading_deg:
            c, s = (
                math.cos(math.radians(self.heading_deg)),
                math.sin(math.radians(self.heading_deg)),
            )
            e, n = c * e + s * n, -s * e + c * n
        return float(e), float(n), float(u)

    def to_wgs84(self, x: float, y: float, z: float = 0.0) -> tuple[float, float, float]:
        """campus_map (x, y, z) -> WGS84 (lat, lon, alt)."""
        e, n = x, y
        if self.heading_deg:
            c, s = (
                math.cos(math.radians(self.heading_deg)),
                math.sin(math.radians(self.heading_deg)),
            )
            e, n = c * x - s * y, s * x + c * y
        lat, lon, alt = pymap3d.enu2geodetic(e, n, z, self.lat_deg, self.lon_deg, self.alt_m)
        return float(lat), float(lon), float(alt)

    def xy(self, lat_deg: float, lon_deg: float, ndigits: int = 3) -> tuple[float, float]:
        """Planar campus_map coordinates (on the datum's altitude), rounded to mm."""
        x, y, _ = self.to_enu(lat_deg, lon_deg)
        return round(x, ndigits), round(y, ndigits)
