"""
Local frames and regular grids, shared by the warp page's travel-time field
(server/warp/field.py) and the transit provider (server/transit/provider.py).

  local metres   metres east (x) and south (y) of an origin, at the origin's latitude: Web
                 Mercator scaled, exactly as static/shared/mesh/frame.js's localFrame (a test
                 vector is shared by tests/test_field.py and tests/js/mesh-decode.test.js)
  grid node      (i, j) sits at (x0 + i·cell, y0 + j·cell); x0 and y0 are multiples of the
                 cell, so the origin is always a node
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

EARTH_CIRCUMFERENCE_M = 40075016.68557849
MAX_LAT = 85.0511287798066
MAX_SIDE = 601                      # nodes per side, at most (Mapbox fields)


# ── Frames (twin of static/shared/mesh/frame.js) ────────────────────
def mercator(lon, lat):
    phi = np.radians(np.clip(lat, -MAX_LAT, MAX_LAT))
    return (np.asarray(lon) + 180) / 360, (1 - np.log(np.tan(np.pi / 4 + phi / 2)) / np.pi) / 2


def unmercator(mx, my):
    lon = np.asarray(mx) * 360 - 180
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * np.asarray(my)))))
    return lon, lat


def metres_per_unit(lat: float) -> float:
    return EARTH_CIRCUMFERENCE_M * math.cos(math.radians(lat))


@dataclass(frozen=True)
class LocalFrame:
    lon: float
    lat: float

    @property
    def m(self) -> float:
        return metres_per_unit(self.lat)

    @property
    def m0(self) -> tuple[float, float]:
        mx, my = mercator(self.lon, self.lat)
        return float(mx), float(my)

    def to_local(self, lon, lat):
        mx, my = mercator(lon, lat)
        mx0, my0 = self.m0
        return (mx - mx0) * self.m, (my - my0) * self.m

    def from_local(self, x, y):
        mx0, my0 = self.m0
        return unmercator(mx0 + np.asarray(x) / self.m, my0 + np.asarray(y) / self.m)


# ── Grids ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Grid:
    cell: float
    x0: float
    y0: float
    nx: int
    ny: int

    @classmethod
    def covering(cls, cell: float, x0: float, y0: float, x1: float, y1: float,
                 max_half: int | None = None) -> Grid:
        """The smallest grid of this cell that covers the box and has the origin as a node,
        at most `max_half` cells (default (MAX_SIDE - 1) / 2) either side of the origin."""
        x0, y0 = min(x0, 0.0), min(y0, 0.0)
        x1, y1 = max(x1, 0.0), max(y1, 0.0)
        i0, j0 = math.floor(x0 / cell), math.floor(y0 / cell)
        i1, j1 = math.ceil(x1 / cell), math.ceil(y1 / cell)
        half = (MAX_SIDE - 1) // 2 if max_half is None else max_half
        i0, i1 = max(i0, -half), min(i1, half)
        j0, j1 = max(j0, -half), min(j1, half)
        return cls(cell, i0 * cell, j0 * cell, i1 - i0 + 1, j1 - j0 + 1)

    def axes(self) -> tuple[np.ndarray, np.ndarray]:
        return self.x0 + self.cell * np.arange(self.nx), self.y0 + self.cell * np.arange(self.ny)

    def mesh(self) -> tuple[np.ndarray, np.ndarray]:
        xs, ys = self.axes()
        return np.meshgrid(xs, ys)          # (ny, nx) each

    def origin_index(self) -> tuple[int, int]:
        return round(-self.y0 / self.cell), round(-self.x0 / self.cell)

    def header(self) -> dict:
        return {"cell": float(self.cell), "nx": int(self.nx), "ny": int(self.ny),
                "x0": float(self.x0), "y0": float(self.y0)}
