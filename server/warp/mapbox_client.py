"""
Mapbox Isochrone client.

One request per call (travel times are capped at 4 values of at most 60 minutes, which is
what Mapbox accepts). Results are cached in a bounded LRU cache for 5 minutes, keyed by
(lon4, lat4, profile, times); a cache hit costs no budget.
"""
from __future__ import annotations

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
    ):
        self.client = client
        self.guard = guard
        self.token = token
        self.cache = cache if cache is not None else BoundedTTLCache(maxsize=256, ttl=300)

    async def fetch_isochrones(
        self,
        lon: float,
        lat: float,
        profile: str,
        minutes: Sequence[int],
    ) -> dict:
        sorted_minutes = tuple(sorted(set(int(m) for m in minutes)))
        cache_key = (round(lon, 4), round(lat, 4), profile, sorted_minutes)
        cached = self.cache.get(cache_key)
        if cached is not None:
            log.info("  isochrones (cache hit)")
            return cached

        async with self.guard.upstream("isochrone", 1):
            resp = await mapbox_send(
                self.client,
                f"{ISOCHRONE_URL}/{profile}/{lon},{lat}",
                api="isochrone",
                profile=profile,
                params={
                    "contours_minutes": ",".join(map(str, sorted_minutes)),
                    "polygons": "true",
                    "access_token": self.token,
                },
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
        self.cache.set(cache_key, result)
        return result
