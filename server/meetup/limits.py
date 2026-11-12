"""
Pre-flight for a solve: everything a request will cost is worked out and checked here,
before any candidate is generated or any upstream call is made.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

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

# Extra requests per mode a solve may spend on recovery (a retried bad reply, or splitting a
# NoRoute reply to find the point that fails). Not part of admission: a plan is admitted on
# its own cost, and recovery spends only what is left after every planned request still to
# be sent, in requests, elements and free rate slots. One bad destination in a
# 23-destination request takes at most 2 * ceil(log2 23) = 10 requests to find.
RECOVERY_REQUESTS = 10


def matrix_max_coords(mode: str) -> int:
    return MATRIX_MAX_COORDS.get(mode, MATRIX_DEFAULT_COORDS)


def tile_elements(n_origins: int, n_destinations: int) -> int:
    """Elements one Matrix request is billed for: Mapbox needs at least 2 per request."""
    return max(2, n_origins * n_destinations)


def matrix_tiles(n_origins: int, n_destinations: int, max_coords: int) -> list[tuple[range, range]]:
    """
    Split an origins x destinations matrix into requests of at most max_coords coordinates.
    Origins are batched as well as destinations: the origin batch is the size with the fewest
    requests, then the fewest billed elements, then the larger batch. The cost plan and the
    router both use this, so what is planned is what is sent.
    """
    if n_origins < 1 or n_destinations < 1:
        return []
    best: tuple[tuple[int, int, int], list[tuple[range, range]]] | None = None
    for per_origin in range(1, min(n_origins, max_coords - 1) + 1):
        per_dest = max_coords - per_origin
        tiles = [
            (range(o, min(o + per_origin, n_origins)), range(d, min(d + per_dest, n_destinations)))
            for o in range(0, n_origins, per_origin)
            for d in range(0, n_destinations, per_dest)
        ]
        key = (len(tiles), sum(tile_elements(len(o), len(d)) for o, d in tiles), -per_origin)
        if best is None or key < best[0]:
            best = (key, tiles)
    assert best is not None
    return best[1]


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
    # The most elements any one planned request is billed for.
    max_tile_elements: int = 2
    # Recovery slack on top of the plan (not part of admission): requests per rate bucket,
    # RECOVERY_REQUESTS for each mode in it, and elements for two of the largest requests
    # per mode.
    recovery_requests: dict[str, int] = field(default_factory=dict)
    recovery_elements: int = 0


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
    """(resolution, coarse_max, fine_max, elements, requests, max_tile) for a cap, or None."""
    res = choose_resolution(radius, cap)
    if res is None:
        return None
    coarse_max = disk_size(disk_k(radius, res)) + n_origins
    fine_max = cap
    elements = 0
    max_tile = 2
    requests: Counter[str] = Counter()
    for mode, n in modes.items():
        for stage in (coarse_max, fine_max):
            tiles = matrix_tiles(n, stage, matrix_max_coords(mode))
            requests[rate_bucket(mode)] += len(tiles)
            for o, d in tiles:
                units = tile_elements(len(o), len(d))
                elements += units
                max_tile = max(max_tile, units)
    return res, coarse_max, fine_max, elements, dict(requests), max_tile


def plan_solve(req: SolveRequest, settings: Settings, limits: SolveLimits | None = None) -> SolvePlan:
    """
    Check the request and size the search to fit every limit, shrinking the candidate count
    (down to MIN_CANDIDATES) before refusing. Nothing here generates cells or calls Mapbox.
    """
    limits = limits or SolveLimits()
    # In id order, so the same group in any order gets the same search.
    origins = [(p.lon, p.lat) for p in sorted(req.participants, key=lambda p: p.id)]
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
            res, coarse_max, fine_max, elements, requests, max_tile = cost
            recovery = Counter()
            for mode in modes:
                recovery[rate_bucket(mode)] += RECOVERY_REQUESTS
            return SolvePlan(center, radius, res, coarse_max, fine_max, elements, requests, max_tile,
                             dict(recovery), 2 * max_tile * len(modes))

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
