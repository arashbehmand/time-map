"""
Viewport and coordinate conversions.

Three coordinate spaces:
  1. Geographic: (longitude, latitude) — EPSG:4326
  2. World pixels: (wx, wy) — Slippy Map convention at a given zoom
     x ∈ [0, 256·2^z), increases east; y ∈ [0, 256·2^z), increases south
  3. Canvas pixels: (cx, cy) — origin at top-left of the canvas,
     user location at (width/2, height/2)
"""
from __future__ import annotations

import math
import numpy as np

TILE_SIZE: float = 256.0

# ── geographic ↔ world pixels ────────────────────────────────────────

def lonlat_to_world(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    scale = TILE_SIZE * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * scale
    lat_r = math.radians(max(-85.0511, min(85.0511, lat)))
    siny = math.sin(lat_r)
    y = (0.5 - math.log((1.0 + siny) / (1.0 - siny)) / (4.0 * math.pi)) * scale
    return x, y

def lonlat_to_world_batch(
    lons: np.ndarray, lats: np.ndarray, zoom: int
) -> tuple[np.ndarray, np.ndarray]:
    scale = TILE_SIZE * (2 ** zoom)
    x = (np.asarray(lons, dtype=float) + 180.0) / 360.0 * scale
    lat_r = np.radians(np.clip(np.asarray(lats, dtype=float), -85.0511, 85.0511))
    siny = np.sin(lat_r)
    y = (0.5 - np.log((1.0 + siny) / (1.0 - siny)) / (4.0 * math.pi)) * scale
    return x, y

def world_to_lonlat(wx: float, wy: float, zoom: int) -> tuple[float, float]:
    scale = TILE_SIZE * (2 ** zoom)
    lon = wx / scale * 360.0 - 180.0
    n = math.pi - 2.0 * math.pi * wy / scale
    lat = math.degrees(math.atan(math.sinh(n)))
    return lon, lat

# ── Viewport ─────────────────────────────────────────────────────────

class Viewport:
    """Defines the map view: center location, zoom level, canvas size."""

    __slots__ = (
        "center_lon", "center_lat", "zoom", "width", "height", "_cw",
    )

    def __init__(
        self,
        center_lon: float,
        center_lat: float,
        zoom: int,
        width: int,
        height: int,
    ):
        self.center_lon = center_lon
        self.center_lat = center_lat
        self.zoom = zoom
        self.width = width
        self.height = height
        cx, cy = lonlat_to_world(center_lon, center_lat, zoom)
        self._cw = np.array([cx, cy], dtype=float)

    @property
    def center_canvas(self) -> np.ndarray:
        return np.array([self.width / 2.0, self.height / 2.0])

    # ── single-point conversions ─────────────────────────────────

    def lonlat_to_canvas(self, lon: float, lat: float) -> tuple[float, float]:
        wx, wy = lonlat_to_world(lon, lat, self.zoom)
        return (
            wx - self._cw[0] + self.width / 2.0,
            wy - self._cw[1] + self.height / 2.0,
        )

    def world_to_canvas(self, wx: float, wy: float) -> tuple[float, float]:
        return (
            wx - self._cw[0] + self.width / 2.0,
            wy - self._cw[1] + self.height / 2.0,
        )

    def canvas_to_lonlat(self, cx: float, cy: float) -> tuple[float, float]:
        wx = cx - self.width / 2.0 + self._cw[0]
        wy = cy - self.height / 2.0 + self._cw[1]
        return world_to_lonlat(wx, wy, self.zoom)

    # ── batch conversions ────────────────────────────────────────

    def lonlat_to_canvas_batch(self, coords: np.ndarray) -> np.ndarray:
        """(N, 2) lon/lat → (N, 2) canvas pixels."""
        c = np.asarray(coords, dtype=float)
        wx, wy = lonlat_to_world_batch(c[:, 0], c[:, 1], self.zoom)
        return np.column_stack([
            wx - self._cw[0] + self.width / 2.0,
            wy - self._cw[1] + self.height / 2.0,
        ])

    def world_to_canvas_batch(self, pts: np.ndarray) -> np.ndarray:
        """(N, 2) world pixels → (N, 2) canvas pixels."""
        pts = np.asarray(pts, dtype=float)
        return np.column_stack([
            pts[:, 0] - self._cw[0] + self.width / 2.0,
            pts[:, 1] - self._cw[1] + self.height / 2.0,
        ])

    # ── bounding helpers ─────────────────────────────────────────

    def world_bbox(self, pad_px: int = 0) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) in world pixels."""
        hw, hh = self.width / 2.0, self.height / 2.0
        return (
            self._cw[0] - hw - pad_px,
            self._cw[1] - hh - pad_px,
            self._cw[0] + hw + pad_px,
            self._cw[1] + hh + pad_px,
        )

    def support_rect(self, pad_px: int) -> tuple[float, float, float, float]:
        """Canvas-space rectangle for the support region."""
        return (-pad_px, -pad_px, self.width + pad_px, self.height + pad_px)
