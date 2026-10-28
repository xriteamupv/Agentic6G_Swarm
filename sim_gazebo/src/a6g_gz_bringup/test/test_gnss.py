import itertools
import math
import statistics

from a6g_gz_bringup.gnss import GnssNoiseModel, offset_latlon


def test_rtk_noise_is_centimetre_level():
    m = GnssNoiseModel(sigma_m=0.02, seed=1)
    errs = [m.step(t * 0.2) for t in range(2000)]
    assert all(e is not None for e in errs)
    assert statistics.pstdev(e[0] for e in errs) == __import__("pytest").approx(0.02, rel=0.1)


def test_bias_is_slow_and_bounded():
    m = GnssNoiseModel(sigma_m=0.0, bias_sigma_m=2.0, bias_tau_s=300.0, seed=2)
    xs = [m.step(t * 1.0)[0] for t in range(60000)]  # 200 correlation times
    steps = [abs(b - a) for a, b in itertools.pairwise(xs)]
    assert statistics.mean(steps) < 0.2  # slow: ~0.13 m mean change between 1 Hz fixes
    assert 1.6 < statistics.pstdev(xs) < 2.4  # stationary around 2 m


def test_dropouts_and_shadows():
    m = GnssNoiseModel(sigma_m=0.1, dropout_per_min=6, dropout_s=5, seed=3)
    out = [m.step(t * 0.2) for t in range(3000)]  # 10 minutes
    dropped = sum(e is None for e in out) * 0.2
    assert 100 < dropped < 500  # ~6/min x 5 s
    shadow = [[(0, 0), (10, 0), (10, 10), (0, 10)]]
    off = GnssNoiseModel(shadows=shadow)
    on = GnssNoiseModel(shadows=shadow, dropout_in_shadows=True)
    assert off.step(0.0, 5, 5) is not None
    assert on.step(0.0, 5, 5) is None and on.step(1.0, 20, 5) is not None


def test_offset_latlon_metres():
    lat, lon = offset_latlon(39.48, -0.335, 10.0, -5.0)
    dn = math.radians(lat - 39.48) * 6378137.0
    de = math.radians(lon + 0.335) * 6378137.0 * math.cos(math.radians(39.48))
    assert (de, dn) == __import__("pytest").approx((10.0, -5.0), abs=1e-6)
