"""
What every build reports about a layout: folds, trustworthiness and continuity, the Wembley
Park ratio, the order of a line's stations, and the bins of the Shepard plot.

Folds are measured on a Delaunay triangulation of the real positions (Bowyer–Watson, built
here because the job is numpy only, and validated before use). Boundary triangles that are
slivers or span an outer gap are peeled off first: they are not land, and a single one would
otherwise decide whether a layout "folds".
"""
from __future__ import annotations

import math

import numpy as np

from transit.layout.smacof import distances

PEEL_MIN_DEG = 10.0
PEEL_EDGE_FACTOR = 4.0
TC_K = 10
TC_SEEDS = 5
TC_JITTER_MIN = 1e-3


# ── triangulation ───────────────────────────────────────────────────
def signed_areas(P: np.ndarray, tri: np.ndarray) -> np.ndarray:
    a, b, c = P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]]
    return 0.5 * ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0]))


def _circumcircles(P: np.ndarray, tri: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a, b, c = P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]]
    d = 2 * (a[:, 0] * (b[:, 1] - c[:, 1]) + b[:, 0] * (c[:, 1] - a[:, 1]) + c[:, 0] * (a[:, 1] - b[:, 1]))
    a2, b2, c2 = (a ** 2).sum(1), (b ** 2).sum(1), (c ** 2).sum(1)
    ux = (a2 * (b[:, 1] - c[:, 1]) + b2 * (c[:, 1] - a[:, 1]) + c2 * (a[:, 1] - b[:, 1])) / d
    uy = (a2 * (c[:, 0] - b[:, 0]) + b2 * (a[:, 0] - c[:, 0]) + c2 * (b[:, 0] - a[:, 0])) / d
    centre = np.stack([ux, uy], axis=1)
    return centre, ((a - centre) ** 2).sum(1)


def convex_hull_area(P: np.ndarray) -> float:
    """Area of the convex hull (Andrew's monotone chain)."""
    pts = sorted(map(tuple, np.asarray(P, dtype=np.float64)))

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = np.array(lower[:-1] + upper[:-1])
    x, y = hull[:, 0], hull[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def delaunay(points: np.ndarray, seed: int = 0) -> np.ndarray:
    """Delaunay triangles (m, 3) of 2-D points, counter-clockwise, sorted. Raises ValueError
    if the result is not a valid triangulation of the convex hull."""
    P = np.asarray(points, dtype=np.float64)
    n = len(P)
    if n < 3:
        raise ValueError("a triangulation needs at least 3 points")
    lo = P.min(axis=0)
    span = float((P.max(axis=0) - lo).max()) or 1.0
    Q = (P - lo) / span
    big = 1e3
    pts = np.vstack([Q, [[-big, -big], [big, -big], [0.5, big]]])
    tri = np.array([[n, n + 1, n + 2]], dtype=np.int64)
    cc, r2 = _circumcircles(pts, tri)
    key_base = n + 3
    for i in np.random.default_rng(seed).permutation(n):
        p = pts[i]
        bad = ((cc - p) ** 2).sum(1) < r2 * (1 - 1e-12)
        if not bad.any():
            raise ValueError("a point lies on no circumcircle (duplicate point?)")
        edges = np.concatenate([tri[bad][:, [0, 1]], tri[bad][:, [1, 2]], tri[bad][:, [2, 0]]])
        keys = edges[:, 0] * key_base + edges[:, 1]
        rev = edges[:, 1] * key_base + edges[:, 0]
        rim = edges[~np.isin(rev, keys)]
        new = np.column_stack([rim, np.full(len(rim), i)])
        ncc, nr2 = _circumcircles(pts, new)
        tri = np.concatenate([tri[~bad], new])
        cc = np.concatenate([cc[~bad], ncc])
        r2 = np.concatenate([r2[~bad], nr2])
    tri = tri[(tri < n).all(axis=1)]
    # canonical: smallest index first (rotation keeps the orientation), rows sorted
    roll = np.argmin(tri, axis=1)
    tri = np.array([np.roll(t, -k) for t, k in zip(tri, roll)])
    tri = tri[np.lexsort((tri[:, 2], tri[:, 1], tri[:, 0]))]
    validate_triangulation(P, tri)
    return tri


def _edge_counts(tri: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Undirected edges (sorted pairs) and how many triangles hold each."""
    e = np.sort(np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]]), axis=1)
    uniq, counts = np.unique(e, axis=0, return_counts=True)
    return uniq, counts


def validate_triangulation(P: np.ndarray, tri: np.ndarray) -> None:
    area = signed_areas(P, tri)
    if (area <= 0).any():
        raise ValueError("a triangle is not counter-clockwise")
    hull = convex_hull_area(P)
    if not math.isclose(float(area.sum()), hull, rel_tol=1e-9):
        raise ValueError(f"triangles cover {area.sum():.6g}, the hull {hull:.6g}")
    directed = np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]])
    if len(np.unique(directed, axis=0)) != len(directed):
        raise ValueError("a directed edge is in two triangles (overlap)")
    _, counts = _edge_counts(tri)
    if (counts > 2).any():
        raise ValueError("an edge is in more than two triangles")
    if len(np.unique(tri)) != len(P):
        raise ValueError("a point is in no triangle")


def _min_angle_deg(P: np.ndarray, tri: np.ndarray) -> np.ndarray:
    out = np.full(len(tri), 180.0)
    for k in range(3):
        a, b, c = P[tri[:, k]], P[tri[:, (k + 1) % 3]], P[tri[:, (k + 2) % 3]]
        u, v = b - a, c - a
        cosang = (u * v).sum(1) / (np.linalg.norm(u, axis=1) * np.linalg.norm(v, axis=1))
        out = np.minimum(out, np.degrees(np.arccos(np.clip(cosang, -1, 1))))
    return out


def _longest_edge(P: np.ndarray, tri: np.ndarray) -> np.ndarray:
    return np.max([np.linalg.norm(P[tri[:, k]] - P[tri[:, (k + 1) % 3]], axis=1) for k in range(3)],
                  axis=0)


def boundary_edges(tri: np.ndarray) -> np.ndarray:
    uniq, counts = _edge_counts(tri)
    return uniq[counts == 1]


def peel(P: np.ndarray, tri: np.ndarray, min_deg: float = PEEL_MIN_DEG,
         edge_factor: float = PEEL_EDGE_FACTOR) -> tuple[np.ndarray, int]:
    """Remove boundary triangles, again and again, while their smallest angle is under
    `min_deg` or their longest edge is over `edge_factor` × the full mesh's median."""
    limit = edge_factor * float(np.median(_longest_edge(P, tri)))
    bad_shape = (_min_angle_deg(P, tri) < min_deg) | (_longest_edge(P, tri) > limit)
    keep = np.ones(len(tri), dtype=bool)
    while True:
        bnd = boundary_edges(tri[keep])
        bset = {tuple(e) for e in bnd}
        on_bnd = np.array([keep[i] and any(tuple(sorted((t[k], t[(k + 1) % 3]))) in bset for k in range(3))
                           for i, t in enumerate(tri)])
        drop = on_bnd & bad_shape
        if not drop.any():
            break
        keep &= ~drop
    return tri[keep], int((~keep).sum())


def _segments_cross(p1, p2, q1, q2) -> np.ndarray:
    def orient(a, b, c):
        return np.sign((b[..., 0] - a[..., 0]) * (c[..., 1] - a[..., 1])
                       - (b[..., 1] - a[..., 1]) * (c[..., 0] - a[..., 0]))
    o1, o2 = orient(p1, p2, q1), orient(p1, p2, q2)
    o3, o4 = orient(q1, q2, p1), orient(q1, q2, p2)
    return (o1 * o2 < 0) & (o3 * o4 < 0)


def boundary_crossings(tri: np.ndarray, X: np.ndarray) -> int:
    """Crossings between boundary edges that share no vertex: a mesh can overlap itself
    with every triangle still the right way round, and this catches it."""
    e = boundary_edges(tri)
    if len(e) < 2:
        return 0
    i, j = np.triu_indices(len(e), 1)
    share = (e[i, 0] == e[j, 0]) | (e[i, 0] == e[j, 1]) | (e[i, 1] == e[j, 0]) | (e[i, 1] == e[j, 1])
    i, j = i[~share], j[~share]
    return int(_segments_cross(X[e[i, 0]], X[e[i, 1]], X[e[j, 0]], X[e[j, 1]]).sum())


def fold_stats(tri: np.ndarray, A: np.ndarray, X: np.ndarray) -> dict:
    """Triangles that flip (signed area ≤ 0) going from A to X, and their share of A's area."""
    a0 = signed_areas(A, tri)
    folded = signed_areas(X, tri) <= 0
    return {"folded": int(folded.sum()), "triangles": int(len(tri)),
            "area_share": float(a0[folded].sum() / a0.sum()) if len(tri) else 0.0,
            "boundary_crossings": boundary_crossings(tri, X)}


# ── neighbourhoods ──────────────────────────────────────────────────
def _ranks(D: np.ndarray) -> np.ndarray:
    """0-based rank of each column within its row, the row's own point last."""
    D = np.array(D, dtype=np.float64)
    np.fill_diagonal(D, np.inf)
    order = np.argsort(D, axis=1, kind="stable")
    ranks = np.empty_like(order)
    rows = np.arange(len(D))[:, None]
    ranks[rows, order] = np.arange(D.shape[1])[None, :]
    return ranks


class TrustContinuity:
    """Trustworthiness and continuity at k (Venna & Kaski): are each station's k nearest by
    travel time also near it on the map (continuity), and are its k nearest on the map near
    it in time (trustworthiness)? Whole-minute ties in time are broken by seeded jitter, the
    result averaged over seeds; breaking them by real distance would favour the real map."""

    def __init__(self, t: np.ndarray, used: np.ndarray, k: int = TC_K, seeds: int = TC_SEEDS,
                 jitter: float = TC_JITTER_MIN):
        self.k = k
        n = len(t)
        if n <= 3 * k + 1:
            raise ValueError(f"trustworthiness at k={k} needs more than {3 * k + 1} nodes")
        base = np.where(used, t, np.inf)
        self.in_ranks = [_ranks(base + np.random.default_rng(s).uniform(0, jitter, base.shape))
                         for s in range(seeds)]
        self.norm = 2.0 / (n * k * (2 * n - 3 * k - 1))

    def __call__(self, X: np.ndarray) -> dict:
        k = self.k
        out_r = _ranks(distances(X))
        out_nn = out_r < k
        T, C = [], []
        for in_r in self.in_ranks:
            in_nn = in_r < k
            T.append(1 - self.norm * float(((in_r + 1 - k) * (out_nn & ~in_nn)).sum()))
            C.append(1 - self.norm * float(((out_r + 1 - k) * (in_nn & ~out_nn)).sum()))
        return {"T": float(np.mean(T)), "T_range": [float(min(T)), float(max(T))],
                "C": float(np.mean(C)), "C_range": [float(min(C)), float(max(C))]}


# ── the issue's checks ──────────────────────────────────────────────
def ratio_check(pairs, X: np.ndarray, station: str, reference: str, sigma: float) -> dict:
    """r = map distance / travel time. Passes when the station–reference ratio is within σ₁
    (a relative error) of the median ratio over all used pairs."""
    i, j = pairs.index_of(station), pairs.index_of(reference)
    if i is None or j is None or i == j:
        return {"status": "absent", "station": station, "reference": reference}
    E = distances(X)
    iu = np.triu_indices(pairs.n, 1)
    use = pairs.used[iu]
    r = (E[iu] / pairs.t[iu])[use]
    med = float(np.median(r))
    if not pairs.used[i, j]:
        return {"status": "no time both ways", "station": station, "reference": reference}
    r_ij = float(E[i, j] / pairs.t[i, j])
    dev = r_ij / med - 1
    return {"status": "pass" if abs(dev) <= sigma else "fail", "ratio": r_ij, "median_ratio": med,
            "ratio_to_median": r_ij / med, "deviation": dev, "tolerance": sigma,
            "percentile": float((r <= r_ij).mean() * 100), "map_m": float(E[i, j]),
            "minutes": float(pairs.t[i, j]),
            "minutes_directed": [int(pairs.directed[i, j]), int(pairs.directed[j, i])]}


def line_order(pairs, X: np.ndarray, runs: list[dict]) -> list[dict]:
    """For each run of stations out from its first station: every station should be further
    from the start on the map than the one before. Each backtrack is listed with its size and
    the table times from and to the start; ids not in the build are listed as absent."""
    out = []
    for run in runs:
        nodes, absent = [], []
        for sid in run["stations"]:
            i = pairs.index_of(sid)
            if i is None:
                absent.append(sid)
            elif not nodes or nodes[-1] != i:
                nodes.append(i)
        res = {"name": run["name"], "stations": len(nodes), "absent": absent, "backtracks": []}
        if len(nodes) >= 2:
            s0 = nodes[0]
            d = np.hypot(*(X[nodes] - X[s0]).T)
            for k in range(1, len(nodes)):
                if d[k] < d[k - 1]:
                    a, b = nodes[k - 1], nodes[k]
                    res["backtracks"].append({
                        "before": pairs.labels[a], "station": pairs.labels[b],
                        "metres": float(d[k - 1] - d[k]),
                        "start_to_before_min": int(pairs.directed[s0, a]),
                        "before_to_start_min": int(pairs.directed[a, s0]),
                        "start_to_station_min": int(pairs.directed[s0, b]),
                        "station_to_start_min": int(pairs.directed[b, s0])})
        res["in_order"] = not res["backtracks"]
        out.append(res)
    return out


def shepard_bins(pairs, X: np.ndarray, bin_min: float = 2.0, band_min: float = 5.0) -> dict:
    """2-D histogram of map distance (as minutes, ÷ s) against travel time over used pairs,
    with the median map minutes per band of travel time."""
    E = distances(X)
    iu = np.triu_indices(pairs.n, 1)
    use = pairs.used[iu]
    x = pairs.t[iu][use]
    y = E[iu][use] / pairs.s
    top = float(math.ceil(max(x.max(), y.max()) / 30) * 30)
    edges = np.arange(0, top + bin_min, bin_min)
    counts, _, _ = np.histogram2d(x, y, bins=[edges, edges])
    bands = np.arange(0, top + band_min, band_min)
    med = []
    for lo, hi in zip(bands[:-1], bands[1:]):
        sel = (x >= lo) & (x < hi)
        if sel.sum() >= 5:
            med.append([float((lo + hi) / 2), float(np.median(y[sel]))])
    return {"edges": edges.tolist(), "counts": counts.astype(int), "median": med, "top": top,
            "pairs": int(len(x))}
