import asyncio
import math
import os
from datetime import UTC, datetime, timedelta

# Fake, token-shaped secrets. Set before server.* is imported.
TILES_TOKEN = "pk.TILESTOKENtilestoken1234567890abcdef.SECRETA"
ROUTING_TOKEN = "pk.ROUTINGTOKENrouting1234567890abcdef.SECRETB"
os.environ["MAPBOX_TILES_TOKEN"] = TILES_TOKEN
os.environ["MAPBOX_ROUTING_TOKEN"] = ROUTING_TOKEN
os.environ.pop("MAPBOX_ACCESS_TOKEN", None)
os.environ.setdefault("ROUTER_PROVIDER", "euclidean")

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server.app import create_app  # noqa: E402
from server.guard.clock import Clock  # noqa: E402
from server.meetup.candidates import haversine_km  # noqa: E402
from server.meetup.routing import MatrixResult, Router, Snap  # noqa: E402
from server.settings import Settings  # noqa: E402

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
    """Answer a Matrix request from straight-line distance at 5 km/h. Each destination
    "snaps" to itself, on a street called Test Street."""
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
        "sources": [{"name": "Test Street", "location": list(coords[s]), "distance": 0} for s in src],
        "destinations": [{"name": "Test Street", "location": list(coords[d]), "distance": 0} for d in dst],
    }


class FixedRouter(Router):
    """
    A router answered by plain functions, for solver tests:
      fn(mode, origin, destination) -> minutes (inf for no route);
      snap_fn(mode, destination) -> Snap (default: the destination itself);
      unresolved_fn(mode, destination) -> True to drop it as unchecked.
    `calls` counts duration_matrix calls (one per mode per stage).
    """

    def __init__(self, fn, snap_fn=None, unresolved_fn=None):
        self.fn = fn
        self.snap_fn = snap_fn
        self.unresolved_fn = unresolved_fn
        self.calls = 0

    async def duration_matrix(self, mode, origins, destinations):
        self.calls += 1
        minutes = np.array([[self.fn(mode, o, d) for d in destinations] for o in origins],
                           dtype=float).reshape(len(origins), len(destinations))
        snaps = [self.snap_fn(mode, d) if self.snap_fn else Snap(*d) for d in destinations]
        unresolved = np.array([bool(self.unresolved_fn and self.unresolved_fn(mode, d))
                               for d in destinations], dtype=bool)
        return MatrixResult(minutes, snaps, unresolved)


SPEEDS = {"walking": 4.8, "cycling": 15.0, "driving": 30.0, "driving-traffic": 22.0}


def straight_line(mode, o, d):
    """Minutes at the mode's average speed, as EuclideanRouter."""
    return haversine_km(*o, *d) / SPEEDS[mode] * 60.0


def bumpy(mode, o, d):
    """Straight-line minutes with a detour of up to 35% that depends on the destination."""
    lon, lat = d
    return straight_line(mode, o, d) * (1 + 0.35 * (0.5 + 0.5 * math.sin(lon * 400 + 1.3)
                                                    * math.cos(lat * 350 + 0.7)))


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


class FakeClock(Clock):
    """Monotonic and wall time that only move when told to (or when something sleeps)."""

    def __init__(self, start: datetime | None = None):
        self.t = 1000.0
        self.start = start or datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def utcnow(self) -> datetime:
        return self.start + timedelta(seconds=self.t - 1000.0)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.t += seconds


GENEROUS = {api: 10_000_000 for api in ("matrix", "isochrone", "tiles", "glyphs", "geocode")}


@pytest.fixture
def mapbox():
    return MockMapbox()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        router_provider="euclidean",
        usage_db_path=str(tmp_path / "usage.sqlite3"),
        monthly_budget=dict(GENEROUS),
        daily_budget=dict(GENEROUS),
        per_ip_per_min={g: 10_000 for g in Settings().per_ip_per_min},
    )


@pytest.fixture
def make_client(mapbox, clock):
    """Build a TestClient for given settings; closes them all at the end."""
    opened = []

    def _make(settings: Settings, raise_errors: bool = False) -> TestClient:
        app = create_app(settings, transport=httpx.MockTransport(mapbox), clock=clock)
        c = TestClient(app, raise_server_exceptions=raise_errors)
        c.__enter__()
        opened.append(c)
        return c

    yield _make
    for c in opened:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client, settings):
    return make_client(settings)


@pytest.fixture
def matrix_client(make_client, settings):
    return make_client(settings.with_(router_provider="mapbox"))


def assert_clean(text: str) -> None:
    for bad in FORBIDDEN:
        assert bad not in text, f"{bad!r} leaked"
