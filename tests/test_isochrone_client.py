"""The isochrone client: distance contours, denoise, and what the cache key holds."""
import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from conftest import MockMapbox

from server.cache import BoundedTTLCache
from server.warp.mapbox_client import MapboxClient


class _OpenGuard:
    """Lets every request through (the guard has its own tests)."""

    @asynccontextmanager
    async def upstream(self, api, units=1, **_):
        yield


def _client(mapbox):
    http = httpx.AsyncClient(transport=httpx.MockTransport(mapbox))
    return MapboxClient(http, _OpenGuard(), "pk.test", BoundedTTLCache(maxsize=16, ttl=300))


def test_metres_and_denoise_are_sent_and_kept_apart_in_the_cache():
    mapbox = MockMapbox()
    c = _client(mapbox)

    async def run():
        a = await c.fetch_isochrones(-0.12, 51.5, "walking", [333, 667], unit="meters", denoise=0)
        b = await c.fetch_isochrones(-0.12, 51.5, "walking", [333, 667], unit="meters", denoise=0)
        m = await c.fetch_isochrones(-0.12, 51.5, "walking", [333, 667])
        return a, b, m

    a, b, m = asyncio.run(run())
    assert mapbox.count("isochrone") == 2            # the repeat was a cache hit
    first, second = mapbox.calls
    assert first.url.params["contours_meters"] == "333,667"
    assert first.url.params["denoise"] == "0"
    assert "contours_minutes" not in first.url.params
    assert second.url.params["contours_minutes"] == "333,667"
    assert "denoise" not in second.url.params        # the raw endpoint's default stays
    assert a is b
    assert [f["properties"]["contour"] for f in a["features"]] == [333, 667]
    assert m is not a


def test_contour_count_and_unit_are_checked():
    c = _client(MockMapbox())
    with pytest.raises(ValueError):
        asyncio.run(c.fetch_isochrones(0, 51, "walking", [1, 2, 3, 4, 5]))
    with pytest.raises(ValueError):
        asyncio.run(c.fetch_isochrones(0, 51, "walking", [1], unit="km"))
