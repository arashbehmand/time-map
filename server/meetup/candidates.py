"""
H3-based candidate generation.

We cover the search area with H3 hexagons, giving us a discrete,
roughly-uniform set of candidate meeting locations. This bounds
complexity and scales well regardless of geographic extent.
"""
from __future__ import annotations
import math
from typing import Sequence
import h3

EARTH_R_KM = 6371.0088

# Approximate hex edge lengths in km per H3 resolution
_EDGE_KM = {6: 3.72, 7: 1.41, 8: 0.53, 9: 0.20, 10: 0.076, 11: 0.029}

def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))

def centroid(points: Sequence[tuple[float, float]]) -> tuple[float, float]:
    n = len(points)
    return sum(p[0] for p in points) / n, sum(p[1] for p in points) / n

def infer_radius_km(origins: list[tuple[float, float]], margin_km: float) -> float:
    c = centroid(origins)
    max_d = max(haversine_km(c[0], c[1], lon, lat) for lon, lat in origins)
    return max(2.0, max_d * 1.3 + margin_km)

def choose_resolution(radius_km: float, max_cells: int) -> int:
    for res in [10, 9, 8, 7, 6]:
        edge = _EDGE_KM[res]
        k = max(1, math.ceil(radius_km / edge))
        approx = 3 * k * (k + 1) + 1
        if approx <= max_cells:
            return res
    return 6

def generate_candidates(
    center_lon: float,
    center_lat: float,
    radius_km: float,
    max_cells: int = 160,
    resolution: int | None = None,
    include_points: list[tuple[float, float]] | None = None,
) -> tuple[int, list[tuple[float, float]]]:
    """
    Returns (resolution_used, list_of_(lon, lat)_candidate_centers).
    """
    res = resolution if resolution is not None else choose_resolution(radius_km, max_cells)
    edge = _EDGE_KM[res]
    k = max(2, math.ceil(radius_km / edge))

    center_cell = h3.latlng_to_cell(center_lat, center_lon, res)
    cells = list(h3.grid_disk(center_cell, k))

    # Filter to circular radius
    kept = []
    for cell in cells:
        lat, lon = h3.cell_to_latlng(cell)
        if haversine_km(center_lon, center_lat, lon, lat) <= radius_km * 1.1:
            kept.append(cell)

    # Ensure participant origins are included as candidates
    if include_points:
        for lon, lat in include_points:
            kept.append(h3.latlng_to_cell(lat, lon, res))

    deduped = sorted(set(kept))
    coords = []
    for cell in deduped:
        lat, lon = h3.cell_to_latlng(cell)
        coords.append((lon, lat))

    return res, coords

def refine_cells(
    parent_cells: Sequence[str], child_res: int
) -> list[str]:
    children = set()
    for cell in parent_cells:
        children.update(h3.cell_to_children(cell, child_res))
    return sorted(children)

def cells_to_geojson(cells: Sequence[str]) -> dict:
    """Convert H3 cells to a GeoJSON polygon (unioned boundaries)."""
    from shapely.geometry import Polygon, mapping
    from shapely.ops import unary_union

    polys = []
    for cell in cells:
        boundary = h3.cell_to_boundary(cell)  # list of (lat, lng)
        coords = [(lng, lat) for lat, lng in boundary]
        polys.append(Polygon(coords))

    union = unary_union(polys).buffer(0)
    return {
        "type": "Feature",
        "properties": {},
        "geometry": mapping(union),
    }
