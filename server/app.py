import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi import Path as PathParam
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from server.cache import BoundedTTLCache
from server.config import MAPBOX_ROUTING_TOKEN, MAPBOX_TILES_TOKEN, SECRETS
from server.errors import ApiError, NotFound, install_error_handlers
from server.guard.budget import UsageStore
from server.guard.clock import Clock
from server.guard.guard import UpstreamGuard, per_ip
from server.logging_setup import configure_logging, set_secrets
from server.meetup.routes import router as meetup_router
from server.meetup.routing import build_router as build_meetup_router
from server.middleware import BodyLimit
from server.settings import Settings
from server.upstream import fail, mapbox_send
from server.warp.mapbox_client import MapboxClient
from server.warp.routes import router as warp_router

# ── Logging ──────────────────────────────────────────────────────────
set_secrets(SECRETS)
configure_logging()
log = logging.getLogger("time_map.app")

_ROOT = Path(__file__).parent.parent


def make_http_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """The one outbound client."""
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(20.0, connect=10.0),
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
    )


# ── Tile & glyph proxies ────────────────────────────────────────────
proxy_router = APIRouter()

MAX_TILE_ZOOM = 16
# The only font stacks the two map styles use.
FONTSTACKS = {
    "Open Sans Regular,Arial Unicode MS Regular",
    "Open Sans Bold,Arial Unicode MS Bold",
}
_GLYPH_RANGE = re.compile(r"^(\d{1,5})-(\d{1,5})\.pbf$")


@proxy_router.get("/tiles/{z}/{x}/{y}", dependencies=[per_ip("tiles")])
async def tile_proxy(
    request: Request,
    z: int = PathParam(ge=0, le=MAX_TILE_ZOOM),
    x: int = PathParam(ge=0),
    y: int = PathParam(ge=0),
):
    if x >= 2**z or y >= 2**z:
        raise ApiError("Invalid tile.", status=422, code="invalid_request")
    state = request.app.state
    url = f"https://api.mapbox.com/v4/mapbox.mapbox-streets-v8/{z}/{x}/{y}.vector.pbf"
    async with state.guard.upstream("tiles", 1):
        resp = await mapbox_send(
            state.http_client, url, api="tiles", params={"access_token": MAPBOX_TILES_TOKEN}
        )
    if resp.status_code == 404:
        return Response(status_code=204)
    if resp.is_error:
        raise fail(resp, api="tiles")
    return Response(
        content=resp.content,
        media_type="application/x-protobuf",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@proxy_router.get("/glyphs/{fontstack}/{range_pbf}", dependencies=[per_ip("glyphs")])
async def glyph_proxy(request: Request, fontstack: str, range_pbf: str):
    m = _GLYPH_RANGE.match(range_pbf)
    if fontstack not in FONTSTACKS or not m:
        raise NotFound()
    start, end = int(m.group(1)), int(m.group(2))
    if start % 256 or end != start + 255 or end > 65535:
        raise NotFound()
    state = request.app.state
    url = f"https://api.mapbox.com/fonts/v1/mapbox/{quote(fontstack, safe=',')}/{range_pbf}"
    async with state.guard.upstream("glyphs", 1):
        resp = await mapbox_send(
            state.http_client, url, api="glyphs", params={"access_token": MAPBOX_TILES_TOKEN}
        )
    if resp.is_error:
        raise fail(resp, api="glyphs")
    return Response(
        content=resp.content,
        media_type="application/x-protobuf",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ── Pages and PWA assets ────────────────────────────────────────────
page_router = APIRouter()


@page_router.get("/")
def portfolio():
    return FileResponse(_ROOT / "static" / "portfolio" / "index.html")


@page_router.get("/manifest.json")
def pwa_manifest():
    return FileResponse(
        _ROOT / "static" / "manifest.json",
        media_type="application/manifest+json",
    )


@page_router.get("/sw.js")
def pwa_service_worker():
    return FileResponse(
        _ROOT / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


# ── App factory ──────────────────────────────────────────────────────
def _check_single_worker() -> None:
    workers = os.environ.get("WEB_CONCURRENCY", "").strip()
    if workers and workers.isdigit() and int(workers) > 1:
        # Startup fails and uvicorn exits: per-process limits would multiply with workers.
        raise RuntimeError(
            f"WEB_CONCURRENCY={workers}: limits and budgets are per process; run one worker"
        )


def create_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    clock = clock or Clock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio

        _check_single_worker()
        state = app.state
        state.settings = settings
        state.http_client = make_http_client(transport)
        state.usage = UsageStore(
            settings.usage_db_path,
            daily={api: settings.daily(api) for api in settings.monthly_budget},
            monthly=dict(settings.monthly_budget),
            clock=clock,
        )
        state.guard = UpstreamGuard(settings, state.usage, clock)
        state.solve_slots = asyncio.Semaphore(settings.max_concurrent_solves)
        state.isochrones = MapboxClient(
            state.http_client, state.guard, MAPBOX_ROUTING_TOKEN,
            BoundedTTLCache(maxsize=256, ttl=300, clock=clock.monotonic),
        )
        state.meetup_router = build_meetup_router(
            settings.router_provider, token=MAPBOX_ROUTING_TOKEN,
            client=state.http_client, guard=state.guard,
        )
        usage_task = asyncio.create_task(state.guard.usage_loop())

        yield

        usage_task.cancel()
        state.guard.log_usage("at shutdown")
        await state.http_client.aclose()
        state.usage.close()

    app = FastAPI(title="Time-Map", lifespan=lifespan)
    install_error_handlers(app)
    app.add_middleware(GZipMiddleware, minimum_size=500)
    # Same-origin pages only: no CORS middleware, so other sites cannot read our API.
    app.add_middleware(BodyLimit, max_bytes=settings.max_body_bytes)

    app.include_router(warp_router, prefix="/api")
    app.include_router(meetup_router, prefix="/api/meetup")
    app.include_router(proxy_router)
    app.include_router(page_router)

    app.mount("/icons", StaticFiles(directory=str(_ROOT / "static" / "icons")), name="icons")
    app.mount("/meetup", StaticFiles(directory=str(_ROOT / "static" / "meetup"), html=True),
              name="meetup")
    app.mount("/warp", StaticFiles(directory=str(_ROOT / "static" / "warp"), html=True),
              name="warp")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server.app:app", host="0.0.0.0", port=8000, reload=True)
