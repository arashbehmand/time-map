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

Walking, cycling and driving fields, and transit's walking near field, carry their uncertain
patches (server/warp/field.py: ground the contours leave out, drawn small on the bent map);
inside 2 km a patch's walking time never beats a time from the tables.

Every field is cached for 5 minutes by (pin to 4 decimal places, mode, window, build), and
concurrent identical requests share one build; a field built without its walking near field
(a refused or failed Mapbox call) is not cached. A build whose licences are not cleared
(TRANSIT_ALLOW_UNCLEARED) is served only to this machine (`local`). No coordinates are
logged.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime

import numpy as np

from server.cache import BoundedTTLCache
from server.errors import (
    ApiError,
    NoIsochrone,
    NoTransitAccess,
    OutsideTransitArea,
    TransitUnavailableError,
)
from server.transit.provider import LONDON, WINDOWS, TransitHolder, window_label
from server.warp import field as F
from server.warp.mapbox_client import MapboxClient

log = logging.getLogger("isochrone_warp")

WALK_METRES = ((333, 667, 1000, 1333), (2000, 2667, 3333, 4000))
TIME_MINUTES = ((5, 10, 15, 20), (30, 40, 50, 60))
NEAR_FIELD_M = 2000.0
MARGIN_M = 1000.0
# speed_m_per_min: how fast you get into an uncertain patch from its edge (field.py's walk-in
# allowance): walking at the field's own 4 km/h, cycling 15 km/h, driving 24 km/h.
MODES = {
    "walking": {"profile": "walking", "unit": "meters", "values": WALK_METRES, "cell": 100.0,
                "speed_m_per_min": F.WALK_M_PER_MIN},
    "cycling": {"profile": "cycling", "unit": "minutes", "values": TIME_MINUTES, "cell": 200.0,
                "speed_m_per_min": 250.0},
    "driving": {"profile": "driving", "unit": "minutes", "values": TIME_MINUTES, "cell": 400.0,
                "speed_m_per_min": 400.0},
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


DEGRADED_TTL_S = 30


class TravelFieldService:
    def __init__(self, isochrones: MapboxClient, transit: TransitHolder, slots: asyncio.Semaphore,
                 cache: BoundedTTLCache, degraded: BoundedTTLCache | None = None):
        self.isochrones = isochrones
        self.transit = transit
        self.slots = slots
        self.cache = cache
        # A field built without its walking near field (a Mapbox refusal) is kept for a short
        # while only: long enough that an outage doesn't send every request back to Mapbox,
        # short enough that the next visitor after it gets the full field.
        self.degraded = degraded if degraded is not None else BoundedTTLCache(maxsize=24, ttl=DEGRADED_TTL_S)
        self._inflight: dict[tuple, asyncio.Task] = {}

    async def get(self, lon: float, lat: float, mode: str, window: str | None, *, smooth: bool = True,
                  local: bool = False) -> dict:
        lon, lat = round(lon, 4), round(lat, 4)
        data = None
        if mode == "transit":
            data = self.transit.usable(local=local)
            if data is None:
                raise TransitUnavailableError(extra={"reason": self.transit.reason_for(local)})
            window = window if window in WINDOWS else default_window()
            # The tables' whole minutes are a diagnostic (Direction 4), for development only.
            smooth = smooth or not data.development_only
        else:
            window = None
            smooth = True
        key = (lon, lat, mode, window, data.build if data else None, smooth)
        cached = self.cache.get(key)
        if cached is None:
            cached = self.degraded.get(key)
        if cached is not None:
            return cached
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._build(lon, lat, mode, window, data, smooth))
            self._inflight[key] = task
            task.add_done_callback(lambda _t, k=key: self._inflight.pop(k, None))
        result = await asyncio.shield(task)
        (self.degraded if result["notes"] else self.cache).set(key, result)
        return result

    async def _contours(self, lon: float, lat: float, mode: str) -> list[dict]:
        spec = MODES["walking" if mode == "transit" else mode]
        replies = await asyncio.gather(*(
            self.isochrones.fetch_isochrones(lon, lat, spec["profile"], values,
                                             unit=spec["unit"], denoise=0)
            for values in spec["values"]))
        return [f for r in replies for f in r.get("features", [])]

    async def _build(self, lon: float, lat: float, mode: str, window: str | None, data,
                     smooth: bool = True) -> dict:
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
                    "valid_until": None, "service_date": None, "service_dates": None, "notes": notes, "credits": [],
                    "development_only": False}

        if not data.covered(lon, lat):
            raise OutsideTransitArea()
        if not data.reachable_from(lon, lat):
            raise NoTransitAccess()

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
            body = await asyncio.to_thread(self._transit_field, features, frame, data, grid, comp, smooth)
        log.info("  field built  mode=transit nodes=%d  %.0fms", body["nx"] * body["ny"],
                 (time.monotonic() - t0) * 1000)
        return {"origin": {"lon": lon, "lat": lat}, "mode": "transit", "window": window,
                "window_label": window_label(data.windows[window]), **body,
                "sources": "mapbox+tables" if features else "tables",
                "limit_minutes": TABLES_LIMIT_MINUTES, "approximate": True,
                "valid_until": data.valid_until.isoformat(),
                "service_date": data.window_date(window), "service_dates": data.service_dates,
                "notes": notes,
                "credits": data.status()["credits"], "development_only": data.development_only,
                "smoothed": smooth}

    # ── CPU parts (run in a thread) ───────────────────────────────────
    @staticmethod
    def _mapbox_field(features: list[dict], frame: F.LocalFrame, spec: dict) -> dict:
        regions = F.contour_regions(features, frame, _to_minutes(spec["unit"]))
        grid = F.Grid.covering(spec["cell"], *F.regions_box(regions, MARGIN_M))
        minutes = F.contour_field(regions, grid)
        filled = np.zeros(minutes.shape, dtype=bool)
        unsure, drawn = _patches(minutes, regions, grid, spec["speed_m_per_min"])
        minutes, filled, grid, unsure, drawn = F.crop(minutes, filled, grid, MARGIN_M, unsure, drawn)
        return _body(grid, minutes, filled, unsure, drawn)

    @staticmethod
    def _transit_field(features: list[dict], frame: F.LocalFrame, data, grid: F.Grid,
                       composed: tuple, smooth: bool = True) -> dict:
        comp, filled, row = composed
        if features:
            regions = F.contour_regions(features, frame, _to_minutes("meters"))
            walk = F.contour_field(regions, grid)
            walk_unsure, _ = _patches(walk, regions, grid, MODES["walking"]["speed_m_per_min"], fill=False)
        else:
            walk = np.full(comp.shape, np.nan)
            walk_unsure = np.zeros(comp.shape, dtype=bool)
        minutes, filled, unsure = F.merge_near(walk, comp, filled, grid, NEAR_FIELD_M, walk_unsure)
        if smooth:
            # After the merge, and only where the tables won: walking times stay as they are.
            from_tables = np.isfinite(minutes) & (minutes == comp)
            minutes = F.dequantise(minutes, only=from_tables)
        oj, oi = grid.origin_index()
        minutes[oj, oi] = 0.0
        filled[oj, oi] = False
        unsure[oj, oi] = False
        # Drawn from the merged field: a patch's rim may be the tables' times.
        drawn, unsure = F.fill_patches(minutes, unsure)
        minutes, filled, grid, unsure, drawn = F.crop(minutes, filled, grid, MARGIN_M, unsure, drawn)
        return {**_body(grid, minutes, filled, unsure, drawn), "stations": _stations(data, row, frame, grid)}


def _patches(minutes: np.ndarray, regions, grid: F.Grid, speed: float, fill: bool = True):
    """Uncertain patches of a field built from contours, and their drawn minutes (field.py)."""
    t0 = time.monotonic()
    unsure = F.uncertain_patches(minutes, [m for m, _ in regions], grid, speed)
    drawn = np.full(minutes.shape, np.nan)
    if fill:
        drawn, unsure = F.fill_patches(minutes, unsure)
    log.debug("  uncertain patches  nodes=%d  %.0fms", int(unsure.sum()), (time.monotonic() - t0) * 1000)
    return unsure, drawn


def _body(grid: F.Grid, minutes: np.ndarray, filled: np.ndarray, unsure: np.ndarray, drawn: np.ndarray) -> dict:
    body = {**grid.header(), "minutes_b64": F.encode(minutes, filled)}
    patches = F.encode_uncertain(unsure, drawn)
    if patches:
        body["uncertain"] = patches
    return body


def _stations(data, row: np.ndarray, frame: F.LocalFrame, grid: F.Grid) -> list[dict]:
    """Usable stations inside the grid with a time. Records with one name within 500 m of each
    other are one station (Bank's Underground and DLR platforms); the quickest is kept.
    Same-named stations further apart stay separate."""
    kept: list[dict] = []
    x1, y1 = grid.x0 + (grid.nx - 1) * grid.cell, grid.y0 + (grid.ny - 1) * grid.cell
    for s in data.stations:
        t = float(row[s.idx])
        if not math.isfinite(t) or t > TABLES_LIMIT_MINUTES:
            continue
        x, y = frame.to_local(s.lon, s.lat)
        x, y = float(x), float(y)
        if not (grid.x0 <= x <= x1 and grid.y0 <= y <= y1):
            continue
        same = next((k for k in kept if k["name"] == s.name and math.hypot(k["_x"] - x, k["_y"] - y) <= 500), None)
        entry = {"name": s.name, "lon": round(s.lon, 5), "lat": round(s.lat, 5), "minutes": round(t, 1), "_x": x, "_y": y}
        if same is None:
            kept.append(entry)
        elif t < same["minutes"]:
            same.update(entry)
    for k in kept:
        del k["_x"], k["_y"]
    return sorted(kept, key=lambda s: (s["minutes"], s["name"]))
