"""
Mapbox API client: isochrones and vector tiles.

Uses httpx async for parallel tile fetching.
PBF decoding runs in a thread-pool executor (it's CPU-bound pure Python).
Tiles are cached in-process with a 1-hour TTL.
Isochrones are cached with a 5-minute TTL keyed by (lon4, lat4, profile, times).
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Sequence

import httpx
import mapbox_vector_tile
import numpy as np

from server.config import MAPBOX_TOKEN
from server.warp.geometry import TILE_SIZE, Viewport

log = logging.getLogger("isochrone_warp")

# Shared decode pool — one thread per core is plenty for CPU-bound PBF work
_decode_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="pbf")

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

_tile_cache = _TTLCache(ttl=3600)       # tiles: 1 hour
_iso_cache  = _TTLCache(ttl=300)        # isochrones: 5 minutes

# ── helpers ──────────────────────────────────────────────────────────

def _chunked(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i : i + n]

def _map_coords(obj, fn):
    """Recursively apply *fn* to every [x, y] leaf in a nested list."""
    if isinstance(obj, (list, tuple)) and len(obj) > 0 and isinstance(obj[0], (int, float)):
        x, y = float(obj[0]), float(obj[1])
        return list(fn(x, y))
    return [_map_coords(child, fn) for child in obj]

def _decode_pbf(data: bytes) -> dict:
    """Synchronous PBF decode — runs in thread pool."""
    try:
        return mapbox_vector_tile.decode(data, y_coord_down=True)
    except TypeError:
        return mapbox_vector_tile.decode(data)

# ── async client ─────────────────────────────────────────────────────

class MapboxClient:

    def __init__(self, token: str = MAPBOX_TOKEN):
        self.token = token

    # ── isochrones ───────────────────────────────────────────────

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
        async with httpx.AsyncClient(timeout=30) as client:
            tasks = [
                client.get(
                    f"https://api.mapbox.com/isochrone/v1/mapbox/{profile}/{lon},{lat}",
                    params={
                        "contours_minutes": ",".join(map(str, batch)),
                        "polygons": "true",
                        "access_token": self.token,
                    },
                )
                for batch in _chunked(sorted_minutes, 4)
            ]
            responses = await asyncio.gather(*tasks)
            for resp in responses:
                resp.raise_for_status()
                out.extend(resp.json().get("features", []))

        out.sort(key=lambda f: float(f["properties"]["contour"]))
        result = {"type": "FeatureCollection", "features": out}
        _iso_cache.set(cache_key, result)
        return result

    # ── vector tiles ─────────────────────────────────────────────

    def _tile_geom_to_canvas(
        self, geometry: dict, tx: int, ty: int, extent: float, vp: Viewport,
    ) -> dict:
        """Convert tile-local geometry coordinates to canvas pixels."""
        def fn(lx: float, ly: float):
            wx = (tx + lx / extent) * TILE_SIZE
            wy = (ty + ly / extent) * TILE_SIZE
            return list(vp.world_to_canvas(wx, wy))

        gt = geometry["type"]
        if gt == "GeometryCollection":
            return {
                "type": "GeometryCollection",
                "geometries": [
                    self._tile_geom_to_canvas(g, tx, ty, extent, vp)
                    for g in geometry["geometries"]
                ],
            }
        return {
            "type": gt,
            "coordinates": _map_coords(geometry["coordinates"], fn),
        }

    async def fetch_vector_features(
        self,
        viewport: Viewport,
        layers: Sequence[str],
        pad_px: int = 512,
    ) -> dict[str, list[dict]]:
        """
        Fetch vector tiles covering the viewport.
        - HTTP requests run concurrently via httpx async
        - PBF decoding runs concurrently in a thread-pool (CPU-bound)
        - Results cached by (z, x, y) for 1 hour
        """
        xmin, ymin, xmax, ymax = viewport.world_bbox(pad_px)
        ntiles = 2 ** viewport.zoom
        tx0 = max(0, int(math.floor(xmin / TILE_SIZE)))
        ty0 = max(0, int(math.floor(ymin / TILE_SIZE)))
        tx1 = min(ntiles - 1, int(math.floor(xmax / TILE_SIZE)))
        ty1 = min(ntiles - 1, int(math.floor(ymax / TILE_SIZE)))

        tile_coords = [
            (tx, ty)
            for tx in range(tx0, tx1 + 1)
            for ty in range(ty0, ty1 + 1)
        ]

        cached_hits = sum(1 for tx, ty in tile_coords
                          if _tile_cache.get((viewport.zoom, tx, ty)) is not None)
        need_fetch = [(tx, ty) for tx, ty in tile_coords
                      if _tile_cache.get((viewport.zoom, tx, ty)) is None]

        log.info("  tiles: %d total, %d cached, %d to fetch  (zoom=%d, %dx%d grid)",
                 len(tile_coords), cached_hits, len(need_fetch),
                 viewport.zoom, tx1 - tx0 + 1, ty1 - ty0 + 1)

        if need_fetch:
            t_net = time.monotonic()
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10, read=30, write=10, pool=5),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            ) as client:
                raw = await asyncio.gather(
                    *[
                        client.get(
                            f"https://api.mapbox.com/v4/mapbox.mapbox-streets-v8"
                            f"/{viewport.zoom}/{tx}/{ty}.vector.pbf",
                            params={"access_token": self.token},
                        )
                        for tx, ty in need_fetch
                    ],
                    return_exceptions=True,
                )
            log.info("  network done  %.0fms  (%d/%d succeeded)",
                     (time.monotonic() - t_net) * 1000,
                     sum(1 for r in raw if not isinstance(r, Exception) and r.status_code == 200),
                     len(need_fetch))

            t_dec = time.monotonic()
            loop = asyncio.get_event_loop()
            decode_tasks = []
            valid_coords = []
            for (tx, ty), result in zip(need_fetch, raw):
                if isinstance(result, Exception):
                    log.debug("    tile %d/%d/%d fetch error: %s", viewport.zoom, tx, ty, result)
                    continue
                if result.status_code != 200:
                    continue
                decode_tasks.append(
                    loop.run_in_executor(_decode_pool, _decode_pbf, result.content)
                )
                valid_coords.append((tx, ty))

            decoded_list = await asyncio.gather(*decode_tasks, return_exceptions=True)
            log.info("  decode done  %.0fms  (%d tiles)",
                     (time.monotonic() - t_dec) * 1000, len(valid_coords))

            for (tx, ty), decoded in zip(valid_coords, decoded_list):
                if not isinstance(decoded, Exception):
                    _tile_cache.set((viewport.zoom, tx, ty), decoded)

        # Build output from cache
        out: dict[str, list[dict]] = {ly: [] for ly in layers}
        for tx, ty in tile_coords:
            decoded = _tile_cache.get((viewport.zoom, tx, ty))
            if not decoded:
                continue
            for layer_name in layers:
                layer = decoded.get(layer_name)
                if not layer:
                    continue
                extent = float(layer.get("extent", 4096))
                for feat in layer.get("features", []):
                    geom = self._tile_geom_to_canvas(
                        feat["geometry"], tx, ty, extent, viewport,
                    )
                    out[layer_name].append({
                        "type": "Feature",
                        "layer": layer_name,
                        "properties": dict(feat.get("properties", {})),
                        "geometry": geom,
                    })
        return out
