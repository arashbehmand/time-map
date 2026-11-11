"""No error response or log line may carry a token, an upstream URL or an upstream body."""
import logging

import httpx
import pytest
from conftest import BODY_MARKER, ROUTING_TOKEN, TILES_TOKEN, assert_clean

from server.warp.mapbox_client import MapboxClient

ISO = ("post", "/api/isochrones", {"longitude": -0.12, "latitude": 51.5, "travel_times": [10]})
WARP = ("post", "/api/warp-params", {"longitude": -0.12, "latitude": 51.5, "travel_times": [5, 10]})
SOLVE = ("post", "/api/meetup/solve", {
    "participants": [
        {"id": "a", "lon": -0.12, "lat": 51.5, "mode": "walking"},
        {"id": "b", "lon": -0.10, "lat": 51.51, "mode": "walking"},
    ],
})
TILE = ("get", "/tiles/14/8185/5448", None)
GLYPH = ("get", "/glyphs/Open Sans Regular,Arial Unicode MS Regular/0-255.pbf", None)

CASES = [("isochrone", ISO), ("isochrone", WARP), ("matrix", SOLVE), ("tiles", TILE), ("glyphs", GLYPH)]


def _call(c, method, path, body):
    return c.post(path, json=body) if method == "post" else c.get(path)


def _connect_error(request: httpx.Request):
    raise httpx.ConnectError(f"cannot reach {request.url}", request=request)


def _timeout(request: httpx.Request):
    raise httpx.ReadTimeout(f"timed out {request.url}", request=request)


def _no_durations(request: httpx.Request):
    return httpx.Response(200, json={"code": "InvalidInput", "message": BODY_MARKER})


def _bad_json(request: httpx.Request):
    return httpx.Response(200, text=f"<html>{BODY_MARKER}</html>")


@pytest.mark.parametrize("api,case", CASES)
@pytest.mark.parametrize("behaviour", [401, 422, 500, _connect_error, _timeout, _bad_json])
def test_upstream_failures_are_fixed_and_clean(mapbox, matrix_client, caplog, api, case, behaviour):
    caplog.set_level(logging.DEBUG)
    mapbox.behaviour[api] = behaviour
    resp = _call(matrix_client, *case)
    if api in ("tiles", "glyphs") and behaviour is _bad_json:
        assert resp.status_code == 200  # binary passthrough; nothing to parse
        return
    if api == "glyphs":
        # A failed glyph range is served empty so MapLibre still draws the tile.
        assert resp.status_code == 200 and resp.content == b"\x0a\x00"
        assert resp.headers["cache-control"] == "no-store"
        assert_clean(caplog.text)
        return
    assert resp.status_code in (502, 504), resp.text
    body = resp.json()
    assert body["code"] in ("upstream_error", "upstream_timeout")
    assert body["detail"].startswith("The map service")
    assert resp.headers["cache-control"] == "no-store"
    assert_clean(resp.text)
    assert_clean(caplog.text)


def test_matrix_reply_without_durations(mapbox, matrix_client, caplog):
    caplog.set_level(logging.DEBUG)
    mapbox.behaviour["matrix"] = _no_durations
    resp = _call(matrix_client, *SOLVE)
    assert resp.status_code == 502
    assert_clean(resp.text)
    assert_clean(caplog.text)


def test_unexpected_exception_is_generic_and_log_is_redacted(client, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)

    async def boom(*_a, **_kw):
        raise RuntimeError(f"failed https://api.mapbox.com/x?access_token={ROUTING_TOKEN} {BODY_MARKER}")

    monkeypatch.setattr(MapboxClient, "fetch_isochrones", boom)
    resp = _call(client, *ISO)
    assert resp.status_code == 500
    assert resp.json() == {"code": "internal_error", "detail": "Something went wrong. Please try again."}
    assert_clean(resp.text)
    assert "unhandled RuntimeError at" in caplog.text  # type and code locations...
    assert "test_errors.py" in caplog.text
    assert_clean(caplog.text)  # ...but not the message
    assert caplog.text.count("unhandled RuntimeError") == 1


def test_log_redaction_covers_args_and_unknown_pk_tokens(caplog):
    caplog.set_level(logging.DEBUG)
    log = logging.getLogger("time_map.test")
    log.warning("url %s", f"https://x/?access_token={TILES_TOKEN}&a=1")
    log.warning("other pk.%s", "ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ")
    try:
        raise ValueError(TILES_TOKEN)
    except ValueError:
        log.exception("caught")
    assert TILES_TOKEN not in caplog.text
    assert "ZZZZZZZZZZZZZZZZZZZZ" not in caplog.text


def test_validation_error_does_not_echo_input(client):
    resp = client.post("/api/isochrones", json={"longitude": "51.123456789", "latitude": None})
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_request"
    assert "51.123456789" not in resp.text


def test_missing_tile_is_204(mapbox, client):
    mapbox.behaviour["tiles"] = 404
    resp = client.get("/tiles/14/8185/5448")
    assert resp.status_code == 204


def test_warped_map_is_gone(client):
    resp = client.post("/api/warped-map", json={"longitude": 0, "latitude": 0})
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
