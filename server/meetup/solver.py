"""
Core optimizer.

Mathematical formulation
========================
Given N participants each with travel-time function T_i(x),
we search over candidate locations x to minimize:

  minimax:  F(x) = max_i  T_i(x)                        [fairest]
  sum:      F(x) = Σ  w_i T_i(x) / Σ w_i                [quickest on average]
  hybrid:   F(x) = α·max_i T_i(x) + (1-α)·weighted_mean [API only]

Algorithm
=========
1. Generate H3 hex candidates covering the search area (coarse stage).
2. Ask the router for network travel times from every participant to every candidate.
   The router snaps each candidate to the street network for each travel mode and says
   where; each mode's times are to its own snapped point. Pairs it could not check are
   marked, never guessed.
3. Score the candidates. A candidate is offered as an answer only if every mode's snapped
   point is near it, and near the point shown, so everyone's trip ends close to the star.
   Others can still guide the next stage.
4. Refine: score the children of the best coarse cells at the next finer resolution.
5. Refine again: score the children of the few best fine cells one resolution finer still
   (at most FINEST_MAX places, about one request per mode), so the answer is a cell about
   half a kilometre across or less rather than one about a kilometre and a half (#26).
6. Keep whichever stage has the best answer (refinement never makes it worse), and
   return the top-K distinct places, each at its snapped point with its street name.
7. If the best place has no street name, show the best place in that stage that has one
   instead, when it scores at most NAMED_SLACK_MIN worse, and say so in a note. This is
   the one step that can make the shown answer worse than the best found.

The same group in any order gets the same answer: people are scored in id order, and
ties are broken by a fixed rule.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import h3
import numpy as np

from server.errors import ApiError, NoFeasiblePoint, UnroutableArea
from server.meetup.candidates import (
    cells_to_geojson,
    edge_km,
    generate_candidates,
    haversine_km,
)
from server.meetup.limits import FINEST_SEEDS, SolvePlan
from server.meetup.models import (
    CandidateResult,
    Participant,
    SolveRequest,
    SolveResponse,
)
from server.meetup.routing import Router, Snap, gather_or_cancel, solve_allowance

log = logging.getLogger("time_map.solver")

Coord = tuple[float, float]

# A candidate is offered only if every mode's snapped point is within this of it (metres),
# or within SNAP_EDGE_SHARE of the cell's edge on coarse grids, where hex centres are rarely
# that close to a road.
MAX_SNAP_M = 150.0
SNAP_EDGE_SHARE = 0.10
# When nothing in the search passes that, candidates up to this many times the limit are
# offered, with a note. Beyond it, never.
WAIVED_SNAP_FACTOR = 3.0
# Every mode's snapped point must be this close to the point shown (metres): each mode's
# times are to its own snapped point, so this bounds how far from the star a trip ends.
MAX_SPREAD_M = 150.0
# Above this, the answer says that some trips end a short way from the star (metres).
NOTE_SPREAD_M = 50.0
# Alternatives closer than this to a better one are left out (metres).
MIN_ALT_GAP_M = 150.0
# "Quickest on average" is shown when it saves at least this much on the average trip
# (minutes), for streets here and for stations and venues (stations.py).
QUICKEST_MIN_GAIN_MIN = 1.0
# More than this share of a stage's candidates left unchecked fails that stage.
MAX_UNRESOLVED_SHARE = 0.25
# More than this share of the reachable places too far from a street gets a note.
FEW_STREETS_SHARE = 0.5
# When the best place has no street name, a place that has one is shown instead if it costs
# at most this much more (minutes, in the objective's score), so people know where to go.
NAMED_SLACK_MIN = 3.0
# The shown point and name come from the first mode present: people meet on foot.
MODE_ORDER = ("walking", "cycling", "driving", "driving-traffic")

NOTE_FINE_SKIPPED = "The closer search didn't finish, so this answer is less precise than usual."
NOTE_UNCHECKED = "Some places in the search area couldn't be checked, so a better place may exist."
NOTE_OFF_STREET = ("Few places here are close to a street or path, so the star is on the nearest "
                   "one we found, a little way from the area searched.")
NOTE_FEW_STREETS = ("Many places here are far from a street or path, so only part of the area "
                    "could be offered.")
NOTE_SPREAD = ("Not every trip ends exactly at the star: for some people it ends on a street "
               "up to 150 m away.")
NOTE_SPREAD_QUICKEST = ("Not every trip ends exactly at the Q: for some people it ends on a "
                        "street up to 150 m away.")
NOTE_BAD_REPLY = ("The map service sent a bad reply, so we asked again. This answer uses only "
                  "replies that came back complete.")
NOTE_NO_ROUTE = ("The map service couldn't route part of a request, so we asked again in pieces. "
                 "Places it still couldn't route were left out.")


def _group_by_mode(
    participants: list[Participant],
) -> dict[str, list[tuple[int, Participant]]]:
    groups: dict[str, list[tuple[int, Participant]]] = defaultdict(list)
    for i, p in enumerate(participants):
        groups[p.mode].append((i, p))
    return dict(groups)


def _score(
    times: np.ndarray,          # (n_participants, n_candidates)
    participants: list[Participant],
    objective: str,
    alpha: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Returns (score, weighted_mean, max_time), each shape (n_candidates,).
    """
    weights = np.array([p.weight for p in participants])
    w_mean = (weights[:, None] * times).sum(axis=0) / weights.sum()
    mx = times.max(axis=0)

    if objective == "minimax":
        score = mx
    elif objective == "sum":
        score = w_mean
    else:  # hybrid
        score = alpha * mx + (1.0 - alpha) * w_mean

    # Invalidate unreachable or over-limit candidates
    bad = ~np.isfinite(times).all(axis=0)
    for i, p in enumerate(participants):
        if p.max_minutes is not None:
            bad |= times[i] > p.max_minutes
    score = np.where(bad, np.inf, score)
    w_mean = np.where(bad, np.inf, w_mean)
    mx = np.where(bad, np.inf, mx)

    return score, w_mean, mx


def _metres(a: Coord, s: Snap) -> float:
    return haversine_km(a[0], a[1], s.lon, s.lat) * 1000.0


@dataclass
class Evaluation:
    """One stage: candidates, their times and scores, and where the router put them."""
    res: int
    coords: list[Coord]          # the grid points searched
    times: np.ndarray            # (n_participants, n_candidates), participants in id order
    unresolved: np.ndarray       # same shape: that pair was never answered
    skipped: np.ndarray          # (n_candidates,) some pair skipped for lack of budget
    raw: np.ndarray              # score, ignoring the snap rules
    mean: np.ndarray
    mx: np.ndarray
    shown: list[Snap]            # where each candidate is shown, and the street's name
    shown_m: np.ndarray          # how far the shown point is from the grid point
    spread_m: np.ndarray         # how far the farthest mode's snapped point is from it
    off_street: np.ndarray       # not offered as an answer
    too_far: np.ndarray          # not offered even when the snap limit is waived

    @property
    def unchecked(self) -> np.ndarray:
        """Candidates someone has no answer for: they can't be offered."""
        return self.unresolved.any(axis=0)

    def eligible(self) -> np.ndarray:
        return np.where(self.off_street, np.inf, self.raw)

    def waived(self) -> np.ndarray:
        return np.where(self.too_far, np.inf, self.raw)


async def _evaluate(
    router: Router,
    participants: list[Participant],
    candidates: list[Coord],
    objective: str,
    alpha: float,
    res: int,
) -> Evaluation:
    n_p, n_c = len(participants), len(candidates)
    times = np.full((n_p, n_c), np.inf)
    unresolved = np.zeros((n_p, n_c), dtype=bool)
    skipped = np.zeros(n_c, dtype=bool)
    groups = _group_by_mode(participants)
    modes = sorted(groups, key=MODE_ORDER.index)
    results = await gather_or_cancel(
        router.duration_matrix(mode, [(p.lon, p.lat) for _, p in groups[mode]], candidates)
        for mode in modes
    )

    for mode, result in zip(modes, results):
        for row, (p_idx, _) in enumerate(groups[mode]):
            times[p_idx, :] = result.minutes[row, :]
            unresolved[p_idx, :] = result.unresolved[row, :]
        skipped |= result.skipped.any(axis=0)
    times[unresolved] = np.inf

    shown = results[0].snaps
    shown_m = np.array([_metres(c, s) for c, s in zip(candidates, shown)])
    worst_m = np.zeros(n_c)   # the farthest any mode's snapped point is from the grid point
    spread_m = np.zeros(n_c)  # the farthest any mode's snapped point is from the shown point
    for result in results:
        worst_m = np.maximum(worst_m, [_metres(c, s) for c, s in zip(candidates, result.snaps)])
        spread_m = np.maximum(spread_m, [_metres((a.lon, a.lat), s)
                                         for a, s in zip(shown, result.snaps)])

    raw, mean, mx = _score(times, participants, objective, alpha)
    limit = max(MAX_SNAP_M, SNAP_EDGE_SHARE * edge_km(res) * 1000.0)
    checked = ~unresolved.any(axis=0)
    off_street = ((worst_m > limit) | (spread_m > MAX_SPREAD_M)) & checked
    too_far = ((worst_m > WAIVED_SNAP_FACTOR * limit) | (spread_m > MAX_SPREAD_M)) & checked
    return Evaluation(res, candidates, times, unresolved, skipped, raw, mean, mx, shown, shown_m,
                      spread_m, off_street, too_far)


def _ranked(ev: Evaluation, score: np.ndarray, objective: str) -> np.ndarray:
    """Finite candidates best first: by score, then max (or mean), then candidate order."""
    valid = np.where(np.isfinite(score))[0]
    second = ev.mx if objective == "sum" else ev.mean
    # Rounded so float noise can't reorder near-ties; lexsort's last key is the primary one.
    return valid[np.lexsort((valid, np.round(second[valid], 6), np.round(score[valid], 6)))]


def _make_result(idx: int, ev: Evaluation, score: np.ndarray,
                 participants: list[Participant]) -> CandidateResult:
    snap = ev.shown[idx]
    return CandidateResult(
        lon=round(float(snap.lon), 7),
        lat=round(float(snap.lat), 7),
        name=snap.name,
        snap_m=round(float(ev.shown_m[idx]), 1),
        kind="street" if snap.name else None,
        score=round(float(score[idx]), 2),
        max_time_min=round(float(ev.mx[idx]), 2),
        mean_time_min=round(float(ev.mean[idx]), 2),
        times={
            p.id: round(float(ev.times[i, idx]), 2)
            for i, p in enumerate(participants)
        },
    )


def _fine_candidates(
    ranked_coords: list[tuple[float, float]],
    coarse_res: int,
    fine_res: int,
    ring: int,
    center: tuple[float, float],
    radius: float,
    cap: int,
) -> list[tuple[float, float]]:
    """Children of the best coarse cells and their neighbours, best seeds first, at most `cap`."""
    c_lon, c_lat = center
    out: list[tuple[float, float]] = []
    seen: set[str] = set()
    for lon, lat in ranked_coords:
        seed = h3.latlng_to_cell(lat, lon, coarse_res)
        for cell in sorted(h3.grid_disk(seed, ring)):
            for child in sorted(h3.cell_to_children(cell, fine_res)):
                if child in seen:
                    continue
                seen.add(child)
                clat, clon = h3.cell_to_latlng(child)
                if haversine_km(c_lon, c_lat, clon, clat) <= radius * 1.1:
                    out.append((clon, clat))
                    if len(out) >= cap:
                        return out
    return out


def _unresolved_share(ev: Evaluation) -> float:
    return float(ev.unchecked.mean()) if len(ev.coords) else 0.0


def _check_coarse(ev: Evaluation, participants: list[Participant], index_of: list[int]) -> None:
    """
    Fail with a reason when the coarse stage has nothing to offer. Raises or returns.
    People are named only from replies that answered for them: a pair that was never
    checked says nothing about anyone's starting point. Too much unchecked is refused first,
    so whoever is named had at least three quarters of their places answered.
    """
    if _unresolved_share(ev) > MAX_UNRESOLVED_SHARE:
        raise UnroutableArea()
    explain_infeasible(ev.times, ev.unresolved, ev.raw, participants, index_of)


def explain_infeasible(times: np.ndarray, unresolved: np.ndarray, raw: np.ndarray,
                       participants: list[Participant], index_of: list[int]) -> None:
    """
    Raise NoFeasiblePoint naming who can't reach what, or return when some candidate is
    feasible (`raw` finite). times and unresolved are (people, candidates), people in id
    order; index_of maps them back to their place in the request. A pair never answered
    says nothing about anyone. Shared by the grid and the station solvers.
    """
    answered = ~unresolved
    reach = np.isfinite(times) & answered
    stuck = [k for k in range(len(participants)) if answered[k].any() and not reach[k].any()]
    if stuck:
        raise NoFeasiblePoint("no_route", [{"index": index_of[k], "reason": "no_route"}
                                           for k in sorted(stuck, key=lambda k: index_of[k])])
    if np.isfinite(raw).any():
        return
    common = reach.all(axis=0)
    if not common.any():
        counts, checked = reach.sum(axis=1), answered.sum(axis=1)
        few = [k for k in range(len(participants)) if counts[k] < counts.max()]
        raise NoFeasiblePoint("no_common_place", [
            {"index": index_of[k], "reason": "reaches_few", "reach": int(counts[k]),
             "of": int(checked[k])}
            for k in sorted(few, key=lambda k: index_of[k])])
    limited = [k for k, p in enumerate(participants) if p.max_minutes is not None]
    over = [k for k in limited if not (times[k, common] <= participants[k].max_minutes).any()]
    who = sorted(over or limited, key=lambda k: index_of[k])
    raise NoFeasiblePoint("over_time_limit",
                          [{"index": index_of[k], "reason": "over_time_limit"} for k in who])


def _distinct(ev: Evaluation, ranked: np.ndarray, k: int) -> list[int]:
    """The best k candidates whose shown points are at least MIN_ALT_GAP_M apart."""
    picked: list[int] = []
    for i in ranked:
        s = ev.shown[int(i)]
        if any(haversine_km(s.lon, s.lat, ev.shown[j].lon, ev.shown[j].lat) * 1000.0 < MIN_ALT_GAP_M
               for j in picked):
            continue
        picked.append(int(i))
        if len(picked) >= k:
            break
    return picked


def _named_near_best(ev: Evaluation, scores: np.ndarray, ranked: np.ndarray,
                     limit: float) -> int | None:
    """The best-ranked place in this stage with a street name scoring at most `limit`."""
    for i in ranked:
        if float(scores[i]) > limit + 1e-9:
            return None
        if ev.shown[int(i)].name:
            return int(i)
    return None


def _minutes_words(x: float) -> str:
    return "less than a minute" if x < 1.0 else f"about {round(x)} minute{'s' if round(x) != 1 else ''}"


def note_named(cost: float, objective: str) -> str:
    """Said when the star is on a named street and an unnamed place nearby scores better."""
    what = {"minimax": "a longest trip", "sum": "an average trip"}.get(objective, "a score")
    return ("The star is on a named street so it is easy to find. A place nearby with no "
            f"street name has {what} {_minutes_words(cost)} shorter.")


def _quickest(stages: list[Evaluation], offered, kept: Evaluation,
              best: int) -> tuple[Evaluation, int] | None:
    """Quickest on average among the places offered in either stage, when it is another
    place at least MIN_ALT_GAP_M from the best and saves at least QUICKEST_MIN_GAIN_MIN on
    the average trip. Ties: lower longest trip, then the earlier stage, then candidate order."""
    found: tuple[float, float, int, int] | None = None
    for s_idx, ev in enumerate(stages):
        mean = np.where(np.isfinite(offered(ev)), ev.mean, np.inf)
        valid = np.where(np.isfinite(mean))[0]
        if not len(valid):
            continue
        order = valid[np.lexsort((valid, np.round(ev.mx[valid], 6), np.round(mean[valid], 6)))]
        i = int(order[0])
        key = (round(float(mean[i]), 6), round(float(ev.mx[i]), 6), s_idx, i)
        if found is None or key < found:
            found = key
    if found is None:
        return None
    ev, i = stages[found[2]], found[3]
    a, b = ev.shown[i], kept.shown[best]
    if haversine_km(a.lon, a.lat, b.lon, b.lat) * 1000.0 < MIN_ALT_GAP_M:
        return None
    if float(ev.mean[i]) > float(kept.mean[best]) - QUICKEST_MIN_GAIN_MIN:
        return None
    return ev, i


async def solve(req: SolveRequest, router: Router, plan: SolvePlan) -> SolveResponse:
    started = time.perf_counter()
    # Score people in id order; index_of maps back to their place in the request.
    index_of = sorted(range(len(req.participants)), key=lambda i: req.participants[i].id)
    participants = [req.participants[i] for i in index_of]
    origins = [(p.lon, p.lat) for p in participants]
    radius = plan.radius_km
    notes: list[str] = []

    # ── Phase 1: coarse search ────────────────────────────────────────
    coarse_res, coarse_coords = await asyncio.to_thread(
        generate_candidates, plan.center[0], plan.center[1], radius, plan.resolution, origins,
    )
    coarse = await _evaluate(router, participants, coarse_coords, req.objective, req.alpha,
                             coarse_res)
    _check_coarse(coarse, participants, index_of)

    # ── Phase 2: refine around the best coarse cells ──────────────────
    # Seeds are ranked ignoring the snap rules: a coarse centre in the river still points
    # at its children on the streets around it.
    fine_res = min(coarse_res + 1, 15)
    seeds = _ranked(coarse, coarse.raw, req.objective)[: req.search.refine_top]
    fine_coords = await asyncio.to_thread(
        _fine_candidates, [coarse_coords[int(i)] for i in seeds], coarse_res, fine_res,
        req.search.neighbor_ring, plan.center, radius, plan.fine_max,
    )
    async def optional_stage(name: str, coords: list[Coord], res: int) -> Evaluation | None:
        """A refining stage: budget, rate, in-flight or upstream trouble, or too much left
        unchecked, drops it with a note. The earlier answer is already paid for, so it is
        returned rather than nothing."""
        if not coords:
            return None
        try:
            ev = await _evaluate(router, participants, coords, req.objective, req.alpha, res)
        except ApiError as exc:
            log.warning("%s stage skipped: %s", name, exc.code)
        else:
            if _unresolved_share(ev) <= MAX_UNRESOLVED_SHARE:
                return ev
            log.warning("%s stage skipped: too many places unchecked", name)
        if NOTE_FINE_SKIPPED not in notes:
            notes.append(NOTE_FINE_SKIPPED)
        return None

    fine = await optional_stage("fine", fine_coords, fine_res)

    # ── Phase 3: refine again around the best fine cells ──────────────
    finest_res: int | None = None
    finest_coords: list[Coord] = []
    finest: Evaluation | None = None
    if fine is not None and plan.finest_max and fine_res < 15:
        finest_res = fine_res + 1
        seeds = _ranked(fine, fine.raw, req.objective)[:FINEST_SEEDS]
        finest_coords = await asyncio.to_thread(
            _fine_candidates, [fine_coords[int(i)] for i in seeds], fine_res, finest_res, 0,
            plan.center, radius, plan.finest_max,
        )
        finest = await optional_stage("finest", finest_coords, finest_res)
    elif fine is not None and not plan.finest_max and NOTE_FINE_SKIPPED not in notes:
        # Admission left the third stage out to fit what is left of the allowance.
        notes.append(NOTE_FINE_SKIPPED)

    # ── Keep the best stage ───────────────────────────────────────────
    named = [(name, ev) for name, ev in (("coarse", coarse), ("fine", fine), ("finest", finest))
             if ev is not None]
    stages = [ev for _, ev in named]
    if any(np.isfinite(ev.eligible()).any() for ev in stages):
        waived = False
    elif any(np.isfinite(ev.waived()).any() for ev in stages):
        waived = True
        notes.append(NOTE_OFF_STREET)
    else:
        raise NoFeasiblePoint("off_street")

    def offered(ev: Evaluation) -> np.ndarray:
        return ev.waived() if waived else ev.eligible()

    # A later stage wins ties: refinement never makes the answer worse.
    kept, stage = coarse, "coarse"
    for name, ev in named[1:]:
        if np.isfinite(offered(ev)).any() and offered(ev).min() <= offered(kept).min():
            kept, stage = ev, name

    # ── Compile results ───────────────────────────────────────────────
    scores = offered(kept)
    ranked = _ranked(kept, scores, req.objective)
    # A best place with no street name gives way to a named one in the same stage at most
    # NAMED_SLACK_MIN worse (#26): a star on an unnamed path is hard to find. The answer says
    # so, and the unnamed places stay in the alternatives.
    named_cost = 0.0
    if not kept.shown[int(ranked[0])].name:
        unnamed_best = float(scores[ranked[0]])
        idx = _named_near_best(kept, scores, ranked, unnamed_best + NAMED_SLACK_MIN)
        if idx is not None:
            ranked = np.concatenate(([idx], ranked[ranked != idx]))
            named_cost = float(scores[idx]) - unnamed_best
            if named_cost > 0:
                notes.append(note_named(named_cost, req.objective))
    picks = _distinct(kept, ranked, req.search.top_k)
    top = [_make_result(i, kept, scores, participants) for i in picks]
    best_score = float(scores[ranked[0]])

    # Fairest answers also say which place has the quickest average trip, from the places
    # already checked (no extra routing). Alternatives at that place are not listed twice.
    quickest: CandidateResult | None = None
    quickest_stage: str | None = None
    quickest_spread_m = 0.0
    if req.objective == "minimax":
        q = _quickest(stages, offered, kept, picks[0])
        if q is not None:
            q_ev, q_idx = q
            quickest = _make_result(q_idx, q_ev, offered(q_ev), participants)
            quickest_stage = next(name for name, ev in named if ev is q_ev)
            quickest_spread_m = float(q_ev.spread_m[q_idx])
            top = [top[0]] + [c for c in top[1:]
                              if haversine_km(c.lon, c.lat, quickest.lon, quickest.lat) * 1000.0
                              >= MIN_ALT_GAP_M]

    reachable = np.isfinite(kept.raw)
    if not waived and reachable.any() and \
            float(kept.off_street[reachable].mean()) > FEW_STREETS_SHARE:
        notes.append(NOTE_FEW_STREETS)
    if float(kept.spread_m[picks[0]]) > NOTE_SPREAD_M:
        notes.append(NOTE_SPREAD)
    if quickest_spread_m > NOTE_SPREAD_M:
        notes.append(NOTE_SPREAD_QUICKEST)
    if any(ev.unchecked.any() for ev in stages):
        notes.append(NOTE_UNCHECKED)
    allowance = solve_allowance.get()
    if allowance is not None and allowance.bad_replies:
        notes.append(NOTE_BAD_REPLY)
    if allowance is not None and allowance.no_route_replies:
        notes.append(NOTE_NO_ROUTE)

    # Near-optimal meeting area: the kept stage's grid cells, and the cells of the points
    # shown for them, so the star is always inside it.
    area_cells: set[str] = set()
    for i in ranked:
        if float(scores[i]) <= best_score * (1.0 + req.search.area_slack):
            lon, lat = kept.coords[int(i)]
            area_cells.add(h3.latlng_to_cell(lat, lon, kept.res))
            s = kept.shown[int(i)]
            area_cells.add(h3.latlng_to_cell(s.lat, s.lon, kept.res))
    meeting_area = (await asyncio.to_thread(cells_to_geojson, sorted(area_cells))
                    if area_cells else None)

    def dropped(mask_of) -> int:
        return int(sum(int(mask_of(ev).sum()) for ev in stages))

    diagnostics: dict[str, Any] = {
        "path": "grid",
        "stage": stage,
        "coarse_resolution": coarse_res,
        "fine_resolution": fine_res,
        "answer_resolution": kept.res,
        "coarse_candidates": len(coarse_coords),
        "fine_candidates": len(fine_coords),
        "finest_resolution": finest_res,
        "finest_candidates": len(finest_coords),
        "feasible_count": int(np.isfinite(scores).sum()),
        "search_radius_km": round(radius, 2),
        "estimated_matrix_elements": plan.elements,
        # Over the stages the answer was chosen from.
        "dropped_no_route": dropped(lambda ev: ev.unchecked & ~ev.skipped),
        "unchecked_for_budget": dropped(lambda ev: ev.unchecked & ev.skipped),
        "dropped_off_street": int((kept.off_street & np.isfinite(kept.raw)).sum()),
        "retries": allowance.retries if allowance is not None else 0,
        "bad_replies": allowance.bad_replies if allowance is not None else 0,
        "no_route_replies": allowance.no_route_replies if allowance is not None else 0,
        "matrix_requests": allowance.sent_requests if allowance is not None else 0,
        "matrix_elements": allowance.sent_elements if allowance is not None else 0,
        "quickest_stage": quickest_stage,
        # Minutes the shown place costs over an unnamed better one (0: none was passed over).
        "named_over_best_min": round(named_cost, 2),
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }
    return SolveResponse(
        objective=req.objective,
        best=top[0],
        top=top,
        meeting_area_geojson=meeting_area,
        diagnostics=diagnostics,
        notes=notes,
        quickest=quickest,
    )
