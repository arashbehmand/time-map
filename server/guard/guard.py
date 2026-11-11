"""
The upstream guard. Every billable call goes through `UpstreamGuard.upstream(...)`:

  1. reserve the units in the daily and monthly budget (refuse at once if spent),
  2. take a slot in the per-token rate window (wait briefly, then refuse),
  3. hold an in-flight slot while the request is sent.

A reservation is refunded only when the request was never sent.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from math import ceil

from fastapi import Depends, Request

from server.errors import ApiError, Busy
from server.guard.budget import UNITS, UsageStore
from server.guard.client_ip import client_ip
from server.guard.clock import Clock
from server.guard.limiter import KeyedLimiter, SlidingWindow
from server.settings import Settings

log = logging.getLogger("time_map.usage")


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

    def window_for(self, api: str, profile: str | None = None) -> SlidingWindow:
        if profile and f"{api}:{profile}" in self.windows:
            return self.windows[f"{api}:{profile}"]
        return self.windows[api]

    @asynccontextmanager
    async def upstream(self, api: str, units: int = 1, *, profile: str | None = None):
        self.store.reserve(api, units)
        try:
            await self.window_for(api, profile).acquire(self.settings.rate_wait_s)
        except Busy:
            self.store.refund(api, units)
            raise
        sem = self.in_flight[api]
        try:
            await asyncio.wait_for(sem.acquire(), self.settings.in_flight_wait_s)
        except TimeoutError:
            self.store.refund(api, units)
            raise Busy() from None
        try:
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


def per_ip(group: str):
    """A route dependency that limits requests per client IP for one route group."""

    async def _check(request: Request) -> None:
        guard: UpstreamGuard = request.app.state.guard
        s = guard.settings
        ip = client_ip(request, s.client_ip_header, s.trusted_proxy_hops)
        retry = guard.per_ip[group].hit(ip)
        if retry is not None:
            raise ApiError(
                "Too many requests. Please wait a moment and try again.",
                status=429, code="rate_limited",
                headers={"Retry-After": str(max(1, ceil(retry)))},
            )

    return Depends(_check)
