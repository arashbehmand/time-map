"""
Mapbox Isochrone client.

Isochrones are cached in-process with a 5-minute TTL keyed by (lon4, lat4, profile, times).
"""
from __future__ import annotations

import logging
import time
from typing import Sequence

import httpx

from server.config import MAPBOX_ROUTING_TOKEN
from server.errors import UpstreamError
from server.upstream import mapbox_get_json

log = logging.getLogger("isochrone_warp")

ISOCHRONE_URL = "https://api.mapbox.com/isochrone/v1/mapbox"


# ── simple TTL cache ─────────────────────────────────────────────────

class _TTLCache:
    def __init__(self, ttl: float):
        self._ttl = ttl
        self._store: dict[tuple, tuple[float, object]] = {}

    def get(self, key: tuple):
        entry = self._store.get(key)
        if entry and time.monotonic() - entry[0] < self._ttl:
            return entry[1]
        return None

    def set(self, key: tuple, value: object):
        self._store[key] = (time.monotonic(), value)


_iso_cache = _TTLCache(ttl=300)        # isochrones: 5 minutes


def _chunked(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


# ── async client ─────────────────────────────────────────────────────

class MapboxClient:

    def __init__(self, client: httpx.AsyncClient, token: str = MAPBOX_ROUTING_TOKEN):
        self.client = client
        self.token = token

    async def fetch_isochrones(
        self,
        lon: float,
        lat: float,
        profile: str,
        minutes: Sequence[int],
    ) -> dict:
        sorted_minutes = tuple(sorted(set(int(m) for m in minutes)))
        cache_key = (round(lon, 4), round(lat, 4), profile, sorted_minutes)
        cached = _iso_cache.get(cache_key)
        if cached is not None:
            log.info("  isochrones (cache hit)")
            return cached

        out: list[dict] = []
        for batch in _chunked(sorted_minutes, 4):
            data = await mapbox_get_json(
                self.client,
                f"{ISOCHRONE_URL}/{profile}/{lon},{lat}",
                api="isochrone",
                profile=profile,
                params={
                    "contours_minutes": ",".join(map(str, batch)),
                    "polygons": "true",
                    "access_token": self.token,
                },
            )
            features = data.get("features") if isinstance(data, dict) else None
            if not isinstance(features, list):
                log.warning("upstream bad payload api=isochrone profile=%s", profile)
                raise UpstreamError()
            out.extend(features)

        try:
            out.sort(key=lambda f: float(f["properties"]["contour"]))
        except (KeyError, TypeError, ValueError):
            log.warning("upstream bad payload api=isochrone profile=%s", profile)
            raise UpstreamError() from None
        result = {"type": "FeatureCollection", "features": out}
        _iso_cache.set(cache_key, result)
        return result
