from fastapi import APIRouter, Request

from server.meetup.models import SolveRequest, SolveResponse
from server.meetup.solver import solve

router = APIRouter(tags=["meetup"])


@router.get("/health")
def health():
    return {"ok": True}


@router.post("/solve", response_model=SolveResponse)
async def solve_endpoint(req: SolveRequest, request: Request):
    return await solve(req, request.app.state.meetup_router)
