"""Regression tests for the review round: periods, send-time rate slots, client shares,
fine-stage fallback, per-solve allowance, glyph degradation and error headers."""
import asyncio
import logging
from datetime import UTC, datetime

import httpx
import pytest
from conftest import FakeClock

from server.errors import Busy
from server.guard.budget import BudgetExhausted, UsageStore
from server.guard.guard import ClientShareExhausted, UpstreamGuard, current_client
from server.meetup.routing import MapboxMatrixRouter, SolveAllowance, solve_allowance
from server.settings import Settings

ISO = {"longitude": -0.12, "latitude": 51.5, "travel_times": [10]}
SOLVE = {"participants": [
    {"id": "a", "lon": -0.12, "lat": 51.5, "mode": "walking"},
    {"id": "b", "lon": -0.10, "lat": 51.51, "mode": "walking"},
]}
GLYPH = "/glyphs/Open Sans Regular,Arial Unicode MS Regular/0-255.pbf"


# ── budget periods ──────────────────────────────────────────────────
def test_refund_goes_back_to_the_period_it_was_charged_to(tmp_path):
    clock = FakeClock(start=datetime(2026, 11, 30, 23, 59, 55, tzinfo=UTC))
    store = UsageStore(str(tmp_path / "u.db"), {"x": 10}, {"x": 100}, clock)
    res = store.reserve("x", 4)
    clock.advance(10)  # now 2026-12-01
    store.refund(res)
    assert store.snapshot("d:2026-11-30")["x"]["units"] == 0
    assert store.snapshot("d:2026-12-01") == {}
    assert store.remaining_today("x") == 10


def test_check_charges_nothing_and_counts_refusals_in_memory(tmp_path):
    store = UsageStore(str(tmp_path / "u.db"), {"x": 1}, {"x": 1}, FakeClock())
    store.check("x", 1)
    assert store.snapshot() == {}
    store.reserve("x", 1)
    with pytest.raises(BudgetExhausted):
        store.check("x", 1)
    assert store.snapshot()["x"] == {"requests": 1, "units": 1, "refused": 1}


def test_monthly_refusal_has_retry_after(tmp_path):
    clock = FakeClock(start=datetime(2026, 11, 30, 23, 0, tzinfo=UTC))
    store = UsageStore(str(tmp_path / "u.db"), {"x": 10}, {"x": 1}, clock)
    store.reserve("x", 1)
    with pytest.raises(BudgetExhausted) as exc:
        store.reserve("x", 1)
    assert exc.value.code == "monthly_budget_exhausted"
    assert int(exc.value.headers["Retry-After"]) == 3601


# ── rate slots track send time ──────────────────────────────────────
def test_rate_slots_are_taken_when_the_request_is_sent(tmp_path):
    clock = FakeClock()
    s = Settings(usage_db_path=str(tmp_path / "u.db"), in_flight={"tiles": 1},
                 rates_per_min={"tiles": 3}, rate_wait_s=120, in_flight_wait_s=120)
    store = UsageStore(s.usage_db_path, {"tiles": 100}, {"tiles": 100}, clock)
    guard = UpstreamGuard(s, store, clock)
    sends: list[float] = []

    async def one():
        async with guard.upstream("tiles", 1):
            sends.append(clock.monotonic())
            await clock.sleep(9)  # a slow request holds the only in-flight slot

    async def run():
        await asyncio.gather(*(one() for _ in range(7)))

    asyncio.run(run())
    assert len(sends) == 7
    for t in sends:  # never more than 3 sends in any 60 s window
        assert sum(1 for u in sends if t <= u < t + 60) <= 3


def test_budget_refusal_after_waiting_returns_the_rate_slot(tmp_path):
    clock = FakeClock()
    s = Settings(usage_db_path=str(tmp_path / "u.db"), rates_per_min={"tiles": 5})
    store = UsageStore(s.usage_db_path, {"tiles": 1}, {"tiles": 100}, clock)
    guard = UpstreamGuard(s, store, clock)

    async def run():
        async with guard.upstream("tiles", 1):
            pass
        with pytest.raises(BudgetExhausted):
            async with guard.upstream("tiles", 1):
                pass

    asyncio.run(run())
    assert guard.windows["tiles"].available() == 4


# ── per-client daily share ──────────────────────────────────────────
def test_one_client_cannot_use_up_a_feature(make_client, settings):
    s = settings.with_(trusted_proxy_hops=1, daily_budget={**settings.daily_budget, "isochrone": 8},
                       client_daily_share=0.25)
    c = make_client(s)
    a = {"x-forwarded-for": "203.0.113.5"}
    for minutes in (10, 20):
        assert c.post("/api/isochrones", json={**ISO, "travel_times": [minutes]}, headers=a).status_code == 200
    third = c.post("/api/isochrones", json={**ISO, "travel_times": [30]}, headers=a)
    assert third.status_code == 429
    assert third.json()["code"] == "client_daily_limit"
    assert "retry-after" in third.headers
    other = {"x-forwarded-for": "203.0.113.6"}
    assert c.post("/api/isochrones", json={**ISO, "travel_times": [30]}, headers=other).status_code == 200


def test_solve_shrinks_to_the_clients_share(make_client, settings, mapbox):
    s = settings.with_(router_provider="mapbox", client_daily_share=0.0001,
                       daily_budget={**settings.daily_budget, "matrix": 1_000_000})
    c = make_client(s)
    resp = c.post("/api/meetup/solve", json=SOLVE)
    assert resp.status_code == 200, resp.text
    share = 100  # ceil(1_000_000 * 0.0001)
    assert resp.json()["diagnostics"]["estimated_matrix_elements"] <= share


def test_share_refusal_without_a_client_is_skipped(tmp_path):
    clock = FakeClock()
    s = Settings(usage_db_path=str(tmp_path / "u.db"), client_daily_share=0.0001)
    store = UsageStore(s.usage_db_path, {"tiles": 10}, {"tiles": 10}, clock)
    guard = UpstreamGuard(s, store, clock)

    async def run():
        async with guard.upstream("tiles", 1):
            pass
        current_client.set("1.2.3.4")
        async with guard.upstream("tiles", 1):
            pass
        with pytest.raises(ClientShareExhausted):
            async with guard.upstream("tiles", 1):
                pass

    asyncio.run(run())


# ── solves: admission, fallback, allowance ──────────────────────────
def test_solve_is_refused_before_spending_when_rate_slots_are_taken(make_client, settings, mapbox):
    c = make_client(settings.with_(router_provider="mapbox"))
    window = c.app.state.guard.windows["matrix"]
    for _ in range(59):
        window.try_acquire()  # another caller used the minute's slots; the smallest plan needs 2
    resp = c.post("/api/meetup/solve", json=SOLVE)
    assert resp.status_code == 503
    assert resp.json()["code"] == "busy"
    assert "retry-after" in resp.headers
    assert mapbox.count() == 0


def test_busy_fine_stage_returns_the_coarse_result(make_client, settings, monkeypatch):
    import server.meetup.solver as solver

    c = make_client(settings.with_(router_provider="mapbox"))
    real = solver._fine_candidates
    started = {"fine": False}

    def fine(*a, **kw):
        started["fine"] = True
        return real(*a, **kw)

    real_upstream = c.app.state.guard.upstream

    def upstream(api, units=1, **kw):
        if started["fine"]:
            raise Busy()
        return real_upstream(api, units, **kw)

    monkeypatch.setattr(solver, "_fine_candidates", fine)
    monkeypatch.setattr(c.app.state.guard, "upstream", upstream)
    resp = c.post("/api/meetup/solve", json=SOLVE)
    assert resp.status_code == 200, resp.text
    assert resp.json()["diagnostics"]["stage"] == "coarse"


def test_retries_count_against_the_solve_allowance(mapbox, tmp_path):
    clock = FakeClock()
    mapbox.behaviour["matrix"] = lambda r: httpx.Response(429, headers={"retry-after": "0"})
    s = Settings(usage_db_path=str(tmp_path / "u.db"))
    store = UsageStore(s.usage_db_path, {"matrix": 10**6}, {"matrix": 10**6}, clock)
    guard = UpstreamGuard(s, store, clock)
    router = MapboxMatrixRouter("pk.test", httpx.AsyncClient(transport=httpx.MockTransport(mapbox)), guard)

    async def run():
        solve_allowance.set(SolveAllowance(elements=10**6, requests={"matrix": 2}))
        with pytest.raises(Busy):
            await router.duration_matrix("walking", [(0, 0)], [(0.01, 0)])

    asyncio.run(run())
    assert mapbox.count("matrix") == 2  # the third attempt was not sent


# ── glyphs, errors, health, startup ─────────────────────────────────
def test_refused_glyphs_are_served_empty(make_client, settings, mapbox):
    c = make_client(settings.with_(monthly_budget={**settings.monthly_budget, "glyphs": 0}))
    resp = c.get(GLYPH)
    assert resp.status_code == 200
    assert resp.content == b""
    assert resp.headers["cache-control"] == "no-store"
    assert mapbox.count() == 0


def test_glyph_per_ip_limit_also_degrades(make_client, settings):
    c = make_client(settings.with_(per_ip_per_min={**settings.per_ip_per_min, "glyphs": 1}))
    assert c.get(GLYPH).content != b""
    second = c.get(GLYPH)
    assert second.status_code == 200 and second.content == b""


def test_405_keeps_allow_header(client):
    resp = client.get("/api/isochrones")
    assert resp.status_code == 405
    assert "POST" in resp.headers["allow"]
    assert resp.json()["code"] == "method_not_allowed"


def test_health_reports_an_unusable_store(make_client, settings, tmp_path):
    c = make_client(settings.with_(usage_db_path=str(tmp_path / "missing" / "dir" / "u.db")))
    resp = c.get("/api/meetup/health")
    assert resp.status_code == 503
    assert resp.json()["usage_store"] == "unavailable"


def test_startup_warns_about_zero_budgets_and_default_db(make_client, settings, caplog):
    caplog.set_level(logging.WARNING)
    make_client(settings.with_(usage_db_path="usage.sqlite3",
                               monthly_budget={**settings.monthly_budget, "glyphs": 0}))
    assert "glyphs has no budget" in caplog.text
    assert "USAGE_DB_PATH is the default" in caplog.text


def test_warns_once_when_proxy_header_is_ignored(make_client, settings, caplog):
    c = make_client(settings.with_(trusted_proxy_hops=0))
    for _ in range(3):
        c.post("/api/isochrones", json=ISO, headers={"x-forwarded-for": "1.2.3.4"})
    assert caplog.text.count("TRUSTED_PROXY_HOPS=0") == 1
