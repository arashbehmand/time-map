from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from server.warp.pipeline import compute_warped_frame, compute_warp_params

router = APIRouter(tags=["warp"])


class WarpRequest(BaseModel):
    longitude: float
    latitude: float
    zoom: int = Field(default=14, ge=8, le=18)
    width: int = Field(default=1280, ge=256, le=2048)
    height: int = Field(default=1280, ge=256, le=2048)
    profile: str = Field(default="walking", pattern="^(walking|cycling|driving)$")
    travel_times: list[int] = Field(default_factory=lambda: [5, 10, 15, 20])
    target_outer_radius: float | None = None


class WarpParamsRequest(BaseModel):
    longitude: float
    latitude: float
    zoom: int = Field(default=14, ge=8, le=18)
    width: int = Field(default=1280, ge=256, le=2048)
    height: int = Field(default=720, ge=256, le=2048)
    profile: str = Field(default="walking", pattern="^(walking|cycling|driving)$")
    travel_times: list[int] = Field(default_factory=lambda: [5, 10, 15, 20])


@router.post("/warped-map")
async def warped_map(req: WarpRequest):
    try:
        return await compute_warped_frame(
            longitude=req.longitude,
            latitude=req.latitude,
            zoom=req.zoom,
            width=req.width,
            height=req.height,
            profile=req.profile,
            travel_times=req.travel_times,
            target_outer_radius=req.target_outer_radius,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/warp-params")
async def warp_params_endpoint(req: WarpParamsRequest):
    try:
        return await compute_warp_params(
            longitude=req.longitude,
            latitude=req.latitude,
            zoom=req.zoom,
            width=req.width,
            height=req.height,
            profile=req.profile,
            travel_times=req.travel_times,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
