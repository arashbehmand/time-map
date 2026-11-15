"""The runtime transit provider (server/transit/provider.py) on a tiny build."""
import json
import math
from datetime import date, timedelta

import h3
import numpy as np
import pytest
from tiny_transit import POND, A, B, C, cell_of, make_build

from server.transit.provider import WINDOWS, TransitData, TransitUnavailable, display_name, window_label
from server.warp.field import LocalFrame
from transit.tables import compose_pair


@pytest.fixture
def build(tmp_path):
    return make_build(tmp_path)


@pytest.fixture
def data(build):
    return TransitData.load(build, allow_uncleared=True)


def test_every_cell_equals_compose_pair(data):
    """The last walk: each walkshed cell's time is #1's pair formula, cell by cell."""
    table = data.table("wd08")
    for origin in (A, (-0.1185, 51.5004), C):
        row = data.row(*origin, "wd08")
        per_cell = data.cell_minutes(row)
        op = data.station_near(*origin)
        for cell, got in zip(data.cells.tolist(), per_cell.tolist()):
            want = compose_pair(table, data.walksheds, cell_of(*origin), cell, origin_point=op)
            assert got == pytest.approx(want), (origin, cell)


def test_a_station_pin_starts_from_its_own_row(data):
    assert data.station_near(*A) == 0
    assert data.station_near(A[0] + 0.01, A[1]) is None          # 700 m away
    row = data.row(*A, "wd08")
    assert row[1] == 5                                           # A → B on the line
    assert math.isinf(row[3])                                    # the masked cell


def test_field_on_the_grid_fills_the_pond_and_marks_no_time(data):
    frame = LocalFrame(*A)
    grid = data.grid_for(frame, 100)
    m, filled, _ = data.field(*A, "sa13", frame, grid)
    X, Y = grid.mesh()

    def at(lon, lat):
        x, y = frame.to_local(lon, lat)
        return int(np.argmin(np.abs(Y[:, 0] - y))), int(np.argmin(np.abs(X[0] - x)))

    j, i = at(*POND)
    assert filled[j, i] and np.isfinite(m[j, i])                 # the pond: filled, flagged
    j, i = at(*C)
    assert np.isinf(m[j, i])                                     # Saturday: no time to C's area
    j, i = at(*B)
    # B, a station: the node nearest it takes its own time; the next ones out add the walk.
    assert m[j, i] == 5 and not filled[j, i]
    x, y = frame.to_local(*B)
    d = float(np.hypot(X[j, i + 1] - x, Y[j, i + 1] - y))
    assert m[j, i + 1] <= 5 + d / (4000 / 60) + 1e-9
    assert np.isnan(m[0, 0])                                     # far corner: no data
    m8, _, _ = data.field(*A, "wd08", frame, grid)
    j, i = at(*C)
    assert np.isfinite(m8[j, i]) and m8[j, i] >= 12


def test_raster_holds_the_cell_under_each_pixel(data):
    """Each 50 m pixel holds the walkshed cell under its centre (−1 for none). A node can be
    up to 35 m from its pixel's centre, so along an edge it gets the next cell, about a
    minute away on foot."""
    from server.warp.field import mercator, unmercator
    rx0, ry0, step, raster = data._r
    rng = np.random.default_rng(1)
    exact = total = 0
    for lon, lat in zip(rng.uniform(-0.13, -0.07, 400), rng.uniform(51.495, 51.52, 400)):
        mx, my = mercator(lon, lat)
        iy, ix = int(round((float(my) - ry0) / step)), int(round((float(mx) - rx0) / step))
        clon, clat = unmercator(rx0 + ix * step, ry0 + iy * step)
        centre = h3.str_to_int(h3.latlng_to_cell(float(clat), float(clon), 10))
        pos = int(np.searchsorted(data.cells, np.uint64(centre)))
        want = pos if pos < len(data.cells) and data.cells[pos] == centre else -1
        assert raster[iy, ix] == want
        mine = h3.str_to_int(h3.latlng_to_cell(lat, lon, 10))
        if want >= 0 and mine in set(data.cells.tolist()):
            total += 1
            exact += int(data.cells[want]) == mine
    assert total > 100 and exact / total > 0.7


def test_coverage(data):
    assert data.covered(*A) and data.covered(*B)
    assert data.covered(A[0], A[1] + 0.0025)       # 280 m north: a ring away at most
    assert not data.covered(-0.5, 51.3)            # 25 km away
    assert data.coverage["type"] in ("Polygon", "MultiPolygon")
    lon0, lat0, lon1, lat1 = data.coverage_bbox
    assert lon0 < A[0] < lon1 and lat0 < A[1] < lat1


@pytest.mark.parametrize("kwargs,reason", [
    ({}, "licences_not_cleared"),
    ({"allow_uncleared": True, "on_railway": True}, "override_refused_here"),
])
def test_uncleared_builds_are_refused(build, kwargs, reason):
    with pytest.raises(TransitUnavailable) as e:
        TransitData.load(build, **kwargs)
    assert e.value.reason == reason


def test_a_cleared_build_needs_no_override(tmp_path):
    d = TransitData.load(make_build(tmp_path, cleared=True))
    assert not d.development_only
    assert TransitData.load(make_build(tmp_path / "x"), allow_uncleared=True).development_only


def test_expired_and_incomplete_builds_are_refused(tmp_path):
    old = make_build(tmp_path / "old", valid_until=date.today() - timedelta(days=1))
    with pytest.raises(TransitUnavailable, match="expired"):
        TransitData.load(old, allow_uncleared=True)
    half = make_build(tmp_path / "half", complete=False)
    with pytest.raises(TransitUnavailable, match="incomplete"):
        TransitData.load(half, allow_uncleared=True)


def test_a_published_package_loads_without_fingerprint_files(tmp_path):
    # transit.build's package stage ships the tables, points, walksheds and manifest, not the
    # tt_*.fingerprint.json files: the manifest's record of complete tables stands in for them.
    d = make_build(tmp_path, cleared=True)
    for w in WINDOWS:
        (d / f"tt_{w}.fingerprint.json").unlink()
    with pytest.raises(TransitUnavailable, match="incomplete"):
        TransitData.load(d)                          # no record anywhere: refused
    manifest = json.loads((d / "manifest.json").read_text())
    manifest["tables_fingerprints"] = {w: {"inputs": w} for w in WINDOWS}
    (d / "manifest.json").write_text(json.dumps(manifest))
    assert TransitData.load(d).table("wd08").shape[0] == 4


def test_changed_files_are_refused(build):
    t = np.load(build / "tt_wd13.npy")
    t[0, 1] = 7
    np.save(build / "tt_wd13.npy", t)               # same size, other bytes
    with pytest.raises(TransitUnavailable, match="files_differ"):
        TransitData.load(build, allow_uncleared=True)
    m = json.loads((build / "manifest.json").read_text())
    m["outputs"]["tt_wd13"]["sha256"] = __import__("hashlib").sha256((build / "tt_wd13.npy").read_bytes()).hexdigest()
    m["outputs"]["points.csv"]["bytes"] += 1
    (build / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(TransitUnavailable, match="files_differ"):
        TransitData.load(build, allow_uncleared=True)


def test_status_names_windows_and_credits_once(data):
    s = data.status()
    assert [w["id"] for w in s["windows"]] == ["wd08", "wd13", "wd19", "sa13"]
    assert s["windows"][0]["label"] == "a weekday, 08:00–09:00"
    assert s["credits"] == ["Powered by TfL Open Data", "© OpenStreetMap contributors"]
    assert s["development_only"] is True


def test_names_read_like_names():
    assert display_name("WEMBLEY PARK UND") == "Wembley Park"
    assert display_name("Abbey Wood (London)") == "Abbey Wood"
    assert display_name("Canary Wharf DLR Station") == "Canary Wharf"
    assert display_name("Bond Street (Elizabeth line)") == "Bond Street (Elizabeth line)"
    assert window_label({"day": "saturday", "start": "13:00", "minutes": 60}) == "a Saturday, 13:00–14:00"


def test_station_names_in_the_build(data):
    assert [s.name for s in data.stations] == ["Alpha Road", "Bravo"]


# ── Fixes from review ────────────────────────────────────────────────
def _node(frame, grid, lon, lat):
    x, y = frame.to_local(lon, lat)
    return round((y - grid.y0) / grid.cell), round((x - grid.x0) / grid.cell)


def test_the_last_steps_from_a_station_never_cover_a_gap(data):
    """POND_B is 120 m from B, inside the straight walk's 150 m: it stays a gap (filled and
    flagged, never a station time)."""
    from tiny_transit import POND_B
    frame = LocalFrame(*A)
    grid = data.grid_for(frame, 100)
    m, filled, _ = data.field(*A, "wd08", frame, grid)
    j, i = _node(frame, grid, *POND_B)
    assert filled[j, i]


@pytest.mark.parametrize("twin,expect", [(9, 5), (3, 3)])
def test_points_on_one_node_keep_the_quickest(tmp_path, twin, expect):
    """B and its twin E (5 m apart) land on one node: the quicker of the two wins, whichever
    is listed last."""
    from tiny_transit import E
    d = TransitData.load(make_build(tmp_path, twin=twin), allow_uncleared=True)
    frame = LocalFrame(*A)
    grid = d.grid_for(frame, 100)
    m, _, _ = d.field(*A, "wd08", frame, grid)
    assert m[_node(frame, grid, *B)] == expect
    assert m[_node(frame, grid, *E)] == expect


def test_a_station_pin_gets_no_ring_offset(data):
    """A pin in B's pond (its own cell has no walkshed, so cell access needs a ring) still
    starts from B's own row, with nothing added."""
    from tiny_transit import POND_B
    pin = (POND_B[0], POND_B[1] - 0.0004)            # 75 m from B, in the pond
    assert data.station_near(*pin) == 1
    assert data.row(*pin, "wd08")[0] == 6            # B → A on the table, not 6 + 2k
    assert data.reachable_from(*pin)


def test_coverage_is_the_polygon_and_access_is_separate(data):
    """covered() is the polygon the page enables the chip by; reachable_from() is whether a
    journey can start there. Inside the polygon but out of reach is a different answer."""
    lon, lat = C
    found = None
    for k in range(1, 40):                           # walk north from C in 25 m steps
        p = (lon, lat + k * 25 / 111_320)
        if data.covered(*p) and not data.reachable_from(*p):
            found = p
            break
    assert found, "a covered point out of every walkshed's reach"
    assert not data.covered(-0.5, 51.3)


def test_the_transit_grid_spans_the_whole_coverage_box(data):
    """No ±30 km clamp: from a pin at one corner, the grid still reaches the far corner."""
    lon0, lat0, lon1, lat1 = data.coverage_bbox
    frame = LocalFrame(lon0 + 0.001, lat1 - 0.001)
    grid = data.grid_for(frame, 100)
    xs, ys = frame.to_local(lon1, lat0)
    assert grid.x0 + (grid.nx - 1) * grid.cell >= float(xs) - 100
    assert grid.y0 + (grid.ny - 1) * grid.cell >= float(ys) - 100
