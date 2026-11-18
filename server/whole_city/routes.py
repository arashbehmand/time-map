"""Whole city's data (#28): one build's time-map files, behind the tables' gate
(server/transit/whole_city.py), checked on every request. The service worker keeps them after a
view for offline use and drops them when this answers 404 (static/sw.js); offline, the page
shows them only until the build's valid-until date (static/shared/offline.js)."""
from fastapi import APIRouter, Request, Response

from server.errors import ApiError, NotFound
from server.guard.guard import per_ip
from server.local import is_local
from server.transit.provider import WINDOWS
from server.transit.whole_city import FILES, SVG_CSP, is_mode_file

router = APIRouter(tags=["whole-city"])


def _accepts_gzip(request: Request) -> bool:
    for part in request.headers.get("accept-encoding", "").split(","):
        name, _, params = part.strip().partition(";")
        if name.strip().lower() in ("gzip", "*") and params.replace(" ", "") not in ("q=0", "q=0.0"):
            return True
    return False


def _matches(header: str, etag: str) -> bool:
    """If-None-Match: a list of tags, weak or strong (weak comparison, as for GET)."""
    tags = {t.strip().removeprefix("W/") for t in header.split(",")}
    return "*" in tags or etag in tags


@router.get("/whole-city/{window}/{name}", dependencies=[per_ip("whole_city")])
async def whole_city_file(window: str, name: str, request: Request):
    if window not in WINDOWS or name not in FILES:
        raise NotFound()
    state = request.app.state
    blob = state.whole_city.file(state.transit, window, name, local=is_local(request))
    if blob is None:
        raise ApiError("The Whole city map isn't available here.", status=404, code="whole_city_unavailable")
    return _send(blob, request)


@router.get("/whole-city/{window}/modes/{name}", dependencies=[per_ip("whole_city")])
async def whole_city_mode_file(window: str, name: str, request: Request):
    """The travel modes (#23): the combinations of modes the window's modes.json lists, under the map's
    own gate."""
    if window not in WINDOWS or not is_mode_file(name):
        raise NotFound()
    state = request.app.state
    blob = state.whole_city.mode_file(state.transit, window, name, local=is_local(request))
    if blob is None:
        raise ApiError("The travel modes aren't available here.", status=404, code="whole_city_modes_unavailable")
    return _send(blob, request)


def _send(blob, request: Request) -> Response:
    # The gzip copy was made once at load; GZipMiddleware leaves a response with
    # Content-Encoding alone.
    gz = blob.gz is not None and _accepts_gzip(request)
    etag = blob.gz_etag if gz else blob.etag
    headers = {"Cache-Control": "no-cache", "ETag": etag, "X-Content-Type-Options": "nosniff",
               "Vary": "Accept-Encoding"}
    if blob.media_type == "image/svg+xml":
        headers["Content-Security-Policy"] = SVG_CSP
    if _matches(request.headers.get("if-none-match", ""), etag):
        return Response(status_code=304, headers=headers)
    if gz:
        headers["Content-Encoding"] = "gzip"
        return Response(content=blob.gz, media_type=blob.media_type, headers=headers)
    return Response(content=blob.body, media_type=blob.media_type, headers=headers)
