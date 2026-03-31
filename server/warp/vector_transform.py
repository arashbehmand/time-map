"""
Apply warp to vector geometries and extract labels.

Key detail: straight segments become curves under non-linear warp,
so we *densify* (subdivide) before transforming.
"""
from __future__ import annotations

import math
import numpy as np

# ── densification ────────────────────────────────────────────────────

def _densify_coords(coords, max_step: float, closed: bool = False) -> np.ndarray:
    pts = np.asarray(coords, dtype=float)
    if len(pts) < 2:
        return pts
    is_closed = closed and np.allclose(pts[0], pts[-1])
    if is_closed:
        pts = pts[:-1]
    work = np.vstack([pts, pts[:1]]) if (closed or is_closed) else pts
    out = [work[0]]
    for a, b in zip(work[:-1], work[1:]):
        seg = b - a
        d = float(np.linalg.norm(seg))
        n = max(1, int(math.ceil(d / max_step)))
        for i in range(1, n + 1):
            out.append(a + seg * (i / n))
    out = np.asarray(out, dtype=float)
    if closed or is_closed:
        out = out[:-1]
    return out

def _ensure_closed(coords: np.ndarray) -> np.ndarray:
    c = np.asarray(coords, dtype=float)
    if len(c) < 2:
        return c
    if not np.allclose(c[0], c[-1]):
        c = np.vstack([c, c[:1]])
    return c

# ── geometry transform ───────────────────────────────────────────────

def transform_geometry(geometry: dict, warp, densify_px: float = 8.0) -> dict:
    """Recursively warp all coordinates in a GeoJSON geometry dict."""
    gt = geometry["type"]

    if gt == "Point":
        pt = np.asarray(geometry["coordinates"], dtype=float)
        out = warp.transform_points(pt)
        return {"type": "Point", "coordinates": _r(out)}

    if gt == "MultiPoint":
        pts = np.asarray(geometry["coordinates"], dtype=float)
        out = warp.transform_points(pts)
        return {"type": "MultiPoint", "coordinates": _rl(out)}

    if gt == "LineString":
        pts = _densify_coords(geometry["coordinates"], densify_px, closed=False)
        out = warp.transform_points(pts)
        return {"type": "LineString", "coordinates": _rl(out)}

    if gt == "MultiLineString":
        return {
            "type": "MultiLineString",
            "coordinates": [
                _rl(warp.transform_points(
                    _densify_coords(line, densify_px, closed=False)))
                for line in geometry["coordinates"]
            ],
        }

    if gt == "Polygon":
        return {
            "type": "Polygon",
            "coordinates": [
                _rl(_ensure_closed(warp.transform_points(
                    _densify_coords(ring, densify_px, closed=True))))
                for ring in geometry["coordinates"]
            ],
        }

    if gt == "MultiPolygon":
        return {
            "type": "MultiPolygon",
            "coordinates": [
                [
                    _rl(_ensure_closed(warp.transform_points(
                        _densify_coords(ring, densify_px, closed=True))))
                    for ring in poly
                ]
                for poly in geometry["coordinates"]
            ],
        }

    if gt == "GeometryCollection":
        return {
            "type": "GeometryCollection",
            "geometries": [
                transform_geometry(g, warp, densify_px)
                for g in geometry["geometries"]
            ],
        }

    return geometry

def transform_feature(feature: dict, warp, densify_px: float = 8.0) -> dict:
    return {
        "type": "Feature",
        "layer": feature.get("layer"),
        "properties": dict(feature.get("properties", {})),
        "geometry": transform_geometry(feature["geometry"], warp, densify_px),
    }

# ── label extraction ─────────────────────────────────────────────────

MIN_ROAD_LABEL_PX = 40.0

def _pick_text(props: dict) -> str | None:
    for key in ("name", "name_en", "name:en", "ref"):
        v = props.get(key)
        if v:
            return str(v)
    return None

def _line_midpoint_and_tangent(coords: np.ndarray):
    if len(coords) < 2:
        return coords[0] if len(coords) else np.zeros(2), np.array([1.0, 0.0])
    seg = coords[1:] - coords[:-1]
    seg_len = np.linalg.norm(seg, axis=1)
    total = float(seg_len.sum())
    if total < 1e-9:
        return coords[0], np.array([1.0, 0.0])
    cum = np.cumsum(seg_len)
    target = total / 2.0
    si = min(int(np.searchsorted(cum, target)), len(seg) - 1)
    prev = cum[si - 1] if si > 0 else 0.0
    frac = (target - prev) / max(seg_len[si], 1e-9)
    point = coords[si] + frac * seg[si]
    tangent = seg[si] / max(seg_len[si], 1e-9)
    return point, tangent

def extract_labels(layers: dict[str, list[dict]], warp, densify_px: float = 8.0) -> list[dict]:
    """Extract positioned, rotated labels from warped road and label layers."""
    labels: list[dict] = []

    # road labels (from road or road_label layer)
    for layer_name in ("road", "road_label"):
        for feat in layers.get(layer_name, []):
            text = _pick_text(feat.get("properties", {}))
            if not text:
                continue
            geom = feat["geometry"]
            if geom["type"] != "LineString":
                continue
            coords = _densify_coords(geom["coordinates"], densify_px, closed=False)
            if _polyline_length(coords) < MIN_ROAD_LABEL_PX:
                continue
            point, tangent = _line_midpoint_and_tangent(coords)
            wp = warp.transform_points(point)
            angle = warp.tangent_angle_deg(point, tangent)
            labels.append({
                "position": _r(wp),
                "text": text,
                "angle": round(angle, 1),
                "type": "road",
                "class": feat["properties"].get("class", ""),
            })

    # point labels
    for layer_name in ("place_label", "poi_label", "natural_label"):
        for feat in layers.get(layer_name, []):
            text = _pick_text(feat.get("properties", {}))
            if not text:
                continue
            geom = feat["geometry"]
            if geom["type"] != "Point":
                continue
            pt = np.asarray(geom["coordinates"], dtype=float)
            wp = warp.transform_points(pt)
            labels.append({
                "position": _r(wp),
                "text": text,
                "angle": 0.0,
                "type": layer_name.replace("_label", ""),
                "class": feat["properties"].get("class", feat["properties"].get("type", "")),
            })

    return labels

def _polyline_length(coords: np.ndarray) -> float:
    if len(coords) < 2:
        return 0.0
    return float(np.linalg.norm(coords[1:] - coords[:-1], axis=1).sum())

# ── rounding helpers ─────────────────────────────────────────────────

def _r(arr) -> list:
    a = np.asarray(arr, dtype=float)
    return [round(float(v), 1) for v in a]

def _rl(arr) -> list:
    return np.round(np.asarray(arr, dtype=float), 1).tolist()
