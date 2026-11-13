"""The travel-time field's maths (server/warp/field.py): frames, contours → minutes, the
transit merge, dequantising, cropping and the wire format."""
import math

import numpy as np
import pytest
from conftest import circle_isochrones

from server.warp import field as F

ORIGIN = F.LocalFrame(-0.1233, 51.5308)


# The vector tests/js/mesh-frame-twin.test.js checks against frame.js.
FRAME_VECTOR = {"origin": (-0.1233, 51.5308), "point": (-0.0195, 51.5035),
                "local": (7188.271472, 3038.111369)}


def test_local_frame_matches_frame_js_formula():
    f = F.LocalFrame(*FRAME_VECTOR["origin"])
    x, y = f.to_local(*FRAME_VECTOR["point"])
    assert (float(x), float(y)) == pytest.approx(FRAME_VECTOR["local"], abs=1e-3)
    lon, lat = f.from_local(x, y)
    assert (float(lon), float(lat)) == pytest.approx(FRAME_VECTOR["point"], abs=1e-9)


def test_grid_has_the_origin_as_a_node_and_is_bounded():
    g = F.Grid.covering(100, -250, -1234, 990, 10)
    oj, oi = g.origin_index()
    xs, ys = g.axes()
    assert xs[oi] == 0 and ys[oj] == 0
    assert g.x0 <= -250 and g.x0 + (g.nx - 1) * 100 >= 990
    big = F.Grid.covering(100, -1e6, -1e6, 1e6, 1e6)
    assert big.nx == big.ny == F.MAX_SIDE


def _circles(minutes_or_metres, metric="time", lon=ORIGIN.lon, lat=ORIGIN.lat):
    return circle_isochrones(lon, lat, minutes_or_metres, metric)["features"]


def test_circles_give_minutes_linear_in_distance():
    metres = [333, 667, 1000, 1333, 2000, 2667, 3333, 4000]
    regions = F.contour_regions(_circles(metres, "distance"), ORIGIN, lambda m: m / F.WALK_M_PER_MIN)
    grid = F.Grid.covering(100, *F.regions_box(regions, 500))
    t = F.contour_field(regions, grid)
    X, Y = grid.mesh()
    r = np.hypot(X, Y)
    inside = r < 3900
    # 4 km/h: one minute per 66.7 m. The mock's circles are 64-gons, a fraction of a percent.
    assert np.nanmax(np.abs(t[inside] - r[inside] / F.WALK_M_PER_MIN)) < 0.25
    assert np.isnan(t[r > 4100]).all()
    oj, oi = grid.origin_index()
    assert t[oj, oi] == 0


def _square(cx, cy, half, hole=None, contour=10):
    """A square polygon in lon/lat around a point given in local metres."""
    def ll(x, y):
        lon, lat = ORIGIN.from_local(x, y)
        return [float(lon), float(lat)]
    ring = [ll(cx - half, cy - half), ll(cx + half, cy - half), ll(cx + half, cy + half),
            ll(cx - half, cy + half), ll(cx - half, cy - half)]
    rings = [ring]
    if hole:
        hx, hy, hh = hole
        rings.append([ll(hx - hh, hy - hh), ll(hx - hh, hy + hh), ll(hx + hh, hy + hh),
                      ll(hx + hh, hy - hh), ll(hx - hh, hy - hh)])
    return {"type": "Feature", "properties": {"contour": contour},
            "geometry": {"type": "Polygon", "coordinates": rings}}


def test_holes_and_islands_survive():
    feats = [
        _square(0, 0, 1000, hole=(500, 0, 200), contour=10),   # a hole east of the pin
        _square(0, 0, 2000, contour=20),
        _square(3000, 0, 300, contour=20),                     # an island beyond
    ]
    regions = F.contour_regions(feats, ORIGIN, float)
    grid = F.Grid.covering(100, -2500, -2500, 3500, 2500)
    t = F.contour_field(regions, grid)
    xs, ys = grid.axes()
    at = lambda x, y: t[int(np.argmin(np.abs(ys - y))), int(np.argmin(np.abs(xs - x)))]  # noqa: E731
    # In the hole: 200 m past the 10-minute line, 1,500 m short of the 20-minute one.
    assert at(500, 0) == pytest.approx(10 + 10 * 200 / 1700, abs=1e-6)
    assert at(400, 0) < at(500, 0)               # it rises into the hole
    assert at(-500, 0) < 10                      # west, inside the 10-minute square
    assert at(3000, 0) == pytest.approx(10 + 10 * (3000 - 1000) / (3000 - 1000 + 300), abs=0.6)
    assert np.isnan(at(2600, 0))                 # between the square and the island


def test_crossing_contours_still_nest():
    # A 20-minute contour that misses part of the 10-minute one: nesting fills it in.
    feats = [_square(0, 0, 1000, contour=10), _square(600, 0, 1000, contour=20)]
    regions = F.contour_regions(feats, ORIGIN, float)
    grid = F.Grid.covering(100, -1200, -1200, 1800, 1200)
    t = F.contour_field(regions, grid)
    xs, ys = grid.axes()
    j = int(np.argmin(np.abs(ys)))
    row = t[j, (xs >= -900) & (xs <= 0)]
    assert np.isfinite(row).all() and (row <= 10).all()


def test_merge_takes_walking_only_within_2km():
    grid = F.Grid.covering(100, -3000, 0, 3000, 0)
    X, _ = grid.mesh()
    walk = np.abs(X) / F.WALK_M_PER_MIN
    comp = np.full(X.shape, 20.0)
    comp[np.abs(X) > 2500] = np.inf
    filled = np.zeros(X.shape, bool)
    filled[np.abs(X) == 300] = True
    out, still = F.merge_near(walk, comp, filled, grid, radius_m=2000)
    assert out[np.abs(X) <= 1000].tolist() == walk[np.abs(X) <= 1000].tolist()
    assert (out[(np.abs(X) > 1400) & (np.abs(X) <= 2000)] == 20).all()   # transit is faster
    assert (out[(np.abs(X) > 2000) & (np.abs(X) <= 2500)] == 20).all()   # beyond 2 km: transit only
    assert np.isinf(out[np.abs(X) > 2500]).all()
    assert not still.any()                          # walking replaced the filled nodes


def test_dequantise_stays_within_half_a_minute():
    rng = np.random.default_rng(5)
    t = np.floor(rng.uniform(0, 60, (40, 40)))
    t[3, 3] = np.nan
    t[5, 5] = np.inf
    d = F.dequantise(t)
    ok = np.isfinite(t)
    assert np.all(np.abs(d[ok] - t[ok]) <= 0.5 + 1e-12)
    assert np.isnan(d[3, 3]) and np.isinf(d[5, 5])
    ramp = np.repeat(np.floor(np.arange(40) / 3.0)[None, :], 5, axis=0)   # a staircase
    smooth = F.dequantise(ramp)
    assert np.unique(np.round(smooth[2, 6:30], 3)).size > np.unique(ramp[2, 6:30]).size


def test_crop_keeps_the_origin_and_the_known_box():
    grid = F.Grid.covering(100, -5000, -5000, 5000, 5000)
    t = np.full((grid.ny, grid.nx), np.nan)
    oj, oi = grid.origin_index()
    t[oj, oi] = 0
    t[oj + 10, oi + 20] = np.inf
    m, f, g = F.crop(t, np.zeros(t.shape, bool), grid, margin_m=200)
    assert g.x0 == -200 and g.y0 == -200
    assert g.nx == 20 + 5 and g.ny == 10 + 5
    j, i = g.origin_index()
    assert m[j, i] == 0 and np.isinf(m[j + 10, i + 20])


def test_wire_round_trip():
    t = np.array([[0.0, 12.34, np.nan], [np.inf, 179.0, 3276.5]])
    filled = np.array([[False, True, True], [True, False, False]])
    b64 = F.encode(t, filled)
    m, f = F.decode(b64, t.shape)
    assert m[0, 1] == pytest.approx(12.3)
    assert np.isnan(m[0, 2]) and np.isinf(m[1, 0])
    assert m[1, 2] == 3276.5
    assert f.tolist() == [[False, True, False], [False, False, False]]


def test_walking_minutes_are_4kmh():
    assert F.WALK_M_PER_MIN * 60 == pytest.approx(4000)
    assert math.isclose(2000 / F.WALK_M_PER_MIN, 30)
