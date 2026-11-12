"""
Error responses.

Every error the API returns has the shape {"code": <stable code>, "detail": <fixed sentence>}.
Nothing from an exception's text, an upstream response or the request body is echoed back.
"""
from __future__ import annotations

import logging
import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("time_map.errors")


class ApiError(Exception):
    status = 500
    code = "internal_error"
    message = "Something went wrong. Please try again."
    default_headers: dict[str, str] = {}

    def __init__(
        self,
        message: str | None = None,
        *,
        status: int | None = None,
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        if message is not None:
            self.message = message
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        self.headers = {**self.default_headers, **(headers or {})}
        super().__init__(self.code)


class UpstreamError(ApiError):
    status = 502
    code = "upstream_error"
    message = "The map service is unavailable right now. Please try again."


class UpstreamTimeout(ApiError):
    status = 504
    code = "upstream_timeout"
    message = "The map service took too long to answer. Please try again."


class Busy(ApiError):
    status = 503
    code = "busy"
    message = "The server is busy. Please try again in a moment."
    default_headers = {"Retry-After": "30"}


class NoFeasiblePoint(ApiError):
    status = 422
    code = "no_feasible_meeting_point"
    message = "No meeting point works for everyone. Try raising the time limits or moving people closer."


class NoIsochrone(ApiError):
    status = 422
    code = "no_isochrone"
    message = "No travel-time area is available for this place."


class NotFound(ApiError):
    status = 404
    code = "not_found"
    message = "Not found."


def error_response(status: int, code: str, message: str, headers: dict[str, str] | None = None):
    return JSONResponse(
        {"code": code, "detail": message},
        status_code=status,
        headers={"Cache-Control": "no-store", **(headers or {})},
    )


def _validation_detail(exc: RequestValidationError) -> str:
    # Field paths and pydantic's short message only; never the submitted input.
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        msg = str(err.get("msg", "invalid value"))[:120]
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "Invalid request. " + "; ".join(parts)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError):
        return error_response(exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError):
        return error_response(422, "invalid_request", _validation_detail(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException):
        headers = dict(exc.headers or {})  # e.g. Allow on a 405
        if exc.status_code == 404:
            return error_response(404, "not_found", "Not found.", headers)
        if exc.status_code == 413:
            return error_response(413, "payload_too_large", "Request is too large.", headers)
        if exc.status_code == 405:
            return error_response(405, "method_not_allowed", "Method not allowed.", headers)
        return error_response(exc.status_code, "http_error", "Request failed.", headers)

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception):
        log_unhandled(exc)
        return error_response(500, "internal_error", ApiError.message)


def log_unhandled(exc: BaseException) -> None:
    """
    Log an unexpected error as its type and the code locations it passed through.
    The message is left out: it can carry request data (coordinates, queries) that the
    token redactor would not remove.
    """
    frames = traceback.extract_tb(exc.__traceback__)[-6:]
    where = " <- ".join(
        f"{_short(f.filename)}:{f.lineno} in {f.name}" for f in reversed(frames)
    )
    log.error("unhandled %s at %s", type(exc).__name__, where or "?")


def _short(path: str) -> str:
    for marker in ("/server/", "/site-packages/"):
        if marker in path:
            return marker.strip("/") + "/" + path.split(marker, 1)[1]
    return path.rsplit("/", 1)[-1]


class CatchAll:
    """
    Outermost app middleware: turns any unexpected exception into the fixed 500 response
    and logs it once (log_unhandled), so the server does not log the raw traceback again.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            log_unhandled(exc)
            if started:
                return  # too late for a response; the connection just ends
            response = error_response(500, "internal_error", ApiError.message)
            await response(scope, receive, send)
