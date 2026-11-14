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

Uncertain patches (uncertain_patches, fill_patches): ground the contours leave out of a hole
through at least two consecutive contours, much slower than the ground around it (a station's
platforms and tracks: the interpolation gives them the next contour's time, 50 minutes at
King's Cross where the streets beyond are at 12). On the grid, contour k's region is exactly
{t ≤ t_k}, so a node lies in a hole of contour k when way_out ≤ t_k < t, where way_out is the
highest, over paths to the grid's edge, of the lowest time along the path. A group of such
nodes (4-connected) is a patch when its largest rise above its way out is more than walking
in from its edge explains: depth × WALK_IN_DETOUR ÷ speed + WALK_IN_MARGIN. Enclosed no-data
holes count too (their rise is at least the last contour minus the way out). A patch keeps
its minutes (the provider's estimate, for audit); it gets drawn minutes, a smooth fill from
the ground around it, which only the bent map's geometry uses.

Wire format (encode): uint16 little-endian, base64, tenths of a minute; 0xFFFF no data,
0xFFFE beyond, bit 15 set on a node whose time was filled in from its neighbours (water,
parkland: drawn, but not a place anyone was routed to). Patches (encode_uncertain), only when
there are any: {"nodes_b64": uint32 little-endian node indices (row-major, increasing),
"drawn_b64": uint16 little-endian tenths, one per node}.
"""
from __future__ import annotations

import base64
import heapq
import math
from collections import deque

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

# Uncertain patches (see the module docstring). Walking in from a patch's edge may take a
# detour of up to WALK_IN_DETOUR times the straight distance, plus WALK_IN_MARGIN minutes of
# slack (one contour step of interpolation); a patch rises more than that above its way out.
UNCERTAIN_MIN_CONTOURS = 2
WALK_IN_DETOUR = 2.0
WALK_IN_MARGIN = 3.0


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


# ── Uncertain patches ────────────────────────────────────────────────
def _neighbours(k: int, nx: int, ny: int):
    i, j = k % nx, k // nx
    if i > 0:
        yield k - 1
    if i + 1 < nx:
        yield k + 1
    if j > 0:
        yield k - nx
    if j + 1 < ny:
        yield k + nx


def way_out(minutes: np.ndarray) -> np.ndarray:
    """Per node, the highest over 4-connected paths from it to the grid's edge of the lowest
    time along the path (itself included): the level you must come down to to get out. No
    data and beyond count as +inf, so open no-data ground is a way out and an enclosed hole
    gets its rim's level. The priority flood of static/shared/mesh/layouts.js cutPeaks."""
    ny, nx = minutes.shape
    v = np.where(np.isfinite(minutes), minutes, np.inf).ravel()
    out = np.full(v.shape, -np.inf)
    seen = np.zeros(v.shape, dtype=bool)
    edge = np.unique(np.concatenate([np.arange(nx), (ny - 1) * nx + np.arange(nx),
                                     np.arange(ny) * nx, np.arange(ny) * nx + nx - 1]))
    heap = [(-float(v[k]), int(k)) for k in edge]
    heapq.heapify(heap)
    out[edge] = v[edge]
    seen[edge] = True
    vals = v.tolist()
    while heap:
        c, k = heapq.heappop(heap)
        c = -c
        for q in _neighbours(k, nx, ny):
            if seen[q]:
                continue
            seen[q] = True
            b = vals[q] if vals[q] < c else c
            out[q] = b
            heapq.heappush(heap, (-b, q))
    return out.reshape(minutes.shape)


def _components(mask: np.ndarray) -> list[np.ndarray]:
    """4-connected components of a boolean grid, as arrays of flat node indices."""
    ny, nx = mask.shape
    flat = mask.ravel()
    label = np.zeros(flat.shape, dtype=bool)
    out = []
    for start in np.flatnonzero(flat):
        if label[start]:
            continue
        label[start] = True
        comp, todo = [int(start)], deque([int(start)])
        while todo:
            k = todo.popleft()
            for q in _neighbours(k, nx, ny):
                if flat[q] and not label[q]:
                    label[q] = True
                    comp.append(q)
                    todo.append(q)
        out.append(np.array(comp, dtype=np.int64))
    return out


def _depth_steps(comp: np.ndarray, nx: int, ny: int) -> int:
    """How many steps the component's deepest node is from its edge (1 for a node on it)."""
    members = set(comp.tolist())
    depth: dict[int, int] = {}
    todo: deque[int] = deque()
    for k in members:
        qs = list(_neighbours(k, nx, ny))
        if len(qs) < 4 or any(q not in members for q in qs):
            depth[k] = 1
            todo.append(k)
    while todo:
        k = todo.popleft()
        for q in _neighbours(k, nx, ny):
            if q in members and q not in depth:
                depth[q] = depth[k] + 1
                todo.append(q)
    return max(depth.values()) if depth else 1


def _finite_box(minutes: np.ndarray, pad: int = 1):
    known = np.isfinite(minutes)
    if not known.any():
        return None
    rows, cols = np.flatnonzero(known.any(axis=1)), np.flatnonzero(known.any(axis=0))
    return (max(0, rows[0] - pad), min(minutes.shape[0], rows[-1] + pad + 1),
            max(0, cols[0] - pad), min(minutes.shape[1], cols[-1] + pad + 1))


def uncertain_patches(minutes: np.ndarray, contour_minutes, grid: Grid,
                      speed_m_per_min: float) -> np.ndarray:
    """The nodes of uncertain patches (module docstring), a boolean grid. `contour_minutes`:
    the contour values the field was built from (contour_regions' minutes), `speed_m_per_min`
    the mode's, for the walk-in allowance. The origin is never one."""
    out = np.zeros(minutes.shape, dtype=bool)
    levels = np.sort(np.asarray(list(contour_minutes), dtype=np.float64))
    box = _finite_box(minutes)
    if box is None or levels.size < UNCERTAIN_MIN_CONTOURS:
        return out
    j0, j1, i0, i1 = box
    # Everything outside the box of known nodes is no data that reaches the edge: the same
    # flood on the box alone.
    t = minutes[j0:j1, i0:i1]
    w = way_out(t)
    nodata = np.isnan(t)
    tt = np.where(nodata, np.inf, t)
    enclosing = np.searchsorted(levels, tt, "left") - np.searchsorted(levels, w, "left")
    rise = np.where(nodata, levels[-1] - w, t - w)
    cand = (enclosing >= UNCERTAIN_MIN_CONTOURS) & np.isfinite(w) & (rise > 0) & ~np.isposinf(t)
    oj, oi = grid.origin_index()
    if j0 <= oj < j1 and i0 <= oi < i1:
        cand[oj - j0, oi - i0] = False
    sub = np.zeros(t.shape, dtype=bool)
    ny, nx = t.shape
    flat_rise = rise.ravel()
    for comp in _components(cand):
        allowance = _depth_steps(comp, nx, ny) * grid.cell * WALK_IN_DETOUR / speed_m_per_min + WALK_IN_MARGIN
        if flat_rise[comp].max() > allowance:
            sub.flat[comp] = True
    out[j0:j1, i0:i1] = sub
    return out


def fill_patches(minutes: np.ndarray, mask: np.ndarray, tol: float = 0.01,
                 max_passes: int = 500) -> tuple[np.ndarray, np.ndarray]:
    """Drawn minutes for the patch nodes in `mask`: a smooth fill from the times around each
    patch (means of finite neighbours working inward, then Jacobi passes with the rim held),
    so a patch takes only the room between the ground around it. Returns (drawn, mask):
    drawn is NaN off the mask; a node that never meets a finite neighbour leaves the mask."""
    drawn = np.full(minutes.shape, np.nan)
    mask = mask.copy()
    ny, nx = minutes.shape
    for comp in _components(mask):
        js, is_ = comp // nx, comp % nx
        j0, j1 = max(0, js.min() - 1), min(ny, js.max() + 2)
        i0, i1 = max(0, is_.min() - 1), min(nx, is_.max() + 2)
        m = np.zeros((j1 - j0, i1 - i0), dtype=bool)
        m[js - j0, is_ - i0] = True
        sub = minutes[j0:j1, i0:i1]
        val = np.where(np.isfinite(sub) & ~m, sub, np.nan)

        def sums(a):
            ok = np.isfinite(a)
            v = np.where(ok, a, 0.0)
            s = sum(_shift(v, b, ax) for ax in (0, 1) for b in (-1, 1))
            n = sum(_shift(ok.astype(np.float64), b, ax) for ax in (0, 1) for b in (-1, 1))
            return s, n

        todo = m.copy()
        for _ in range(max_passes):
            if not todo.any():
                break
            s, n = sums(val)
            ready = todo & (n > 0)
            if not ready.any():
                break
            val[ready] = s[ready] / n[ready]
            todo &= ~ready
        m &= ~todo
        for _ in range(max_passes):
            if not m.any():
                break
            s, n = sums(val)
            new = s[m] / n[m]
            change = float(np.max(np.abs(new - val[m])))
            val[m] = new
            if change < tol:
                break
        lost = np.zeros(m.shape, dtype=bool)
        lost[js - j0, is_ - i0] = True
        lost &= ~m
        mask[j0:j1, i0:i1] &= ~lost
        drawn[j0:j1, i0:i1][m] = val[m]
    return drawn, mask


# ── Combining, smoothing, cropping ───────────────────────────────────
def merge_near(walk: np.ndarray, comp: np.ndarray, filled: np.ndarray, grid: Grid,
               radius_m: float = 2000.0, walk_uncertain: np.ndarray | None = None
               ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Transit: within radius_m of the pin the faster of walking and the composition, beyond
    it the composition alone. A walking time beats no data and beyond. A walking time in an
    uncertain patch never beats a time from the tables; where the tables have none it is
    kept, still uncertain. Returns (minutes, filled, uncertain)."""
    X, Y = grid.mesh()
    near = np.hypot(X, Y) <= radius_m
    take = near & np.isfinite(walk) & ~(comp <= walk)       # comp NaN/inf or slower
    unsure = np.zeros(walk.shape, dtype=bool) if walk_uncertain is None else walk_uncertain
    take &= ~unsure | ~np.isfinite(comp)
    out = comp.copy()
    out[take] = walk[take]
    return out, filled & ~take, unsure & take


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


def crop(minutes: np.ndarray, filled: np.ndarray, grid: Grid, margin_m: float = 1000.0, *extra):
    """Trim to the box of nodes that hold a time or "beyond", plus a margin. `extra`: more
    arrays on the same grid (the uncertain mask, drawn minutes), trimmed the same way and
    returned after the grid."""
    known = ~np.isnan(minutes)
    if not known.any():
        return (minutes, filled, grid, *extra)
    rows, cols = np.flatnonzero(known.any(axis=1)), np.flatnonzero(known.any(axis=0))
    pad = int(math.ceil(margin_m / grid.cell))
    j0, j1 = max(0, rows[0] - pad), min(grid.ny - 1, rows[-1] + pad)
    i0, i1 = max(0, cols[0] - pad), min(grid.nx - 1, cols[-1] + pad)
    oj, oi = grid.origin_index()
    j0, j1, i0, i1 = int(min(j0, oj)), int(max(j1, oj)), int(min(i0, oi)), int(max(i1, oi))
    g = Grid(grid.cell, grid.x0 + i0 * grid.cell, grid.y0 + j0 * grid.cell, i1 - i0 + 1, j1 - j0 + 1)
    box = (slice(j0, j1 + 1), slice(i0, i1 + 1))
    return (minutes[box], filled[box], g, *(a[box] for a in extra))


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


def encode_uncertain(mask: np.ndarray, drawn: np.ndarray) -> dict | None:
    """The patches' wire form (module docstring), or None when there are none."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return None
    tenths = np.clip(np.round(drawn.ravel()[idx] * 10), 0, WIRE_MAX).astype("<u2")
    return {"nodes_b64": base64.b64encode(idx.astype("<u4").tobytes()).decode("ascii"),
            "drawn_b64": base64.b64encode(tenths.tobytes()).decode("ascii")}


def decode_uncertain(obj: dict | None, shape_: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """The inverse of encode_uncertain(), for tests and scripts: (mask, drawn minutes, NaN off
    the mask)."""
    mask = np.zeros(shape_, dtype=bool)
    drawn = np.full(shape_, np.nan)
    if obj:
        idx = np.frombuffer(base64.b64decode(obj["nodes_b64"]), dtype="<u4").astype(np.int64)
        tenths = np.frombuffer(base64.b64decode(obj["drawn_b64"]), dtype="<u2")
        if idx.size != tenths.size or (idx.size and (idx.max() >= mask.size or np.any(np.diff(idx) <= 0))):
            raise ValueError("bad uncertain patches")
        mask.flat[idx] = True
        drawn.flat[idx] = tenths / 10.0
    return mask, drawn
