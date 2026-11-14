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


LOOPBACK = ("127.", "::1", "::ffff:127.")
FORWARDED = ("x-forwarded-for", "forwarded", "x-real-ip")


def is_local(request: Request) -> bool:
    """The request comes from this machine and through no proxy: the only place a build whose
    licences are not cleared may be served (TRANSIT_ALLOW_UNCLEARED). A proxy or tunnel on the
    same machine that adds none of these headers looks local, so the override never belongs on
    a reachable server (README)."""
    host = request.client.host if request.client else ""
    settings = getattr(request.app.state, "settings", None)
    headers = (*FORWARDED, settings.client_ip_header) if settings else FORWARDED
    return host.startswith(LOOPBACK) and not any(h in request.headers for h in headers)


@router.post("/travel-time-field", dependencies=[per_ip("travel_time_field")])
async def travel_time_field_endpoint(req: TravelFieldRequest, request: Request):
    """One travel-time field for a pin and a way of travelling (server/warp/travel_field.py)."""
    return await request.app.state.fields.get(req.longitude, req.latitude, req.mode, req.window,
                                              smooth=req.smooth, local=is_local(request))


@router.get("/transit/status", dependencies=[per_ip("transit_status")])
async def transit_status_endpoint(request: Request):
    """Whether public transport times are available, for which windows, where, and until when."""
    return request.app.state.transit.status(local=is_local(request))
