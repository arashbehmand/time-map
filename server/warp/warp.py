"""
Radial isochrone warp engine.

For each angular direction θ from the canvas centre, the engine knows how
far each isochrone boundary reaches (the "radial profile").  The warp
remaps distances so that equal travel-time ⟹ equal radius.

Every operation is NumPy-vectorized.  After a one-time build step the
per-point evaluation is O(1) (bilinear LUT lookup).
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

EPS = 1e-9
log = logging.getLogger("isochrone_warp")

# ── helpers ──────────────────────────────────────────────────────────

def _normalize_ring(coords: np.ndarray) -> np.ndarray:
    """Strip closing duplicate from a polygon ring."""
    c = np.asarray(coords, dtype=float)
    if len(c) >= 2 and np.allclose(c[0], c[-1]):
        c = c[:-1]
    return c

def _densify(coords: np.ndarray, max_step: float, closed: bool = False) -> np.ndarray:
    """
    Subdivide so that no segment exceeds *max_step* pixels.
    Fully vectorised per-segment to avoid Python-per-point overhead.
    """
    c = _normalize_ring(coords) if closed else np.asarray(coords, dtype=float)
    if len(c) < 2:
        return c
    work = np.vstack([c, c[:1]]) if closed else c

    diffs = np.diff(work, axis=0)                              # (M, 2)
    dists = np.linalg.norm(diffs, axis=1)                     # (M,)
    ns = np.maximum(1, np.ceil(dists / max_step).astype(int)) # subdivisions

    pieces = [work[:1]]
    for a, diff, n in zip(work[:-1], diffs, ns):
        t = np.arange(1, n + 1, dtype=float) / n   # (n,)
        pieces.append(a + diff * t[:, None])        # (n, 2)

    result = np.vstack(pieces)
    if closed:
        result = result[:-1]                        # drop closing duplicate
    return result

def _fill_nan_circular(v: np.ndarray) -> np.ndarray:
    """Linearly interpolate NaN gaps in a circular array."""
    n = len(v)
    idx = np.flatnonzero(np.isfinite(v))
    if len(idx) == 0:
        raise ValueError("Radial profile has no valid samples.")
    if len(idx) == n:
        return v
    xp = np.r_[idx - n, idx, idx + n]
    fp = np.r_[v[idx], v[idx], v[idx]]
    return np.interp(np.arange(n), xp, fp)

def _smooth_circular(v: np.ndarray, w: int = 7) -> np.ndarray:
    """Circular moving-average smoothing."""
    if w <= 1:
        return v
    w = w | 1  # ensure odd
    pad = w // 2
    kernel = np.ones(w, dtype=float) / w
    padded = np.r_[v[-pad:], v, v[:pad]]
    return np.convolve(padded, kernel, mode="same")[pad:-pad]

def _build_radial_profile(
    ring_canvas: np.ndarray,
    center: np.ndarray,
    n_angles: int,
    max_step_px: float = 4.0,
    smooth_window: int = 7,
) -> np.ndarray:
    """
    Sample the radial-distance profile of one isochrone ring.

    Densify the ring, project each vertex to an angular bin, and record
    the maximum distance.  Fill gaps and smooth.
    """
    t0 = time.monotonic()

    # Adaptive step: never create more than 4×n_angles points (ample coverage)
    # This prevents explosion when isochrone vertices are far apart in canvas px.
    diffs = np.diff(np.vstack([ring_canvas, ring_canvas[:1]]), axis=0)
    perimeter = float(np.sum(np.linalg.norm(diffs, axis=1)))
    step = max(max_step_px, perimeter / (4 * n_angles))

    log.debug("    densify: %d verts, perimeter=%.0fpx, step=%.1fpx",
              len(ring_canvas), perimeter, step)

    ring = _densify(ring_canvas, step, closed=True)
    log.debug("    densified to %d points  %.0fms", len(ring), (time.monotonic()-t0)*1000)

    delta = ring - center[None, :]
    theta = np.arctan2(delta[:, 1], delta[:, 0])  # y-down canvas
    radius = np.linalg.norm(delta, axis=1)

    bins = np.floor((theta + math.pi) * n_angles / (2.0 * math.pi)).astype(int) % n_angles
    profile = np.full(n_angles, -np.inf, dtype=float)
    # For each bin keep the farthest boundary point (correct for star-shaped)
    np.maximum.at(profile, bins, radius)
    # Bins with no sample stay as -inf; convert to NaN for gap interpolation
    profile[np.isneginf(profile)] = np.nan

    profile = _fill_nan_circular(profile)
    profile = _smooth_circular(profile, smooth_window)
    return profile

def _point_in_ring(lon: float, lat: float, ring: np.ndarray) -> bool:
    """Vectorised ray-casting point-in-polygon test (no Shapely)."""
    xs, ys = ring[:, 0], ring[:, 1]
    xn, yn = np.roll(xs, -1), np.roll(ys, -1)
    cross = ((ys > lat) != (yn > lat)) & (
        lon < (xn - xs) * (lat - ys) / (yn - ys + 1e-15) + xs
    )
    return bool(np.sum(cross) % 2 == 1)


def _pick_center_ring(
    feature: dict, center_lon: float, center_lat: float
) -> np.ndarray:
    """Select the polygon exterior ring containing the user — pure NumPy."""
    geom = feature["geometry"]
    gtype = geom["type"]
    coords = geom["coordinates"]

    if gtype == "Polygon":
        return np.asarray(coords[0], dtype=float)

    if gtype == "MultiPolygon":
        rings = [np.asarray(poly[0], dtype=float) for poly in coords]
        for ring in rings:
            if _point_in_ring(center_lon, center_lat, ring):
                return ring
        # fallback: largest ring by vertex count
        return max(rings, key=len)

    raise ValueError(f"Unsupported isochrone geometry: {gtype}")

def _ray_rect_distance(
    center: np.ndarray,
    rect: tuple[float, float, float, float],
    theta: float,
) -> float:
    """Distance from *center* to *rect* boundary along ray at angle *theta*."""
    cx, cy = center
    xmin, ymin, xmax, ymax = rect
    dx, dy = math.cos(theta), math.sin(theta)
    ts: list[float] = []
    if abs(dx) > EPS:
        for xb in (xmin, xmax):
            t = (xb - cx) / dx
            if t > 0:
                yh = cy + t * dy
                if ymin - EPS <= yh <= ymax + EPS:
                    ts.append(t)
    if abs(dy) > EPS:
        for yb in (ymin, ymax):
            t = (yb - cy) / dy
            if t > 0:
                xh = cx + t * dx
                if xmin - EPS <= xh <= xmax + EPS:
                    ts.append(t)
    if not ts:
        return float(max(rect[2] - rect[0], rect[3] - rect[1]))
    return min(ts)

def _support_radii(
    center: np.ndarray,
    rect: tuple[float, float, float, float],
    n_angles: int,
) -> np.ndarray:
    angles = -math.pi + 2.0 * math.pi * np.arange(n_angles) / n_angles
    return np.array(
        [_ray_rect_distance(center, rect, float(a)) for a in angles], dtype=float
    )

# ── main warp class ──────────────────────────────────────────────────

@dataclass
class RadialIsochroneWarp:
    """
    Polar radial warp engine.

    After construction via ``from_isochrones``, call ``transform_points``
    on (N, 2) canvas coordinates to get warped (N, 2) canvas coordinates.
    """

    center: np.ndarray               # (2,) canvas centre
    contour_minutes: np.ndarray      # (K,)
    source_radii: np.ndarray         # (K, A) — actual boundary distance
    target_radii: np.ndarray         # (K,)   — circle radii
    support_radii: np.ndarray        # (A,)   — identity boundary
    n_angles: int
    support_pad_px: int

    # ── construction ─────────────────────────────────────────────

    @classmethod
    def from_isochrones(
        cls,
        geojson: dict,
        viewport,
        n_angles: int = 2048,
        max_step_px: float = 4.0,
        smooth_window: int = 7,
        target_outer_radius: Optional[float] = None,
        support_pad_px: Optional[int] = None,
    ) -> "RadialIsochroneWarp":
        center = viewport.center_canvas
        items: list[tuple[float, np.ndarray]] = []

        for feat in geojson["features"]:
            minutes = float(feat["properties"]["contour"])
            t_ring = time.monotonic()
            log.info("  profiling %d-min isochrone...", int(minutes))

            ring_ll = _pick_center_ring(feat, viewport.center_lon, viewport.center_lat)
            log.debug("    picked ring: %d lon/lat vertices", len(ring_ll))

            ring_cv = viewport.lonlat_to_canvas_batch(ring_ll)
            radii_from_center = np.linalg.norm(ring_cv - center, axis=1)
            log.debug("    canvas coords: r_min=%.0f r_max=%.0f px",
                      radii_from_center.min(), radii_from_center.max())

            profile = _build_radial_profile(
                ring_cv, center, n_angles, max_step_px, smooth_window,
            )
            log.info("    %d-min profile done  %.0fms", int(minutes), (time.monotonic()-t_ring)*1000)
            items.append((minutes, profile))

        if not items:
            raise ValueError("No valid isochrone features.")

        items.sort(key=lambda x: x[0])
        contour_minutes = np.array([t for t, _ in items], dtype=float)
        source_radii = np.vstack([p for _, p in items])  # (K, A)

        # enforce nesting: each outer contour ≥ inner contour at every angle
        source_radii = np.maximum.accumulate(source_radii, axis=0)

        # determine support pad
        if support_pad_px is None:
            outer_ring = items[-1][1]
            overshoot = max(0.0, float(np.max(outer_ring)) - min(viewport.width, viewport.height) / 2)
            support_pad_px = max(128, int(math.ceil(overshoot + 128)))

        rect = viewport.support_rect(support_pad_px)
        support = _support_radii(center, rect, n_angles)

        # safety: outer isochrone must be inside support
        if np.any(source_radii[-1] >= support - 8.0):
            support_pad_px = int(np.max(source_radii[-1]) + 256)
            rect = viewport.support_rect(support_pad_px)
            support = _support_radii(center, rect, n_angles)

        # target circle radii
        if target_outer_radius is None:
            target_outer_radius = 0.42 * min(viewport.width, viewport.height)
        target_outer_radius = min(float(target_outer_radius), float(np.min(support) - 8.0))
        if target_outer_radius <= 0:
            target_outer_radius = 0.3 * min(viewport.width, viewport.height)

        target_radii = target_outer_radius * contour_minutes / contour_minutes[-1]

        return cls(
            center=center,
            contour_minutes=contour_minutes,
            source_radii=source_radii,
            target_radii=target_radii,
            support_radii=support,
            n_angles=n_angles,
            support_pad_px=support_pad_px,
        )

    # ── angular interpolation helpers ────────────────────────────

    def _angle_pos(self, theta: np.ndarray) -> np.ndarray:
        """Map angle → fractional bin position."""
        return (theta + math.pi) * self.n_angles / (2.0 * math.pi)

    def _interp_1d(self, arr: np.ndarray, theta: np.ndarray) -> np.ndarray:
        """Bilinear interpolation of a (A,) array at given angles."""
        pos = self._angle_pos(theta)
        i0 = np.floor(pos).astype(np.intp) % self.n_angles
        i1 = (i0 + 1) % self.n_angles
        a = pos - np.floor(pos)
        return (1.0 - a) * arr[i0] + a * arr[i1]

    def _interp_2d(self, arr: np.ndarray, theta: np.ndarray) -> np.ndarray:
        """Bilinear interpolation of a (K, A) array → (N, K)."""
        pos = self._angle_pos(theta)
        i0 = np.floor(pos).astype(np.intp) % self.n_angles
        i1 = (i0 + 1) % self.n_angles
        a = pos - np.floor(pos)
        v0 = arr[:, i0].T        # (N, K)
        v1 = arr[:, i1].T
        return (1.0 - a)[:, None] * v0 + a[:, None] * v1

    # ── forward warp ─────────────────────────────────────────────

    def transform_points(self, points: np.ndarray) -> np.ndarray:
        """
        Warp (N, 2) canvas coordinates.

        Returns (N, 2) warped canvas coordinates.  Also accepts a single
        (2,) point.
        """
        single = np.asarray(points).ndim == 1
        pts = np.atleast_2d(np.asarray(points, dtype=float))

        delta = pts - self.center[None, :]
        r = np.linalg.norm(delta, axis=1)
        theta = np.arctan2(delta[:, 1], delta[:, 0])

        src = self._interp_2d(self.source_radii, theta)   # (N, K)
        sup = self._interp_1d(self.support_radii, theta)   # (N,)

        out_r = np.zeros_like(r)

        # region 0: centre → first contour (linear scaling)
        first = src[:, 0]
        m0 = r <= first
        out_r[m0] = self.target_radii[0] * r[m0] / np.maximum(first[m0], EPS)

        # region 1..K-1: between consecutive contour bands
        for i in range(len(self.target_radii) - 1):
            lo, hi = src[:, i], src[:, i + 1]
            mask = (r > lo) & (r <= hi)
            alpha = (r[mask] - lo[mask]) / np.maximum(hi[mask] - lo[mask], EPS)
            out_r[mask] = (
                (1.0 - alpha) * self.target_radii[i]
                + alpha * self.target_radii[i + 1]
            )

        # region K: outer contour → support boundary (blend to identity)
        last = src[:, -1]
        m_out = (r > last) & (r < sup)
        offset = self.target_radii[-1] - last[m_out]
        w = (sup[m_out] - r[m_out]) / np.maximum(sup[m_out] - last[m_out], EPS)
        out_r[m_out] = r[m_out] + w * offset

        # region ∞: beyond support → identity
        m_far = r >= sup
        out_r[m_far] = r[m_far]

        # reconstruct Cartesian
        result = np.tile(self.center[None, :], (len(pts), 1))
        nz = r > EPS
        result[nz] = self.center + (delta[nz] / r[nz, None]) * out_r[nz, None]

        return result[0] if single else result

    # ── inverse warp ─────────────────────────────────────────────

    def inverse_transform_points(self, points: np.ndarray) -> np.ndarray:
        """Inverse of transform_points (analytical, same O(1) cost)."""
        single = np.asarray(points).ndim == 1
        pts = np.atleast_2d(np.asarray(points, dtype=float))

        delta = pts - self.center[None, :]
        rt = np.linalg.norm(delta, axis=1)
        theta = np.arctan2(delta[:, 1], delta[:, 0])

        src = self._interp_2d(self.source_radii, theta)
        sup = self._interp_1d(self.support_radii, theta)

        out_r = np.zeros_like(rt)

        # inverse of region 0
        m0 = rt <= self.target_radii[0]
        out_r[m0] = src[m0, 0] * rt[m0] / max(float(self.target_radii[0]), EPS)

        # inverse of region 1..K-1
        for i in range(len(self.target_radii) - 1):
            lo_t, hi_t = self.target_radii[i], self.target_radii[i + 1]
            mask = (rt > lo_t) & (rt <= hi_t)
            alpha = (rt[mask] - lo_t) / max(float(hi_t - lo_t), EPS)
            out_r[mask] = (1.0 - alpha) * src[mask, i] + alpha * src[mask, i + 1]

        # inverse of outer blend
        last = src[:, -1]
        m_out = (rt > self.target_radii[-1]) & (rt < sup)
        offset = self.target_radii[-1] - last[m_out]
        denom = sup[m_out] - last[m_out]
        a_coeff = 1.0 - offset / np.maximum(denom, EPS)
        b_coeff = offset * sup[m_out] / np.maximum(denom, EPS)
        out_r[m_out] = (rt[m_out] - b_coeff) / np.maximum(a_coeff, EPS)

        m_far = rt >= sup
        out_r[m_far] = rt[m_far]

        result = np.tile(self.center[None, :], (len(pts), 1))
        nz = rt > EPS
        result[nz] = self.center + (delta[nz] / rt[nz, None]) * out_r[nz, None]

        return result[0] if single else result

    # ── label orientation ────────────────────────────────────────

    def tangent_angle_deg(
        self,
        anchor: np.ndarray,
        tangent: np.ndarray,
        eps: float = 4.0,
    ) -> float:
        """
        Compute the warped angle (degrees) for a label at *anchor*
        with original tangent direction *tangent*.
        """
        anchor = np.asarray(anchor, dtype=float)
        tangent = np.asarray(tangent, dtype=float)
        n = np.linalg.norm(tangent)
        if n < EPS:
            return 0.0
        u = tangent / n
        a = self.transform_points(anchor - eps * u)
        b = self.transform_points(anchor + eps * u)
        v = b - a
        ang = math.degrees(math.atan2(v[1], v[0]))
        # keep text upright
        if ang > 90:
            ang -= 180
        elif ang < -90:
            ang += 180
        return ang
