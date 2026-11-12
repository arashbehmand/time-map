"""The warp's scale, the per-deploy service worker, the shared files and the saved examples."""
import json
import math
import pathlib
import re

import numpy as np

from server.app import static_build_id
from server.meetup.models import SolveResponse
from server.warp.geometry import TILE_SIZE, Viewport, lonlat_to_world
from server.warp.warp import RadialIsochroneWarp
from tests.conftest import circle_isochrones

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"


# ── Scale ────────────────────────────────────────────────────────────
def test_world_pixels_match_maplibre():
    # MapLibre draws a 512-px world at zoom 0; radii must be in the same pixels.
    assert TILE_SIZE == 512
    for z in (0, 11, 14):
        assert lonlat_to_world(180, 0, z)[0] == 512 * 2**z


def _outer_median(zoom: int) -> float:
    lon, lat = -0.1233, 51.5308
    warp = RadialIsochroneWarp.from_isochrones(
        circle_isochrones(lon, lat, [5, 10, 15, 20]), Viewport(lon, lat, zoom, 1024, 1024),
    )
    return float(np.median(warp.source_radii[-1]))


def test_radii_halve_per_zoom_step():
    # The page computes an answer once and rescales it by 2^(zoom - refZoom).
    assert math.isclose(_outer_median(13), _outer_median(14) / 2, rel_tol=0.01)


# ── Service worker ───────────────────────────────────────────────────
def test_sw_is_versioned_per_build(client):
    first = client.get("/sw.js")
    assert first.headers["cache-control"] == "no-cache"
    assert "javascript" in first.headers["content-type"]
    m = re.search(r'CACHE_NAME = "time-map-([0-9a-f]{12})"', first.text)
    assert m, first.text[:300]
    assert "__BUILD__" not in first.text
    assert client.get("/sw.js").text == first.text


def test_build_id_changes_with_the_files(tmp_path):
    (tmp_path / "a.js").write_text("one")
    (tmp_path / "sw.js").write_text("worker")
    before = static_build_id(tmp_path)
    (tmp_path / "sw.js").write_text("worker, edited")      # the worker itself doesn't count
    assert static_build_id(tmp_path) == before
    (tmp_path / "a.js").write_text("two")
    assert static_build_id(tmp_path) != before


def test_shared_files_are_served(client):
    for path in ("/shared/ui.css", "/shared/api.js", "/shared/latest.js", "/warp/warpmath.js"):
        resp = client.get(path)
        assert resp.status_code == 200, path


def test_app_files_are_revalidated(client):
    # Without this, browsers cache app code heuristically and a deploy can mix old and new
    # modules. no-cache means "ask the server first" (a cheap 304 when unchanged).
    for path in ("/", "/manifest.json", "/sw.js", "/warp/", "/warp/app.js", "/meetup/",
                 "/meetup/example.json", "/shared/api.js", "/icons/mapbox-logo.svg"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers.get("cache-control") == "no-cache", path


# ── Saved examples ───────────────────────────────────────────────────
def _warpmath_constant(name: str) -> str:
    js = (STATIC / "warp/warpmath.js").read_text()
    return re.search(rf"export const {name} = (\{{.*?\}});", js, re.S).group(1)


def test_warp_example_is_drawable():
    ex = json.loads((STATIC / "warp/example.json").read_text())
    assert ex["profile"] == "walking"
    assert f'walking: {ex["ref_zoom"]}' in _warpmath_constant("REF_ZOOM")
    assert ex["contour_minutes"] == [5, 10, 15, 20]
    assert "walking: [5, 10, 15, 20]" in _warpmath_constant("RING_TIMES")
    rows = np.array(ex["source_radii"])
    assert rows.shape == (4, 2048)
    assert np.isfinite(rows).all() and (rows >= 0).all()
    assert (np.diff(rows, axis=0) >= 0).all()          # each line outside the one before
    assert set(ex["origin"]) == {"lon", "lat", "label"}


def test_meetup_example_is_a_valid_answer():
    ex = json.loads((STATIC / "meetup/example.json").read_text())
    result = SolveResponse.model_validate(ex["result"])
    ids = {p["id"] for p in ex["participants"]}
    assert set(result.best.times) == ids
    assert ex["objective"] in ("minimax", "sum")
    assert all(p["mode"] in ("walking", "cycling", "driving") for p in ex["participants"])
