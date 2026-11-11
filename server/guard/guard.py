"""
The upstream guard. Every billable call goes through `UpstreamGuard.upstream(...)`:

  1. refuse at once if the daily/monthly budget, or this client's share of it, is spent;
  2. hold an in-flight slot (wait briefly, then refuse);
  3. take a slot in the per-token rate window (wait briefly, then refuse);
  4. reserve the units in the budget and in the client's share, then send.

Rate slots and reservations are taken last, right before the send, so the rate window
tracks real send times and a reservation is charged to the period the request is sent in.
Nothing is refunded once a request has been sent.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
from collections import Counter
from contextlib import asynccontextmanager
from math import ceil

from fastapi import Depends, Request

from server.errors import ApiError, Busy
from server.guard.budget import UNITS, UsageStore, _next_midnight, seconds_until
from server.guard.client_ip import client_ip
from server.guard.clock import Clock
from server.guard.limiter import KeyedLimiter, SlidingWindow
from server.settings import Settings

log = logging.getLogger("time_map.usage")

# The resolved client key of the request being handled (set by the per-IP dependency).
current_client: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_client",
                                                                            default=None)


class ClientShareExhausted(ApiError):
    status = 429
    code = "client_daily_limit"
    message = ("You have used your share of today's allowance for this feature. "
               "It resets at midnight UTC.")


class ClientShares:
    """
    In-memory daily units per (client, API), capped at a share of the API's daily budget,
    so one visitor cannot use up a feature for everyone. Resets on restart.
    """

    def __init__(self, settings: Settings, clock: Clock, max_keys: int = 50_000):
        self.settings = settings
        self.clock = clock
        self.max_keys = max_keys
        self._used: Counter[tuple[str, str, str]] = Counter()
        self._day = ""

    def _today(self) -> str:
        day = f"{self.clock.utcnow():%Y-%m-%d}"
        if day != self._day:
            self._used.clear()
            self._day = day
        return day

    def cap(self, api: str) -> int:
        return max(1, ceil(self.settings.daily(api) * self.settings.client_daily_share))

    def remaining(self, client: str | None, api: str) -> int | None:
        if client is None:
            return None
        day = self._today()
        return max(0, self.cap(api) - self._used[(day, client, api)])

    def _refuse(self) -> ClientShareExhausted:
        now = self.clock.utcnow()
        return ClientShareExhausted(
            headers={"Retry-After": str(seconds_until(_next_midnight(now), now))})

    def check(self, client: str | None, api: str, units: int) -> None:
        left = self.remaining(client, api)
        if left is not None and units > left:
            raise self._refuse()

    def add(self, client: str | None, api: str, units: int) -> None:
        if client is None:
            return
        day = self._today()
        self._used[(day, client, api)] += units
        if len(self._used) > self.max_keys:  # a flood of keys: drop the smallest users
            for key, _ in self._used.most_common()[self.max_keys // 2:]:
                del self._used[key]


class UpstreamGuard:
    def __init__(self, settings: Settings, store: UsageStore, clock: Clock):
        self.settings = settings
        self.store = store
        self.clock = clock
        self.windows = {
            name: SlidingWindow(limit, 60.0, clock) for name, limit in settings.rates_per_min.items()
        }
        self.in_flight = {api: asyncio.Semaphore(n) for api, n in settings.in_flight.items()}
        self.per_ip = {
            group: KeyedLimiter(limit, 60.0, settings.per_ip_max_keys, clock)
            for group, limit in settings.per_ip_per_min.items()
        }
        self.shares = ClientShares(settings, clock)
        self._warned_no_hops = False

    def window_for(self, api: str, profile: str | None = None) -> SlidingWindow:
        if profile and f"{api}:{profile}" in self.windows:
            return self.windows[f"{api}:{profile}"]
        return self.windows[api]

    @asynccontextmanager
    async def upstream(self, api: str, units: int = 1, *, profile: str | None = None):
        client = current_client.get()
        # Cheap refusals first, before any waiting.
        self.store.check(api, units)
        self.shares.check(client, api, units)

        sem = self.in_flight[api]
        try:
            await asyncio.wait_for(sem.acquire(), self.settings.in_flight_wait_s)
        except TimeoutError:
            raise Busy() from None
        try:
            window = self.window_for(api, profile)
            stamp = await window.acquire(self.settings.rate_wait_s)
            try:
                self.shares.check(client, api, units)
                self.store.reserve(api, units)
            except BaseException:
                window.give_back(stamp)
                raise
            self.shares.add(client, api, units)
            yield
        finally:
            sem.release()

    # ── usage log ───────────────────────────────────────────────────
    def log_usage(self, label: str = "so far", period: str | None = None) -> str:
        snap = self.store.snapshot(period)
        day = period or f"d:{self.clock.utcnow():%Y-%m-%d}"
        parts = []
        for api in sorted(set(snap) | set(self.settings.monthly_budget)):
            row = snap.get(api, {"requests": 0, "units": 0, "refused": 0})
            unit = UNITS.get(api, "requests")
            left = self.store.remaining_today(api) if period is None else "-"
            parts.append(
                f"{api}: {row['requests']} req, {row['units']} {unit}, "
                f"{row['refused']} refused, {left} left"
            )
        line = f"usage {day[2:]} ({label}) " + "; ".join(parts)
        log.info("%s", line)
        return line

    async def usage_loop(self) -> None:
        day = f"d:{self.clock.utcnow():%Y-%m-%d}"
        while True:
            await asyncio.sleep(self.settings.usage_log_interval_s)
            today = f"d:{self.clock.utcnow():%Y-%m-%d}"
            if today != day:
                self.log_usage("day total", period=day)
                day = today
            self.log_usage()

    # ── per-IP ──────────────────────────────────────────────────────
    def enforce_per_ip(self, request: Request, group: str) -> str:
        s = self.settings
        if s.trusted_proxy_hops == 0 and not self._warned_no_hops and \
                request.headers.get(s.client_ip_header):
            self._warned_no_hops = True
            log.warning(
                "requests carry %s but TRUSTED_PROXY_HOPS=0: every visitor shares one per-IP "
                "bucket (the proxy's address). Set TRUSTED_PROXY_HOPS behind a proxy.",
                s.client_ip_header,
            )
        ip = client_ip(request, s.client_ip_header, s.trusted_proxy_hops)
        current_client.set(ip)
        retry = self.per_ip[group].hit(ip)
        if retry is not None:
            raise ApiError(
                "Too many requests. Please wait a moment and try again.",
                status=429, code="rate_limited",
                headers={"Retry-After": str(max(1, ceil(retry)))},
            )
        return ip


def per_ip(group: str):
    """A route dependency that limits requests per client IP for one route group."""

    async def _check(request: Request) -> None:
        request.app.state.guard.enforce_per_ip(request, group)

    return Depends(_check)
