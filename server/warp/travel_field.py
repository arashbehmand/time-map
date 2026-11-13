"""
POST /api/travel-time-field: one travel-time field per (pin, mode, window), for both views
of the warp page (#5, W-02). See server/warp/field.py for the grid and the wire format.

  walking   two Mapbox distance isochrones, 8 contours, read at 4 km/h (R5's walking speed,
            so walking agrees with the London tables): 5–60 minutes
  cycling,  two Mapbox time isochrones, 8 contours, 5–60 minutes at Mapbox's speeds
  driving
  transit   the same two walking requests as walking (one cache entry serves both modes),
            the faster of walking and the London tables within 2 km of the pin, and the
            tables alone beyond it. No Matrix call. London only, behind the licence flag.

Every field is cached for 5 minutes by (pin to 4 decimal places, mode, window, build), and
concurrent identical requests share one build. No coordinates are logged.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime

import numpy as np

from server.cache import BoundedTTLCache
from server.errors import ApiError, NoIsochrone, OutsideTransitArea, TransitUnavailableError
from server.transit.provider import LONDON, WINDOWS, TransitHolder, window_label
from server.warp import field as F
from server.warp.mapbox_client import MapboxClient

log = logging.getLogger("isochrone_warp")

WALK_METRES = ((333, 667, 1000, 1333), (2000, 2667, 3333, 4000))
TIME_MINUTES = ((5, 10, 15, 20), (30, 40, 50, 60))
NEAR_FIELD_M = 2000.0
MARGIN_M = 1000.0
MODES = {
    "walking": {"profile": "walking", "unit": "meters", "values": WALK_METRES, "cell": 100.0},
    "cycling": {"profile": "cycling", "unit": "minutes", "values": TIME_MINUTES, "cell": 200.0},
    "driving": {"profile": "driving", "unit": "minutes", "values": TIME_MINUTES, "cell": 400.0},
}
MAPBOX_LIMIT_MINUTES = 60
TABLES_LIMIT_MINUTES = 180


def default_window(now: datetime | None = None) -> str:
    """The timetable window nearest the current London time."""
    now = now or datetime.now(LONDON)
    if now.weekday() >= 5:
        return "sa13"
    if now.hour < 11:
        return "wd08"
    return "wd13" if now.hour < 16 else "wd19"


def _to_minutes(unit: str):
    return (lambda v: v / F.WALK_M_PER_MIN) if unit == "meters" else float


class TravelFieldService:
    def __init__(self, isochrones: MapboxClient, transit: TransitHolder, slots: asyncio.Semaphore,
                 cache: BoundedTTLCache):
        self.isochrones = isochrones
        self.transit = transit
        self.slots = slots
        self.cache = cache
        self._inflight: dict[tuple, asyncio.Task] = {}

    async def get(self, lon: float, lat: float, mode: str, window: str | None) -> dict:
        lon, lat = round(lon, 4), round(lat, 4)
        data = None
        if mode == "transit":
            data = self.transit.usable()
            if data is None:
                raise TransitUnavailableError(extra={"reason": self.transit.reason or "unavailable"})
            window = window if window in WINDOWS else default_window()
        else:
            window = None
        key = (lon, lat, mode, window, data.build if data else None)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._build(lon, lat, mode, window, data))
            self._inflight[key] = task
            task.add_done_callback(lambda _t, k=key: self._inflight.pop(k, None))
        result = await asyncio.shield(task)
        self.cache.set(key, result)
        return result

    async def _contours(self, lon: float, lat: float, mode: str) -> list[dict]:
        spec = MODES["walking" if mode == "transit" else mode]
        replies = await asyncio.gather(*(
            self.isochrones.fetch_isochrones(lon, lat, spec["profile"], values,
                                             unit=spec["unit"], denoise=0)
            for values in spec["values"]))
        return [f for r in replies for f in r.get("features", [])]

    async def _build(self, lon: float, lat: float, mode: str, window: str | None, data) -> dict:
        t0 = time.monotonic()
        frame = F.LocalFrame(lon, lat)
        notes: list[str] = []
        if mode != "transit":
            features = await self._contours(lon, lat, mode)
            if not features:
                raise NoIsochrone()
            spec = MODES[mode]
            async with self.slots:
                body = await asyncio.to_thread(self._mapbox_field, features, frame, spec)
            log.info("  field built  mode=%s nodes=%d  %.0fms", mode, body["nx"] * body["ny"],
                     (time.monotonic() - t0) * 1000)
            return {"origin": {"lon": lon, "lat": lat}, "mode": mode, "window": None,
                    "window_label": None, **body, "sources": "mapbox",
                    "limit_minutes": MAPBOX_LIMIT_MINUTES, "approximate": True,
                    "valid_until": None, "notes": notes, "credits": [],
                    "development_only": False}

        if not data.covered(lon, lat):
            raise OutsideTransitArea()

        async def walking():
            try:
                return await self._contours(lon, lat, "walking")
            except ApiError as exc:
                log.warning("transit field without its walking near field (%s)", exc.code)
                notes.append("near_field_missing")
                return []

        async def composition():
            async with self.slots:
                grid = data.grid_for(frame, MODES["walking"]["cell"])
                return grid, await asyncio.to_thread(data.field, lon, lat, window, frame, grid)

        features, (grid, comp) = await asyncio.gather(walking(), composition())
        async with self.slots:
            body = await asyncio.to_thread(self._transit_field, features, frame, data, grid, comp)
        log.info("  field built  mode=transit nodes=%d  %.0fms", body["nx"] * body["ny"],
                 (time.monotonic() - t0) * 1000)
        return {"origin": {"lon": lon, "lat": lat}, "mode": "transit", "window": window,
                "window_label": window_label(data.windows[window]), **body,
                "sources": "mapbox+tables" if features else "tables",
                "limit_minutes": TABLES_LIMIT_MINUTES, "approximate": True,
                "valid_until": data.valid_until.isoformat(), "notes": notes,
                "credits": data.status()["credits"], "development_only": data.development_only}

    # ── CPU parts (run in a thread) ───────────────────────────────────
    @staticmethod
    def _mapbox_field(features: list[dict], frame: F.LocalFrame, spec: dict) -> dict:
        regions = F.contour_regions(features, frame, _to_minutes(spec["unit"]))
        grid = F.Grid.covering(spec["cell"], *F.regions_box(regions, MARGIN_M))
        minutes = F.contour_field(regions, grid)
        filled = np.zeros(minutes.shape, dtype=bool)
        minutes, filled, grid = F.crop(minutes, filled, grid, MARGIN_M)
        return {**grid.header(), "minutes_b64": F.encode(minutes, filled)}

    @staticmethod
    def _transit_field(features: list[dict], frame: F.LocalFrame, data, grid: F.Grid,
                       composed: tuple) -> dict:
        comp, filled, row = composed
        comp = F.dequantise(comp)
        if features:
            regions = F.contour_regions(features, frame, _to_minutes("meters"))
            walk = F.contour_field(regions, grid)
        else:
            walk = np.full(comp.shape, np.nan)
        minutes, filled = F.merge_near(walk, comp, filled, grid, NEAR_FIELD_M)
        oj, oi = grid.origin_index()
        minutes[oj, oi] = 0.0
        filled[oj, oi] = False
        minutes, filled, grid = F.crop(minutes, filled, grid, MARGIN_M)
        return {**grid.header(), "minutes_b64": F.encode(minutes, filled),
                "stations": _stations(data, row, frame, grid)}


def _stations(data, row: np.ndarray, frame: F.LocalFrame, grid: F.Grid) -> list[dict]:
    """Usable stations inside the grid with a time, one per name (the quickest)."""
    best: dict[str, dict] = {}
    x1, y1 = grid.x0 + (grid.nx - 1) * grid.cell, grid.y0 + (grid.ny - 1) * grid.cell
    for s in data.stations:
        t = float(row[s.idx])
        if not math.isfinite(t) or t > TABLES_LIMIT_MINUTES:
            continue
        x, y = frame.to_local(s.lon, s.lat)
        if not (grid.x0 <= x <= x1 and grid.y0 <= y <= y1):
            continue
        if s.name not in best or t < best[s.name]["minutes"]:
            best[s.name] = {"name": s.name, "lon": round(s.lon, 5), "lat": round(s.lat, 5),
                            "minutes": round(t, 1)}
    return sorted(best.values(), key=lambda s: (s["minutes"], s["name"]))
