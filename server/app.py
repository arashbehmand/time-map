import logging
import logging.config
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from server.config import MAPBOX_TOKEN
from server.warp.routes import router as warp_router
from server.meetup.routes import router as meetup_router
from server.meetup.routing import build_router as build_meetup_router

# ── Logging ──────────────────────────────────────────────────────────
logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {
            "format": "%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
            "datefmt": "%H:%M:%S",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "loggers": {
        "isochrone_warp": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
    },
})


# ── Lifespan ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI):
    import os

    # Shared HTTP client for Mapbox tile/glyph proxies
    _app.state.http_client = httpx.AsyncClient(
        timeout=30.0,
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
    )

    # Meetup router (Mapbox Matrix API or Euclidean)
    provider = os.getenv("ROUTER_PROVIDER", "mapbox")
    router = build_meetup_router(provider, token=MAPBOX_TOKEN)
    _app.state.meetup_router = router

    yield

    await _app.state.http_client.aclose()
    if hasattr(router, "aclose"):
        await router.aclose()


# ── App ──────────────────────────────────────────────────────────────
app = FastAPI(title="Time-Map", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API routers ──────────────────────────────────────────────────────
app.include_router(warp_router, prefix="/api")
app.include_router(meetup_router, prefix="/api/meetup")


# ── Shared: isochrone endpoint ───────────────────────────────────────
class IsochroneRequest(BaseModel):
    longitude: float
    latitude: float
    profile: str = Field(default="walking", pattern="^(walking|cycling|driving)$")
    travel_times: list[int] = Field(default_factory=lambda: [5, 10, 15, 20])


@app.post("/api/isochrones")
async def fetch_isochrones_endpoint(req: IsochroneRequest):
    """Fetch raw isochrone polygons for a single point."""
    try:
        from server.warp.mapbox_client import MapboxClient

        client = MapboxClient()
        return await client.fetch_isochrones(
            lon=req.longitude,
            lat=req.latitude,
            profile=req.profile,
            minutes=req.travel_times,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── Shared: tile & glyph proxies ────────────────────────────────────
@app.get("/tiles/{z}/{x}/{y}")
async def tile_proxy(z: int, x: int, y: int):
    url = (
        f"https://api.mapbox.com/v4/mapbox.mapbox-streets-v8"
        f"/{z}/{x}/{y}.vector.pbf"
    )
    resp = await app.state.http_client.get(url, params={"access_token": MAPBOX_TOKEN})
    if resp.status_code == 404:
        return Response(status_code=204)
    resp.raise_for_status()
    return Response(
        content=resp.content,
        media_type="application/x-protobuf",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/glyphs/{fontstack}/{range_pbf}")
async def glyph_proxy(fontstack: str, range_pbf: str):
    encoded = quote(fontstack, safe=",")
    url = f"https://api.mapbox.com/fonts/v1/mapbox/{encoded}/{range_pbf}"
    resp = await app.state.http_client.get(url, params={"access_token": MAPBOX_TOKEN})
    resp.raise_for_status()
    return Response(
        content=resp.content,
        media_type="application/x-protobuf",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ── Portfolio landing page ───────────────────────────────────────────
_ROOT = Path(__file__).parent.parent


@app.get("/")
def portfolio():
    return FileResponse(_ROOT / "static" / "portfolio" / "index.html")


# ── PWA assets ──────────────────────────────────────────────────────
@app.get("/manifest.json")
def pwa_manifest():
    return FileResponse(
        _ROOT / "static" / "manifest.json",
        media_type="application/manifest+json",
    )


@app.get("/sw.js")
def pwa_service_worker():
    return FileResponse(
        _ROOT / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


app.mount("/icons", StaticFiles(directory=str(_ROOT / "static" / "icons")), name="icons")


# ── Static frontends ────────────────────────────────────────────────
app.mount("/meetup", StaticFiles(directory=str(_ROOT / "static" / "meetup"), html=True), name="meetup")
app.mount("/warp", StaticFiles(directory=str(_ROOT / "static" / "warp"), html=True), name="warp")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server.app:app", host="0.0.0.0", port=8000, reload=True)
