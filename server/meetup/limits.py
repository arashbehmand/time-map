"""
Pre-flight for a solve: everything a request will cost is worked out and checked here,
before any candidate is generated or any upstream call is made.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from math import ceil

from server.errors import ApiError
from server.meetup.candidates import centroid, choose_resolution, disk_k, disk_size, infer_radius_km
from server.meetup.models import SolveRequest
from server.settings import Settings

# Widest search radius per travel mode (km). A group is held to its slowest mode.
MODE_RADIUS_KM = {"walking": 15.0, "cycling": 40.0, "driving": 150.0, "driving-traffic": 150.0}
_MODE_WORDS = {"walking": "walking", "cycling": "cycling", "driving": "car",
               "driving-traffic": "car"}

# Coordinates per Matrix request (Mapbox limits).
MATRIX_MAX_COORDS = {"driving-traffic": 10}
MATRIX_DEFAULT_COORDS = 25


def matrix_max_coords(mode: str) -> int:
    return MATRIX_MAX_COORDS.get(mode, MATRIX_DEFAULT_COORDS)


def rate_bucket(mode: str) -> str:
    return "matrix:driving-traffic" if mode == "driving-traffic" else "matrix"


@dataclass(frozen=True)
class SolvePlan:
    center: tuple[float, float]
    radius_km: float
    resolution: int
    coarse_max: int
    fine_max: int
    elements: int
    requests: dict[str, int] = field(default_factory=dict)


class GroupTooSpread(ApiError):
    status = 422
    code = "group_too_spread"


class SearchTooExpensive(ApiError):
    status = 422
    code = "search_too_expensive"
    message = "This search is too large. Try fewer people or a smaller search area."


def plan_solve(req: SolveRequest, settings: Settings) -> SolvePlan:
    origins = [(p.lon, p.lat) for p in req.participants]
    center = centroid(origins)
    radius = infer_radius_km(origins, req.search.margin_km)

    modes = Counter(p.mode for p in req.participants)
    slowest = min(modes, key=lambda m: MODE_RADIUS_KM[m])
    limit = MODE_RADIUS_KM[slowest]
    if radius > limit:
        raise GroupTooSpread(
            f"These people are too far apart to meet by {_MODE_WORDS[slowest]}: the search "
            f"would cover {radius:.0f} km around the group, and the limit for "
            f"{_MODE_WORDS[slowest]} is {limit:.0f} km. Pick a faster mode or move people closer."
        )

    res = choose_resolution(radius, req.search.max_candidates)
    if res is None:
        raise SearchTooExpensive()
    coarse_max = disk_size(disk_k(radius, res)) + len(origins)
    fine_max = req.search.max_candidates

    elements = 0
    requests: Counter[str] = Counter()
    for mode, n in modes.items():
        per_request = matrix_max_coords(mode) - n
        if per_request < 1:
            raise SearchTooExpensive(
                "Too many people use the same travel mode for one search."
            )
        for stage in (coarse_max, fine_max):
            k = ceil(stage / per_request)
            requests[rate_bucket(mode)] += k
            elements += n * max(stage, 2 * k)  # Mapbox needs >= 2 elements per request

    if elements > settings.max_solve_elements:
        raise SearchTooExpensive()
    for bucket, count in requests.items():
        if count > settings.rates_per_min.get(bucket, 0):
            raise SearchTooExpensive()

    return SolvePlan(center, radius, res, coarse_max, fine_max, elements, dict(requests))
