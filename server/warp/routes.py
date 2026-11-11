from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from server.warp.pipeline import compute_warp_params

router = APIRouter(tags=["warp"])


class WarpParamsRequest(BaseModel):
    longitude: float
    latitude: float
    zoom: int = Field(default=14, ge=8, le=18)
    width: int = Field(default=1280, ge=256, le=2048)
    height: int = Field(default=720, ge=256, le=2048)
    profile: str = Field(default="walking", pattern="^(walking|cycling|driving)$")
    travel_times: list[int] = Field(default_factory=lambda: [5, 10, 15, 20])


@router.post("/warp-params")
async def warp_params_endpoint(req: WarpParamsRequest, request: Request):
    return await compute_warp_params(
        request.app.state.http_client,
        longitude=req.longitude,
        latitude=req.latitude,
        zoom=req.zoom,
        width=req.width,
        height=req.height,
        profile=req.profile,
        travel_times=req.travel_times,
    )
