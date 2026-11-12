"""
Pre-flight for a solve: everything a request will cost is worked out and checked here,
before any candidate is generated or any upstream call is made.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import ceil

from server.errors import ApiError, Busy
from server.guard.budget import _next_midnight, daily_exhausted, seconds_until
from server.guard.guard import ClientShareExhausted
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

# The smallest candidate count a plan shrinks to before refusing.
MIN_CANDIDATES = 20


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


@dataclass(frozen=True)
class SolveLimits:
    """Live limits a plan must fit (None means "no such limit", e.g. the Euclidean router)."""
    elements_left_today: int | None = None
    elements_left_client: int | None = None
    free_requests: dict[str, int] | None = None
    now: datetime | None = None


def _candidate_caps(max_candidates: int) -> list[int]:
    caps, c = [], max_candidates
    while c > MIN_CANDIDATES:
        caps.append(c)
        c = int(c * 0.75)
    return caps + [MIN_CANDIDATES]


def _cost(radius: float, n_origins: int, modes: Counter, cap: int):
    """(resolution, coarse_max, fine_max, elements, requests) for a candidate cap, or None."""
    res = choose_resolution(radius, cap)
    if res is None:
        return None
    coarse_max = disk_size(disk_k(radius, res)) + n_origins
    fine_max = cap
    elements = 0
    requests: Counter[str] = Counter()
    for mode, n in modes.items():
        per_request = matrix_max_coords(mode) - n
        if per_request < 1:
            return None
        for stage in (coarse_max, fine_max):
            k = ceil(stage / per_request)
            requests[rate_bucket(mode)] += k
            elements += n * max(stage, 2 * k)  # Mapbox needs >= 2 elements per request
    return res, coarse_max, fine_max, elements, dict(requests)


def plan_solve(req: SolveRequest, settings: Settings, limits: SolveLimits | None = None) -> SolvePlan:
    """
    Check the request and size the search to fit every limit, shrinking the candidate count
    (down to MIN_CANDIDATES) before refusing. Nothing here generates cells or calls Mapbox.
    """
    limits = limits or SolveLimits()
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
    if any(matrix_max_coords(m) - n < 1 for m, n in modes.items()):
        raise SearchTooExpensive("Too many people use the same travel mode for one search.")

    static_elements = settings.max_solve_elements
    if limits.elements_left_today is not None:
        # A plan bigger than a whole day's budget is "too large", not "used up".
        static_elements = min(static_elements, settings.daily("matrix"))

    def over_static(c) -> bool:
        return c[3] > static_elements or any(
            v > settings.rates_per_min.get(b, 0) for b, v in c[4].items())

    def over_live(c) -> str | None:
        if limits.elements_left_today is not None and c[3] > limits.elements_left_today:
            return "today"
        if limits.elements_left_client is not None and c[3] > limits.elements_left_client:
            return "client"
        if limits.free_requests is not None and any(
                v > limits.free_requests.get(b, 0) for b, v in c[4].items()):
            return "rate"
        return None

    smallest = None
    for cap in _candidate_caps(req.search.max_candidates):
        cost = _cost(radius, len(origins), modes, cap)
        if cost is None:
            continue
        smallest = cost
        if not over_static(cost) and over_live(cost) is None:
            res, coarse_max, fine_max, elements, requests = cost
            return SolvePlan(center, radius, res, coarse_max, fine_max, elements, requests)

    if smallest is None or over_static(smallest):
        raise SearchTooExpensive()
    reason = over_live(smallest)
    now = limits.now or datetime.now(UTC)
    if reason == "today":
        raise daily_exhausted(now, "Today's allowance for meeting-point searches is used up. "
                                   "It resets at midnight UTC.")
    if reason == "client":
        raise ClientShareExhausted(
            headers={"Retry-After": str(seconds_until(_next_midnight(now), now))})
    raise Busy("The routing service is busy. Please try again in a minute.",
               headers={"Retry-After": "60"})
