"""
Full pipeline:  location → isochrones → warp → tiles → transform → JSON
"""
from __future__ import annotations

import logging
import time

from server.config import DEFAULT_LAYERS, NUM_ANGULAR_SAMPLES, DENSIFY_PX, SMOOTH_WINDOW
from server.warp.geometry import TILE_SIZE, Viewport
from server.warp.warp import RadialIsochroneWarp
from server.warp.mapbox_client import MapboxClient
from server.warp.vector_transform import transform_feature, extract_labels

log = logging.getLogger("isochrone_warp")


def _elapsed(t0: float) -> str:
    return f"{(time.monotonic() - t0) * 1000:.0f}ms"


async def compute_warp_params(
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
    log.info("▶ warp-params  lon=%.5f lat=%.5f zoom=%d profile=%s times=%s",
             longitude, latitude, zoom, profile, travel_times)

    client = MapboxClient()
    viewport = Viewport(longitude, latitude, zoom, width, height)

    t = time.monotonic()
    iso_gj = await client.fetch_isochrones(longitude, latitude, profile, travel_times)
    log.info("  isochrones fetched (%d features)  %s", len(iso_gj.get("features", [])), _elapsed(t))
    if not iso_gj.get("features"):
        raise ValueError("No isochrone features returned from Mapbox.")

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


async def compute_warped_frame(
    longitude: float,
    latitude: float,
    zoom: int = 14,
    width: int = 1280,
    height: int = 1280,
    profile: str = "walking",
    travel_times: list[int] | None = None,
    layers: list[str] | None = None,
    densify_px: float = DENSIFY_PX,
    target_outer_radius: float | None = None,
) -> dict:
    if travel_times is None:
        travel_times = [5, 10, 15, 20]
    if layers is None:
        layers = DEFAULT_LAYERS.copy()

    t_total = time.monotonic()
    log.info("▶ warped-map  lon=%.5f lat=%.5f zoom=%d profile=%s times=%s",
             longitude, latitude, zoom, profile, travel_times)

    client = MapboxClient()

    viewport = Viewport(longitude, latitude, zoom, width, height)

    t = time.monotonic()
    iso_gj = await client.fetch_isochrones(longitude, latitude, profile, travel_times)
    log.info("  isochrones fetched (%d features)  %s", len(iso_gj.get("features", [])), _elapsed(t))
    if not iso_gj.get("features"):
        raise ValueError("No isochrone features returned from Mapbox.")

    t = time.monotonic()
    warp = RadialIsochroneWarp.from_isochrones(
        geojson=iso_gj,
        viewport=viewport,
        n_angles=NUM_ANGULAR_SAMPLES,
        max_step_px=densify_px,
        smooth_window=SMOOTH_WINDOW,
        target_outer_radius=target_outer_radius,
    )
    log.info("  warp built  %s", _elapsed(t))

    t = time.monotonic()
    tile_pad = min(warp.support_pad_px + 64, int(TILE_SIZE * 1.5))
    raw_layers = await client.fetch_vector_features(viewport, layers, pad_px=tile_pad)
    n_features = sum(len(v) for v in raw_layers.values())
    log.info("  tiles fetched (%d features across %d layers)  %s",
             n_features, len(raw_layers), _elapsed(t))

    t = time.monotonic()
    warped_layers: dict[str, list[dict]] = {}
    for layer_name, features in raw_layers.items():
        warped_layers[layer_name] = [
            transform_feature(f, warp, densify_px=densify_px)
            for f in features
        ]
    log.info("  geometry transformed  %s", _elapsed(t))

    t = time.monotonic()
    labels = extract_labels(raw_layers, warp, densify_px=densify_px)
    log.info("  labels extracted (%d)  %s", len(labels), _elapsed(t))

    rings = [
        {"minutes": float(m), "radius_px": round(float(r), 1)}
        for m, r in zip(warp.contour_minutes, warp.target_radii)
    ]

    log.info("✔ done  total=%s", _elapsed(t_total))

    return {
        "center": [width / 2.0, height / 2.0],
        "canvas": {"width": width, "height": height},
        "rings": rings,
        "layers": warped_layers,
        "labels": labels,
    }
