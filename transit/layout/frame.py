"""
The renderer's `local` frame (static/shared/mesh/frame.js) in Python: Web Mercator scaled to
metres at the origin's latitude, x east and y south. It lines up with MapLibre, so the London
map's renderer (#6, L-02) can take these coordinates as they are. "Metres" are exact only at
the origin's latitude (under 1% off at the edge of Greater London).
"""
from __future__ import annotations

import math

import numpy as np

EARTH_CIRCUMFERENCE_M = 40075016.68557849
MAX_LAT = 85.0511287798066
ORIGIN = (-0.1276, 51.5073)          # Charing Cross (lon, lat), fixed so builds compare


def mercator(lon, lat):
    """lon/lat to Web Mercator in [0, 1), y down (frame.js `mercator`)."""
    phi = np.radians(np.clip(np.asarray(lat, dtype=np.float64), -MAX_LAT, MAX_LAT))
    mx = (np.asarray(lon, dtype=np.float64) + 180) / 360
    my = (1 - np.log(np.tan(np.pi / 4 + phi / 2)) / np.pi) / 2
    return mx, my


def metres_per_unit(lat: float) -> float:
    """Metres per Mercator unit at a latitude (frame.js `metresPerUnit`)."""
    return EARTH_CIRCUMFERENCE_M * math.cos(math.radians(lat))


def to_local(lon, lat, origin=ORIGIN) -> np.ndarray:
    """lon/lat to local metres around `origin`, shape (..., 2) (frame.js `toLocal`)."""
    mx0, my0 = mercator(origin[0], origin[1])
    m = metres_per_unit(origin[1])
    mx, my = mercator(lon, lat)
    return np.stack([(mx - mx0) * m, (my - my0) * m], axis=-1)


def from_local(x, y, origin=ORIGIN) -> np.ndarray:
    """Local metres back to lon/lat, shape (..., 2) (frame.js `fromLocal`)."""
    mx0, my0 = mercator(origin[0], origin[1])
    m = metres_per_unit(origin[1])
    mx = mx0 + np.asarray(x, dtype=np.float64) / m
    my = my0 + np.asarray(y, dtype=np.float64) / m
    lon = mx * 360 - 180
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * my))))
    return np.stack([lon, lat], axis=-1)
