"""
TfL Journey Planner client for the gate.

- One journey = GET /Journey/JourneyResults/{lat,lon}/to/{lat,lon} with the settings in
  transit.settings.JP. TfL's time for a departure t is the earliest arrival among the
  journeys returned, minus t, so the first wait counts as it does in R5.
- A pair's value is the median over the six departures in the window. A departure TfL
  answers with no journey counts as no time; an error (HTTP, 300 disambiguation, a reply that
  can't be read) is never read as "no route": the pair is reported as errored.
- Replies are cached in SQLite (gzip), keyed by the request without the key, so reruns cost
  nothing. The key is sent as a query parameter and is never logged or stored.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import math
import sqlite3
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from transit import settings

log = logging.getLogger("transit.tfl")


class TflError(Exception):
    """A reply that is not an answer (HTTP error, disambiguation, unreadable body)."""


@dataclass
class Journey:
    status: str                    # "ok" or "no_journey"
    minutes: float                 # earliest arrival - requested departure; inf for no_journey
    arrival: str | None = None
    legs: list[dict] = field(default_factory=list)


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", ""))


def parse_journeys(status: int, payload: dict | None, departure: datetime) -> Journey:
    """Read one Journey Planner reply. Raises TflError for anything that is not an answer."""
    if status == 300:
        raise TflError("disambiguation")
    if status == 404 and isinstance(payload, dict):
        msg = str(payload.get("message", "")).lower()
        if "no journey" in msg or ("journey" in msg and "not found" in msg):
            return Journey("no_journey", math.inf)
        raise TflError("not found")
    if status != 200 or not isinstance(payload, dict):
        raise TflError(f"status {status}")
    journeys = payload.get("journeys")
    if journeys is None:
        raise TflError("no journeys field")
    best: tuple[datetime, dict] | None = None
    for j in journeys:
        try:
            start = _parse_dt(j["startDateTime"])
            arr = _parse_dt(j["arrivalDateTime"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TflError("journey times") from exc
        if start < departure - timedelta(minutes=1):
            continue  # a journey that leaves before the requested time is not an answer to it
        if best is None or arr < best[0]:
            best = (arr, j)
    if best is None:
        return Journey("no_journey", math.inf)
    arr, j = best
    legs = []
    for leg in j.get("legs", []):
        mode = (leg.get("mode") or {}).get("id")
        names = [o.get("name") for o in leg.get("routeOptions", []) if o.get("name")]
        legs.append({"mode": mode, "routes": names, "minutes": leg.get("duration"),
                     "summary": (leg.get("instruction") or {}).get("summary")})
    return Journey("ok", (arr - departure).total_seconds() / 60.0, arr.isoformat(), legs)


def window_median(journeys: list[Journey]) -> float:
    """Median minutes over the window's departures (inf counts as no time)."""
    vals = [j.minutes for j in journeys]
    if not vals:
        return math.nan
    return float(statistics.median(vals))


def walk_only_minutes(payload: dict | None) -> float | None:
    """Duration of the walking journey in a walk-only reply."""
    if not isinstance(payload, dict):
        return None
    for j in payload.get("journeys", []):
        modes = {(leg.get("mode") or {}).get("id") for leg in j.get("legs", [])}
        if modes == {"walking"}:
            return float(j.get("duration"))
    return None


# ── cache ───────────────────────────────────────────────────────────
class Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS replies (key TEXT PRIMARY KEY, request TEXT, "
                        "status INTEGER, body BLOB, fetched_at TEXT)")
        self.db.commit()

    @staticmethod
    def key(path: str, params: dict) -> str:
        canon = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        return hashlib.sha256(canon.encode()).hexdigest()

    def get(self, key: str) -> tuple[int, dict | None] | None:
        row = self.db.execute("SELECT status, body FROM replies WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        body = json.loads(gzip.decompress(row[1])) if row[1] else None
        return row[0], body

    def put(self, key: str, request: str, status: int, body: dict | None) -> None:
        blob = gzip.compress(json.dumps(body).encode()) if body is not None else None
        self.db.execute("INSERT OR REPLACE INTO replies VALUES (?, ?, ?, ?, ?)",
                        (key, request, status, blob, datetime.now().isoformat(timespec="seconds")))
        self.db.commit()


# ── client ──────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, per_min: int):
        self.interval = 60.0 / per_min
        self.next = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            if self.next > now:
                await asyncio.sleep(self.next - now)
            self.next = max(now, self.next) + self.interval


class JourneyPlanner:
    def __init__(self, app_key: str | None, cache_path: Path = settings.CACHE / "tfl.sqlite3",
                 rate_per_min: int = settings.TFL_RATE_PER_MIN,
                 in_flight: int = settings.TFL_IN_FLIGHT, cache_only: bool = False):
        self.app_key = app_key
        self.cache_only = cache_only     # never send a request: an uncached one is an error
        self.cache = Cache(cache_path)
        self.limiter = RateLimiter(rate_per_min)
        self.gate = asyncio.Semaphore(in_flight)
        self.client = None
        self.sent = 0
        self.cached = 0

    async def __aenter__(self) -> JourneyPlanner:
        import httpx

        self.client = httpx.AsyncClient(base_url=settings.TFL_API,
                                        headers={"User-Agent": settings.USER_AGENT},
                                        timeout=settings.TFL_TIMEOUT_S)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.client.aclose()

    async def get(self, path: str, params: dict) -> tuple[int, dict | None]:
        """GET with cache, rate limit and retries. Only answers are cached (200, 404)."""
        key = Cache.key(path, params)
        hit = self.cache.get(key)
        if hit is not None:
            self.cached += 1
            return hit
        if self.cache_only:
            raise TflError("not in cache")
        send = dict(params)
        if self.app_key:
            send["app_key"] = self.app_key
        delay = 1.0
        for attempt in range(settings.TFL_RETRIES + 1):
            await self.limiter.wait()
            async with self.gate:
                try:
                    resp = await self.client.get(path, params=send)
                except Exception as exc:  # timeouts and connection errors: try again
                    log.warning("tfl request failed path=%s error=%s", path.split("/")[1],
                                type(exc).__name__)
                    resp = None
            self.sent += 1
            if resp is not None and resp.status_code == 429:
                wait = float(resp.headers.get("retry-after", delay))
                log.warning("tfl 429, waiting %.0f s", wait)
                await asyncio.sleep(min(wait, 60))
                delay *= 2
                continue
            if resp is None or resp.status_code >= 500:
                if attempt < settings.TFL_RETRIES:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise TflError("upstream failed")
            try:
                body = resp.json()
            except ValueError:
                body = None
            # Only answers are cached: a 200 whose body is not JSON is an error to ask again.
            if resp.status_code == 404 or (resp.status_code == 200 and body is not None):
                self.cache.put(key, path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params)),
                               resp.status_code, body)
            return resp.status_code, body
        raise TflError("rate limited")

    async def journey(self, origin: tuple[float, float] | str, dest: tuple[float, float] | str,
                      departure: datetime, extra: dict | None = None) -> Journey:
        """
        origin/dest are (lon, lat), or a NaPTAN station id to go station to station (given a
        coordinate, Journey Planner walks from the nearest street address). Departure is
        London local time.
        """
        path = f"/Journey/JourneyResults/{_place(origin)}/to/{_place(dest)}"
        params = dict(settings.JP)
        params.update({"date": departure.strftime("%Y%m%d"), "time": departure.strftime("%H%M")})
        if extra:
            params.update(extra)
        status, body = await self.get(path, params)
        return parse_journeys(status, body, departure)

    async def window(self, origin, dest, start: datetime, extra: dict | None = None,
                     offsets=settings.TFL_DEPARTURE_OFFSETS_MIN) -> tuple[float, list[Journey]]:
        """Median over the window's departures; raises TflError if any departure errored."""
        js = await asyncio.gather(*(self.journey(origin, dest, start + timedelta(minutes=o), extra)
                                    for o in offsets))
        return window_median(list(js)), list(js)

    async def walk(self, origin, dest, departure: datetime) -> float | None:
        path = (f"/Journey/JourneyResults/{origin[1]:.6f},{origin[0]:.6f}/to/"
                f"{dest[1]:.6f},{dest[0]:.6f}")
        params = dict(settings.JP)
        params.update({"mode": "walking", "date": departure.strftime("%Y%m%d"),
                       "time": departure.strftime("%H%M")})
        status, body = await self.get(path, params)
        if status != 200:
            raise TflError(f"status {status}")
        return walk_only_minutes(body)

    async def lines(self, modes: str) -> list[dict]:
        status, body = await self.get(f"/Line/Mode/{modes}", {})
        if status != 200 or not isinstance(body, list):
            raise TflError(f"line list status {status}")
        return body

    async def planned_closures(self, line_ids: list[str], day: datetime) -> int:
        """Planned-works disruptions on these lines during `day` (counted per line)."""
        start = day.strftime("%Y-%m-%dT00:00:00")
        end = day.strftime("%Y-%m-%dT23:59:59")
        status, body = await self.get(f"/Line/{','.join(line_ids)}/Status/{start}/to/{end}", {})
        if status != 200 or not isinstance(body, list):
            raise TflError(f"status {status}")
        n = 0
        for line in body:
            for s in line.get("lineStatuses", []):
                if s.get("statusSeverity", 10) < 10:
                    n += 1
        return n


def _place(p) -> str:
    return p if isinstance(p, str) else f"{p[1]:.6f},{p[0]:.6f}"


def key_ok(app_key: str | None) -> bool:
    """One status call with the key; True when TfL accepts it."""
    import httpx

    if not app_key:
        return False
    resp = httpx.get(f"{settings.TFL_API}/Line/Mode/tube/Status", params={"app_key": app_key},
                     headers={"User-Agent": settings.USER_AGENT}, timeout=30)
    return resp.status_code == 200
