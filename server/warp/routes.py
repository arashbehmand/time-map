from fastapi import APIRouter, Request

from server.guard.guard import per_ip
from server.warp.models import IsochroneRequest, TravelFieldRequest

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


@router.post("/travel-time-field", dependencies=[per_ip("travel_time_field")])
async def travel_time_field_endpoint(req: TravelFieldRequest, request: Request):
    """One travel-time field for a pin and a way of travelling (server/warp/travel_field.py)."""
    return await request.app.state.fields.get(req.longitude, req.latitude, req.mode, req.window,
                                              smooth=req.smooth)


@router.get("/transit/status", dependencies=[per_ip("transit_status")])
async def transit_status_endpoint(request: Request):
    """Whether public transport times are available, for which windows, where, and until when."""
    return request.app.state.transit.status()
