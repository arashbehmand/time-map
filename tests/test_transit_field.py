"""The runtime transit provider (server/transit/provider.py) on a tiny build."""
import json
import math
from datetime import date, timedelta

import h3
import numpy as np
import pytest
from tiny_transit import POND, A, B, C, cell_of, make_build

from server.transit.provider import TransitData, TransitUnavailable, display_name, window_label
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
    assert m[j, i] <= 5 + 2 and not filled[j, i]                 # B: on the line
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
