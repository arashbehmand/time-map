import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.errors import Busy
from server.guard.guard import current_client, per_ip
from server.meetup.limits import SolveLimits, plan_solve, rate_bucket
from server.meetup.models import SolveRequest, SolveResponse
from server.meetup.routing import MapboxMatrixRouter, SolveAllowance, solve_allowance
from server.meetup.solver import solve

router = APIRouter(tags=["meetup"])


@router.get("/health")
def health(request: Request):
    usage = request.app.state.usage
    if not usage.available:
        return JSONResponse({"ok": False, "usage_store": "unavailable"}, status_code=503)
    return {"ok": True, "usage_store": "ok"}


@router.post("/solve", response_model=SolveResponse, dependencies=[per_ip("solve")])
async def solve_endpoint(req: SolveRequest, request: Request):
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
        token = solve_allowance.set(SolveAllowance.for_plan(plan, state.settings))
        try:
            return await solve(req, meetup_router, plan)
        finally:
            solve_allowance.reset(token)
    finally:
        sem.release()
