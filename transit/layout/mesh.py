"""
The mesh of the full London time map (#6, L-02): every station node and 300 H3 cell centres
between them, joined by the Delaunay triangulation of their real positions, and a ring of fixed
guard nodes outside them, joined to the inner triangulation's convex hull by an annulus of
triangles. The triangulation never changes: the springs sit on its edges, the no-collapse terms
on its triangles, and the land is cut along its inner part.

Why the ring: if the outer boundary of a triangle mesh stays where it is and every triangle
keeps a positive signed area, the piecewise-linear map from the real map to a layout is one to
one, so no part of the map can lie over another. The guard nodes are that boundary. They sit
outside London, carry no land and are never drawn. The annulus keeps them out of the inner
triangulation: every triangle inside the stations' and cells' convex hull has only stations and
cells for corners, and the land (clipped to that hull) lies in those triangles only.

Numpy only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from transit.layout.frame import from_local
from transit.layout.measures import boundary_edges, delaunay, signed_areas, validate_triangulation

N_CELLS = 300
CELL_MIN_M = 50.0            # a cell centre this close to a station node adds nothing
RING_N = 40
RING_MARGIN_M = 8000.0       # the guard ring's distance beyond the farthest node

WEIGHT_Q = 65535             # carried vertices store their weights in 1/WEIGHT_Q-ths (land.py, carry.js)

STATION, CELL, RING = 0, 1, 2
KIND_NAMES = {STATION: "station", CELL: "cell", RING: "ring"}


def _min_dist(a: np.ndarray, b: np.ndarray, chunk: int = 2048) -> np.ndarray:
    """For each point of a, the distance to the nearest point of b."""
    out = np.empty(len(a))
    for s in range(0, len(a), chunk):
        d = a[s:s + chunk, None, :] - b[None, :, :]
        out[s:s + chunk] = np.sqrt((d ** 2).sum(-1)).min(axis=1)
    return out


def thin_cells(node_xy: np.ndarray, cell_xy: np.ndarray, n: int = N_CELLS,
               min_m: float = CELL_MIN_M) -> np.ndarray:
    """Up to n cells by farthest-point sampling: each pick is the cell farthest from every
    station node and every cell picked before it. Cells within min_m of a node are never
    picked; ties go to the lowest index. Returns indices into cell_xy, in pick order."""
    cell_xy = np.asarray(cell_xy, dtype=np.float64)
    if not len(cell_xy) or n <= 0:
        return np.zeros(0, dtype=np.int64)
    d = _min_dist(cell_xy, np.asarray(node_xy, dtype=np.float64))
    d[d < min_m] = -np.inf
    picks = []
    for _ in range(n):
        k = int(np.argmax(d))
        if not np.isfinite(d[k]):
            break
        picks.append(k)
        d = np.minimum(d, np.hypot(*(cell_xy - cell_xy[k]).T))
        d[k] = -np.inf
    return np.array(picks, dtype=np.int64)


def guard_ring(xy: np.ndarray, n: int = RING_N, margin_m: float = RING_MARGIN_M) -> np.ndarray:
    """n points on a circle round the mean of xy, margin_m beyond the farthest point. The angles
    are offset by half a step so the ring never lines up with an axis."""
    xy = np.asarray(xy, dtype=np.float64)
    c = xy.mean(axis=0)
    r = float(np.hypot(*(xy - c).T).max()) + margin_m
    a = (np.arange(n) + 0.5) * 2 * math.pi / n
    return c + r * np.stack([np.cos(a), np.sin(a)], axis=1)


@dataclass
class Mesh:
    P: np.ndarray            # (N, 2) real positions, local metres: stations, then cells, then the ring
    kinds: np.ndarray        # (N,) STATION, CELL or RING
    tri: np.ndarray          # (T, 3) positive signed area on P
    edges: np.ndarray        # (E, 2) unique, i < j
    l0: np.ndarray           # (E,) real edge lengths
    a_edge: np.ndarray       # (E,) a third of the real area of the triangles on each edge
    A0: np.ndarray           # (T,) real signed areas

    @property
    def n(self) -> int:
        return len(self.P)

    @property
    def fixed(self) -> np.ndarray:
        return self.kinds == RING

    @property
    def n_station(self) -> int:
        return int((self.kinds == STATION).sum())

    @property
    def n_cell(self) -> int:
        return int((self.kinds == CELL).sum())

    @property
    def n_ring(self) -> int:
        return int((self.kinds == RING).sum())

    @property
    def median_edge(self) -> float:
        return float(np.median(self.l0))


def mesh_edges(tri: np.ndarray, A0: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Unique edges (i < j) and a third of the area of the triangles on each."""
    e = np.sort(np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]]), axis=1)
    edges, inv = np.unique(e, axis=0, return_inverse=True)
    a = np.bincount(inv.ravel(), weights=np.tile(A0 / 3.0, 3), minlength=len(edges))
    return edges, a


def hull_loop(tri: np.ndarray) -> list[int]:
    """The boundary vertices of a triangulation whose triangles are all positive, in order
    (counter-clockwise in the signed-area sense)."""
    directed = np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]]).tolist()
    seen = {(a, b) for a, b in directed}
    nxt = {a: b for a, b in directed if (b, a) not in seen}
    start = min(nxt)
    loop = [start]
    while nxt[loop[-1]] != start:
        loop.append(nxt[loop[-1]])
        if len(loop) > len(nxt):
            raise ValueError("the inner triangulation's boundary is not one loop")
    return loop


def annulus(P: np.ndarray, hull: list[int], ring: list[int], centre: np.ndarray) -> np.ndarray:
    """Triangles between a convex hull loop and a ring round it (both counter-clockwise),
    zipped by angle about the centre: (h[i+1], h[i], r[j]) to step along the hull,
    (r[j], r[j+1], h[i]) to step along the ring."""
    H, R = len(hull), len(ring)
    r0 = math.atan2(*(P[ring[0]] - centre)[::-1])
    cut = r0 - math.pi / R

    def ang(v):
        return (math.atan2(*(P[v] - centre)[::-1]) - cut) % (2 * math.pi)

    k = min(range(H), key=lambda i: ang(hull[i]))
    h = hull[k:] + hull[:k]
    ah = [ang(v) for v in h] + [ang(h[0]) + 2 * math.pi]
    ar = [ang(v) for v in ring] + [ang(ring[0]) + 2 * math.pi]
    out, i, j = [], 0, 0
    while i < H or j < R:
        if i < H and (j == R or ah[i + 1] <= ar[j + 1]):
            out.append((h[(i + 1) % H], h[i], ring[j % R]))
            i += 1
        else:
            out.append((ring[j], ring[(j + 1) % R], h[i % H]))
            j += 1
    return np.array(out, dtype=np.int64)


def build_mesh(station_xy: np.ndarray, cell_xy: np.ndarray, ring_n: int = RING_N,
               ring_margin_m: float = RING_MARGIN_M) -> Mesh:
    """Stations and cells triangulated once (Delaunay), and the guard ring joined to their
    convex hull by an annulus. Raises ValueError unless every node is in a triangle, every
    triangle has a positive area, the triangles tile the ring's polygon without overlap and the
    ring is the boundary. Positions are rounded to whole decimetres, the form every layout ships
    in, so the real map and the fixed ring ship exactly as they are here."""
    station_xy = np.asarray(station_xy, dtype=np.float64).reshape(-1, 2)
    cell_xy = np.asarray(cell_xy, dtype=np.float64).reshape(-1, 2)
    inner = np.vstack([station_xy, cell_xy])
    n_in = len(inner)
    centre = inner.mean(axis=0)
    ring = guard_ring(inner, ring_n, ring_margin_m)
    P = np.round(np.vstack([inner, ring]) * 10) / 10
    kinds = np.concatenate([np.full(len(station_xy), STATION), np.full(len(cell_xy), CELL),
                            np.full(ring_n, RING)]).astype(np.int8)
    inner_tri = delaunay(P[:n_in])          # validated: positive, covers the hull, every point used
    outer = annulus(P, hull_loop(inner_tri), list(range(n_in, n_in + ring_n)), centre)
    tri = np.concatenate([inner_tri, outer])
    validate_triangulation(P, tri)
    A0 = signed_areas(P, tri)
    bnd = boundary_edges(tri)
    if len(bnd) != ring_n or not (kinds[bnd] == RING).all():
        raise ValueError("the guard ring is not the mesh's boundary")
    edges, a = mesh_edges(tri, A0)
    l0 = np.hypot(*(P[edges[:, 0]] - P[edges[:, 1]]).T)
    return Mesh(P, kinds, tri, edges, l0, a, A0)


# ── checks on a layout ──────────────────────────────────────────────
def area_ratios(mesh: Mesh, X: np.ndarray) -> np.ndarray:
    return signed_areas(np.asarray(X, dtype=np.float64), mesh.tri) / mesh.A0


def fold_check(mesh: Mesh, X: np.ndarray, real: np.ndarray | None = None) -> dict:
    """What makes a layout of the mesh one to one: every coordinate finite, every triangle's
    signed area positive, and the fixed ring exactly where it is on the real map (`real`,
    default mesh.P). Checked on X as given: the job passes the shipped decimetres, and their
    Float32 image against the real map's."""
    X = np.asarray(X, dtype=np.float64)
    P = mesh.P if real is None else np.asarray(real, dtype=np.float64)
    finite = bool(np.isfinite(X).all())
    area = signed_areas(X, mesh.tri) if finite else np.full(len(mesh.tri), np.nan)
    flipped = int((~(area > 0)).sum())
    ring = mesh.fixed
    moved = float(np.abs(X[ring] - P[ring]).max()) if finite and ring.any() else 0.0
    return {"flipped": flipped, "triangles": int(len(mesh.tri)), "ring_moved_m": moved, "finite": finite,
            "ok": finite and flipped == 0 and moved == 0.0}


def stretch(mesh: Mesh, X: np.ndarray) -> np.ndarray:
    """Each triangle's anisotropy: the larger singular value of its real-to-layout map over the
    smaller (1 for a rotation or a uniform scale)."""
    P, t = mesh.P, mesh.tri
    E0 = np.stack([P[t[:, 1]] - P[t[:, 0]], P[t[:, 2]] - P[t[:, 0]]], axis=2)
    E1 = np.stack([X[t[:, 1]] - X[t[:, 0]], X[t[:, 2]] - X[t[:, 0]]], axis=2)
    F = E1 @ np.linalg.inv(E0)
    s = np.linalg.svd(F, compute_uv=False)
    return s[:, 0] / np.maximum(s[:, 1], 1e-300)


def _where(mesh: Mesh, t: int, station_labels: list[str]) -> dict:
    """Triangle t's real centroid as lon/lat, and the nearest station to it."""
    c = mesh.P[mesh.tri[t]].mean(axis=0)
    st = mesh.P[mesh.kinds == STATION]
    d = np.hypot(*(st - c).T)
    j = int(np.argmin(d)) if len(st) else -1
    lon, lat = from_local(c[0], c[1])
    return {"triangle": int(t), "nodes": [int(v) for v in mesh.tri[t]],
            "kinds": [KIND_NAMES[int(mesh.kinds[v])] for v in mesh.tri[t]],
            "near": station_labels[j] if j >= 0 else None, "near_m": float(d[j]) if j >= 0 else None,
            "lon": round(float(lon), 5), "lat": round(float(lat), 5)}


def squeezed(mesh: Mesh, X: np.ndarray, station_labels: list[str], k: int = 5,
             mask: np.ndarray | None = None) -> list[dict]:
    """The k triangles squeezed most (smallest area ratio to the real map), among `mask` (default
    all), each with the nearest station to its real centroid and where that centroid is."""
    r = area_ratios(mesh, X)
    idx = np.arange(len(r)) if mask is None else np.where(mask)[0]
    order = idx[np.argsort(r[idx], kind="stable")[:k]]
    return [{"ratio": float(r[i]), **_where(mesh, int(i), station_labels)} for i in order]


def worst(mesh: Mesh, values: np.ndarray, station_labels: list[str], mask: np.ndarray | None = None,
          largest: bool = True) -> dict:
    """The triangle with the largest (or smallest) value among `mask`, and where it is."""
    idx = np.arange(len(values)) if mask is None else np.where(mask)[0]
    t = int(idx[np.argmax(values[idx])] if largest else idx[np.argmin(values[idx])])
    return {"value": float(values[t]), **_where(mesh, t, station_labels)}


def locate(P: np.ndarray, tri: np.ndarray, pts: np.ndarray, eps: float = 1e-9) -> tuple[np.ndarray, np.ndarray]:
    """The triangle holding each point (-1 outside) and its barycentric weights there. A point
    on a shared edge goes to the lowest triangle index. For checks and tests: the job cuts the
    land per triangle, so it never has to look a point up."""
    pts = np.atleast_2d(np.asarray(pts, dtype=np.float64))
    a, b, c = P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]]
    det = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    which = np.full(len(pts), -1, dtype=np.int64)
    w = np.zeros((len(pts), 3))
    for k, p in enumerate(pts):
        w1 = ((p[0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (p[1] - a[:, 1]) * (c[:, 0] - a[:, 0])) / det
        w2 = ((b[:, 0] - a[:, 0]) * (p[1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (p[0] - a[:, 0])) / det
        w0 = 1 - w1 - w2
        inside = (w0 >= -eps) & (w1 >= -eps) & (w2 >= -eps)
        if inside.any():
            i = int(np.argmax(inside))
            which[k] = i
            w[k] = (w0[i], w1[i], w2[i])
    return which, w


def carry_route(mesh: Mesh, nodes: list[int]) -> tuple[np.ndarray, np.ndarray]:
    """The real-map polyline through `nodes` (in order), split where it crosses a mesh edge, as
    land.py stores a vertex: (nodes (n, 3), weights (n, 2) in 1/WEIGHT_Q-ths). A crossing is stored
    by its edge (lower node, higher node, higher node; weight along from the lower one), a station
    by its node. Between two consecutive vertices the polyline lies in one triangle, so placing the
    vertices on a layout draws the polyline's image under the mesh's map exactly."""
    P, E = mesh.P, mesh.edges
    C, W = [], []

    def push(c, w):
        if not C or C[-1] != c or W[-1] != w:
            C.append(c)
            W.append(w)

    p0, e = P[E[:, 0]], P[E[:, 1]] - P[E[:, 0]]
    for a, b in zip(nodes, nodes[1:]):
        push((a, a, a), (0, 0))
        A, d = P[a], P[b] - P[a]
        den = d[0] * e[:, 1] - d[1] * e[:, 0]
        q = p0 - A
        with np.errstate(divide="ignore", invalid="ignore"):
            s_ = (q[:, 0] * e[:, 1] - q[:, 1] * e[:, 0]) / den
            u_ = (q[:, 0] * d[1] - q[:, 1] * d[0]) / den
        eps = 1e-9
        hit = (np.abs(den) > 1e-12) & (s_ > eps) & (s_ < 1 - eps) & (u_ > eps) & (u_ < 1 - eps)
        for k in np.where(hit)[0][np.argsort(s_[hit], kind="stable")]:
            lo, hi = int(E[k, 0]), int(E[k, 1])
            tq = int(round(float(u_[k]) * WEIGHT_Q))
            if tq <= 0:
                push((lo, lo, lo), (0, 0))
            elif tq >= WEIGHT_Q:
                push((hi, hi, hi), (0, 0))
            else:
                push((lo, hi, hi), (tq, 0))
    if nodes:
        push((nodes[-1],) * 3, (0, 0))
    return np.array(C, dtype=np.int64).reshape(-1, 3), np.array(W, dtype=np.int64).reshape(-1, 2)


def blend_min_area(tri: np.ndarray, A: np.ndarray, B: np.ndarray) -> np.ndarray:
    """Each triangle's smallest doubled signed area along the straight blend (1 − s)·A + s·B,
    s in [0, 1], for layouts in whole decimetres (int arrays). Along a blend the area is a quadratic
    c0 + c1·s + c2·s², so its minimum is exact: at an end, or at the vertex −c1 / 2c2 when that lies
    inside. Integer arithmetic throughout (the vertex value, c0 − c1²/4c2, is compared exactly as
    4·c0·c2 − c1² in Python integers), so a blend this says is unfolded is unfolded."""
    A = np.asarray(A, dtype=np.int64)
    B = np.asarray(B, dtype=np.int64)
    u, v = A[tri[:, 1]] - A[tri[:, 0]], A[tri[:, 2]] - A[tri[:, 0]]
    du = (B[tri[:, 1]] - B[tri[:, 0]]) - u
    dv = (B[tri[:, 2]] - B[tri[:, 0]]) - v

    def cross(p, q):
        return p[:, 0] * q[:, 1] - p[:, 1] * q[:, 0]

    c0 = cross(u, v)
    c1 = cross(u, dv) + cross(du, v)
    c2 = cross(du, dv)
    out = np.minimum(c0, c0 + c1 + c2).astype(np.float64)
    inside = (c2 > 0) & (c1 < 0) & (-c1 < 2 * c2)              # the vertex lies in (0, 1)
    for k in np.where(inside)[0]:
        a0, a1, a2 = int(c0[k]), int(c1[k]), int(c2[k])
        num = 4 * a0 * a2 - a1 * a1                           # 4·c2 times the vertex value, exactly
        out[k] = min(out[k], num / (4 * a2))                    # its sign is num's: exact
    return out


def blend_ok(tri: np.ndarray, A: np.ndarray, B: np.ndarray) -> bool:
    """True when every triangle keeps a positive area all the way along the blend from A to B."""
    return bool((blend_min_area(tri, A, B) > 0).all())
