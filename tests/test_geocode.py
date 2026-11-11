"""Place search: one shared queue at <= 1 request/s, cache, coalescing, User-Agent, privacy."""
import asyncio
import logging

import httpx
import pytest
from conftest import FakeClock

from server.errors import Busy
from server.geocode import Geocoder
from server.guard.budget import UsageStore
from server.guard.guard import UpstreamGuard
from server.settings import Settings


def _geocoder(tmp_path, mapbox, clock, **kw):
    s = Settings(usage_db_path=str(tmp_path / "u.db"), **kw)
    store = UsageStore(s.usage_db_path, {"geocode": 100}, {"geocode": 1000}, clock)
    guard = UpstreamGuard(s, store, clock)
    client = httpx.AsyncClient(transport=httpx.MockTransport(mapbox))
    return Geocoder(client, guard, s, clock)


def test_endpoint_and_cache(client, mapbox, caplog):
    caplog.set_level(logging.DEBUG)
    q = {"q": "10 Downing Street, London"}
    first = client.post("/api/geocode", json=q)
    assert first.status_code == 200
    assert first.json() == {"lat": 51.5, "lon": -0.12}
    again = client.post("/api/geocode", json={"q": "  10 downing street,   LONDON "})
    assert again.json() == first.json()
    assert mapbox.count("geocode") == 1
    req = mapbox.calls[0]
    assert req.headers["user-agent"].startswith("time-map/")
    assert req.url.params["limit"] == "1"
    assert "Downing" not in caplog.text


def test_not_found_and_bad_input(client, mapbox):
    mapbox.behaviour["geocode"] = lambda r: httpx.Response(200, json=[])
    resp = client.post("/api/geocode", json={"q": "nowhere at all"})
    assert resp.status_code == 404 and resp.json()["code"] == "not_found"
    assert client.post("/api/geocode", json={"q": "x" * 201}).status_code == 422
    assert client.post("/api/geocode", json={"q": "   "}).status_code == 422


def test_upstream_failure_is_clean(client, mapbox):
    mapbox.behaviour["geocode"] = 500
    resp = client.post("/api/geocode", json={"q": "somewhere"})
    assert resp.status_code == 502
    assert "UPSTREAM" not in resp.text


def test_calls_are_spaced_across_clients(tmp_path, mapbox):
    clock = FakeClock()
    g = _geocoder(tmp_path, mapbox, clock)
    times = []
    orig = mapbox.ok

    def stamp(api, request):
        times.append(clock.monotonic())
        return orig(api, request)

    mapbox.ok = stamp

    async def run():
        await asyncio.gather(g.lookup("a place"), g.lookup("b place"), g.lookup("c place"))

    asyncio.run(run())
    assert len(times) == 3
    assert all(b - a >= 1.1 - 1e-9 for a, b in zip(times, times[1:], strict=False))


def test_identical_concurrent_queries_share_one_request(tmp_path, mapbox):
    g = _geocoder(tmp_path, mapbox, FakeClock())

    async def run():
        return await asyncio.gather(*(g.lookup("Same Place") for _ in range(4)))

    results = asyncio.run(run())
    assert all(r == results[0] for r in results)
    assert mapbox.count("geocode") == 1


def test_queue_is_bounded(tmp_path, mapbox):
    clock = FakeClock()
    g = _geocoder(tmp_path, mapbox, clock, geocode_max_waiting=5)

    async def run():
        await g._lock.acquire()  # an upstream call is in progress
        tasks = [asyncio.create_task(g.lookup(f"place {i}")) for i in range(6)]
        for _ in range(3):
            await asyncio.sleep(0)
        g._lock.release()
        return await asyncio.gather(*tasks, return_exceptions=True)

    results = asyncio.run(run())
    busy = [r for r in results if isinstance(r, Busy)]
    assert len(busy) == 1  # five may wait; the sixth is refused
    assert sum(isinstance(r, dict) for r in results) == 5


def test_per_ip_limit(make_client, settings):
    c = make_client(settings.with_(per_ip_per_min={**settings.per_ip_per_min, "geocode": 1}))
    assert c.post("/api/geocode", json={"q": "one"}).status_code == 200
    assert c.post("/api/geocode", json={"q": "two"}).status_code == 429


@pytest.mark.parametrize("path", ["/tiles/14/8185/5448", "/glyphs/Open Sans Regular,Arial Unicode MS Regular/0-255.pbf"])
def test_proxy_paths_are_dropped_from_access_log(path):
    from server.logging_setup import _DropProxyAccess

    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                            '%s - "%s %s HTTP/%s" %d', ("1.2.3.4:5", "GET", path, "1.1", 200), None)
    assert _DropProxyAccess().filter(rec) is False
    rec2 = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                             '%s - "%s %s HTTP/%s" %d', ("1.2.3.4:5", "GET", "/meetup/", "1.1", 200), None)
    assert _DropProxyAccess().filter(rec2) is True
