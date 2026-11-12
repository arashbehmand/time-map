import json
import math
import os

# Fake, token-shaped secrets. Set before server.* is imported.
TILES_TOKEN = "pk.TILESTOKENtilestoken1234567890abcdef.SECRETA"
ROUTING_TOKEN = "pk.ROUTINGTOKENrouting1234567890abcdef.SECRETB"
os.environ["MAPBOX_TILES_TOKEN"] = TILES_TOKEN
os.environ["MAPBOX_ROUTING_TOKEN"] = ROUTING_TOKEN
os.environ.pop("MAPBOX_ACCESS_TOKEN", None)
os.environ.setdefault("ROUTER_PROVIDER", "euclidean")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server.app as app_module  # noqa: E402
from server.meetup.candidates import haversine_km  # noqa: E402

BODY_MARKER = "UPSTREAM-BODY-MARKER"
FORBIDDEN = (TILES_TOKEN, ROUTING_TOKEN, "access_token", "api.mapbox.com", BODY_MARKER)


def circle_isochrones(lon: float, lat: float, minutes: list[int]) -> dict:
    """A FeatureCollection of circular contours, shaped like Mapbox's reply."""
    feats = []
    for m in minutes:
        r_deg = m * 0.004
        ring = [
            [lon + r_deg * math.cos(t) / math.cos(math.radians(lat)), lat + r_deg * math.sin(t)]
            for t in (2 * math.pi * i / 64 for i in range(65))
        ]
        feats.append({
            "type": "Feature",
            "properties": {"contour": m},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        })
    return {"type": "FeatureCollection", "features": feats}


def matrix_reply(request: httpx.Request) -> dict:
    """Answer a Matrix request from straight-line distance at 5 km/h."""
    path = request.url.path
    coord_str = path.rsplit("/", 1)[-1]
    coords = [tuple(map(float, c.split(","))) for c in coord_str.split(";")]
    src = [int(i) for i in request.url.params["sources"].split(";")]
    dst = [int(i) for i in request.url.params["destinations"].split(";")]
    return {
        "code": "Ok",
        "durations": [
            [haversine_km(*coords[s], *coords[d]) / 5.0 * 3600 for d in dst] for s in src
        ],
    }


class MockMapbox:
    """Routes each Mapbox API to a behaviour: 'ok', a status code, or a callable."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.behaviour: dict[str, object] = {}

    def api_of(self, request: httpx.Request) -> str:
        p = request.url.path
        if p.startswith("/isochrone/"):
            return "isochrone"
        if p.startswith("/directions-matrix/"):
            return "matrix"
        if p.startswith("/v4/"):
            return "tiles"
        if p.startswith("/fonts/"):
            return "glyphs"
        if request.url.host.endswith("nominatim.openstreetmap.org"):
            return "geocode"
        return "other"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        api = self.api_of(request)
        b = self.behaviour.get(api, "ok")
        if callable(b):
            return b(request)
        if isinstance(b, int):
            return httpx.Response(
                b, text=f"{BODY_MARKER} url={request.url}", request=request
            )
        return self.ok(api, request)

    def ok(self, api: str, request: httpx.Request) -> httpx.Response:
        if api == "isochrone":
            lon, lat = map(float, request.url.path.rsplit("/", 1)[-1].split(","))
            minutes = [int(m) for m in request.url.params["contours_minutes"].split(",")]
            return httpx.Response(200, json=circle_isochrones(lon, lat, minutes))
        if api == "matrix":
            return httpx.Response(200, json=matrix_reply(request))
        if api in ("tiles", "glyphs"):
            return httpx.Response(200, content=b"\x1a\x00pbf")
        if api == "geocode":
            return httpx.Response(200, json=[{"lat": "51.5", "lon": "-0.12"}])
        return httpx.Response(404)

    def count(self, api: str | None = None) -> int:
        return sum(1 for r in self.calls if api is None or self.api_of(r) == api)


@pytest.fixture
def mapbox(monkeypatch):
    mock = MockMapbox()
    from server.warp import mapbox_client

    mapbox_client._iso_cache._store.clear()
    monkeypatch.setattr(
        app_module, "make_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(mock)),
    )
    return mock


@pytest.fixture
def client(mapbox):
    with TestClient(app_module.app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def matrix_client(mapbox, monkeypatch):
    monkeypatch.setenv("ROUTER_PROVIDER", "mapbox")
    with TestClient(app_module.app, raise_server_exceptions=False) as c:
        yield c


def assert_clean(text: str) -> None:
    for bad in FORBIDDEN:
        assert bad not in text, f"{bad!r} leaked"


def body_text(resp) -> str:
    return resp.text if isinstance(resp.text, str) else json.dumps(resp.json())
