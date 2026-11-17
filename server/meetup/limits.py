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
MODE_WORDS = {"walking": "walking", "cycling": "cycling", "driving": "car",
               "driving-traffic": "car"}

# Coordinates per Matrix request (Mapbox limits).
MATRIX_MAX_COORDS = {"driving-traffic": 10}
MATRIX_DEFAULT_COORDS = 25

# The smallest candidate count a plan shrinks to before refusing.
MIN_CANDIDATES = 20

# The third (finest) stage refines around this many of the best fine cells, their children
# only (7 each), so it has at most FINEST_MAX places. 21 destinations and up to 4 origins fit
# one 25-coordinate request, so for most groups it costs one request per mode (issue #26).
FINEST_SEEDS = 3
FINEST_MAX = 7 * FINEST_SEEDS

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
    # Places in the third stage at two resolutions finer than coarse (0: no third stage).
    finest_max: int = 0


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


def _cost(radius: float, n_origins: int, modes: Counter, cap: int, finest: bool = True):
    """(resolution, coarse_max, fine_max, elements, requests, max_tile, finest_max) for a cap,
    or None. finest=False plans no third stage."""
    res = choose_resolution(radius, cap)
    if res is None:
        return None
    coarse_max = disk_size(disk_k(radius, res)) + n_origins
    fine_max = cap
    finest_max = min(FINEST_MAX, cap) if finest else 0
    elements = 0
    max_tile = 2
    requests: Counter[str] = Counter()
    for mode, n in modes.items():
        for stage in (coarse_max, fine_max, finest_max):
            tiles = matrix_tiles(n, stage, matrix_max_coords(mode))
            requests[rate_bucket(mode)] += len(tiles)
            for o, d in tiles:
                units = tile_elements(len(o), len(d))
                elements += units
                max_tile = max(max_tile, units)
    return res, coarse_max, fine_max, elements, dict(requests), max_tile, finest_max


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
            f"These people are too far apart to meet by {MODE_WORDS[slowest]}: the search "
            f"would cover {radius:.0f} km around the group, and the limit for "
            f"{MODE_WORDS[slowest]} is {limit:.0f} km. Pick a faster mode or move people closer."
        )
    smallest = None
    # The third stage is dropped only as a last resort, at the smallest cap, before refusing.
    tries = [(cap, True) for cap in _candidate_caps(req.search.max_candidates)]
    for cap, finest in tries + [(MIN_CANDIDATES, False)]:
        cost = _cost(radius, len(origins), modes, cap, finest)
        if cost is None:
            continue
        smallest = cost
        if not _over_static(cost[3], cost[4], settings, limits) and \
                _over_live(cost[3], cost[4], limits) is None:
            res, coarse_max, fine_max, elements, requests, max_tile, finest_max = cost
            recovery = Counter()
            for mode in modes:
                recovery[rate_bucket(mode)] += RECOVERY_REQUESTS
            return SolvePlan(center, radius, res, coarse_max, fine_max, elements, requests, max_tile,
                             dict(recovery), 2 * max_tile * len(modes), finest_max)

    if smallest is None:
        raise SearchTooExpensive()
    _refuse(smallest[3], smallest[4], settings, limits)


def _over_static(elements: int, requests: dict[str, int], settings: Settings,
                 limits: SolveLimits) -> bool:
    """Bigger than any solve may be: the per-solve cap, a whole day's budget, a rate."""
    static_elements = settings.max_solve_elements
    if limits.elements_left_today is not None:
        # A plan bigger than a whole day's budget is "too large", not "used up".
        static_elements = min(static_elements, settings.daily("matrix"))
    return elements > static_elements or any(
        v > settings.rates_per_min.get(b, 0) for b, v in requests.items())


def _over_live(elements: int, requests: dict[str, int], limits: SolveLimits) -> str | None:
    """Bigger than what is left right now: today's budget, this client's share, free slots."""
    if limits.elements_left_today is not None and elements > limits.elements_left_today:
        return "today"
    if limits.elements_left_client is not None and elements > limits.elements_left_client:
        return "client"
    if limits.free_requests is not None and any(
            v > limits.free_requests.get(b, 0) for b, v in requests.items()):
        return "rate"
    return None


def _refuse(elements: int, requests: dict[str, int], settings: Settings, limits: SolveLimits):
    """Raise the reason the smallest plan doesn't fit."""
    if _over_static(elements, requests, settings, limits):
        raise SearchTooExpensive()
    reason = _over_live(elements, requests, limits)
    now = limits.now or datetime.now(UTC)
    if reason == "today":
        raise daily_exhausted(now, "Today's allowance for meeting-point searches is used up. "
                                   "It resets at midnight UTC.")
    if reason == "client":
        raise ClientShareExhausted(
            headers={"Retry-After": str(seconds_until(_next_midnight(now), now))})
    raise Busy("The routing service is busy. Please try again in a minute.",
               headers={"Retry-After": "60"})


# ── the station path (server/meetup/stations.py) ────────────────────
# At most this many places are asked about by road (Mapbox's 25 coordinates, one an origin).
STATION_SHORTLIST = 24
# Matrix requests per person on Mapbox, retries and NoRoute recovery included (issue #4).
REQUESTS_PER_ROAD_PERSON = 2


def plan_stations(req: SolveRequest, settings: Settings, limits: SolveLimits | None,
                  n_places: int) -> SolvePlan:
    """
    Size a station solve: K places (coarse_max) asked about by road for everyone not on
    public transport. Each road person is asked about alone: one planned request of one
    origin and K destinations, so K stays within the profile's coordinate limit. The solve
    gives each person their own allowance of REQUESTS_PER_ROAD_PERSON (stations.py), so a
    retry or a NoRoute split has one spare and nobody's requests are spent on anyone else.
    K shrinks until the plan fits every cap; nothing here calls Mapbox.
    An all-transit group costs nothing.
    """
    limits = limits or SolveLimits()
    road = [p.mode for p in req.participants if p.mode != "transit"]
    if not road or n_places < 1:
        return SolvePlan((0.0, 0.0), 0.0, 0, 0, 0, 0)
    start = min(STATION_SHORTLIST, n_places, *(matrix_max_coords(m) - 1 for m in road))
    requests: Counter[str] = Counter(rate_bucket(m) for m in road)
    for k in range(start, 0, -1):
        max_tile = tile_elements(1, k)
        elements = max_tile * len(road)
        if _over_static(elements, requests, settings, limits) or \
                _over_live(elements, requests, limits) is not None:
            continue
        recovery = {b: (REQUESTS_PER_ROAD_PERSON - 1) * n for b, n in requests.items()}
        return SolvePlan((0.0, 0.0), 0.0, 0, k, 0, elements, dict(requests), max_tile,
                         recovery, (REQUESTS_PER_ROAD_PERSON - 1) * max_tile * len(road))
    _refuse(tile_elements(1, 1) * len(road), requests, settings, limits)
    raise SearchTooExpensive()  # unreachable: _refuse always raises
