"""
Place search through our server, per the Nominatim usage policy
(https://operations.osmfoundation.org/policies/nominatim/):

  - one shared queue for the whole site, at most one upstream request per `geocode_spacing_s`;
  - results cached (24 h, "not found" for 1 h); identical concurrent searches share one request;
  - an identifying User-Agent;
  - search on submit only (the pages no longer search while typing).

Queries are never logged: they are often home addresses.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, StringConstraints

from server.cache import BoundedTTLCache
from server.errors import Busy, NotFound, UpstreamError
from server.guard.clock import Clock
from server.guard.guard import UpstreamGuard, per_ip
from server.settings import Settings
from server.upstream import json_or_fail, mapbox_send

log = logging.getLogger("time_map.geocode")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOT_FOUND = object()


class Geocoder:
    def __init__(self, client, guard: UpstreamGuard, settings: Settings, clock: Clock):
        self.client = client
        self.guard = guard
        self.settings = settings
        self.clock = clock
        self.cache = BoundedTTLCache(maxsize=2000, ttl=24 * 3600, clock=clock.monotonic)
        self._lock = asyncio.Lock()
        self._last = -math.inf
        self._waiting = 0
        self._inflight: dict[str, asyncio.Future] = {}

    @staticmethod
    def _key(q: str) -> str:
        return " ".join(q.casefold().split())

    async def lookup(self, q: str) -> dict | None:
        key = self._key(q)
        hit = self.cache.get(key)
        if hit is not None:
            return None if hit is _NOT_FOUND else hit
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key])
        if self._waiting >= self.settings.geocode_max_waiting:
            raise Busy("Place search is busy. Please try again in a few seconds.")

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            result = await self._fetch_queued(q)
            self.cache.set(key, _NOT_FOUND if result is None else result,
                           ttl=3600 if result is None else None)
            fut.set_result(result)
            return result
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved when nobody else is waiting
            raise
        finally:
            del self._inflight[key]

    async def _fetch_queued(self, q: str) -> dict | None:
        self._waiting += 1
        try:
            await self._lock.acquire()
        finally:
            self._waiting -= 1
        try:
            wait = self._last + self.settings.geocode_spacing_s - self.clock.monotonic()
            if wait > 0:
                await self.clock.sleep(wait)
            try:
                async with self.guard.upstream("geocode", 1):
                    resp = await mapbox_send(
                        self.client, NOMINATIM_URL, api="geocode",
                        params={"q": q, "format": "jsonv2", "limit": "1"},
                        headers={"User-Agent": self.settings.geocoder_user_agent},
                    )
            finally:
                self._last = self.clock.monotonic()
        finally:
            self._lock.release()

        data = json_or_fail(resp, api="geocode")
        if not isinstance(data, list):
            raise UpstreamError()
        if not data:
            return None
        try:
            lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
        except (KeyError, TypeError, ValueError):
            raise UpstreamError() from None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise UpstreamError()
        return {"lat": lat, "lon": lon}


class GeocodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


router = APIRouter(tags=["geocode"])


@router.post("/geocode", dependencies=[per_ip("geocode")])
async def geocode_endpoint(req: GeocodeRequest, request: Request):
    # POST, so the query stays out of access logs and URLs.
    result = await request.app.state.geocoder.lookup(req.q)
    if result is None:
        raise NotFound("No place found for that search.")
    return result
