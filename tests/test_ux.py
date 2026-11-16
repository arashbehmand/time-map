"""The per-deploy service worker, the shared files and the saved examples."""
import json
import pathlib
import re

import numpy as np

from server.app import static_build_id
from server.meetup.models import SolveResponse
from server.warp.field import WALK_M_PER_MIN, Grid, decode, decode_uncertain, uncertain_patches

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"


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
    for path in ("/shared/ui.css", "/shared/api.js", "/shared/latest.js", "/shared/transit.js", "/shared/tools.js",
                 "/shared/handoff.js", "/shared/nav.js", "/shared/locate.js", "/from-here/views.js"):
        resp = client.get(path)
        assert resp.status_code == 200, path


def test_app_files_are_revalidated(client):
    # Without this, browsers cache app code heuristically and a deploy can mix old and new
    # modules. no-cache means "ask the server first" (a cheap 304 when unchanged).
    for path in ("/", "/manifest.json", "/sw.js", "/from-here/", "/from-here/app.js", "/meet-up/",
                 "/meet-up/example.json", "/whole-city/", "/whole-city/app.js", "/shared/api.js",
                 "/icons/mapbox-logo.svg"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers.get("cache-control") == "no-cache", path


# ── Saved examples ───────────────────────────────────────────────────
def test_warp_example_is_drawable():
    ex = json.loads((STATIC / "warp/example-field.json").read_text())
    assert ex["mode"] == "walking"
    assert set(ex["origin"]) == {"lon", "lat", "label"}
    minutes, filled = decode(ex["minutes_b64"], (ex["ny"], ex["nx"]))
    j, i = round(-ex["y0"] / ex["cell"]), round(-ex["x0"] / ex["cell"])
    assert minutes[j, i] == 0                          # the pin
    known = minutes[np.isfinite(minutes)]
    assert known.size > 100 and 30 <= known.max() <= 60      # the page opens at 30
    assert not filled.any()


def test_warp_example_draws_the_platforms_small():
    """The check that would have caught the bent map's wedge north of King's Cross (2026-11):
    the platforms 200 and 300 m north of the pin are an uncertain patch, keep the provider's
    minutes (about 50) and are drawn between the station and the streets beyond."""
    ex = json.loads((STATIC / "warp/example-field.json").read_text())
    shape = (ex["ny"], ex["nx"])
    minutes, _ = decode(ex["minutes_b64"], shape)
    patch, drawn = decode_uncertain(ex.get("uncertain"), shape)
    j, i = round(-ex["y0"] / ex["cell"]), round(-ex["x0"] / ex["cell"])
    for step in (2, 3):
        assert patch[j - step, i]
        assert minutes[j - step, i] >= 30                    # kept, for audit
        assert drawn[j - step, i] < 15
    # The server's rule, run on the numbers shipped, finds them too.
    grid = Grid(ex["cell"], ex["x0"], ex["y0"], ex["nx"], ex["ny"])
    levels = [m / WALK_M_PER_MIN for m in (333, 667, 1000, 1333, 2000, 2667, 3333, 4000)]
    again = uncertain_patches(minutes, levels, grid, WALK_M_PER_MIN)
    assert again[j - 2, i] and again[j - 3, i]


def test_meetup_example_is_a_valid_answer():
    ex = json.loads((STATIC / "meetup/example.json").read_text())
    result = SolveResponse.model_validate(ex["result"])
    ids = {p["id"] for p in ex["participants"]}
    assert set(result.best.times) == ids
    assert ex["objective"] in ("minimax", "sum")
    assert all(p["mode"] in ("walking", "cycling", "driving") for p in ex["participants"])
