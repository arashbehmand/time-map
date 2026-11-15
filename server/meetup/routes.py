import asyncio
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.errors import Busy, TransitUnavailableError
from server.guard.guard import current_client, per_ip
from server.meetup import stations
from server.meetup.limits import SolveLimits, plan_solve, plan_stations, rate_bucket
from server.meetup.models import DEFAULT_WINDOW, SolveRequest, SolveResponse
from server.meetup.routing import MapboxMatrixRouter, SolveAllowance, solve_allowance
from server.meetup.solver import solve
from server.warp.routes import is_local

router = APIRouter(tags=["meetup"])


@router.get("/health")
def health(request: Request):
    usage = request.app.state.usage
    if not usage.available:
        return JSONResponse({"ok": False, "usage_store": "unavailable"}, status_code=503)
    return {"ok": True, "usage_store": "ok"}


@router.post("/solve", response_model=SolveResponse, dependencies=[per_ip("solve")])
async def solve_endpoint(req: SolveRequest, request: Request):
    if any(p.mode == "transit" for p in req.participants):
        return await _solve_at_stations(req, request)
    state = request.app.state
    guard = state.guard
    meetup_router = state.meetup_router
    uses_mapbox = isinstance(meetup_router, MapboxMatrixRouter)

    sem: asyncio.Semaphore = state.solve_slots
    try:
        await asyncio.wait_for(sem.acquire(), state.settings.solve_wait_s)
    except TimeoutError:
        raise Busy() from None
    try:
        # Every cap is checked, and the search sized to fit what is left of the budget,
        # this client's share and the free rate slots, before any candidate is generated
        # or Mapbox is called. One solve runs at a time, so the free slots stay free.
        limits = SolveLimits()
        if uses_mapbox:
            buckets = {rate_bucket(p.mode) for p in req.participants}
            limits = SolveLimits(
                elements_left_today=guard.store.remaining_today("matrix"),
                elements_left_client=guard.shares.remaining(current_client.get(), "matrix"),
                free_requests={b: guard.windows[b].available() for b in buckets},
                now=guard.clock.utcnow(),
            )
        plan = plan_solve(req, state.settings, limits)
        live = [v for v in (limits.elements_left_today, limits.elements_left_client) if v is not None]
        token = solve_allowance.set(SolveAllowance.for_plan(
            plan, state.settings, live_elements=min(live) if live else None))
        try:
            return await solve(req, meetup_router, plan)
        finally:
            solve_allowance.reset(token)
    finally:
        sem.release()


async def _solve_at_stations(req: SolveRequest, request: Request) -> SolveResponse:
    """A group with someone on public transport meets at a station (server/meetup/stations.py).
    The tables are served on the same terms as the warp page's fields: unexpired, and an
    uncleared build to this machine only. An all-transit group sends no Matrix request, so it
    doesn't wait for a solve slot."""
    state = request.app.state
    started = time.perf_counter()
    local = is_local(request)
    data = state.transit.usable(local=local)
    if data is None:
        raise TransitUnavailableError(extra={"reason": state.transit.reason_for(local)})
    window = req.window or DEFAULT_WINDOW
    group = stations.prepare(req, data)
    meetup_router = state.meetup_router
    if not group.road:
        plan = plan_stations(req, state.settings, None, len(group.cand))
        return await stations.solve_stations(req, data, meetup_router, plan, window, group,
                                             started=started)

    guard = state.guard
    sem: asyncio.Semaphore = state.solve_slots
    try:
        await asyncio.wait_for(sem.acquire(), state.settings.solve_wait_s)
    except TimeoutError:
        raise Busy() from None
    try:
        limits = SolveLimits()
        if isinstance(meetup_router, MapboxMatrixRouter):
            buckets = {rate_bucket(p.mode) for p in req.participants if p.mode != "transit"}
            limits = SolveLimits(
                elements_left_today=guard.store.remaining_today("matrix"),
                elements_left_client=guard.shares.remaining(current_client.get(), "matrix"),
                free_requests={b: guard.windows[b].available() for b in buckets},
                now=guard.clock.utcnow(),
            )
        plan = plan_stations(req, state.settings, limits, len(group.cand))
        live = [v for v in (limits.elements_left_today, limits.elements_left_client) if v is not None]
        token = solve_allowance.set(SolveAllowance.for_plan(
            plan, state.settings, live_elements=min(live) if live else None))
        try:
            return await stations.solve_stations(req, data, meetup_router, plan, window, group,
                                                 started=started)
        finally:
            solve_allowance.reset(token)
    finally:
        sem.release()
