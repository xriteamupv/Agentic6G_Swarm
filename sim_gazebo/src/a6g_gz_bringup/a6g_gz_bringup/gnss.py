"""Simulated GNSS error (SPEC A1 8; ROS-free, unit-tested).

White noise plus a first-order Gauss-Markov bias per horizontal axis (a slow random walk
that stays bounded at ``bias_sigma_m``), random dropouts, and optional dropouts inside
coverage shadows.
"""

import math
import random
from dataclasses import dataclass, field

EARTH_RADIUS_M = 6378137.0


def point_in_polygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


@dataclass
class GnssNoiseModel:
    sigma_m: float = 0.02
    bias_sigma_m: float = 0.0
    bias_tau_s: float = 300.0
    dropout_per_min: float = 0.0
    dropout_s: float = 0.0
    seed: int = 0
    shadows: list = field(default_factory=list)  # polygons in campus_map
    dropout_in_shadows: bool = False
    _rng: random.Random = field(init=False, repr=False)
    _bias: list = field(init=False)
    _t: float | None = field(init=False, default=None)
    _dropout_until: float = field(init=False, default=-1.0)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self._bias = [self._rng.gauss(0.0, self.bias_sigma_m) for _ in range(2)]

    @property
    def variance_m2(self) -> float:
        return self.sigma_m**2 + self.bias_sigma_m**2

    def step(self, t: float, x: float = 0.0, y: float = 0.0) -> tuple[float, float] | None:
        """Error (east, north) in metres at time ``t``; None while the fix is dropped.

        ``x, y`` (true position in campus_map) is only used for coverage-shadow dropouts.
        """
        dt = 0.0 if self._t is None else max(0.0, t - self._t)
        self._t = t
        if self.bias_sigma_m > 0 and dt > 0:
            a = math.exp(-dt / self.bias_tau_s)
            s = self.bias_sigma_m * math.sqrt(1 - a * a)
            self._bias = [a * b + self._rng.gauss(0.0, s) for b in self._bias]
        starts = 1 - math.exp(-self.dropout_per_min / 60.0 * dt)
        if self.dropout_per_min > 0 and t >= self._dropout_until and self._rng.random() < starts:
            self._dropout_until = t + self.dropout_s
        if t < self._dropout_until:
            return None
        if self.dropout_in_shadows and any(point_in_polygon(x, y, p) for p in self.shadows):
            return None
        return (
            self._bias[0] + self._rng.gauss(0.0, self.sigma_m),
            self._bias[1] + self._rng.gauss(0.0, self.sigma_m),
        )


def offset_latlon(lat: float, lon: float, east_m: float, north_m: float) -> tuple[float, float]:
    """Small ENU offset applied to a WGS84 position (spherical approximation, mm-level here)."""
    dlat = math.degrees(north_m / EARTH_RADIUS_M)
    dlon = math.degrees(east_m / (EARTH_RADIUS_M * math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon
