"""Per-IP limits behind the proxy, daily/monthly budgets, rate windows and concurrency."""
import asyncio
import os
import stat
from datetime import UTC, datetime

import httpx
import pytest
from conftest import FakeClock

from server.cache import BoundedTTLCache
from server.errors import Busy
from server.guard.budget import BudgetExhausted, UsageStore
from server.guard.client_ip import client_ip
from server.guard.guard import UpstreamGuard
from server.guard.limiter import KeyedLimiter, SlidingWindow
from server.meetup.routing import MapboxMatrixRouter
from server.settings import Settings

ISO = {"longitude": -0.12, "latitude": 51.5, "travel_times": [10]}
SOLVE = {"participants": [
    {"id": "a", "lon": -0.12, "lat": 51.5, "mode": "walking"},
    {"id": "b", "lon": -0.10, "lat": 51.51, "mode": "walking"},
]}


# ── per-IP limits behind a proxy ────────────────────────────────────
def test_two_clients_behind_the_proxy_get_separate_limits(make_client, settings):
    s = settings.with_(trusted_proxy_hops=1, per_ip_per_min={**settings.per_ip_per_min, "isochrones": 2})
    c = make_client(s)
    a = {"x-forwarded-for": "203.0.113.5"}
    b = {"x-forwarded-for": "203.0.113.6"}
    assert c.post("/api/isochrones", json=ISO, headers=a).status_code == 200
    assert c.post("/api/isochrones", json=ISO, headers=a).status_code == 200
    third = c.post("/api/isochrones", json=ISO, headers=a)
    assert third.status_code == 429
    assert third.json()["code"] == "rate_limited"
    assert int(third.headers["retry-after"]) >= 1
    assert c.post("/api/isochrones", json=ISO, headers=b).status_code == 200
    # A forged left-hand entry does not give a fresh bucket.
    forged = {"x-forwarded-for": "9.9.9.9, 203.0.113.5"}
    assert c.post("/api/isochrones", json=ISO, headers=forged).status_code == 429


def test_without_trusted_hops_the_header_is_ignored(make_client, settings):
    s = settings.with_(trusted_proxy_hops=0, per_ip_per_min={**settings.per_ip_per_min, "isochrones": 1})
    c = make_client(s)
    assert c.post("/api/isochrones", json=ISO, headers={"x-forwarded-for": "1.1.1.1"}).status_code == 200
    assert c.post("/api/isochrones", json=ISO, headers={"x-forwarded-for": "2.2.2.2"}).status_code == 429


class _Req:
    def __init__(self, headers, peer="10.0.0.1"):
        self.headers = headers
        self.client = type("C", (), {"host": peer})()


@pytest.mark.parametrize("hops,header,expected", [
    (0, "1.1.1.1", "10.0.0.1"),
    (1, "1.1.1.1", "1.1.1.1"),
    (1, "9.9.9.9, 1.1.1.1", "1.1.1.1"),
    (2, "9.9.9.9, 1.1.1.1, 10.1.1.1", "1.1.1.1"),
    (2, "1.1.1.1", "10.0.0.1"),
    (1, "", "10.0.0.1"),
])
def test_client_ip(hops, header, expected):
    assert client_ip(_Req({"x-forwarded-for": header}), "x-forwarded-for", hops) == expected


def test_per_ip_key_store_is_bounded():
    lim = KeyedLimiter(5, 60, max_keys=3, clock=FakeClock())
    for i in range(10):
        lim.hit(f"10.0.0.{i}")
    assert len(lim) == 3


# ── budgets ─────────────────────────────────────────────────────────
def test_daily_budget_refuses_once_spent_and_resets_at_midnight(make_client, settings, mapbox, clock):
    c = make_client(settings.with_(router_provider="mapbox"))
    first = c.post("/api/meetup/solve", json=SOLVE)
    assert first.status_code == 200, first.text
    store = c.app.state.usage
    estimate = first.json()["diagnostics"]["estimated_matrix_elements"]
    assert store.snapshot()["matrix"]["units"] <= estimate
    store.daily["matrix"] = estimate + 10  # one solve a day
    calls = mapbox.count("matrix")
    second = c.post("/api/meetup/solve", json=SOLVE)
    assert second.status_code == 503
    assert second.json()["code"] == "daily_budget_exhausted"
    assert "retry-after" in second.headers
    assert mapbox.count("matrix") == calls  # refused before any upstream call

    clock.advance(13 * 3600)  # 12:00 → 01:00 next day (UTC)
    assert c.post("/api/meetup/solve", json=SOLVE).status_code == 200


def test_budget_refusal_mid_stage_never_queues(tmp_path):
    store = UsageStore(str(tmp_path / "u.db"), {"matrix": 10}, {"matrix": 100}, FakeClock())
    store.reserve("matrix", 10)
    with pytest.raises(BudgetExhausted) as exc:
        store.reserve("matrix", 1)
    assert exc.value.code == "daily_budget_exhausted"


def test_monthly_budget_refuses(make_client, settings, mapbox):
    s = settings.with_(monthly_budget={**settings.monthly_budget, "isochrone": 1})
    c = make_client(s)
    assert c.post("/api/isochrones", json=ISO).status_code == 200
    resp = c.post("/api/isochrones", json={**ISO, "travel_times": [20]})
    assert resp.status_code == 503
    assert resp.json()["code"] == "monthly_budget_exhausted"
    assert mapbox.count("isochrone") == 1
    # A cache hit costs nothing.
    assert c.post("/api/isochrones", json=ISO).status_code == 200
    assert mapbox.count("isochrone") == 1


def test_fine_stage_out_of_budget_returns_coarse(make_client, settings, monkeypatch):
    import server.meetup.solver as solver

    c = make_client(settings.with_(router_provider="mapbox"))
    store = c.app.state.usage
    real_reserve, real_fine = store.reserve, solver._fine_candidates
    fine_started = {"yes": False}

    def fine(*a, **kw):
        fine_started["yes"] = True
        return real_fine(*a, **kw)

    def reserve(api, units):
        if fine_started["yes"]:
            raise BudgetExhausted("x", code="daily_budget_exhausted")
        real_reserve(api, units)

    monkeypatch.setattr(solver, "_fine_candidates", fine)
    store.reserve = reserve
    resp = c.post("/api/meetup/solve", json=SOLVE)
    assert resp.status_code == 200, resp.text
    assert resp.json()["diagnostics"]["stage"] == "coarse"


def test_counters_survive_restart(make_client, settings):
    s = settings.with_(monthly_budget={**settings.monthly_budget, "isochrone": 1})
    c1 = make_client(s)
    assert c1.post("/api/isochrones", json=ISO).status_code == 200
    c1.__exit__(None, None, None)
    c2 = make_client(s)
    assert c2.post("/api/isochrones", json=ISO).status_code == 503


def test_unwritable_store_fails_closed(make_client, settings, tmp_path, mapbox):
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)
    try:
        c = make_client(settings.with_(usage_db_path=str(ro / "usage.sqlite3")))
        resp = c.post("/api/isochrones", json=ISO)
        assert resp.status_code == 503
        assert resp.json()["code"] == "busy"
        assert mapbox.count() == 0
    finally:
        os.chmod(ro, stat.S_IRWXU)


def test_last_unit_goes_to_one_reservation(tmp_path):
    store = UsageStore(str(tmp_path / "u.db"), {"x": 5}, {"x": 5}, FakeClock())
    store.reserve("x", 4)
    store.reserve("x", 1)
    with pytest.raises(BudgetExhausted):
        store.reserve("x", 1)
    assert store.snapshot()["x"] == {"requests": 2, "units": 5, "refused": 1}


def test_usage_log_line(tmp_path, caplog):
    clock = FakeClock()
    s = Settings(usage_db_path=str(tmp_path / "u.db"))
    store = UsageStore(s.usage_db_path, {"matrix": 100}, {"matrix": 1000}, clock)
    guard = UpstreamGuard(s, store, clock)
    store.reserve("matrix", 30)
    store.reserve("matrix", 12)
    line = guard.log_usage()
    assert "matrix: 2 req, 42 elements, 0 refused, 58 left" in line


# ── rate windows and in-flight bound ────────────────────────────────
def _matrix_router(mapbox, clock, tmp_path, **settings_kw):
    s = Settings(usage_db_path=str(tmp_path / "u.db"), monthly_budget={"matrix": 10**9},
                 daily_budget={"matrix": 10**9}, **settings_kw)
    store = UsageStore(s.usage_db_path, {"matrix": 10**9}, {"matrix": 10**9}, clock)
    guard = UpstreamGuard(s, store, clock)
    client = httpx.AsyncClient(transport=httpx.MockTransport(mapbox))
    return MapboxMatrixRouter("pk.test", client, guard), guard, store


@pytest.mark.parametrize("mode,limit", [("walking", 60), ("driving-traffic", 30)])
def test_rate_window_per_profile(mapbox, clock, tmp_path, mode, limit):
    router, guard, _ = _matrix_router(mapbox, clock, tmp_path, rate_wait_s=61.0)

    async def run():
        for _ in range(limit):
            await router.duration_matrix(mode, [(0, 0)], [(0.01, 0)])
        clock.slept.clear()
        # The next one waits for the window (fake sleep), it is not sent early.
        await router.duration_matrix(mode, [(0, 0)], [(0.01, 0)])
        assert clock.slept and abs(sum(clock.slept) - 60.0) < 1e-6

    asyncio.run(run())
    assert mapbox.count("matrix") == limit + 1


def test_rate_window_refuses_after_max_wait(mapbox, clock, tmp_path):
    router, guard, store = _matrix_router(mapbox, clock, tmp_path, rate_wait_s=5.0)

    async def run():
        for _ in range(60):
            await router.duration_matrix("walking", [(0, 0)], [(0.01, 0)])
        with pytest.raises(Busy):
            await router.duration_matrix("walking", [(0, 0)], [(0.01, 0)])

    asyncio.run(run())
    assert store.snapshot()["matrix"]["requests"] == 60  # the refused one was refunded


def test_429_retries_take_slots_and_budget(mapbox, clock, tmp_path):
    replies = iter([429, 429])

    def flaky(request):
        code = next(replies, None)
        if code:
            return httpx.Response(code, headers={"retry-after": "1"})
        return mapbox.ok("matrix", request)

    mapbox.behaviour["matrix"] = flaky
    router, guard, store = _matrix_router(mapbox, clock, tmp_path)
    asyncio.run(router.duration_matrix("walking", [(0, 0)], [(0.01, 0)]))
    assert store.snapshot()["matrix"]["requests"] == 3
    assert len(guard.windows["matrix"]._hits) == 3
    assert clock.slept == [1.0, 1.0]


def test_in_flight_bound(tmp_path):
    clock = FakeClock()
    s = Settings(usage_db_path=str(tmp_path / "u.db"), in_flight={"tiles": 2})
    store = UsageStore(s.usage_db_path, {"tiles": 100}, {"tiles": 100}, clock)
    guard = UpstreamGuard(s, store, clock)
    peak = {"now": 0, "max": 0}

    async def one():
        async with guard.upstream("tiles", 1):
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.01)
            peak["now"] -= 1

    async def run():
        await asyncio.gather(*(one() for _ in range(8)))

    asyncio.run(run())
    assert peak["max"] == 2


def test_sliding_window_frees_slots():
    clock = FakeClock()
    w = SlidingWindow(2, 60, clock)
    assert w.try_acquire() is None and w.try_acquire() is None
    assert w.try_acquire() == 60
    clock.advance(60)
    assert w.try_acquire() is None


# ── cache ───────────────────────────────────────────────────────────
def test_bounded_ttl_cache():
    now = [0.0]
    c = BoundedTTLCache(maxsize=2, ttl=10, clock=lambda: now[0])
    c.set("a", 1)
    c.set("b", 2)
    c.get("a")          # a is now most recent
    c.set("c", 3)       # evicts b
    assert c.get("b") is None and c.get("a") == 1 and c.get("c") == 3
    now[0] = 11
    assert c.get("a") is None
    assert len(c) == 1  # expired entry was deleted on read


def test_more_than_one_worker_exits(make_client, settings, monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(RuntimeError, match="WEB_CONCURRENCY"):
        make_client(settings)


def test_month_rollover(tmp_path):
    clock = FakeClock(start=datetime(2026, 11, 30, 23, 59, tzinfo=UTC))
    store = UsageStore(str(tmp_path / "u.db"), {"x": 10}, {"x": 1}, clock)
    store.reserve("x", 1)
    with pytest.raises(BudgetExhausted):
        store.reserve("x", 1)
    clock.advance(120)
    store.reserve("x", 1)
