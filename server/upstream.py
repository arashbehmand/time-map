"""
The one way to call Mapbox.

Errors never carry the request URL (which holds the access token and, for Matrix,
participant coordinates) or the upstream body. Callers get a fixed-message ApiError.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from server.errors import ApiError, Busy, UpstreamError, UpstreamTimeout

log = logging.getLogger("time_map.upstream")


async def mapbox_send(
    client: httpx.AsyncClient,
    url: str,
    *,
    api: str,
    profile: str = "-",
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Send a GET and return the response, whatever its status. Transport errors become ApiErrors."""
    try:
        return await client.get(url, params=params)
    except httpx.TimeoutException as exc:
        log.warning("upstream timeout api=%s profile=%s error=%s", api, profile, type(exc).__name__)
        raise UpstreamTimeout() from None
    except httpx.HTTPError as exc:
        log.warning("upstream transport api=%s profile=%s error=%s", api, profile, type(exc).__name__)
        raise UpstreamError() from None


def fail(resp: httpx.Response, *, api: str, profile: str = "-") -> ApiError:
    """Log a failed upstream status (no URL, no body) and build the error to raise."""
    log.warning("upstream status api=%s profile=%s status=%d", api, profile, resp.status_code)
    if resp.status_code == 429:
        return Busy("The map service is busy. Please try again in a minute.")
    return UpstreamError()


def json_or_fail(resp: httpx.Response, *, api: str, profile: str = "-") -> Any:
    if resp.is_error:
        raise fail(resp, api=api, profile=profile)
    try:
        return resp.json()
    except ValueError:
        log.warning("upstream bad json api=%s profile=%s status=%d", api, profile, resp.status_code)
        raise UpstreamError() from None


async def mapbox_get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    api: str,
    profile: str = "-",
    params: dict[str, Any] | None = None,
) -> Any:
    resp = await mapbox_send(client, url, api=api, profile=profile, params=params)
    return json_or_fail(resp, api=api, profile=profile)
