"""
Warp parameters:  location → isochrones → radial warp LUT → JSON
"""
from __future__ import annotations

import logging
import time

from server.config import DENSIFY_PX, NUM_ANGULAR_SAMPLES, SMOOTH_WINDOW
from server.errors import NoIsochrone
from server.warp.geometry import Viewport
from server.warp.mapbox_client import MapboxClient
from server.warp.warp import RadialIsochroneWarp

log = logging.getLogger("isochrone_warp")


def _elapsed(t0: float) -> str:
    return f"{(time.monotonic() - t0) * 1000:.0f}ms"


async def compute_warp_params(
    client: MapboxClient,
    longitude: float,
    latitude: float,
    zoom: int = 14,
    width: int = 1280,
    height: int = 720,
    profile: str = "walking",
    travel_times: list[int] | None = None,
) -> dict:
    """Return only the warp LUT (isochrones → radial profiles). No tile fetching."""
    if travel_times is None:
        travel_times = [5, 10, 15, 20]

    t_total = time.monotonic()
    # No coordinates in logs: a location is often someone's home.
    log.info("▶ warp-params  zoom=%d profile=%s times=%d", zoom, profile, len(travel_times))

    viewport = Viewport(longitude, latitude, zoom, width, height)

    t = time.monotonic()
    iso_gj = await client.fetch_isochrones(longitude, latitude, profile, travel_times)
    log.info("  isochrones fetched (%d features)  %s", len(iso_gj.get("features", [])), _elapsed(t))
    if not iso_gj.get("features"):
        raise NoIsochrone()

    t = time.monotonic()
    warp = RadialIsochroneWarp.from_isochrones(
        geojson=iso_gj,
        viewport=viewport,
        n_angles=NUM_ANGULAR_SAMPLES,
        max_step_px=DENSIFY_PX,
        smooth_window=SMOOTH_WINDOW,
    )
    log.info("  warp built  %s  total=%s", _elapsed(t), _elapsed(t_total))

    k = len(warp.contour_minutes)
    return {
        "k": k,
        "center": [width / 2.0, height / 2.0],
        "canvas": {"width": width, "height": height},
        "contour_minutes": warp.contour_minutes.tolist(),
        "target_radii": warp.target_radii.tolist(),
        "source_radii": warp.source_radii.tolist(),
        "support_radii": warp.support_radii.tolist(),
        "rings": [
            {"minutes": float(m), "radius_px": round(float(r), 1)}
            for m, r in zip(warp.contour_minutes, warp.target_radii)
        ],
    }
