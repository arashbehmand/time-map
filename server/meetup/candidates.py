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

# Average hex edge lengths in km per H3 resolution. Coarse resolutions (3-5) let wide
# driving groups fit under the candidate cap.
_EDGE_KM = {3: 68.98, 4: 26.07, 5: 9.85, 6: 3.72, 7: 1.41, 8: 0.53, 9: 0.20, 10: 0.076}

# Hard bound on cells materialised by one grid_disk call, whatever the inputs.
MAX_DISK_CELLS = 2000

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

def disk_k(radius_km: float, res: int) -> int:
    return max(2, math.ceil(radius_km / _EDGE_KM[res]))


def disk_size(k: int) -> int:
    return 3 * k * (k + 1) + 1


def choose_resolution(radius_km: float, max_cells: int) -> int | None:
    """The finest resolution whose disk fits in max_cells, or None if none does."""
    for res in sorted(_EDGE_KM, reverse=True):
        if disk_size(disk_k(radius_km, res)) <= max_cells:
            return res
    return None


def generate_candidates(
    center_lon: float,
    center_lat: float,
    radius_km: float,
    resolution: int,
    include_points: list[tuple[float, float]] | None = None,
) -> tuple[int, list[tuple[float, float]]]:
    """
    Returns (resolution_used, list_of_(lon, lat)_candidate_centers).
    The caller picks the resolution (see server/meetup/limits.py); the disk size is
    checked here again before any cells are generated.
    """
    res = resolution
    k = disk_k(radius_km, res)
    if disk_size(k) > MAX_DISK_CELLS:
        raise ValueError("candidate disk too large")  # unreachable via the API: plan_solve checks first

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
