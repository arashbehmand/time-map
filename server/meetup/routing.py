"""
Travel-time routers.

Two implementations:
  1. MapboxMatrixRouter  — real network travel times via the Directions Matrix API
  2. EuclideanRouter     — haversine / speed estimate (zero API calls, for testing)

Each returns a MatrixResult: minutes from every origin to every destination, where the
router put each destination (Mapbox snaps it to the nearest road or path and names that
street), and which destinations could not be checked at all.

The Mapbox router reads every reply strictly. A null entry means that one pair has no
route. A NoRoute reply is split to find the destination that fails, which is then dropped
and counted. Anything else that is missing or malformed is a provider error: retried once,
then a 502. Nothing is dropped silently.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Iterable

import httpx
import numpy as np

from server.errors import Busy, UpstreamError
from server.guard.guard import UpstreamGuard
from server.meetup.candidates import haversine_km
from server.meetup.limits import (
    RECOVERY_REQUESTS,
    SolvePlan,
    matrix_max_coords,
    matrix_tiles,
    rate_bucket,
)
from server.settings import DEFAULT_IN_FLIGHT, Settings
from server.upstream import fail, mapbox_send

log = logging.getLogger("time_map.matrix")

Coord = tuple[float, float]  # (lon, lat)

FALLBACK_SPEEDS_KMH = {
    "walking": 4.8,
    "cycling": 15.0,
    "driving": 30.0,
    "driving-traffic": 22.0,
}

# Seconds to wait before asking again after a bad reply (on the guard's clock).
BAD_REPLY_RETRY_S = 0.5
MAX_NAME_CHARS = 80


@dataclass(frozen=True)
class Snap:
    """Where the router put a destination, and the street's name if it has one."""
    lon: float
    lat: float
    name: str | None = None


@dataclass
class MatrixResult:
    minutes: np.ndarray        # (n_origins, n_destinations); inf = no route for that pair
    snaps: list[Snap]          # one per destination
    unresolved: np.ndarray     # bool per destination: dropped after NoRoute, no times at all


@dataclass
class SolveAllowance:
    """
    What one solve may send, retries included: elements and requests per rate bucket.
    Also holds the solve's concurrency gate and its recovery counters.
    """
    elements: int
    requests: dict[str, int]
    concurrency: int = DEFAULT_IN_FLIGHT["matrix"]
    spent_elements: int = 0
    spent_requests: Counter = field(default_factory=Counter)
    retries: int = 0
    _gate: asyncio.Semaphore | None = field(default=None, repr=False)

    @classmethod
    def for_plan(cls, plan: SolvePlan, settings: Settings) -> SolveAllowance:
        """The plan's cost plus a little recovery slack, never above a bucket's rate."""
        requests = {
            b: max(n, min(n + RECOVERY_REQUESTS, settings.rates_per_min.get(b, n)))
            for b, n in plan.requests.items()
        }
        elements = max(plan.elements,
                       min(plan.elements + 2 * plan.max_tile_elements, settings.max_solve_elements))
        return cls(elements, requests, concurrency=max(1, settings.in_flight.get("matrix", 1)))

    @property
    def gate(self) -> asyncio.Semaphore:
        # Requests queue here, not in the guard (which gives up after a few seconds).
        if self._gate is None:
            self._gate = asyncio.Semaphore(self.concurrency)
        return self._gate

    def can_take(self, units: int, bucket: str) -> bool:
        return (self.spent_elements + units <= self.elements
                and self.spent_requests[bucket] + 1 <= self.requests.get(bucket, 0))

    def take(self, units: int, bucket: str) -> None:
        if not self.can_take(units, bucket):
            raise Busy("The routing service is busy. Please try again in a minute.",
                       headers={"Retry-After": "60"})
        self.spent_elements += units
        self.spent_requests[bucket] += 1


# Set by the solve route; None for callers without a per-solve plan.
solve_allowance: contextvars.ContextVar[SolveAllowance | None] = contextvars.ContextVar(
    "solve_allowance", default=None)


async def gather_or_cancel(aws: Iterable[Awaitable[Any]]) -> list[Any]:
    """
    Run awaitables at once and return their results in order. The first failure cancels
    the rest (and waits for them), then is raised as itself, never as an ExceptionGroup.
    """
    tasks = [asyncio.ensure_future(a) for a in aws]
    if not tasks:
        return []
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    finally:
        pending = [t for t in tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    errors = [t.exception() for t in tasks if not t.cancelled() and t.exception() is not None]
    if errors:
        raise errors[0]
    return [t.result() for t in tasks]


class Router:
    async def duration_matrix(
        self, mode: str, origins: list[Coord], destinations: list[Coord]
    ) -> MatrixResult:
        """Minutes from each origin to each destination, with where each destination is."""
        raise NotImplementedError


class EuclideanRouter(Router):
    """Haversine distance / average speed. Good for smoke tests."""

    async def duration_matrix(
        self, mode: str, origins: list[Coord], destinations: list[Coord]
    ) -> MatrixResult:
        speed = FALLBACK_SPEEDS_KMH.get(mode, 5.0)  # km/h
        out = np.empty((len(origins), len(destinations)), dtype=np.float64)
        for i, (olon, olat) in enumerate(origins):
            for j, (dlon, dlat) in enumerate(destinations):
                out[i, j] = haversine_km(olon, olat, dlon, dlat) / speed * 60.0
        return MatrixResult(out, [Snap(lon, lat) for lon, lat in destinations],
                            np.zeros(len(destinations), dtype=bool))


class _NoRoute(Exception):
    """Mapbox answered code NoRoute for a whole request."""


class _Malformed(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Skipped(Exception):
    """A recovery request that this solve cannot afford right now."""


@dataclass
class _Block:
    minutes: np.ndarray
    snaps: list[Snap]
    unresolved: np.ndarray

    @classmethod
    def dropped(cls, n_orig: int, dests: list[Coord]) -> _Block:
        return cls(np.full((n_orig, len(dests)), np.inf), [Snap(lon, lat) for lon, lat in dests],
                   np.ones(len(dests), dtype=bool))

    @classmethod
    def join(cls, blocks: list[_Block]) -> _Block:
        return cls(np.hstack([b.minutes for b in blocks]), [s for b in blocks for s in b.snaps],
                   np.concatenate([b.unresolved for b in blocks]))


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def _clean_name(name: str | None) -> str | None:
    if not name:
        return None
    name = " ".join(_CONTROL.sub(" ", name).split())[:MAX_NAME_CHARS]
    return name or None


def parse_matrix(data: Any, n_orig: int, n_dest: int) -> tuple[np.ndarray, list[Snap]]:
    """
    Read one Matrix reply for n_orig sources and n_dest destinations (padding included).
    Returns minutes (inf for a null pair) and one snap per destination. Raises _NoRoute for
    code NoRoute and _Malformed for anything else that is not exactly what was asked for.
    """
    if not isinstance(data, dict):
        raise _Malformed("not an object")
    code = data.get("code")
    if code == "NoRoute":
        raise _NoRoute()
    if code != "Ok":
        raise _Malformed("code")
    rows = data.get("durations")
    if not isinstance(rows, list) or len(rows) != n_orig:
        raise _Malformed("row count")
    out = np.full((n_orig, n_dest), np.inf)
    for i, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != n_dest:
            raise _Malformed("row length")
        for j, value in enumerate(row):
            if value is None:
                continue  # no route for this one pair
            if not _number(value) or value < 0:
                raise _Malformed("duration")
            out[i, j] = float(value) / 60.0  # seconds → minutes
    waypoints = data.get("destinations")
    if not isinstance(waypoints, list) or len(waypoints) != n_dest:
        raise _Malformed("destinations")
    snaps = []
    for w in waypoints:
        loc = w.get("location") if isinstance(w, dict) else None
        if not (isinstance(loc, list) and len(loc) == 2 and all(_number(x) for x in loc)
                and -180 <= loc[0] <= 180 and -90 <= loc[1] <= 90):
            raise _Malformed("location")
        name = w.get("name")
        if name is not None and not isinstance(name, str):
            raise _Malformed("name")
        snaps.append(Snap(float(loc[0]), float(loc[1]), _clean_name(name)))
    return out, snaps


class MapboxMatrixRouter(Router):
    """
    Mapbox Directions Matrix API.
    Limit: 25 total coordinates per request (10 for driving-traffic); origins and
    destinations are both batched to fit. A solve's requests run at once, a few at a time.
    Every request goes through the upstream guard (budget in elements, rate, in-flight).
    """

    URL = "https://api.mapbox.com/directions-matrix/v1/mapbox"
    MAX_RETRY_AFTER_S = 5.0

    def __init__(self, token: str, client: httpx.AsyncClient, guard: UpstreamGuard):
        self.token = token
        self.client = client
        self.guard = guard

    async def duration_matrix(
        self, mode: str, origins: list[Coord], destinations: list[Coord]
    ) -> MatrixResult:
        n_o, n_d = len(origins), len(destinations)
        minutes = np.full((n_o, n_d), np.inf)
        snaps: list[Snap | None] = [None] * n_d
        unresolved = np.zeros(n_d, dtype=bool)
        if n_o == 0 or n_d == 0:
            return MatrixResult(minutes, [Snap(lon, lat) for lon, lat in destinations], unresolved)

        allowance = solve_allowance.get()
        gate = allowance.gate if allowance is not None else asyncio.Semaphore(
            self.guard.settings.in_flight.get("matrix", 1))

        async def run(o_rng: range, d_rng: range) -> None:
            block = await self._tile(mode, [origins[i] for i in o_rng],
                                     [destinations[j] for j in d_rng], gate)
            minutes[o_rng.start:o_rng.stop, d_rng.start:d_rng.stop] = block.minutes
            for k, j in enumerate(d_rng):
                unresolved[j] |= block.unresolved[k]
                if o_rng.start == 0:  # several origin batches: the first one's snap
                    snaps[j] = block.snaps[k]

        await gather_or_cancel(run(o, d) for o, d in matrix_tiles(n_o, n_d, matrix_max_coords(mode)))
        dropped = int(unresolved.sum())
        if dropped:
            log.warning("matrix no-route profile=%s dropped=%d of=%d", mode, dropped, n_d)
        return MatrixResult(
            minutes,
            [s if s is not None else Snap(*destinations[j]) for j, s in enumerate(snaps)],
            unresolved,
        )

    async def _tile(self, mode: str, origins: list[Coord], dests: list[Coord],
                    gate: asyncio.Semaphore) -> _Block:
        try:
            return await self._fetch(mode, origins, dests, gate, recovery=False)
        except _NoRoute:
            log.warning("matrix no-route reply profile=%s size=%d", mode, len(dests))
            return await self._split(mode, origins, dests, gate)

    async def _split(self, mode: str, origins: list[Coord], dests: list[Coord],
                     gate: asyncio.Semaphore) -> _Block:
        """
        `dests` answered NoRoute together. Ask for each half: a half that still says NoRoute
        while the other is fine is split again, down to the one destination that fails,
        which is dropped. If both halves say NoRoute, the whole block is dropped: the cause is
        an origin, or the whole area. A request this solve can't afford drops its half.
        """
        if len(dests) == 1:
            return _Block.dropped(len(origins), dests)
        mid = len(dests) // 2
        halves = [dests[:mid], dests[mid:]]

        async def attempt(half: list[Coord]) -> _Block | str:
            try:
                return await self._fetch(mode, origins, half, gate, recovery=True)
            except _NoRoute:
                return "no_route"
            except _Skipped:
                return "skipped"

        results = await gather_or_cancel(attempt(h) for h in halves)
        if all(r == "no_route" for r in results):
            return _Block.dropped(len(origins), dests)
        blocks = []
        for half, r in zip(halves, results):
            if r == "no_route":
                blocks.append(await self._split(mode, origins, half, gate))
            elif r == "skipped":
                blocks.append(_Block.dropped(len(origins), half))
            else:
                blocks.append(r)
        return _Block.join(blocks)

    async def _fetch(self, mode: str, origins: list[Coord], destinations: list[Coord],
                     gate: asyncio.Semaphore, recovery: bool) -> _Block:
        # Mapbox requires at least 2 matrix elements (n_origins * n_destinations >= 2).
        # Pad with a duplicate destination if needed; we discard the extra column below.
        n_orig, n_dest = len(origins), len(destinations)
        pad = max(0, 2 - n_orig * n_dest)
        padded = destinations + destinations[:1] * pad

        coords = origins + padded
        coord_str = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in coords)
        sources = ";".join(str(i) for i in range(n_orig))
        dests = ";".join(str(n_orig + j) for j in range(len(padded)))

        # Build the full URL as a string so httpx never re-encodes the semicolons
        # in the coordinate path or the sources/destinations query params.
        url = (
            f"{self.URL}/{mode}/{coord_str}"
            f"?annotations=duration&sources={sources}&destinations={dests}"
            f"&access_token={self.token}"
        )
        async with gate:
            minutes, snaps = await self._get(url, mode, n_orig, len(padded), recovery)
        return _Block(minutes[:, :n_dest], snaps[:n_dest], np.zeros(n_dest, dtype=bool))

    async def _get(self, url: str, mode: str, n_orig: int, n_padded: int,
                   recovery: bool) -> tuple[np.ndarray, list[Snap]]:
        # Each attempt, retries included, takes a rate slot, is charged to the budget and
        # counts against this solve's allowance. A recovery request, or the one retry of a
        # bad reply, is sent only if the allowance has room and a rate slot is free now.
        allowance = solve_allowance.get()
        bucket = rate_bucket(mode)
        units = n_orig * n_padded
        window = self.guard.window_for("matrix", mode)
        retried = False
        for attempt in range(3):
            if allowance is not None:
                if (recovery or retried) and not (allowance.can_take(units, bucket)
                                                  and window.available() > 0):
                    if retried:
                        raise UpstreamError()
                    raise _Skipped()
                allowance.take(units, bucket)
            async with self.guard.upstream("matrix", units, profile=mode):
                resp = await mapbox_send(self.client, url, api="matrix", profile=mode)
            if resp.status_code == 429:
                fail(resp, api="matrix", profile=mode)
                await self.guard.clock.sleep(_retry_after(resp, 0.6 * 2**attempt,
                                                          self.MAX_RETRY_AFTER_S))
                continue
            if resp.status_code >= 500:
                fail(resp, api="matrix", profile=mode)  # logs status only
            elif resp.is_error:
                raise fail(resp, api="matrix", profile=mode)
            else:
                try:
                    data = resp.json()
                except ValueError:
                    log.warning("upstream bad json api=matrix profile=%s status=%d",
                                mode, resp.status_code)
                else:
                    try:
                        return parse_matrix(data, n_orig, n_padded)
                    except _Malformed as exc:
                        log.warning("upstream bad payload api=matrix profile=%s reason=%s",
                                    mode, exc.reason)
            # A 5xx or a bad reply: one more try, then a provider error.
            if retried:
                raise UpstreamError()
            retried = True
            if allowance is not None:
                allowance.retries += 1
            await self.guard.clock.sleep(BAD_REPLY_RETRY_S)
        raise Busy("The routing service is busy. Please try again in a minute.")


def _retry_after(resp: httpx.Response, default: float, cap: float) -> float:
    try:
        return min(cap, max(0.0, float(resp.headers.get("retry-after", default))))
    except ValueError:
        return min(cap, default)


def build_router(provider: str, token: str = "", client: httpx.AsyncClient | None = None,
                 guard: UpstreamGuard | None = None) -> Router:
    provider = provider.strip().lower()
    if provider == "mapbox":
        if client is None or guard is None:
            raise ValueError("MapboxMatrixRouter needs the shared httpx client and guard.")
        return MapboxMatrixRouter(token, client, guard)
    if provider == "euclidean":
        return EuclideanRouter()
    raise ValueError(f"Unknown ROUTER_PROVIDER: {provider!r}")
