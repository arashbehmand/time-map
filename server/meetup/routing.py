"""
Travel-time routers.

Two implementations:
  1. MapboxMatrixRouter  — real network travel times via Directions Matrix API
  2. EuclideanRouter     — haversine / speed estimate (zero API calls, for testing)

The Matrix API is the key architectural decision: it gives actual
network travel times from each participant to each candidate location
in a single batched call per transport mode, completely eliminating
the isochrone → polygon → interpolation pipeline.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
import numpy as np

from server.errors import ApiError, Busy, UpstreamError
from server.meetup.candidates import haversine_km
from server.upstream import fail, json_or_fail, mapbox_send

log = logging.getLogger("time_map.matrix")

Coord = tuple[float, float]  # (lon, lat)

FALLBACK_SPEEDS_KMH = {
    "walking": 4.8,
    "cycling": 15.0,
    "driving": 30.0,
    "driving-traffic": 22.0,
}

class Router:
    async def duration_matrix(
        self, mode: str, origins: list[Coord], destinations: list[Coord]
    ) -> np.ndarray:
        """Returns shape (len(origins), len(destinations)), values in minutes."""
        raise NotImplementedError

class EuclideanRouter(Router):
    """Haversine distance / average speed. Good for smoke tests."""

    async def duration_matrix(
        self, mode: str, origins: list[Coord], destinations: list[Coord]
    ) -> np.ndarray:
        speed = FALLBACK_SPEEDS_KMH.get(mode, 5.0)  # km/h
        out = np.empty((len(origins), len(destinations)), dtype=np.float64)
        for i, (olon, olat) in enumerate(origins):
            for j, (dlon, dlat) in enumerate(destinations):
                out[i, j] = haversine_km(olon, olat, dlon, dlat) / speed * 60.0
        return out

class MapboxMatrixRouter(Router):
    """
    Mapbox Directions Matrix API.
    Limit: 25 total coordinates per request.
    Returns travel times in minutes.
    """

    URL = "https://api.mapbox.com/directions-matrix/v1/mapbox"

    def __init__(self, token: str, client: httpx.AsyncClient, max_coords: int = 25):
        self.token = token
        self.max_coords = max_coords
        self.client = client

    async def duration_matrix(
        self, mode: str, origins: list[Coord], destinations: list[Coord]
    ) -> np.ndarray:
        if not destinations:
            return np.empty((len(origins), 0))

        max_dest = self.max_coords - len(origins)
        if max_dest < 1:
            raise ApiError(
                "Too many people use the same travel mode for one search.",
                status=422, code="invalid_request",
            )

        chunks: list[np.ndarray] = []
        for start in range(0, len(destinations), max_dest):
            chunk_dest = destinations[start : start + max_dest]
            mat = await self._fetch_chunk(mode, origins, chunk_dest)
            chunks.append(mat)

        return np.hstack(chunks)

    async def _fetch_chunk(
        self, mode: str, origins: list[Coord], destinations: list[Coord]
    ) -> np.ndarray:
        # Mapbox requires at least 2 matrix elements (n_origins * n_destinations >= 2).
        # Pad with a duplicate destination if needed; we discard the extra column below.
        n_orig, n_dest = len(origins), len(destinations)
        pad = max(0, 2 - n_orig * n_dest)
        padded = destinations + destinations[:pad]

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

        data = await self._get_json(url, mode)
        raw = data.get("durations") if isinstance(data, dict) else None
        if not isinstance(raw, list) or len(raw) < n_orig:
            log.warning("upstream bad payload api=matrix profile=%s", mode)
            raise UpstreamError()

        out = np.full((n_orig, n_dest), np.inf)
        try:
            for i, row in enumerate(raw[:n_orig]):
                for j, val in enumerate(row[:n_dest]):  # discard any padding columns
                    if val is not None:
                        out[i, j] = float(val) / 60.0  # seconds → minutes
        except (TypeError, ValueError):
            log.warning("upstream bad payload api=matrix profile=%s", mode)
            raise UpstreamError() from None
        return out

    async def _get_json(self, url: str, mode: str) -> dict:
        for attempt in range(3):
            resp = await mapbox_send(self.client, url, api="matrix", profile=mode)
            if resp.status_code == 429:
                fail(resp, api="matrix", profile=mode)
                await asyncio.sleep(0.6 * 2**attempt)
                continue
            return json_or_fail(resp, api="matrix", profile=mode)
        raise Busy("The routing service is busy. Please try again in a minute.")

def build_router(provider: str, token: str = "", client: httpx.AsyncClient | None = None,
                 **kw) -> Router:
    provider = provider.strip().lower()
    if provider == "mapbox":
        if client is None:
            raise ValueError("MapboxMatrixRouter needs a shared httpx client.")
        return MapboxMatrixRouter(token, client, **kw)
    if provider == "euclidean":
        return EuclideanRouter()
    raise ValueError(f"Unknown ROUTER_PROVIDER: {provider!r}")
