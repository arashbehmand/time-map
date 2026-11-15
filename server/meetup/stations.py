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
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

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
from server.meetup.solver import NOTE_BAD_REPLY, NOTE_NO_ROUTE, NOTE_UNCHECKED, explain_infeasible
from server.transit.provider import TransitData, credits, window_label
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
# "Quickest on average" is shown when it is another place this far away, and saves this much.
QUICKEST_MIN_GAIN_MIN = 1.0
# A road trip ends where the map service put the station; the walk from there to the station
# is added (4 km/h). Past this, the station is out for the group: too far from any road.
MAX_STATION_WALK_M = 400.0
# Above this, the answer says that some road trips end a short walk from the station.
NOTE_STATION_WALK_M = 150.0

NOTE_NO_TIME_OF_DAY = "Walking, cycling and driving times don't depend on the time of day."
NOTE_STATION_WALK = ("Some trips by road end a short walk from the station. That walk is "
                     "counted in their times.")


def note_window(label: str) -> str:
    return f"Public transport times are for {label}."


def note_shortlist(k: int) -> str:
    if k == 1:
        return "This is the only station we could check by road, so a better station may exist."
    return f"This is the best of the {k} stations we checked by road, not of every station."


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
    cand: np.ndarray             # indexes into data.places
    lons: np.ndarray             # the candidate places' points
    lats: np.ndarray


def prepare(req: SolveRequest, data: TransitData) -> Group:
    """
    Check the group before anything is planned or sent: everyone on public transport starts
    inside the tables' area and near a street or station they reach, and every road person
    is within their mode's reach of some station. Raises with who is stuck, in request order.
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

    lons = np.array([p.lon for p in data.places])
    lats = np.array([p.lat for p in data.places])
    near = {k: haversine_m(people[k].lon, people[k].lat, lons, lats) / 1000.0
            <= MODE_RADIUS_KM[people[k].mode] for k in road}
    alone = [k for k in road if not near[k].any()]
    if alone:
        # Name whoever has no station within their own mode's reach, with that mode's limit.
        k = min(alone, key=lambda k: (MODE_RADIUS_KM[people[k].mode], index_of[k]))
        mode = people[k].mode
        who = "one person starts" if len(alone) == 1 else f"{len(alone)} people start"
        raise GroupTooSpread(
            f"From where {who}, no station is within the {MODE_RADIUS_KM[mode]:.0f} km limit "
            f"for {MODE_WORDS[mode]}. Pick a faster way for them to travel, or move them closer.",
            extra={"who": [{"index": index_of[k], "reason": "too_far"}
                           for k in sorted(alone, key=lambda k: index_of[k])]})
    keep = np.ones(len(data.places), dtype=bool)
    for k in road:
        keep &= near[k]
    cand = np.flatnonzero(keep)
    if not len(cand):
        raise GroupTooSpread(
            "No station is within reach of everyone: each person can reach some stations, but "
            "none that all of you can. Pick faster ways to travel or move people closer.")
    return Group(people, index_of, transit, road, cand, lons[cand], lats[cand])


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
        rows = [data.place_minutes(data.row(people[k].lon, people[k].lat, window))[g.cand]
                for k in g.transit]
        return np.array(rows).reshape(len(g.transit), n_c)

    times = np.full((len(people), n_c), np.inf)
    times[g.transit] = await asyncio.to_thread(table_rows)
    # Who on public transport is stuck can be told before anyone else is asked about. The
    # tables keep trips up to 3 hours, so "no time" there is not "no route".
    try:
        t_longest, _ = scores(times[g.transit], weights[g.transit], limits[g.transit])
        explain_infeasible(times[g.transit], np.zeros((len(g.transit), n_c), dtype=bool), t_longest,
                           [people[k] for k in g.transit], [index_of[k] for k in g.transit])
    except NoFeasiblePoint as exc:
        if exc.extra.get("reason") != "no_route":
            raise
        raise NoFeasiblePoint("no_transit_time", [{**w, "reason": "no_transit_time"}
                                                  for w in exc.extra["who"]]) from None

    cols = np.arange(n_c)                       # the places scored, as indexes into g.cand
    unresolved = np.zeros((len(people), n_c), dtype=bool)
    skipped = np.zeros((len(people), n_c), dtype=bool)
    snap_m = np.zeros((len(people), n_c))
    proven: bool | None = None
    notes: list[str] = [note_window(window_label(data.windows[window]))]

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
    if g.road:
        clean = np.zeros(n_c, dtype=bool)
        clean[cols[checked]] = True
        proven = bool((b_longest[~clean] >= np.round(off_longest[best], 6) - 1e-9).all())
        notes.append(NOTE_NO_TIME_OF_DAY)
        if int(checked.sum()) < n_c:
            notes.append(note_shortlist(int(checked.sum())))
        if (snap_m[:, shown] > NOTE_STATION_WALK_M).any():
            notes.append(NOTE_STATION_WALK)
    if unresolved.any():
        notes.append(NOTE_UNCHECKED)
    allowance = solve_allowance.get()
    if allowance is not None and allowance.bad_replies:
        notes.append(NOTE_BAD_REPLY)
    if allowance is not None and allowance.no_route_replies:
        notes.append(NOTE_NO_ROUTE)

    def result(j: int, tie: bool = False) -> CandidateResult:
        place = data.places[int(g.cand[cols[j]])]
        return CandidateResult(
            lon=round(place.lon, 7), lat=round(place.lat, 7), name=place.name,
            score=round(float(off_longest[j]), 2),
            max_time_min=round(float(off_longest[j]), 2),
            mean_time_min=round(float(off_mean[j]), 2),
            times={p.id: round(float(times[i, j]), 2) for i, p in enumerate(people)},
            kind="station", place_id=place.pid, tie=tie)

    top = [result(picks[0])] + [result(j, tie=t) for j, t in zip(picks[1:], ties)]
    unchecked = unresolved.any(axis=0)
    diagnostics: dict[str, Any] = {
        "path": "stations",
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
        credits=credits(data.manifest),
    )
