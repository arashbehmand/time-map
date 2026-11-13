"""
The travel-time field the warp page draws (#5, W-02): minutes on a regular grid in local
metres around the pin, the shape static/shared/mesh/field.js takes.

  local metres   metres east (x) and south (y) of the origin, at the origin's latitude: Web
                 Mercator scaled, exactly as frame.js's localFrame (the JS test shares a
                 vector with tests/test_field.py)
  node (i, j)    sits at (x0 + i·cell, y0 + j·cell); x0 and y0 are multiples of the cell, so
                 the origin is always a node
  minutes        float64, NaN = no data (outside what we know), inf = beyond (inside what we
                 know, but no time within the tables' limit)

Contours → minutes: a node inside contour k but outside contour k−1 gets
    t = t(k−1) + (t(k) − t(k−1)) · d_in / (d_in + d_out)
where d_in is its distance to the boundary of contour k−1 (to the pin for the first contour)
and d_out its distance to the boundary of contour k. Every piece, island and hole of a contour
counts, and the contours are made to nest (contour k is the union of contours 1..k), so
nothing assumes one reach per direction.

Wire format (encode): uint16 little-endian, base64, tenths of a minute; 0xFFFF no data,
0xFFFE beyond, bit 15 set on a node whose time was filled in from its neighbours (water,
parkland: drawn, but not a place anyone was routed to).
"""
from __future__ import annotations

import base64
import math

import numpy as np
import shapely
from shapely.geometry import shape

from server.localgrid import (  # noqa: F401 - re-exported for callers and tests
    EARTH_CIRCUMFERENCE_M,
    MAX_LAT,
    MAX_SIDE,
    Grid,
    LocalFrame,
    mercator,
    metres_per_unit,
    unmercator,
)

WALK_M_PER_MIN = 4000 / 60          # 4 km/h: R5's walking speed in the London tables
WIRE_NO_DATA = 0xFFFF
WIRE_BEYOND = 0xFFFE
WIRE_FILLED = 0x8000
WIRE_MAX = 0x7FFD                   # 3,276.5 minutes


# ── Contours → minutes ───────────────────────────────────────────────
def contour_regions(features: list[dict], frame: LocalFrame, to_minutes) -> list[tuple[float, object]]:
    """[(minutes, nested region in local metres)], quickest first. Every feature with the same
    contour value is one region (all its pieces and holes); region k is the union of 1..k."""
    by_value: dict[float, list] = {}
    for f in features:
        geom = shapely.make_valid(shape(f["geometry"]))
        if geom.is_empty:
            continue
        by_value.setdefault(float(f["properties"]["contour"]), []).append(geom)
    out, acc = [], None

    def project(coords: np.ndarray) -> np.ndarray:
        x, y = frame.to_local(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    for value in sorted(by_value):
        region = shapely.transform(shapely.union_all(by_value[value]), project)
        # Keep only the areas: make_valid can leave stray lines and points.
        region = shapely.union_all([g for g in shapely.get_parts(region)
                                    if g.geom_type in ("Polygon", "MultiPolygon")])
        acc = region if acc is None else shapely.union(acc, region)
        out.append((float(to_minutes(value)), acc))
    return out


def _segments(region) -> np.ndarray:
    """Every boundary segment of a (multi)polygon, holes included, as LineStrings."""
    segs = []
    for ring in shapely.get_rings(shapely.get_parts(region)):
        c = shapely.get_coordinates(ring)
        if len(c) >= 2:
            segs.append(np.stack([c[:-1], c[1:]], axis=1))
    if not segs:
        return np.zeros(0, dtype=object)
    return shapely.linestrings(np.concatenate(segs))


def _boundary_distance(region, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    segs = _segments(region)
    if len(xs) == 0 or len(segs) == 0:
        return np.full(len(xs), np.inf)
    tree = shapely.STRtree(segs)
    _, dist = tree.query_nearest(shapely.points(xs, ys), return_distance=True, all_matches=False)
    return dist


def contour_field(regions: list[tuple[float, object]], grid: Grid) -> np.ndarray:
    """Minutes at every node (NaN outside the outer contour), from contour_regions()."""
    X, Y = grid.mesh()
    out = np.full(X.shape, np.nan)
    if not regions:
        return out
    flat_x, flat_y = X.ravel(), Y.ravel()
    k_of = np.full(flat_x.shape, -1, dtype=np.int64)
    for k in range(len(regions) - 1, -1, -1):          # the smallest holding contour wins
        geom = regions[k][1]
        shapely.prepare(geom)
        k_of[shapely.contains_xy(geom, flat_x, flat_y)] = k
    t = np.full(flat_x.shape, np.nan)
    d_in = np.full(flat_x.shape, np.nan)
    d_out = np.full(flat_x.shape, np.nan)
    first = k_of == 0
    d_in[first] = np.hypot(flat_x[first], flat_y[first])
    for k, (_, geom) in enumerate(regions):
        band, nxt = k_of == k, k_of == k + 1
        sel = band | nxt
        if not sel.any():
            continue
        d = _boundary_distance(geom, flat_x[sel], flat_y[sel])
        idx = np.flatnonzero(sel)
        d_out[idx[band[sel]]] = d[band[sel]]
        d_in[idx[nxt[sel]]] = d[nxt[sel]]
    for k, (minutes, _) in enumerate(regions):
        band = k_of == k
        if not band.any():
            continue
        t_in = 0.0 if k == 0 else regions[k - 1][0]
        total = d_in[band] + d_out[band]
        share = np.where(total > 0, d_in[band] / np.where(total > 0, total, 1), 1.0)
        t[band] = t_in + (minutes - t_in) * share
    out = t.reshape(X.shape)
    oj, oi = grid.origin_index()
    if 0 <= oj < grid.ny and 0 <= oi < grid.nx and np.isfinite(out[oj, oi]):
        out[oj, oi] = 0.0
    return out


def regions_box(regions: list[tuple[float, object]], margin: float) -> tuple[float, float, float, float]:
    if not regions:
        return (-margin, -margin, margin, margin)
    x0, y0, x1, y1 = shapely.bounds(regions[-1][1])
    return (x0 - margin, y0 - margin, x1 + margin, y1 + margin)


# ── Combining, smoothing, cropping ───────────────────────────────────
def merge_near(walk: np.ndarray, comp: np.ndarray, filled: np.ndarray, grid: Grid,
               radius_m: float = 2000.0) -> tuple[np.ndarray, np.ndarray]:
    """Transit: within radius_m of the pin the faster of walking and the composition, beyond
    it the composition alone. A walking time beats no data and beyond."""
    X, Y = grid.mesh()
    near = np.hypot(X, Y) <= radius_m
    take = near & np.isfinite(walk) & ~(comp <= walk)       # comp NaN/inf or slower
    out = comp.copy()
    out[take] = walk[take]
    return out, filled & ~take


def dequantise(minutes: np.ndarray, passes: int = 2, only: np.ndarray | None = None) -> np.ndarray:
    """Remove whole-minute steps: [1 2 1] passes over finite neighbours, each node clamped to
    within half a minute of its own value, so nothing leaves its rounding interval. `only`:
    the nodes to change (the rest, such as walking times, are left as they are but still
    count as neighbours)."""
    ok = np.isfinite(minutes)
    if not ok.any():
        return minutes.copy()
    base = np.where(ok, minutes, 0.0)
    cur = base.copy()
    w = ok.astype(np.float64)
    for _ in range(passes):
        for axis in (0, 1):
            num = 2 * cur * w
            den = 2 * w.copy()
            for shift in (-1, 1):
                num += _shift(cur * w, shift, axis)
                den += _shift(w, shift, axis)
            cur = np.where(den > 0, num / np.where(den > 0, den, 1), cur)
    cur = np.clip(cur, base - 0.5, base + 0.5)
    change = ok if only is None else ok & only
    return np.where(change, cur, minutes)


def _shift(a: np.ndarray, by: int, axis: int) -> np.ndarray:
    out = np.zeros_like(a)
    if axis == 0:
        if by > 0:
            out[by:] = a[:-by]
        else:
            out[:by] = a[-by:]
    else:
        if by > 0:
            out[:, by:] = a[:, :-by]
        else:
            out[:, :by] = a[:, -by:]
    return out


def crop(minutes: np.ndarray, filled: np.ndarray, grid: Grid, margin_m: float = 1000.0):
    """Trim to the box of nodes that hold a time or "beyond", plus a margin."""
    known = ~np.isnan(minutes)
    if not known.any():
        return minutes, filled, grid
    rows, cols = np.flatnonzero(known.any(axis=1)), np.flatnonzero(known.any(axis=0))
    pad = int(math.ceil(margin_m / grid.cell))
    j0, j1 = max(0, rows[0] - pad), min(grid.ny - 1, rows[-1] + pad)
    i0, i1 = max(0, cols[0] - pad), min(grid.nx - 1, cols[-1] + pad)
    oj, oi = grid.origin_index()
    j0, j1, i0, i1 = int(min(j0, oj)), int(max(j1, oj)), int(min(i0, oi)), int(max(i1, oi))
    g = Grid(grid.cell, grid.x0 + i0 * grid.cell, grid.y0 + j0 * grid.cell, i1 - i0 + 1, j1 - j0 + 1)
    return minutes[j0:j1 + 1, i0:i1 + 1], filled[j0:j1 + 1, i0:i1 + 1], g


# ── Wire format ──────────────────────────────────────────────────────
def encode(minutes: np.ndarray, filled: np.ndarray | None = None) -> str:
    finite = np.isfinite(minutes)
    tenths = np.clip(np.round(np.where(finite, minutes, 0) * 10), 0, WIRE_MAX).astype(np.uint16)
    out = np.where(finite, tenths, np.where(np.isnan(minutes), WIRE_NO_DATA, WIRE_BEYOND))
    out = out.astype(np.uint16)
    if filled is not None:
        out = np.where(finite & filled, out | WIRE_FILLED, out).astype(np.uint16)
    return base64.b64encode(out.astype("<u2").tobytes()).decode("ascii")


def decode(b64: str, shape_: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """The inverse of encode(), for tests and scripts: (minutes, filled)."""
    raw = np.frombuffer(base64.b64decode(b64), dtype="<u2").reshape(shape_)
    minutes = np.where(raw == WIRE_NO_DATA, np.nan,
                       np.where(raw == WIRE_BEYOND, np.inf, (raw & 0x7FFF) / 10.0))
    filled = (raw & WIRE_FILLED).astype(bool) & (raw < WIRE_BEYOND)
    return minutes, filled
