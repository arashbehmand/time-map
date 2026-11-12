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
   The router snaps each candidate to the street network and says where; times are to
   that point. Candidates it could not check at all are dropped and counted.
3. Score the candidates. A candidate whose snapped point is far from it (in water, a rail
   yard, a park) is not offered as an answer, but can still guide the next stage.
4. Refine: score the children of the best coarse cells at the next finer resolution.
5. Keep whichever stage has the better answer (refinement never makes it worse), and
   return the top-K distinct places, each at its snapped point with its street name.

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
from server.meetup.limits import SolvePlan
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
# Alternatives closer than this to a better one are left out (metres).
MIN_ALT_GAP_M = 150.0
# More than this share of a stage's candidates left unchecked (NoRoute) fails that stage.
MAX_UNRESOLVED_SHARE = 0.25
# The shown point and name come from the first mode present: people meet on foot.
MODE_ORDER = ("walking", "cycling", "driving", "driving-traffic")

NOTE_FINE_SKIPPED = "The closer search didn't finish, so this answer is less precise than usual."
NOTE_UNCHECKED = "Some places in the search area couldn't be checked, so a better place may exist."
NOTE_OFF_STREET = "There are few streets or paths here, so the star is on the nearest one we found."


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


@dataclass
class Evaluation:
    """One stage: candidates, their times and scores, and where the router put them."""
    res: int
    coords: list[Coord]          # the grid points searched
    times: np.ndarray            # (n_participants, n_candidates), participants in id order
    raw: np.ndarray              # score, ignoring the snap rule
    mean: np.ndarray
    mx: np.ndarray
    shown: list[Snap]            # where each candidate is shown, and the street's name
    snap_m: np.ndarray           # the largest snap distance over the group's modes
    unresolved: np.ndarray       # the router could not check it at all (dropped)
    off_street: np.ndarray       # snapped too far away to offer as an answer

    def eligible(self) -> np.ndarray:
        return np.where(self.off_street, np.inf, self.raw)


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
    groups = _group_by_mode(participants)
    modes = sorted(groups, key=MODE_ORDER.index)
    results = await gather_or_cancel(
        router.duration_matrix(mode, [(p.lon, p.lat) for _, p in groups[mode]], candidates)
        for mode in modes
    )

    snap_m = np.zeros(n_c)
    unresolved = np.zeros(n_c, dtype=bool)
    for mode, result in zip(modes, results):
        for row, (p_idx, _) in enumerate(groups[mode]):
            times[p_idx, :] = result.minutes[row, :]
        unresolved |= result.unresolved
        dist = np.array([haversine_km(c[0], c[1], s.lon, s.lat) * 1000.0
                         for c, s in zip(candidates, result.snaps)])
        snap_m = np.maximum(snap_m, dist)
    shown = results[0].snaps

    times[:, unresolved] = np.inf
    raw, mean, mx = _score(times, participants, objective, alpha)
    limit = max(MAX_SNAP_M, SNAP_EDGE_SHARE * edge_km(res) * 1000.0)
    off_street = (snap_m > limit) & ~unresolved
    return Evaluation(res, candidates, times, raw, mean, mx, shown, snap_m, unresolved, off_street)


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
        snap_m=round(float(ev.snap_m[idx]), 1),
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
    return float(ev.unresolved.mean()) if len(ev.coords) else 0.0


def _check_coarse(ev: Evaluation, participants: list[Participant], index_of: list[int]) -> None:
    """Fail with a reason when the coarse stage has nothing to offer. Raises or returns."""
    checked = ~ev.unresolved
    reach = np.isfinite(ev.times) & checked
    stuck = [k for k in range(len(participants)) if not reach[k].any()]
    if stuck:
        raise NoFeasiblePoint("no_route", [{"index": index_of[k], "reason": "no_route"}
                                           for k in sorted(stuck, key=lambda k: index_of[k])])
    if _unresolved_share(ev) > MAX_UNRESOLVED_SHARE:
        raise UnroutableArea()
    if np.isfinite(ev.raw).any():
        return
    common = reach.all(axis=0)
    if not common.any():
        raise NoFeasiblePoint("no_common_place")
    limited = [k for k, p in enumerate(participants) if p.max_minutes is not None]
    over = [k for k in limited if not (ev.times[k, common] <= participants[k].max_minutes).any()]
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
    evaluated = [coarse]

    # ── Phase 2: refine around the best coarse cells ──────────────────
    # Seeds are ranked ignoring the snap rule: a coarse centre in the river still points
    # at its children on the streets around it.
    fine_res = min(coarse_res + 1, 15)
    seeds = _ranked(coarse, coarse.raw, req.objective)[: req.search.refine_top]
    fine_coords = await asyncio.to_thread(
        _fine_candidates, [coarse_coords[int(i)] for i in seeds], coarse_res, fine_res,
        req.search.neighbor_ring, plan.center, radius, plan.fine_max,
    )
    fine: Evaluation | None = None
    if fine_coords:
        try:
            fine = await _evaluate(router, participants, fine_coords, req.objective, req.alpha,
                                   fine_res)
        except ApiError as exc:
            # Budget, rate, in-flight or upstream trouble in the optional fine stage: the
            # coarse answer is already paid for, so return it rather than nothing.
            log.warning("fine stage skipped: %s", exc.code)
            notes.append(NOTE_FINE_SKIPPED)
        else:
            evaluated.append(fine)
            if _unresolved_share(fine) > MAX_UNRESOLVED_SHARE:
                log.warning("fine stage skipped: too many places unchecked")
                notes.append(NOTE_FINE_SKIPPED)
                fine = None

    # ── Keep the better stage ─────────────────────────────────────────
    stages = [coarse] + ([fine] if fine is not None else [])
    waived = not any(np.isfinite(ev.eligible()).any() for ev in stages)

    def offered(ev: Evaluation) -> np.ndarray:
        return ev.raw if waived else ev.eligible()

    kept, stage = coarse, "coarse"
    if fine is not None and np.isfinite(offered(fine)).any() and \
            offered(fine).min() <= offered(coarse).min():
        kept, stage = fine, "fine"

    # ── Compile results ───────────────────────────────────────────────
    scores = offered(kept)
    ranked = _ranked(kept, scores, req.objective)
    top = [_make_result(i, kept, scores, participants)
           for i in _distinct(kept, ranked, req.search.top_k)]
    best_score = float(scores[ranked[0]])

    if waived:
        notes.append(NOTE_OFF_STREET)
    if any(ev.unresolved.any() for ev in evaluated):
        notes.append(NOTE_UNCHECKED)

    # Near-optimal meeting area (grid cells of the kept stage)
    area_cells = [
        h3.latlng_to_cell(kept.coords[int(i)][1], kept.coords[int(i)][0], kept.res)
        for i in ranked if float(scores[i]) <= best_score * (1.0 + req.search.area_slack)
    ]
    meeting_area = await asyncio.to_thread(cells_to_geojson, area_cells) if area_cells else None

    allowance = solve_allowance.get()
    diagnostics: dict[str, Any] = {
        "stage": stage,
        "coarse_resolution": coarse_res,
        "fine_resolution": kept.res,
        "coarse_candidates": len(coarse_coords),
        "fine_candidates": len(fine_coords),
        "feasible_count": int(np.isfinite(scores).sum()),
        "search_radius_km": round(radius, 2),
        "estimated_matrix_elements": plan.elements,
        "dropped_no_route": int(sum(int(ev.unresolved.sum()) for ev in evaluated)),
        "dropped_off_street": 0 if waived else int((kept.off_street & np.isfinite(kept.raw)).sum()),
        "retries": allowance.retries if allowance is not None else 0,
        "matrix_requests": sum(allowance.spent_requests.values()) if allowance is not None else 0,
        "matrix_elements": allowance.spent_elements if allowance is not None else 0,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
    }
    return SolveResponse(
        objective=req.objective,
        best=top[0],
        top=top,
        meeting_area_geojson=meeting_area,
        diagnostics=diagnostics,
        notes=notes,
    )
