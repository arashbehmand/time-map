from fastapi import APIRouter, Request

from server.guard.guard import per_ip
from server.warp.models import IsochroneRequest, WarpParamsRequest
from server.warp.pipeline import compute_warp_params

router = APIRouter(tags=["warp"])


@router.post("/isochrones", dependencies=[per_ip("isochrones")])
async def fetch_isochrones_endpoint(req: IsochroneRequest, request: Request):
    """Fetch raw isochrone polygons for a single point."""
    return await request.app.state.isochrones.fetch_isochrones(
        lon=req.longitude,
        lat=req.latitude,
        profile=req.profile,
        minutes=req.travel_times,
    )


@router.post("/warp-params", dependencies=[per_ip("warp_params")])
async def warp_params_endpoint(req: WarpParamsRequest, request: Request):
    return await compute_warp_params(
        request.app.state.isochrones,
        longitude=req.longitude,
        latitude=req.latitude,
        zoom=req.zoom,
        width=req.width,
        height=req.height,
        profile=req.profile,
        travel_times=req.travel_times,
    )
