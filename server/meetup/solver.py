"""
Core optimizer.

Mathematical formulation
========================
Given N participants each with travel-time function T_i(x),
we search over candidate locations x to minimize:

  minimax:  F(x) = max_i  T_i(x)                        [fairness]
  sum:      F(x) = Σ  w_i T_i(x) / Σ w_i                [efficiency]
  hybrid:   F(x) = α·max_i T_i(x) + (1-α)·weighted_mean [balanced]

Algorithm
=========
1. Generate H3 hex candidates covering the search area.
2. Query the routing engine for actual network travel times
   from every participant to every candidate (batched Matrix API).
3. Score all candidates; pick the best.
4. Optionally refine: zoom into the best coarse cells at finer
   H3 resolution and re-score.
5. Return the top-K candidates and the near-optimal meeting area
   (all cells within score ≤ best × (1 + slack)).
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

import h3
import numpy as np

from server.errors import NoFeasiblePoint
from server.guard.budget import BudgetExhausted
from server.meetup.candidates import (
    cells_to_geojson,
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
from server.meetup.routing import Router


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
    score[bad] = np.inf
    w_mean[bad] = np.inf
    mx[bad] = np.inf

    return score, w_mean, mx

async def _evaluate(
    router: Router,
    participants: list[Participant],
    candidates: list[tuple[float, float]],
    objective: str,
    alpha: float,
) -> dict[str, Any]:
    n_p = len(participants)
    n_c = len(candidates)
    times = np.full((n_p, n_c), np.inf)

    for mode, group in _group_by_mode(participants).items():
        origins = [(p.lon, p.lat) for _, p in group]
        mat = await router.duration_matrix(mode, origins, candidates)
        for row_idx, (p_idx, _) in enumerate(group):
            times[p_idx, :] = mat[row_idx, :]

    score, w_mean, mx = _score(times, participants, objective, alpha)
    return {
        "candidates": candidates,
        "times": times,
        "score": score,
        "w_mean": w_mean,
        "mx": mx,
    }

def _make_result(
    idx: int,
    ev: dict,
    participants: list[Participant],
) -> CandidateResult:
    lon, lat = ev["candidates"][idx]
    return CandidateResult(
        lon=round(float(lon), 7),
        lat=round(float(lat), 7),
        score=round(float(ev["score"][idx]), 2),
        max_time_min=round(float(ev["mx"][idx]), 2),
        mean_time_min=round(float(ev["w_mean"][idx]), 2),
        times={
            p.id: round(float(ev["times"][i, idx]), 2)
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


async def solve(req: SolveRequest, router: Router, plan: SolvePlan) -> SolveResponse:
    participants = req.participants
    origins = [(p.lon, p.lat) for p in participants]
    c_lon, c_lat = plan.center
    radius = plan.radius_km

    # ── Phase 1: coarse search ────────────────────────────────────────
    coarse_res, coarse_coords = await asyncio.to_thread(
        generate_candidates, c_lon, c_lat, radius, plan.resolution, origins,
    )

    coarse_ev = await _evaluate(
        router, participants, coarse_coords, req.objective, req.alpha
    )

    feasible = np.where(np.isfinite(coarse_ev["score"]))[0]
    if len(feasible) == 0:
        raise NoFeasiblePoint()

    ranked = feasible[np.argsort(coarse_ev["score"][feasible])]

    # ── Phase 2: refine best region at finer H3 resolution ────────────
    fine_res = min(coarse_res + 1, 15)
    seed_coords = [coarse_coords[int(i)] for i in ranked[: req.search.refine_top]]
    fine_coords = await asyncio.to_thread(
        _fine_candidates, seed_coords, coarse_res, fine_res,
        req.search.neighbor_ring, plan.center, radius, plan.fine_max,
    )

    # Use fine results if available, else fall back to coarse
    stage = "coarse"
    best_ev = coarse_ev
    used_res = coarse_res

    if fine_coords:
        try:
            fine_ev = await _evaluate(
                router, participants, fine_coords, req.objective, req.alpha
            )
        except BudgetExhausted:
            # The coarse answer is already paid for; return it rather than nothing.
            fine_ev = None
        if fine_ev is not None and np.isfinite(fine_ev["score"]).any():
            best_ev = fine_ev
            stage = "fine"
            used_res = fine_res

    # ── Compile results ───────────────────────────────────────────────
    scores = best_ev["score"]
    valid = np.where(np.isfinite(scores))[0]
    if len(valid) == 0:
        raise NoFeasiblePoint()

    order = valid[np.argsort(scores[valid])]
    best_idx = int(order[0])
    best_score = float(scores[best_idx])

    top = [_make_result(int(i), best_ev, participants) for i in order[: req.search.top_k]]
    best = top[0]

    # Near-optimal meeting area
    area_cells = []
    for i in order:
        if float(scores[i]) <= best_score * (1.0 + req.search.area_slack):
            lon, lat = best_ev["candidates"][int(i)]
            area_cells.append(h3.latlng_to_cell(lat, lon, used_res))

    meeting_area = await asyncio.to_thread(cells_to_geojson, area_cells) if area_cells else None

    return SolveResponse(
        objective=req.objective,
        best=best,
        top=top,
        meeting_area_geojson=meeting_area,
        diagnostics={
            "stage": stage,
            "coarse_resolution": coarse_res,
            "fine_resolution": used_res,
            "coarse_candidates": len(coarse_coords),
            "fine_candidates": len(fine_coords) if fine_coords else 0,
            "feasible_count": int(len(valid)),
            "search_radius_km": round(radius, 2),
            "estimated_matrix_elements": plan.elements,
        },
    )
