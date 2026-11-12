"""The Mapbox Matrix router: order and units, tiling, strict replies, NoRoute, caps."""
import asyncio
import logging
import math

import httpx
import numpy as np
import pytest
from conftest import FakeClock, assert_clean, matrix_reply

from server.errors import UpstreamError
from server.guard.budget import UsageStore
from server.guard.guard import UpstreamGuard
from server.meetup.limits import matrix_max_coords, matrix_tiles
from server.meetup.routing import (
    MapboxMatrixRouter,
    SolveAllowance,
    gather_or_cancel,
    solve_allowance,
)
from server.settings import Settings


def _router(handler, tmp_path, clock=None, **settings_kw):
    clock = clock or FakeClock()
    s = Settings(usage_db_path=str(tmp_path / "u.db"), **settings_kw)
    big = {"matrix": 10**9}
    guard = UpstreamGuard(s, UsageStore(s.usage_db_path, big, big, clock), clock)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MapboxMatrixRouter("pk.test", client, guard)


def _coords(request: httpx.Request) -> tuple[list, list, list]:
    coords = [tuple(map(float, c.split(","))) for c in request.url.path.rsplit("/", 1)[-1].split(";")]
    src = [int(i) for i in request.url.params["sources"].split(";")]
    dst = [int(i) for i in request.url.params["destinations"].split(";")]
    return coords, src, dst


def _run(router, coro_fn, allowance=None):
    async def go():
        if allowance is not None:
            solve_allowance.set(allowance)
        return await coro_fn(router)
    return asyncio.run(go())


def _roomy():
    return SolveAllowance(elements=10**6, requests={"matrix": 1000, "matrix:driving-traffic": 1000})


# ── order and units ─────────────────────────────────────────────────
# Origin i sits at lat 10, lon i/100; destination j at lat 20, lon j/1000. The reply says
# 60 * (1000 i + j) seconds, so minutes[i][j] must be exactly 1000 i + j.
def _encoded(request):
    coords, src, dst = _coords(request)
    oi = [round(coords[s][0] * 100) for s in src]
    dj = [round(coords[d][0] * 1000) for d in dst]
    body = matrix_reply(request)
    body["durations"] = [[60.0 * (1000 * i + j) for j in dj] for i in oi]
    return httpx.Response(200, json=body)


@pytest.mark.parametrize("mode,n_orig,n_dest", [
    ("walking", 3, 60),            # several destination tiles
    ("driving-traffic", 8, 30),    # origins batched too (10 coordinates a request)
    ("cycling", 1, 1),             # padded to the 2-element minimum
])
def test_matrix_order_and_seconds_to_minutes(tmp_path, mode, n_orig, n_dest):
    calls = []

    def handler(request):
        calls.append(request)
        return _encoded(request)

    router = _router(handler, tmp_path)
    origins = [(i / 100, 10.0) for i in range(n_orig)]
    dests = [(j / 1000, 20.0) for j in range(n_dest)]
    result = _run(router, lambda r: r.duration_matrix(mode, origins, dests), _roomy())
    expected = np.array([[1000 * i + j for j in range(n_dest)] for i in range(n_orig)], dtype=float)
    np.testing.assert_allclose(result.minutes, expected)
    assert result.minutes.shape == (n_orig, n_dest)
    np.testing.assert_allclose([(s.lon, s.lat) for s in result.snaps], dests, atol=1e-6)
    assert not result.unresolved.any()
    assert len(calls) == len(matrix_tiles(n_orig, n_dest, matrix_max_coords(mode)))
    for r in calls:
        coords, _, _ = _coords(r)
        assert len(coords) <= matrix_max_coords(mode)


@pytest.mark.parametrize("max_coords", [25, 10])
def test_tiles_cover_every_pair_once_in_no_more_requests(max_coords):
    for n_orig in range(1, 9):
        for n_dest in (1, 2, 3, 9, 16, 17, 23, 24, 25, 50, 101, 160, 299, 300):
            tiles = matrix_tiles(n_orig, n_dest, max_coords)
            pairs = [(i, j) for o, d in tiles for i in o for j in d]
            assert len(pairs) == len(set(pairs)) == n_orig * n_dest
            assert all(len(o) + len(d) <= max_coords for o, d in tiles)
            # Never more requests than chunking destinations only (all origins in each).
            assert len(tiles) <= math.ceil(n_dest / (max_coords - n_orig))


def test_eight_traffic_drivers_are_batched():
    assert len(matrix_tiles(8, 160, 10)) == 54  # 4 x 6 tiles; destination-only chunking: 80


# ── strict replies ──────────────────────────────────────────────────
def _corrupt(how):
    def handler(request):
        body = matrix_reply(request)
        rows, dests = body["durations"], body["destinations"]
        if how == "no durations":
            del body["durations"]
        elif how == "missing row":
            rows.pop()
        elif how == "extra row":
            rows.append(rows[0])
        elif how == "short row":
            rows[0].pop()
        elif how == "long row":
            rows[0].append(1.0)
        elif how == "string":
            rows[0][0] = "600"
        elif how == "negative":
            rows[0][0] = -60.0
        elif how == "bool":
            rows[0][0] = True
        elif how == "not a list":
            body["durations"] = {"0": rows[0]}
        elif how == "no destinations":
            del body["destinations"]
        elif how == "short destinations":
            dests.pop()
        elif how == "bad location":
            dests[0]["location"] = ["x", 51.5]
        elif how == "location out of range":
            dests[0]["location"] = [0.0, 95.0]
        elif how == "name not a string":
            dests[0]["name"] = 7
        elif how == "no code":
            del body["code"]
        elif how == "InvalidInput":
            body["code"] = "InvalidInput"
        return httpx.Response(200, json=body)
    return handler


BAD = ["no durations", "missing row", "extra row", "short row", "long row", "string", "negative",
       "bool", "not a list", "no destinations", "short destinations", "bad location",
       "location out of range", "name not a string", "no code", "InvalidInput"]


@pytest.mark.parametrize("how", BAD)
def test_a_malformed_reply_is_retried_once_then_a_clean_502(tmp_path, caplog, how):
    caplog.set_level(logging.DEBUG)
    calls = []

    def handler(request):
        calls.append(request)
        return _corrupt(how)(request)

    router = _router(handler, tmp_path)
    allowance = _roomy()
    with pytest.raises(UpstreamError):
        _run(router, lambda r: r.duration_matrix("walking", [(-0.12, 51.5), (-0.1, 51.51)],
                                                 [(-0.11, 51.505), (-0.13, 51.49)]), allowance)
    assert len(calls) == 2
    assert allowance.retries == 1
    assert "upstream bad" in caplog.text
    assert_clean(caplog.text)
    assert "51.5" not in caplog.text


def test_nan_in_the_reply_is_malformed(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        text = httpx.Response(200, json=matrix_reply(request)).text
        head, rest = text.split('"durations":[[', 1)
        return httpx.Response(200, text=head + '"durations":[[NaN' + rest[rest.index(","):],
                              headers={"content-type": "application/json"})

    router = _router(handler, tmp_path)
    with pytest.raises(UpstreamError):
        _run(router, lambda r: r.duration_matrix("walking", [(0.0, 51.5)], [(0.01, 51.5), (0.02, 51.5)]),
             _roomy())
    assert len(calls) == 2


def test_malformed_once_then_good_succeeds(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return _corrupt("short row")(request) if len(calls) == 1 else httpx.Response(200, json=matrix_reply(request))

    router = _router(handler, tmp_path)
    allowance = _roomy()
    result = _run(router, lambda r: r.duration_matrix("walking", [(0.0, 51.5)], [(0.01, 51.5), (0.02, 51.5)]),
                  allowance)
    assert np.isfinite(result.minutes).all()
    assert allowance.retries == 1 and len(calls) == 2


def test_a_null_entry_is_one_unreachable_pair(tmp_path):
    def handler(request):
        body = matrix_reply(request)
        body["durations"][0][1] = None
        return httpx.Response(200, json=body)

    router = _router(handler, tmp_path)
    result = _run(router, lambda r: r.duration_matrix(
        "walking", [(0.0, 51.5), (0.001, 51.5)], [(0.01, 51.5), (0.02, 51.5), (0.03, 51.5)]), _roomy())
    assert np.isinf(result.minutes[0, 1])
    assert np.isfinite(np.delete(result.minutes.ravel(), 1)).all()
    assert not result.unresolved.any()


def test_snapped_points_and_names_are_returned(tmp_path):
    def handler(request):
        body = matrix_reply(request)
        for w in body["destinations"]:
            w["location"] = [w["location"][0], w["location"][1] + 0.001]
            w["name"] = "Baker\x00 Street\n"
        body["destinations"][-1]["name"] = ""
        return httpx.Response(200, json=body)

    router = _router(handler, tmp_path)
    result = _run(router, lambda r: r.duration_matrix("walking", [(0.0, 51.5)], [(0.01, 51.5), (0.02, 51.5)]),
                  _roomy())
    assert result.snaps[0].lat == pytest.approx(51.501)
    assert result.snaps[0].name == "Baker Street"
    assert result.snaps[1].name is None


# ── NoRoute ─────────────────────────────────────────────────────────
POISON = (0.017, 51.5)


def _poisoned(calls):
    def handler(request):
        calls.append(request)
        coords, _, dst = _coords(request)
        if any(coords[d] == pytest.approx(POISON) for d in dst):
            return httpx.Response(200, json={"code": "NoRoute", "message": "No route found"})
        return httpx.Response(200, json=matrix_reply(request))
    return handler


def test_noroute_isolates_and_drops_the_one_bad_destination(tmp_path):
    calls = []
    router = _router(_poisoned(calls), tmp_path)
    dests = [(0.001 * (j + 1), 51.5) for j in range(20)]
    result = _run(router, lambda r: r.duration_matrix("walking", [(0.0, 51.5)], dests), _roomy())
    bad = dests.index(POISON)
    assert result.unresolved.tolist() == [j == bad for j in range(20)]
    assert np.isinf(result.minutes[:, bad]).all()
    assert np.isfinite(np.delete(result.minutes, bad, axis=1)).all()
    assert len(calls) <= 1 + 2 * math.ceil(math.log2(20))


def test_a_wholly_unroutable_tile_is_split_once_then_dropped(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"code": "NoRoute"})

    router = _router(handler, tmp_path)
    dests = [(0.001 * (j + 1), 51.5) for j in range(20)]
    result = _run(router, lambda r: r.duration_matrix("walking", [(0.0, 51.5)], dests), _roomy())
    assert result.unresolved.all()
    assert len(calls) == 3


def test_noroute_without_slack_drops_what_it_cannot_check(tmp_path):
    calls = []
    router = _router(_poisoned(calls), tmp_path)
    dests = [(0.001 * (j + 1), 51.5) for j in range(20)]
    allowance = SolveAllowance(elements=10**6, requests={"matrix": 1})
    result = _run(router, lambda r: r.duration_matrix("walking", [(0.0, 51.5)], dests), allowance)
    assert result.unresolved.all()
    assert len(calls) == 1
    assert allowance.spent_requests["matrix"] == 1


def test_noroute_recovery_waits_for_no_rate_slot(tmp_path):
    calls = []
    clock = FakeClock()
    router = _router(_poisoned(calls), tmp_path, clock=clock, rates_per_min={"matrix": 1})
    dests = [(0.001 * (j + 1), 51.5) for j in range(20)]
    result = _run(router, lambda r: r.duration_matrix("walking", [(0.0, 51.5)], dests), _roomy())
    assert result.unresolved.all()
    assert len(calls) == 1 and clock.slept == []


# ── caps and concurrency ────────────────────────────────────────────
def test_requests_run_at_once_but_never_more_than_the_gate(tmp_path):
    state = {"now": 0, "peak": 0}
    calls = []

    async def handler(request):
        calls.append(request)
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return httpx.Response(200, json=matrix_reply(request))

    router = _router(handler, tmp_path)
    allowance = _roomy()
    dests = [(0.001 * j, 51.5) for j in range(200)]

    async def both(r):
        return await gather_or_cancel([
            r.duration_matrix("walking", [(0.0, 51.5), (0.01, 51.5)], dests),
            r.duration_matrix("driving-traffic", [(0.0, 51.51)] * 3, dests),
        ])

    walking, traffic = _run(router, both, allowance)
    assert np.isfinite(walking.minutes).all() and np.isfinite(traffic.minutes).all()
    assert 2 <= state["peak"] <= 3
    for r in calls:
        coords, _, _ = _coords(r)
        limit = 10 if "/driving-traffic/" in r.url.path else 25
        assert len(coords) <= limit
    assert allowance.spent_requests["matrix"] + allowance.spent_requests["matrix:driving-traffic"] == len(calls)


def test_one_failed_request_cancels_the_rest(tmp_path):
    calls = []

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(401, text="nope")
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=matrix_reply(request))

    router = _router(handler, tmp_path)
    dests = [(0.001 * j, 51.5) for j in range(200)]
    with pytest.raises(UpstreamError):
        _run(router, lambda r: r.duration_matrix("walking", [(0.0, 51.5)], dests), _roomy())
    # Of 9 tiles, only the gate's first three (and at most one that took the failed one's
    # slot before the cancel landed) were sent.
    assert len(calls) <= 4


def test_a_solve_spends_what_its_diagnostics_say_and_stays_inside_the_allowance(mapbox, matrix_client):
    body = {"participants": [
        {"id": "a", "lon": -0.12, "lat": 51.5, "mode": "walking"},
        {"id": "b", "lon": -0.10, "lat": 51.51, "mode": "cycling"},
        {"id": "c", "lon": -0.11, "lat": 51.52, "mode": "driving-traffic"},
    ]}
    resp = matrix_client.post("/api/meetup/solve", json=body)
    assert resp.status_code == 200, resp.text
    diag = resp.json()["diagnostics"]
    sent = mapbox.calls
    elements = sum(len(r.url.params["sources"].split(";")) * len(r.url.params["destinations"].split(";"))
                   for r in sent)
    assert diag["matrix_requests"] == len(sent)
    assert diag["matrix_elements"] == elements <= diag["estimated_matrix_elements"]
