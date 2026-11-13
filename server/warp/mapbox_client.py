"""
Mapbox Isochrone client.

One request per call: at most 4 contours, in minutes (at most 60) or in metres (at most
100,000), which is what Mapbox accepts. Results are cached in a bounded LRU cache for 5
minutes, keyed by (lon4, lat4, profile, unit, values, denoise); a cache hit costs no budget.

`denoise`: Mapbox's default (1.0) returns only the largest piece of each contour. The travel-
time field asks for 0, so every piece, island and hole comes back; the raw /api/isochrones
endpoint keeps the default.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Sequence

import httpx

from server.cache import BoundedTTLCache
from server.errors import UpstreamError
from server.guard.guard import UpstreamGuard
from server.upstream import json_or_fail, mapbox_send

log = logging.getLogger("isochrone_warp")

ISOCHRONE_URL = "https://api.mapbox.com/isochrone/v1/mapbox"


class MapboxClient:

    def __init__(
        self,
        client: httpx.AsyncClient,
        guard: UpstreamGuard,
        token: str,
        cache: BoundedTTLCache | None = None,
        cache_profiles: Sequence[str] | None = None,
    ):
        self.client = client
        self.guard = guard
        self.token = token
        self.cache = cache if cache is not None else BoundedTTLCache(maxsize=256, ttl=300)
        # Profiles whose replies are kept (None: all). The field keeps walking only: walk and
        # transit at one pin share it, while a cycling reply (up to about 4 MB parsed) is only
        # ever used once, by the field that the field cache then keeps.
        self.cache_profiles = None if cache_profiles is None else frozenset(cache_profiles)
        # Identical requests in flight share one call (a walk and a transit field asked for at
        # once at one pin, say), so a new origin never costs more than its two requests.
        self._inflight: dict[tuple, asyncio.Task] = {}

    async def fetch_isochrones(
        self,
        lon: float,
        lat: float,
        profile: str,
        minutes: Sequence[int],
        *,
        unit: str = "minutes",
        denoise: float | None = None,
    ) -> dict:
        """`minutes`: the contour values, in `unit` ("minutes" or "meters")."""
        if unit not in ("minutes", "meters"):
            raise ValueError("unit must be minutes or meters")
        values = tuple(sorted(set(int(m) for m in minutes)))
        if not 1 <= len(values) <= 4:
            raise ValueError("Mapbox takes 1 to 4 contours per request")
        cache_key = (round(lon, 4), round(lat, 4), profile, unit, values, denoise)
        cached = self.cache.get(cache_key)
        if cached is not None:
            log.info("  isochrones (cache hit)")
            return cached
        task = self._inflight.get(cache_key)
        if task is None:
            task = asyncio.create_task(self._fetch(lon, lat, profile, unit, values, denoise, cache_key))
            self._inflight[cache_key] = task
            task.add_done_callback(lambda _t, k=cache_key: self._inflight.pop(k, None))
        return await asyncio.shield(task)

    async def _fetch(self, lon, lat, profile, unit, values, denoise, cache_key) -> dict:
        params = {
            f"contours_{unit}": ",".join(map(str, values)),
            "polygons": "true",
            "access_token": self.token,
        }
        if denoise is not None:
            params["denoise"] = f"{float(denoise):g}"
        async with self.guard.upstream("isochrone", 1):
            resp = await mapbox_send(
                self.client,
                f"{ISOCHRONE_URL}/{profile}/{lon},{lat}",
                api="isochrone",
                profile=profile,
                params=params,
            )
        data = json_or_fail(resp, api="isochrone", profile=profile)
        features = data.get("features") if isinstance(data, dict) else None
        if not isinstance(features, list):
            log.warning("upstream bad payload api=isochrone profile=%s", profile)
            raise UpstreamError()

        try:
            features.sort(key=lambda f: float(f["properties"]["contour"]))
        except (KeyError, TypeError, ValueError):
            log.warning("upstream bad payload api=isochrone profile=%s", profile)
            raise UpstreamError() from None
        result = {"type": "FeatureCollection", "features": features}
        if self.cache_profiles is None or profile in self.cache_profiles:
            self.cache.set(cache_key, result)
        return result
