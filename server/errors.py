"""
Error responses.

Every error the API returns has the shape {"code": <stable code>, "detail": <fixed sentence>}.
Nothing from an exception's text, an upstream response or the request body is echoed back.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("time_map.errors")


class ApiError(Exception):
    status = 500
    code = "internal_error"
    message = "Something went wrong. Please try again."

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
        self.headers = headers or {}
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
        if exc.status_code == 404:
            return error_response(404, "not_found", "Not found.")
        if exc.status_code == 413:
            return error_response(413, "payload_too_large", "Request is too large.")
        if exc.status_code == 405:
            return error_response(405, "method_not_allowed", "Method not allowed.")
        return error_response(exc.status_code, "http_error", "Request failed.")

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception):
        # Log the type and traceback (redacted by the log record factory), never the request.
        log.error("unhandled %s", type(exc).__name__, exc_info=exc)
        return error_response(500, "internal_error", ApiError.message)
