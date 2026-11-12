import hashlib
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
from server.errors import ApiError, CatchAll, NotFound, install_error_handlers
from server.geocode import Geocoder
from server.geocode import router as geocode_router
from server.guard.budget import UsageStore
from server.guard.clock import Clock
from server.guard.guard import UpstreamGuard, per_ip
from server.logging_setup import configure_logging, set_secrets
from server.meetup.routes import router as meetup_router
from server.meetup.routing import build_router as build_meetup_router
from server.middleware import BodyLimit
from server.settings import DEFAULT_USAGE_DB_PATH, Settings, on_railway
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
# A valid glyph PBF with one empty font stack: MapLibre parses it as "no glyphs here".
# (Not zero bytes, so no client mistakes it for a missing body.)
EMPTY_GLYPH_RANGE = b"\x0a\x00"
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


@proxy_router.get("/glyphs/{fontstack}/{range_pbf}")
async def glyph_proxy(request: Request, fontstack: str, range_pbf: str):
    m = _GLYPH_RANGE.match(range_pbf)
    if fontstack not in FONTSTACKS or not m:
        raise NotFound()
    start, end = int(m.group(1)), int(m.group(2))
    if start % 256 or end != start + 255 or end > 65535:
        raise NotFound()
    state = request.app.state
    url = f"https://api.mapbox.com/fonts/v1/mapbox/{quote(fontstack, safe=',')}/{range_pbf}"
    # MapLibre fails the whole vector tile (geometry too) when a glyph range fails to load,
    # and remembers the failure until the page reloads. So a refused or failed glyph range
    # is answered with an empty range: labels go missing, the map still draws.
    try:
        state.guard.enforce_per_ip(request, "glyphs")
        async with state.guard.upstream("glyphs", 1):
            resp = await mapbox_send(
                state.http_client, url, api="glyphs", params={"access_token": MAPBOX_TILES_TOKEN}
            )
        if resp.is_error:
            raise fail(resp, api="glyphs")
    except ApiError as exc:
        log.warning("glyphs: serving an empty range (%s)", exc.code)
        return Response(content=EMPTY_GLYPH_RANGE, media_type="application/x-protobuf",
                        headers={"Cache-Control": "no-store"})
    return Response(
        content=resp.content,
        media_type="application/x-protobuf",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ── Pages and PWA assets ────────────────────────────────────────────
page_router = APIRouter()

# Our own pages, scripts and styles: the browser revalidates them on every load (a 304 when
# unchanged), so a deploy shows up at once. Without this, browsers cache them heuristically
# and could mix an old module with a new one.
APP_CACHE = {"Cache-Control": "no-cache"}


class AppFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


@page_router.get("/")
def portfolio():
    return FileResponse(_ROOT / "static" / "portfolio" / "index.html", headers=APP_CACHE)


@page_router.get("/manifest.json")
def pwa_manifest():
    return FileResponse(
        _ROOT / "static" / "manifest.json",
        media_type="application/manifest+json",
        headers=APP_CACHE,
    )


def static_build_id(root: Path) -> str:
    """A short hash of every file under static/ except sw.js: it changes exactly when the
    shipped files change, so each deploy gets its own service-worker cache."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel == "sw.js":
            continue
        digest.update(rel.encode() + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:12]


@page_router.get("/sw.js")
def pwa_service_worker(request: Request):
    source = (_ROOT / "static" / "sw.js").read_text()
    return Response(
        content=source.replace("__BUILD__", request.app.state.build_id),
        media_type="application/javascript",
        headers=APP_CACHE,
    )


# ── App factory ──────────────────────────────────────────────────────
def _startup_warnings(settings: Settings) -> None:
    workers = os.environ.get("WEB_CONCURRENCY", "").strip()
    if workers.isdigit() and int(workers) > 1:
        # The start commands pass --workers 1, which wins over this variable; warn only.
        log.warning(
            "WEB_CONCURRENCY=%s is set, but limits and budgets are per process: keep "
            "--workers 1 on the start command", workers,
        )
    for api, monthly in sorted(settings.monthly_budget.items()):
        if monthly <= 0 or settings.daily(api) <= 0:
            effect = ("glyph ranges will be served empty, so maps have no labels" if api == "glyphs"
                      else "its requests will be refused")
            log.warning("%s has no budget: %s (set %s_MONTHLY_BUDGET)", api, effect, api.upper())
    if settings.usage_db_path == DEFAULT_USAGE_DB_PATH:
        log.warning(
            "USAGE_DB_PATH is the default (%s in the working directory). Without a persistent "
            "volume, this month's spend counters reset on every redeploy.", DEFAULT_USAGE_DB_PATH,
        )
    if settings.trusted_proxy_hops == 0 and on_railway():
        log.warning("TRUSTED_PROXY_HOPS=0 on Railway: every visitor shares one per-IP bucket")


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

        _startup_warnings(settings)
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
            # Small on purpose: entries are full polygon sets, and repeats come within seconds.
            BoundedTTLCache(maxsize=64, ttl=300, clock=clock.monotonic),
        )
        state.meetup_router = build_meetup_router(
            settings.router_provider, token=MAPBOX_ROUTING_TOKEN,
            client=state.http_client, guard=state.guard,
        )
        state.geocoder = Geocoder(state.http_client, state.guard, settings, clock)
        usage_task = asyncio.create_task(state.guard.usage_loop())

        yield

        usage_task.cancel()
        state.guard.log_usage("at shutdown")
        await state.http_client.aclose()
        state.usage.close()

    app = FastAPI(title="Time-Map", lifespan=lifespan)
    app.state.build_id = static_build_id(_ROOT / "static")
    install_error_handlers(app)
    app.add_middleware(GZipMiddleware, minimum_size=500)
    # Same-origin pages only: no CORS middleware, so other sites cannot read our API.
    app.add_middleware(BodyLimit, max_bytes=settings.max_body_bytes)
    app.add_middleware(CatchAll)  # added last, so it wraps the others

    app.include_router(warp_router, prefix="/api")
    app.include_router(meetup_router, prefix="/api/meetup")
    app.include_router(geocode_router, prefix="/api")
    app.include_router(proxy_router)
    app.include_router(page_router)

    app.mount("/icons", AppFiles(directory=str(_ROOT / "static" / "icons")), name="icons")
    app.mount("/shared", AppFiles(directory=str(_ROOT / "static" / "shared")), name="shared")
    app.mount("/meetup", AppFiles(directory=str(_ROOT / "static" / "meetup"), html=True),
              name="meetup")
    app.mount("/warp", AppFiles(directory=str(_ROOT / "static" / "warp"), html=True),
              name="warp")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server.app:app", host="0.0.0.0", port=8000, reload=True)
