import asyncio

from fastapi import APIRouter, Request

from server.errors import Busy
from server.guard.budget import daily_exhausted
from server.guard.guard import per_ip
from server.meetup.limits import plan_solve
from server.meetup.models import SolveRequest, SolveResponse
from server.meetup.routing import MapboxMatrixRouter
from server.meetup.solver import solve

router = APIRouter(tags=["meetup"])


@router.get("/health")
def health():
    return {"ok": True}


@router.post("/solve", response_model=SolveResponse, dependencies=[per_ip("solve")])
async def solve_endpoint(req: SolveRequest, request: Request):
    state = request.app.state
    # Every cap is checked here, before any candidate is generated or Mapbox is called.
    plan = plan_solve(req, state.settings)

    meetup_router = state.meetup_router
    if isinstance(meetup_router, MapboxMatrixRouter):
        if state.guard.store.remaining_today("matrix") < plan.elements:
            raise daily_exhausted(
                state.guard.clock.utcnow(),
                "Today's allowance for meeting-point searches is used up. "
                "It resets at midnight UTC.",
            )

    sem: asyncio.Semaphore = state.solve_slots
    try:
        await asyncio.wait_for(sem.acquire(), state.settings.solve_wait_s)
    except TimeoutError:
        raise Busy() from None
    try:
        return await solve(req, meetup_router, plan)
    finally:
        sem.release()
