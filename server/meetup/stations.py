"""
Meet at a station (issue #4, v2): the solver for any group with someone on public transport.

Candidates are the London tables' usable stations, each on its own column and at its own
street point (server/transit/provider.py, TransitData.places), so every time is to the point
shown. Who travels how decides the scoring:

  public transport   minutes from the tables (TransitData.row), for one timetable window;
                     no Mapbox call
  walk, cycle, drive every station is first ranked with the table times plus a straight-line
                     estimate for these people; Mapbox Matrix is asked about only the best K,
                     one request per person with one spare (server/meetup/limits.py
                     plan_stations), and the answer says it is the best of that shortlist.
                     A road trip that ends a short walk from the station has the walk added.

Two answers instead of a weight: Fairest (the smallest longest trip, then the smallest
average) is `best`; Quickest on average is `quickest` when it is another place. Close
alternatives are places within max(3 min, 10%) of the best; every place listed, quickest
included, is at least 500 m from the others and listed once. Those within the tables' error
of the best are marked as ties. No shaded area: the places are discrete.

The same group in any order gets the same answer: people are scored in id order, and ties
are broken by place order.

Venues (#19): asked for a pub, bar, café, restaurant or park, the solve scores every station
and every venue of that kind together (TransitData.pool), a venue's time being the best over
the stations and table cells within a 10-minute walk of it of that point's table time plus the
walk. The answer is venues only, scored and listed as above, unless no venue works for everyone
or the fairest venue's longest trip is more than VENUE_SLACK_MIN above the fairest station's:
then it is the station answer, and a note says why. For road people the shortlist keeps a few
stations next to the venues, so that comparison costs no second request.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from server.errors import NoFeasiblePoint, NoTransitAccess, OutsideTransitArea, UnroutableArea
from server.meetup.limits import (
    MODE_RADIUS_KM,
    MODE_WORDS,
    REQUESTS_PER_ROAD_PERSON,
    GroupTooSpread,
    SolvePlan,
    rate_bucket,
    tile_elements,
)
from server.meetup.models import CandidateResult, Participant, SolveRequest, SolveResponse
from server.meetup.routing import (
    FALLBACK_SPEEDS_KMH,
    MatrixResult,
    Router,
    SolveAllowance,
    gather_or_cancel,
    solve_allowance,
)
from server.meetup.solver import (
    NOTE_BAD_REPLY,
    NOTE_NO_ROUTE,
    NOTE_UNCHECKED,
    QUICKEST_MIN_GAIN_MIN,
    explain_infeasible,
)
from server.transit.provider import TransitData, credits, window_label
from server.transit.venues import PLURAL, VENUE_WALK_MAX_MIN, WORD
from server.warp.field import WALK_M_PER_MIN
from transit.tables import band_of, haversine_m

log = logging.getLogger("time_map.stations")

# A lower bound on road speed (km/h), taken to be above anything the map service returns for
# these modes in a city. Used to drop stations nobody on that mode could reach within their
# own time limit, and to record whether the shortlist could have missed a better station.
LOWER_BOUND_KMH = {"walking": 6.0, "cycling": 30.0, "driving": 120.0, "driving-traffic": 120.0}
# Close alternatives: within max(ALT_MIN_MIN, ALT_SHARE x best) of the best longest trip.
ALT_MIN_MIN = 3.0
ALT_SHARE = 0.10
# Places listed are at least this far apart (metres).
MIN_PLACE_GAP_M = 500.0
# At most this many places, the best and the quickest included.
MAX_PLACES = 5
# "Quickest on average" is shown when it is another place MIN_PLACE_GAP_M away, and saves
# QUICKEST_MIN_GAIN_MIN (solver.py: the street answers use the same gain).
# A road trip ends where the map service put the station; the walk from there to the station
# is added (4 km/h). Past this, the station is out for the group: too far from any road.
MAX_STATION_WALK_M = 400.0
# Above this, the answer says that some road trips end a short walk from the station.
NOTE_STATION_WALK_M = 150.0

# Venues: shown unless the fairest one's longest trip is more than this above the fairest
# station's (minutes). A venue exactly this much slower is still shown.
VENUE_SLACK_MIN = 10.0
# Venues on the road shortlist are at least this far apart (metres), so one street of pubs
# can't fill it; a third of the places on it are stations, so the station the best venue is
# compared with (and the one shown when it falls back) is the best of several. A shortlist is
# never shorter than two places when there are two (plan_stations: Mapbox bills two elements a
# request whatever its size), so it holds a station and a venue whenever both are in reach.
SHORTLIST_GAP_M = 250.0
SHORTLIST_STATION_SHARE = 1 / 3

NOTE_NO_TIME_OF_DAY = "Walking, cycling and driving times don't depend on the time of day."
NOTE_STATION_WALK = ("Some trips by road end a short walk from the station. That walk is "
                     "counted in their times.")
NOTE_PLACE_WALK = ("Some trips by road end a short walk from the place. That walk is "
                   "counted in their times.")


def note_window(label: str) -> str:
    return f"Public transport times are for {label}."


def note_shortlist(k: int, word: str = "station", plural: str = "stations") -> str:
    if k == 1:
        return f"This is the only {word} we could check by road, so a better {word} may exist."
    return f"This is the best of the {k} {plural} we checked by road, not of every {word}."


def gap_text(minutes: float) -> str:
    """A gap over the 10-minute line, never shown as 10: whole minutes when it is whole to a
    tenth, else rounded up to a tenth ('10.1')."""
    tenths = math.ceil(round(minutes, 6) * 10) / 10
    return f"{tenths:.0f}" if tenths == int(tenths) else f"{tenths:.1f}"


def note_venue_fallback(kind: str, reason: str, minutes: float = 0.0, station: str = "") -> str:
    """
    Why a station is shown when a venue was asked for:
      not_loaded     the build has no venues of this kind
      none_in_reach  it has, but none within a 10-minute walk of public transport
      none_work      some are in reach, but none works for everyone: over someone's time
                     limit, beyond a road person's reach, no time from the tables, or too far
                     from a road (and so never on the road shortlist, or dropped from it)
      slower         the fairest venue's longest trip is more than 10 min over the station's
    """
    word, plural = WORD[kind], PLURAL[kind]
    if reason == "slower":
        return (f"The fairest {word} makes the longest trip {gap_text(minutes)} min longer than "
                f"{station or 'the fairest station'}, so this is the station.")
    if reason == "not_loaded":
        return f"These travel times come without {plural}, so this is a station."
    if reason == "none_in_reach":
        return (f"No {word} is within a {VENUE_WALK_MAX_MIN:.0f}-minute walk of public transport, "
                f"so this is a station.")
    return f"No {word} works for all of you, so this is a station."


# ── pure parts ──────────────────────────────────────────────────────
def scores(times: np.ndarray, weights: np.ndarray, limits: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    (longest, mean) trip per place from times (people, places): the longest unweighted, the
    mean weighted. inf where someone has no time or is over their limit (limits: minutes per
    person, inf for none).
    """
    t = np.asarray(times, dtype=np.float64)
    if t.shape[1] == 0:
        return np.zeros(0), np.zeros(0)
    bad = ~np.isfinite(t).all(axis=0) | (t > limits[:, None]).any(axis=0)
    finite = np.where(np.isfinite(t), t, 0.0)
    longest = finite.max(axis=0)
    mean = (weights[:, None] * finite).sum(axis=0) / weights.sum()
    return np.where(bad, np.inf, longest), np.where(bad, np.inf, mean)


def rank(longest: np.ndarray, mean: np.ndarray, key: str) -> np.ndarray:
    """Feasible places best first. Fairest: longest, then mean, then place order. Quickest:
    mean, then longest, then place order. Rounded so float noise can't reorder near-ties."""
    primary, second = (longest, mean) if key == "fairest" else (mean, longest)
    valid = np.flatnonzero(np.isfinite(primary))
    return valid[np.lexsort((valid, np.round(second[valid], 6), np.round(primary[valid], 6)))]


def line_minutes(people: list[Participant], lons: np.ndarray, lats: np.ndarray,
                 speeds: dict[str, float]) -> np.ndarray:
    """Straight-line minutes from each person to each place at their mode's speed."""
    out = np.empty((len(people), len(lons)))
    for i, p in enumerate(people):
        out[i] = haversine_m(p.lon, p.lat, lons, lats) / 1000.0 / speeds[p.mode] * 60.0
    return out


def _turns(a: np.ndarray, b: np.ndarray):
    """The distinct entries of a and b taken in turn (a first), lazily."""
    seen: set[int] = set()
    for n in range(max(len(a), len(b))):
        for x in (a, b):
            if n < len(x) and int(x[n]) not in seen:
                seen.add(int(x[n]))
                yield int(x[n])


def interleave(a: np.ndarray, b: np.ndarray, k: int) -> list[int]:
    """The first k distinct entries of a and b taken in turn (a first)."""
    out: list[int] = []
    seen: set[int] = set()
    for n in range(max(len(a), len(b))):
        for x in (a, b):
            if n < len(x) and int(x[n]) not in seen:
                seen.add(int(x[n]))
                out.append(int(x[n]))
                if len(out) >= k:
                    return out
    return out


def _apart(lons, lats, i: int, picked: list[int], gap_m: float) -> bool:
    if not picked:
        return True
    d = haversine_m(lons[i], lats[i], lons[picked], lats[picked])
    return bool((d >= gap_m).all())


def pick_quickest(fairest: np.ndarray, quick: np.ndarray, mean: np.ndarray, lons: np.ndarray,
                  lats: np.ndarray, gap_m: float = MIN_PLACE_GAP_M) -> int | None:
    """Quickest on average, when it is another place at least gap_m from the fairest and
    saves at least QUICKEST_MIN_GAIN_MIN on the average trip."""
    if not len(quick):
        return None
    best, q = int(fairest[0]), int(quick[0])
    if q == best or not _apart(lons, lats, q, [best], gap_m):
        return None
    return q if mean[q] <= mean[best] - QUICKEST_MIN_GAIN_MIN else None


def pick_places(order: np.ndarray, longest: np.ndarray, lons: np.ndarray, lats: np.ndarray,
                k: int, gap_m: float = MIN_PLACE_GAP_M, keep: tuple[int, ...] = ()) -> list[int]:
    """The best place, then the next best within max(3 min, 10%) of it, each at least gap_m
    from every place already listed. `keep` are places listed elsewhere (the quickest): they
    count toward k, are never repeated, and the gap applies to them too. Never relaxed to
    fill the list."""
    if not len(order):
        return []
    best = float(np.round(longest[order[0]], 6))
    slack = max(ALT_MIN_MIN, ALT_SHARE * best)
    picked = [int(order[0])]
    others = [int(i) for i in keep if int(i) != int(order[0])]
    for i in order[1:]:
        if len(picked) + len(others) >= k:
            break
        if float(np.round(longest[i], 6)) - best > slack + 1e-9:
            break   # sorted by longest trip: nothing later is closer
        if int(i) in others:
            continue
        if _apart(lons, lats, int(i), picked + others, gap_m):
            picked.append(int(i))
    return picked


def pool_shortlist(fair: np.ndarray, quick: np.ndarray, is_venue: np.ndarray, lons: np.ndarray,
                   lats: np.ndarray, k: int, gap_m: float = SHORTLIST_GAP_M) -> list[int]:
    """
    The places to ask about by road when stations and venues are scored together: venues by
    the estimate (fairest and quickest in turn), each at least gap_m from the venues already
    taken, in about two thirds of the k slots; stations, the same way, in the rest. A single
    slot is a station when there is one.
    """
    # Slots for stations only as far as there are stations to fill them (a group can be
    # within reach of a venue and of no station).
    n_st = min(max(1, round(k * SHORTLIST_STATION_SHARE)), int((~is_venue[fair]).sum()))
    venues: list[int] = []
    for i in _turns(fair[is_venue[fair]], quick[is_venue[quick]]):
        if len(venues) >= k - n_st:
            break
        if _apart(lons, lats, i, venues, gap_m):
            venues.append(i)
    return interleave(fair[~is_venue[fair]], quick[~is_venue[quick]], k - len(venues)) + venues


def choose(longest: np.ndarray, is_venue: np.ndarray,
           slack: float = VENUE_SLACK_MIN) -> tuple[np.ndarray, str | None, float, float]:
    """
    (places to answer from, why a station instead, fairest venue's longest trip, fairest
    station's): the venues, unless none works for everyone ("none_work") or the fairest
    one's longest trip is more than `slack` above the fairest station's ("slower").
    """
    r = np.round(np.asarray(longest, dtype=np.float64), 6)
    v = float(r[is_venue].min()) if is_venue.any() else np.inf
    s = float(r[~is_venue].min()) if (~is_venue).any() else np.inf
    if not np.isfinite(v):
        return ~is_venue, "none_work", v, s
    if np.isfinite(s) and v > s + slack + 1e-9:
        return ~is_venue, "slower", v, s
    return is_venue.copy(), None, v, s


def tie_line(minutes: float, lines: dict[str, float]) -> float:
    """The tables' error for a trip this long (minutes)."""
    band = band_of(minutes)
    return float(lines.get(band, 0.0)) if band else 0.0


def tie_flags(longest: np.ndarray, picks: list[int], lines: dict[str, float],
              band_minutes: float | None = None) -> list[bool]:
    """For each listed place after the best: within the tables' error of the best's longest
    trip, so the times can't tell the two apart. The band is that of `band_minutes` (the
    longest public transport trip at the best place: the tables' error is about table
    times), else of the best's longest trip."""
    best = float(np.round(longest[picks[0]], 6))
    line = tie_line(best if band_minutes is None else band_minutes, lines)
    return [float(np.round(longest[j], 6)) - best <= line + 1e-9 for j in picks[1:]]


# ── the group ───────────────────────────────────────────────────────
@dataclass
class Group:
    """A station solve's people (in id order) and the places it may choose from."""
    people: list[Participant]
    index_of: list[int]          # position in the request, for each person
    transit: list[int]           # people on public transport (positions in `people`)
    road: list[int]              # everyone else
    cand: np.ndarray             # indexes into `places`
    lons: np.ndarray             # the candidate places' points
    lats: np.ndarray
    places: list = field(default_factory=list)     # data.places, or the venue pool's
    minutes: Callable[[np.ndarray], np.ndarray] | None = None   # a table row to `places`
    is_venue: np.ndarray | None = None              # per candidate
    venue: str = "station"                          # the kind asked for
    fallback: str | None = None                     # why stations only, when a venue was asked for
    pool: Any = None                                # the venue pool (TransitData.pool), if any


def prepare(req: SolveRequest, data: TransitData) -> Group:
    """
    Check the group before anything is planned or sent: everyone on public transport starts
    inside the tables' area and near a street or station they reach, and every road person
    is within their mode's reach of some station. Raises with who is stuck, in request order.
    Asked for a venue, the places are every station and every venue of that kind within every
    road person's reach; with no such venue, the group stays on stations and says why.
    """
    index_of = sorted(range(len(req.participants)), key=lambda i: req.participants[i].id)
    people = [req.participants[i] for i in index_of]
    transit = [k for k, p in enumerate(people) if p.mode == "transit"]
    road = [k for k, p in enumerate(people) if p.mode != "transit"]

    outside = sorted(index_of[k] for k in transit if not data.covered(people[k].lon, people[k].lat))
    if outside:
        raise OutsideTransitArea(extra={"who": [{"index": i, "reason": "outside_transit_area"}
                                                for i in outside]})
    stuck = sorted(index_of[k] for k in transit
                   if not data.reachable_from(people[k].lon, people[k].lat))
    if stuck:
        raise NoTransitAccess(extra={"who": [{"index": i, "reason": "no_transit_access"}
                                             for i in stuck]})
    if not data.places:
        raise NoFeasiblePoint()

    # The places: every station, then (asked for a venue the build has) every venue of that
    # kind. Road people's reach is checked against all of them, so a venue everyone can reach
    # is never refused for want of a station everyone can reach.
    fallback = None
    pool = None
    if req.venue != "station":
        pool = data.pool(req.venue)
        if pool is None:
            fallback = "not_loaded"
        elif not pool.is_venue.any():
            fallback, pool = "none_in_reach", None
    lons = np.array([p.lon for p in data.places])
    lats = np.array([p.lat for p in data.places])
    is_venue = np.zeros(len(lons), dtype=bool)
    if pool is not None:
        lons, lats = np.r_[lons, pool.venue_lons], np.r_[lats, pool.venue_lats]
        is_venue = pool.is_venue
    what = "station" if pool is None else f"station or {WORD[req.venue]}"
    near = {k: haversine_m(people[k].lon, people[k].lat, lons, lats) / 1000.0
            <= MODE_RADIUS_KM[people[k].mode] for k in road}
    alone = [k for k in road if not near[k].any()]
    if alone:
        # Name whoever has no place within their own mode's reach, with that mode's limit.
        k = min(alone, key=lambda k: (MODE_RADIUS_KM[people[k].mode], index_of[k]))
        mode = people[k].mode
        who = "one person starts" if len(alone) == 1 else f"{len(alone)} people start"
        raise GroupTooSpread(
            f"From where {who}, no {what} is within the {MODE_RADIUS_KM[mode]:.0f} km limit "
            f"for {MODE_WORDS[mode]}. Pick a faster way for them to travel, or move them closer.",
            extra={"who": [{"index": index_of[k], "reason": "too_far"}
                           for k in sorted(alone, key=lambda k: index_of[k])]})
    keep = np.ones(len(lons), dtype=bool)
    for k in road:
        keep &= near[k]
    cand = np.flatnonzero(keep)
    if not len(cand):
        raise GroupTooSpread(
            f"No {what} is within reach of everyone: each person can reach some places, but "
            "none that all of you can. Pick faster ways to travel or move people closer.")
    if pool is None:
        return Group(people, index_of, transit, road, cand, lons[cand], lats[cand], data.places,
                     data.place_minutes, np.zeros(len(cand), dtype=bool), req.venue, fallback)
    if not is_venue[cand].any():
        # Every venue is beyond some road person's reach: the stations answer, and say why.
        return Group(people, index_of, transit, road, cand, lons[cand], lats[cand], data.places,
                     data.place_minutes, np.zeros(len(cand), dtype=bool), req.venue, "none_work")
    return Group(people, index_of, transit, road, cand, lons[cand], lats[cand], pool.places,
                 pool.minutes, is_venue[cand], req.venue, None, pool)


# ── road people, one request each ───────────────────────────────────
@dataclass
class RoadTimes:
    """Mapbox's answers for the road people (rows) at the shortlisted places (columns)."""
    minutes: np.ndarray          # to the snapped point, inf for no route or not answered
    unresolved: np.ndarray       # never answered
    skipped: np.ndarray          # never answered for lack of allowance
    snap_m: np.ndarray           # how far the snapped point is from the station


async def measure_road(router: Router, people: list[Participant],
                       dests: list[tuple[float, float]]) -> RoadTimes:
    """
    Ask about each road person alone (one origin, the shortlist as destinations), at once.
    Each person spends only their own REQUESTS_PER_ROAD_PERSON (retries and NoRoute splitting
    included), on the solve's shared gate; their counters are added to the solve's.
    """
    parent = solve_allowance.get()

    async def one(p: Participant) -> tuple[MatrixResult, SolveAllowance | None]:
        child = parent.for_person(rate_bucket(p.mode), tile_elements(1, len(dests)),
                                  REQUESTS_PER_ROAD_PERSON, len(people)) if parent else None
        token = solve_allowance.set(child)
        try:
            return await router.duration_matrix(p.mode, [(p.lon, p.lat)], dests), child
        finally:
            solve_allowance.reset(token)

    out = await gather_or_cancel(one(p) for p in people)
    for _, child in out:
        if parent is not None and child is not None:
            parent.absorb(child)
    lons = np.array([d[0] for d in dests])
    lats = np.array([d[1] for d in dests])
    return RoadTimes(
        minutes=np.array([r.minutes[0] for r, _ in out]).reshape(len(people), len(dests)),
        unresolved=np.array([r.unresolved[0] for r, _ in out]).reshape(len(people), len(dests)),
        skipped=np.array([r.skipped[0] for r, _ in out]).reshape(len(people), len(dests)),
        snap_m=np.array([haversine_m(lons, lats, np.array([s.lon for s in r.snaps]),
                                     np.array([s.lat for s in r.snaps])) for r, _ in out]
                        ).reshape(len(people), len(dests)))


# ── the solve ───────────────────────────────────────────────────────
async def solve_stations(req: SolveRequest, data: TransitData, router: Router, plan: SolvePlan,
                         window: str, group: Group | None = None,
                         started: float | None = None) -> SolveResponse:
    started = time.perf_counter() if started is None else started
    g = group or prepare(req, data)
    people, index_of = g.people, g.index_of
    n_c = len(g.cand)
    weights = np.array([p.weight for p in people], dtype=np.float64)
    limits = np.array([p.max_minutes if p.max_minutes is not None else np.inf for p in people])

    # ── public transport, from the tables ─────────────────────────────
    def table_rows() -> np.ndarray:
        rows = []
        for k in g.transit:
            p = people[k]
            row = data.row(p.lon, p.lat, window)
            if g.pool is None:
                rows.append(g.minutes(row))
            else:
                # Someone starting near a venue may walk there straight (VenueLinks.walk_from).
                rows.append(g.pool.minutes(row, g.pool.walk_from(p.lon, p.lat,
                                                                 data.access_points(p.lon, p.lat))))
        return np.array(rows).reshape(len(g.transit), len(g.places))

    full = await asyncio.to_thread(table_rows)      # every station, before the radius filter
    times = np.full((len(people), n_c), np.inf)
    times[g.transit] = full[:, g.cand]
    # Who on public transport is stuck can be told before anyone else is asked about. The
    # tables keep trips up to 3 hours, so "no time" there is not "no route"; and it is said
    # only when no station at all has a time, not just none of those in road people's reach.
    no_time = [k for k, row in zip(g.transit, full) if not np.isfinite(row).any()]
    if no_time:
        raise NoFeasiblePoint("no_transit_time", [
            {"index": index_of[k], "reason": "no_transit_time"}
            for k in sorted(no_time, key=lambda k: index_of[k])])
    try:
        t_longest, _ = scores(times[g.transit], weights[g.transit], limits[g.transit])
        explain_infeasible(times[g.transit], np.zeros((len(g.transit), n_c), dtype=bool), t_longest,
                           [people[k] for k in g.transit], [index_of[k] for k in g.transit])
    except NoFeasiblePoint as exc:
        if exc.extra.get("reason") != "no_route":
            raise
        # Times to some stations, but none within the road people's reach: no common place.
        raise NoFeasiblePoint("no_common_place", [
            {"index": w["index"], "reason": "reaches_few", "reach": 0, "of": int((~g.is_venue).sum())}
            for w in exc.extra["who"]]) from None

    cols = np.arange(n_c)                       # the places scored, as indexes into g.cand
    unresolved = np.zeros((len(people), n_c), dtype=bool)
    skipped = np.zeros((len(people), n_c), dtype=bool)
    snap_m = np.zeros((len(people), n_c))
    proven: bool | None = None
    notes: list[str] = [note_window(window_label(data.windows[window]))]
    fallback = g.fallback

    # ── everyone else, by road, on a shortlist ────────────────────────
    if g.road:
        road_people = [people[k] for k in g.road]
        bound = times.copy()
        bound[g.road] = line_minutes(road_people, g.lons, g.lats, LOWER_BOUND_KMH)
        b_longest, _ = scores(bound, weights, limits)
        # The shortlist: table times, plus a straight-line estimate for road people. An
        # estimate never rules a place out, but a station a road person can't reach within
        # their own limit even at LOWER_BOUND_KMH is not worth asking about.
        est = times.copy()
        est_road = line_minutes(road_people, g.lons, g.lats, FALLBACK_SPEEDS_KMH)
        est[g.road] = np.where(bound[g.road] > limits[g.road][:, None], np.inf, est_road)
        est_limits = limits.copy()
        est_limits[g.road] = np.inf
        e_longest, e_mean = scores(est, weights, est_limits)
        if g.is_venue.any():
            cols = np.array(pool_shortlist(rank(e_longest, e_mean, "fairest"),
                                           rank(e_longest, e_mean, "quickest"), g.is_venue,
                                           g.lons, g.lats, plan.coarse_max), dtype=np.int64)
        else:
            cols = np.array(interleave(rank(e_longest, e_mean, "fairest"),
                                       rank(e_longest, e_mean, "quickest"), plan.coarse_max),
                            dtype=np.int64)
        if not len(cols):
            raise NoFeasiblePoint("over_time_limit", [
                {"index": index_of[k], "reason": "over_time_limit"} for k in
                sorted((k for k in g.road if people[k].max_minutes is not None),
                       key=lambda k: index_of[k])])
        road = await measure_road(router, road_people,
                                  [(float(g.lons[j]), float(g.lats[j])) for j in cols])
        times = times[:, cols].copy()
        times[g.road] = np.where(road.unresolved, np.inf, road.minutes + road.snap_m / WALK_M_PER_MIN)
        unresolved = np.zeros((len(people), len(cols)), dtype=bool)
        skipped = np.zeros((len(people), len(cols)), dtype=bool)
        snap_m = np.zeros((len(people), len(cols)))
        unresolved[g.road], skipped[g.road], snap_m[g.road] = road.unresolved, road.skipped, road.snap_m
        # Places someone was never answered for are left out, and said. Only when no place
        # was answered for everyone is the shortlist a failure.
        if not (~unresolved.any(axis=0)).any():
            raise UnroutableArea()

    far = (snap_m > MAX_STATION_WALK_M).any(axis=0)
    longest, mean = scores(times, weights, limits)
    explain_infeasible(times, unresolved, longest, people, index_of)
    off_longest = np.where(far, np.inf, longest)
    off_mean = np.where(far, np.inf, mean)
    if not np.isfinite(off_longest).any():
        raise NoFeasiblePoint("off_street")
    # Venues or the station: the places the answer is made from; the rest are left out.
    venue_cols = g.is_venue[cols]
    best_v = best_s = None
    if venue_cols.any():
        keep, why, best_v, best_s = choose(off_longest, venue_cols)
        fallback = fallback or why
        off_longest = np.where(keep, off_longest, np.inf)
        off_mean = np.where(keep, off_mean, np.inf)
    elif g.is_venue.any() and fallback is None:
        fallback = "none_work"          # no venue on the road shortlist: none estimated to work
    at_venue = venue_cols.any() and fallback is None

    # ── answers ───────────────────────────────────────────────────────
    lons, lats = g.lons[cols], g.lats[cols]
    fairest = rank(off_longest, off_mean, "fairest")
    quick = rank(off_longest, off_mean, "quickest")
    best = int(fairest[0])
    quickest = pick_quickest(fairest, quick, off_mean, lons, lats)
    k = min(req.search.top_k, MAX_PLACES)
    picks = pick_places(fairest, off_longest, lons, lats, k,
                        keep=(quickest,) if quickest is not None else ())
    lines = data.tie_lines()
    t_best = float(times[g.transit, best].max())
    ties = tie_flags(off_longest, picks, lines, band_minutes=t_best)
    shown = picks + ([quickest] if quickest is not None else [])

    checked = ~unresolved.any(axis=0) & ~far
    if fallback is not None:
        station_name = ""
        if fallback == "slower":
            station_name = g.places[int(g.cand[cols[best]])].name
        notes.append(note_venue_fallback(g.venue, fallback,
                                         (best_v or 0.0) - (best_s or 0.0), station_name))
    if g.road:
        same = g.is_venue == at_venue                 # the kind of place answered with
        clean = np.zeros(n_c, dtype=bool)
        clean[cols[checked]] = True
        proven = bool((b_longest[~clean & same] >= np.round(off_longest[best], 6) - 1e-9).all())
        notes.append(NOTE_NO_TIME_OF_DAY)
        n_checked = int((checked & (venue_cols == at_venue)).sum())
        if n_checked < int(same.sum()):
            notes.append(note_shortlist(n_checked, *((WORD[g.venue], PLURAL[g.venue]) if at_venue
                                                     else ("station", "stations"))))
        if (snap_m[:, shown] > NOTE_STATION_WALK_M).any():
            notes.append(NOTE_PLACE_WALK if at_venue else NOTE_STATION_WALK)
    if unresolved.any():
        notes.append(NOTE_UNCHECKED)
    allowance = solve_allowance.get()
    if allowance is not None and allowance.bad_replies:
        notes.append(NOTE_BAD_REPLY)
    if allowance is not None and allowance.no_route_replies:
        notes.append(NOTE_NO_ROUTE)

    def result(j: int, tie: bool = False) -> CandidateResult:
        place = g.places[int(g.cand[cols[j]])]
        return CandidateResult(
            lon=round(place.lon, 7), lat=round(place.lat, 7), name=place.name,
            score=round(float(off_longest[j]), 2),
            max_time_min=round(float(off_longest[j]), 2),
            mean_time_min=round(float(off_mean[j]), 2),
            times={p.id: round(float(times[i, j]), 2) for i, p in enumerate(people)},
            kind=place.kind, place_id=place.pid, tie=tie)

    top = [result(picks[0])] + [result(j, tie=t) for j, t in zip(picks[1:], ties)]
    unchecked = unresolved.any(axis=0)
    diagnostics: dict[str, Any] = {
        "path": "venues" if at_venue else "stations",
        "venue": g.venue,
        "venue_fallback": fallback,
        "venue_pool": int(g.is_venue.sum()),
        "best_venue_max_min": best_v if best_v is None or np.isfinite(best_v) else None,
        "best_station_max_min": best_s if best_s is None or np.isfinite(best_s) else None,
        "build": data.build,
        "window": window,
        "places": int(n_c),
        "shortlist": int(len(cols)) if g.road else None,
        "checked_by_road": int(checked.sum()) if g.road else None,
        "lower_bound_proven": proven,
        "feasible_count": int(np.isfinite(off_longest).sum()),
        "tie_minutes": tie_line(t_best, lines),
        "dropped_off_street": int((far & np.isfinite(longest)).sum()),
        "dropped_no_route": int((unchecked & ~skipped.any(axis=0)).sum()),
        "unchecked_for_budget": int((unchecked & skipped.any(axis=0)).sum()),
        "estimated_matrix_elements": plan.elements,
        "retries": allowance.retries if allowance is not None else 0,
        "bad_replies": allowance.bad_replies if allowance is not None else 0,
        "no_route_replies": allowance.no_route_replies if allowance is not None else 0,
        "matrix_requests": allowance.sent_requests if allowance is not None else 0,
        "matrix_elements": allowance.sent_elements if allowance is not None else 0,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }
    return SolveResponse(
        objective="minimax",
        best=top[0],
        top=top,
        meeting_area_geojson=None,
        diagnostics=diagnostics,
        notes=notes,
        quickest=result(quickest) if quickest is not None else None,
        window=window,
        window_label=window_label(data.windows[window]),
        service_date=data.window_date(window),
        valid_until=data.valid_until.isoformat(),
        credits=credits(data.manifest),
        venue=g.venue,
        venue_fallback=fallback,
    )
