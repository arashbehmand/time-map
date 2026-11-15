"""The travel-time field's maths (server/warp/field.py): frames, contours → minutes, the
transit merge, dequantising, cropping and the wire format."""
import math

import numpy as np
import pytest
from conftest import circle_isochrones

from server.warp import field as F

ORIGIN = F.LocalFrame(-0.1233, 51.5308)


# The vector tests/js/mesh-decode.test.js checks against frame.js.
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
    # A wide hole in one contour only is slow ground, not an uncertain patch.
    assert not F.uncertain_patches(t, [10, 20], grid, F.WALK_M_PER_MIN).any()


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
    out, still, unsure = F.merge_near(walk, comp, filled, grid, radius_m=2000)
    assert out[np.abs(X) <= 1000].tolist() == walk[np.abs(X) <= 1000].tolist()
    assert (out[(np.abs(X) > 1400) & (np.abs(X) <= 2000)] == 20).all()   # transit is faster
    assert (out[(np.abs(X) > 2000) & (np.abs(X) <= 2500)] == 20).all()   # beyond 2 km: transit only
    assert np.isinf(out[np.abs(X) > 2500]).all()
    assert not still.any()                          # walking replaced the filled nodes
    assert not unsure.any()


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


def test_dequantise_leaves_the_nodes_it_is_told_to():
    t = np.repeat(np.floor(np.arange(20) / 3.0)[None, :], 4, axis=0)
    only = np.zeros(t.shape, bool)
    only[:, 10:] = True
    d = F.dequantise(t, only=only)
    assert (d[:, :10] == t[:, :10]).all()
    assert not (d[:, 10:] == t[:, 10:]).all()
    assert np.all(np.abs(d - t) <= 0.5 + 1e-12)


def test_a_grid_can_reach_further_one_way_than_the_other():
    g = F.Grid.covering(100, -500, -500, 56_000, 500, max_half=800)
    assert g.x0 == -500 and g.x0 + (g.nx - 1) * 100 >= 56_000
    assert F.Grid.covering(100, -500, -500, 56_000, 500).nx == 5 + 300 + 1   # default: ±300 cells


# ── Uncertain patches ────────────────────────────────────────────────
def _rect(x0, y0, x1, y1, holes=(), contour=10):
    """A rectangle in lon/lat from local metres (y south), with rectangular holes."""
    def ring(a0, b0, a1, b1):
        pts = [(a0, b0), (a1, b0), (a1, b1), (a0, b1), (a0, b0)]
        return [[float(v) for v in ORIGIN.from_local(x, y)] for x, y in pts]
    return {"type": "Feature", "properties": {"contour": contour},
            "geometry": {"type": "Polygon", "coordinates": [ring(x0, y0, x1, y1), *(ring(*h) for h in holes)]}}


def _contours(spec):
    """[(minutes, half size, holes)] → (field, grid, contour minutes)."""
    feats = [_rect(-h, -h, h, h, holes, contour=m) for m, h, holes in spec]
    regions = F.contour_regions(feats, ORIGIN, float)
    grid = F.Grid.covering(100, -4000, -4000, 4000, 4000)
    return F.contour_field(regions, grid), grid, [m for m, _ in regions]


# King's Cross: the platforms (x −150…150, 150–450 m north) are left out of every contour up
# to 50 minutes; only the 60-minute one covers them.
PLATFORMS = (-150, -450, 150, -150)
KX = [(5, 120, ()), (10, 600, (PLATFORMS,)), (15, 900, (PLATFORMS,)), (20, 1200, (PLATFORMS,)),
      (30, 1800, (PLATFORMS,)), (40, 2400, (PLATFORMS,)), (50, 3000, (PLATFORMS,)), (60, 3600, ())]


def _node(grid, x, y):
    xs, ys = grid.axes()
    return int(np.argmin(np.abs(ys - y))), int(np.argmin(np.abs(xs - x)))


def test_the_platforms_are_an_uncertain_patch_drawn_between_their_rim():
    t, grid, levels = _contours(KX)
    before = t.copy()
    mask = F.uncertain_patches(t, levels, grid, F.WALK_M_PER_MIN)
    patch = [_node(grid, x, y) for x in (-100, 0, 100) for y in (-200, -300, -400)]
    assert all(mask[n] for n in patch) and mask.sum() == len(patch)
    assert all(t[n] > 50 for n in patch)               # the interpolation's time, kept
    drawn, mask2 = F.fill_patches(t, mask)
    assert (mask2 == mask).all() and np.array_equal(t, before, equal_nan=True)
    rim = [_node(grid, x, y) for x in (-200, 200) for y in (-200, -300, -400)] + \
          [_node(grid, x, y) for x in (-100, 0, 100) for y in (-100, -500)]
    lo, hi = min(t[n] for n in rim), max(t[n] for n in rim)
    assert all(lo - 1e-6 <= drawn[n] <= hi + 1e-6 and drawn[n] < 15 for n in patch)
    assert np.isnan(drawn[~mask]).all()
    assert not mask[grid.origin_index()]


def test_a_big_walkable_patch_is_kept_by_the_walk_in_allowance():
    # A park 1,200 m across, in holes of the 10- and 15-minute contours, covered at 20.
    park = (-600, 900, 600, 2100)
    t, grid, levels = _contours([(5, 300, ()), (10, 2500, (park,)), (15, 3000, (park,)), (20, 3500, ())])
    assert not F.uncertain_patches(t, levels, grid, F.WALK_M_PER_MIN).any()
    # Only the size keeps it: with no time to walk in, the same ground would be a patch.
    assert F.uncertain_patches(t, levels, grid, 1e9).any()


def test_a_slow_inlet_open_to_the_outside_is_not_a_patch():
    # Cut out of 10–50 minutes all the way to their edges, covered at 60: the way out is
    # outward, along the inlet itself. Each cut contour is three rectangles around the inlet.
    feats = [_rect(-120, -120, 120, 120, contour=5), _rect(-3600, -3600, 3600, 3600, contour=60)]
    for m, h in ((10, 600), (15, 900), (20, 1200), (30, 1800), (40, 2400), (50, 3000)):
        feats += [_rect(-h, -h, -150, h, contour=m), _rect(150, -h, h, h, contour=m),
                  _rect(-150, -150, 150, h, contour=m)]
    regions = F.contour_regions(feats, ORIGIN, float)
    grid = F.Grid.covering(100, -4000, -4000, 4000, 4000)
    t = F.contour_field(regions, grid)
    assert t[_node(grid, 0, -1000)] > 50                  # slow all along the inlet
    assert not F.uncertain_patches(t, [m for m, _ in regions], grid, F.WALK_M_PER_MIN).any()


def test_an_enclosed_hole_with_no_data_is_a_patch_and_keeps_no_data():
    hole = (-150, 250, 150, 550)
    t, grid, levels = _contours([(5, 120, ())] + [(m, h, (hole,)) for m, h in
                                                  ((10, 600), (15, 900), (20, 1200), (30, 1800),
                                                   (40, 2400), (50, 3000), (60, 3600))])
    inside = [_node(grid, x, y) for x in (-100, 0, 100) for y in (300, 400, 500)]
    assert all(np.isnan(t[n]) for n in inside)
    mask = F.uncertain_patches(t, levels, grid, F.WALK_M_PER_MIN)
    drawn, mask = F.fill_patches(t, mask)
    assert all(mask[n] and 0 < drawn[n] < 15 for n in inside)
    assert all(np.isnan(t[n]) for n in inside)
    assert np.isnan(t[_node(grid, 3900, 3900)]) and not mask[_node(grid, 3900, 3900)]   # outside


def test_a_patch_walk_never_beats_a_table_time():
    grid = F.Grid.covering(100, -3000, 0, 3000, 0)
    X, _ = grid.mesh()
    walk = np.abs(X) / F.WALK_M_PER_MIN
    unsure = np.zeros(X.shape, bool)
    at = lambda x: (0, int(np.argmin(np.abs(X[0] - x))))   # noqa: E731
    for x in (300, -300, 2500):
        walk[at(x)] = 50.0
        unsure[at(x)] = True
    comp = np.full(X.shape, 20.0)
    comp[at(300)] = 55.0
    comp[at(-300)] = np.nan
    out, _, still = F.merge_near(walk, comp, np.zeros(X.shape, bool), grid, 2000, unsure)
    assert out[at(300)] == 55 and not still[at(300)]       # the tables win, even when slower
    assert out[at(-300)] == 50 and still[at(-300)]         # nothing else: kept, still uncertain
    assert out[at(2500)] == 20 and not still[at(2500)]     # beyond 2 km: the tables alone


def test_patches_survive_the_wire_and_the_crop():
    t, grid, levels = _contours(KX)
    mask = F.uncertain_patches(t, levels, grid, F.WALK_M_PER_MIN)
    drawn, mask = F.fill_patches(t, mask)
    m, filled, g, mask_c, drawn_c = F.crop(t, np.zeros(t.shape, bool), grid, 300, mask, drawn)
    assert mask_c.sum() == mask.sum()
    obj = F.encode_uncertain(mask_c, drawn_c)
    back, back_drawn = F.decode_uncertain(obj, m.shape)
    assert (back == mask_c).all()
    assert np.allclose(back_drawn[back], np.round(drawn_c[mask_c] * 10) / 10)
    assert F.encode_uncertain(np.zeros(m.shape, bool), drawn_c) is None
    with pytest.raises(ValueError):
        F.decode_uncertain({**obj, "drawn_b64": ""}, m.shape)


def test_way_out_is_the_best_path_out():
    t = np.array([[np.nan, np.nan, np.nan, np.nan, np.nan],
                  [np.nan, 5.0, 6.0, 7.0, np.nan],
                  [np.nan, 6.0, 40.0, 3.0, np.nan],
                  [np.nan, 4.0, 6.0, 9.0, np.nan],
                  [np.nan, np.nan, np.nan, np.nan, np.nan]])
    w = F.way_out(t)
    assert w[2, 2] == 6.0 and w[1, 1] == 5.0 and np.isposinf(w[0, 0])


def test_a_single_dot_in_one_outline_gap_is_a_sliver_and_caught():
    # A walk at 4 km/h; dots 100 m apart. One dot stands over the 30 line, far above the dots
    # next to it: a gap in one outline (behind houses, a yard). Two such dots side by side, or
    # one only a little above its neighbours, are left as they are.
    grid = F.Grid.covering(100, -3000, -3000, 3000, 3000)
    X, Y = grid.mesh()
    t = np.hypot(X, Y) / F.WALK_M_PER_MIN
    levels = [5, 10, 15, 20, 30, 40, 50, 60]
    at = lambda x, y: _node(grid, x, y)                      # noqa: E731
    t[at(1500, 0)] = 31.0                                     # 22.5 around it: 1 line, up 8.5
    t[at(0, 1800)] = t[at(100, 1800)] = 35.0                  # two dots side by side
    t[at(-1800, 0)] = 31.5                                    # 27 around it: up only 4.5
    mask = F.uncertain_patches(t, levels, grid, F.WALK_M_PER_MIN)
    assert mask[at(1500, 0)]
    assert not mask[at(0, 1800)] and not mask[at(100, 1800)]
    assert not mask[at(-1800, 0)]
    assert mask.sum() == 1
    drawn, mask = F.fill_patches(t, mask)
    assert abs(drawn[at(1500, 0)] - 22.5) < 0.5                # drawn at the times around it
