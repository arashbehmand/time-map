"""POST /api/travel-time-field and GET /api/transit/status, with Mapbox mocked and a tiny
transit build (tests/tiny_transit.py)."""
import asyncio
from contextlib import asynccontextmanager

import httpx
import numpy as np
import pytest
from conftest import MockMapbox
from tiny_transit import A, C, make_build

from server.cache import BoundedTTLCache
from server.transit.provider import TransitHolder
from server.warp import field as F
from server.warp.mapbox_client import MapboxClient
from server.warp.travel_field import DEGRADED_TTL_S, TravelFieldService, default_window

FIELD = "/api/travel-time-field"


@pytest.fixture
def transit_client(make_client, settings, tmp_path):
    """A cleared build: served to any client."""
    build = make_build(tmp_path, cleared=True)
    return make_client(settings.with_(transit_dir=str(build), transit_block_startup=True))


@pytest.fixture
def dev_app(make_client, settings, tmp_path):
    """An uncleared build under TRANSIT_ALLOW_UNCLEARED: for this machine only."""
    build = make_build(tmp_path)
    return make_client(settings.with_(transit_dir=str(build), transit_allow_uncleared=True,
                                      transit_block_startup=True)).app


def _decode(body):
    return F.decode(body["minutes_b64"], (body["ny"], body["nx"]))


def _at(body, minutes, lon, lat):
    frame = F.LocalFrame(body["origin"]["lon"], body["origin"]["lat"])
    x, y = frame.to_local(lon, lat)
    return minutes[round((y - body["y0"]) / body["cell"]), round((x - body["x0"]) / body["cell"])]


def test_walking_field_from_two_distance_requests(mapbox, client):
    resp = client.post(FIELD, json={"longitude": -0.12345678, "latitude": 51.5, "mode": "walking"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["origin"] == {"lon": -0.1235, "lat": 51.5}
    assert body["sources"] == "mapbox" and body["limit_minutes"] == 60 and body["approximate"]
    assert mapbox.count("isochrone") == 2 and mapbox.count("matrix") == 0
    for call in mapbox.calls:
        assert "contours_meters" in call.url.params and call.url.params["denoise"] == "0"
    m, filled = _decode(body)
    j, i = round(-body["y0"] / body["cell"]), round(-body["x0"] / body["cell"])
    assert m[j, i] == 0 and not filled.any()
    assert 59 <= np.nanmax(m) <= 60.1                       # 4 km at 4 km/h
    assert client.post(FIELD, json={"longitude": -0.12345678, "latitude": 51.5}).json() == body
    assert mapbox.count("isochrone") == 2                   # the repeat was cached


@pytest.mark.parametrize("mode,cell", [("cycling", 200), ("driving", 400)])
def test_cycling_and_driving_use_time_contours(mapbox, client, mode, cell):
    body = client.post(FIELD, json={"longitude": -0.12, "latitude": 51.5, "mode": mode}).json()
    assert body["cell"] == cell and mapbox.count("isochrone") == 2
    assert all("contours_minutes" in c.url.params for c in mapbox.calls)


def test_no_contours_is_no_isochrone(mapbox, client):
    mapbox.behaviour["isochrone"] = lambda r: httpx.Response(200, json={"type": "FeatureCollection", "features": []})
    resp = client.post(FIELD, json={"longitude": -0.12, "latitude": 51.5})
    assert resp.status_code == 422 and resp.json()["code"] == "no_isochrone"


def test_transit_field_composes_without_matrix(mapbox, transit_client):
    body = transit_client.post(FIELD, json={"longitude": A[0], "latitude": A[1], "mode": "transit",
                                            "window": "wd08"}).json()
    assert body["mode"] == "transit" and body["window"] == "wd08"
    assert body["window_label"] == "a weekday, 08:00–09:00"
    assert body["sources"] == "mapbox+tables" and body["limit_minutes"] == 180
    assert body["development_only"] is False and body["valid_until"]
    assert body["service_date"] == "2026-12-09"           # the weekday the times are for (#30)
    assert body["service_dates"] == {"weekday": "2026-12-09", "saturday": "2026-12-12"}
    assert "Powered by TfL Open Data" in body["credits"]
    assert mapbox.count("isochrone") == 2 and mapbox.count("matrix") == 0
    names = {s["name"]: s["minutes"] for s in body["stations"]}
    assert names == {"Alpha Road": 0.0, "Bravo": 5.0}
    m, _ = _decode(body)
    assert _at(body, m, -0.0800, 51.5000) <= 5.5          # Bravo, 2.8 km east: by the line
    assert _at(body, m, -0.1200, 51.5000) == 0


def test_walk_then_transit_at_one_pin_costs_two_requests(mapbox, transit_client):
    pin = {"longitude": A[0], "latitude": A[1]}
    walk = transit_client.post(FIELD, json={**pin, "mode": "walking"})
    assert walk.status_code == 200 and walk.json()["service_date"] is None and walk.json()["service_dates"] is None
    assert transit_client.post(FIELD, json={**pin, "mode": "transit", "window": "wd13"}).status_code == 200
    sat = transit_client.post(FIELD, json={**pin, "mode": "transit", "window": "sa13"})
    assert sat.status_code == 200 and sat.json()["service_date"] == "2026-12-12"
    assert mapbox.count("isochrone") == 2 and mapbox.count("matrix") == 0


def test_outside_london_is_refused_before_any_call(mapbox, transit_client):
    resp = transit_client.post(FIELD, json={"longitude": -1.5, "latitude": 52.0, "mode": "transit"})
    assert resp.status_code == 422 and resp.json()["code"] == "outside_transit_area"
    assert mapbox.count() == 0


def test_without_tables_transit_is_unavailable(mapbox, client):
    resp = client.post(FIELD, json={"longitude": A[0], "latitude": A[1], "mode": "transit"})
    assert resp.status_code == 503 and resp.json()["code"] == "transit_unavailable"
    assert resp.json()["reason"] == "not_configured"
    assert client.get("/api/transit/status").json() == {
        "available": False, "loading": False, "reason": "not_configured",
        "whole_city": {"available": False, "reason": "not_configured"}}
    assert mapbox.count() == 0


def test_a_refused_near_field_still_gives_the_tables_and_is_kept_only_briefly(mapbox, transit_client, clock):
    pin = {"longitude": A[0], "latitude": A[1], "mode": "transit", "window": "wd08"}
    mapbox.behaviour["isochrone"] = 429
    body = transit_client.post(FIELD, json=pin).json()
    assert body["sources"] == "tables" and body["notes"] == ["near_field_missing"]
    calls = mapbox.count("isochrone")
    again = transit_client.post(FIELD, json=pin).json()       # an outage: not asked again at once
    assert again["notes"] == ["near_field_missing"] and mapbox.count("isochrone") == calls
    mapbox.behaviour.pop("isochrone")
    clock.t += DEGRADED_TTL_S + 1
    later = transit_client.post(FIELD, json=pin).json()       # then the full field
    assert later["sources"] == "mapbox+tables" and later["notes"] == []


def test_status_with_tables(transit_client):
    s = transit_client.get("/api/transit/status").json()
    assert s["available"] and not s["loading"] and not s["development_only"]
    assert [w["id"] for w in s["windows"]] == ["wd08", "wd13", "wd19", "sa13"]
    assert s["coverage"]["type"] in ("Polygon", "MultiPolygon")


@pytest.mark.parametrize("body", [
    {"longitude": 0, "latitude": 51, "mode": "flying"},
    {"longitude": 0, "latitude": 51, "mode": "transit", "window": "xx"},
    {"longitude": 0, "latitude": 89, "mode": "walking"},
    {"longitude": 0, "latitude": 51, "extra": 1},
])
def test_bad_bodies(mapbox, client, body):
    assert client.post(FIELD, json=body).status_code == 422
    assert mapbox.count() == 0


def test_per_ip_limit(make_client, settings, mapbox):
    c = make_client(settings.with_(per_ip_per_min={**settings.per_ip_per_min, "travel_time_field": 1}))
    assert c.post(FIELD, json={"longitude": -0.12, "latitude": 51.5}).status_code == 200
    assert c.post(FIELD, json={"longitude": -0.12, "latitude": 51.5}).status_code == 429


class _OpenGuard:
    @asynccontextmanager
    async def upstream(self, api, units=1, **_):
        await asyncio.sleep(0.01)
        yield


def test_walk_and_transit_asked_at_once_share_the_two_requests(tmp_path):
    """A new pin, walk and transit (and two windows) in flight together: still two calls."""
    mapbox = MockMapbox()
    http = httpx.AsyncClient(transport=httpx.MockTransport(mapbox))
    iso = MapboxClient(http, _OpenGuard(), "pk.test", BoundedTTLCache(16, 300))
    holder = TransitHolder(True)
    holder.load(str(make_build(tmp_path, cleared=True)), allow_uncleared=False, on_railway=False)
    service = TravelFieldService(iso, holder, asyncio.Semaphore(2), BoundedTTLCache(8, 300))

    async def run():
        return await asyncio.gather(
            service.get(A[0], A[1], "walking", None),
            service.get(A[0], A[1], "transit", "wd08"),
            service.get(A[0], A[1], "transit", "sa13"),
        )

    walk, t8, sa = asyncio.run(run())
    assert walk["mode"] == "walking" and t8["window"] == "wd08" and sa["window"] == "sa13"
    assert mapbox.count("isochrone") == 2


def test_concurrent_identical_requests_share_one_build():
    mapbox = MockMapbox()
    http = httpx.AsyncClient(transport=httpx.MockTransport(mapbox))
    iso = MapboxClient(http, _OpenGuard(), "pk.test", BoundedTTLCache(16, 300))
    service = TravelFieldService(iso, TransitHolder(False), asyncio.Semaphore(2), BoundedTTLCache(8, 300))

    async def run():
        return await asyncio.gather(*(service.get(-0.12, 51.5, "walking", None) for _ in range(5)))

    results = asyncio.run(run())
    assert all(r is results[0] for r in results)
    assert mapbox.count("isochrone") == 2


def test_default_window_follows_london_time():
    from datetime import datetime

    from server.transit.provider import LONDON
    assert default_window(datetime(2026, 11, 16, 8, 30, tzinfo=LONDON)) == "wd08"
    assert default_window(datetime(2026, 11, 16, 13, 0, tzinfo=LONDON)) == "wd13"
    assert default_window(datetime(2026, 11, 16, 21, 0, tzinfo=LONDON)) == "wd19"
    assert default_window(datetime(2026, 11, 15, 9, 0, tzinfo=LONDON)) == "sa13"


def _local(app):
    from fastapi.testclient import TestClient
    return TestClient(app, client=("127.0.0.1", 50000))


def test_an_uncleared_build_is_served_to_this_machine_only(dev_app):
    pin = {"longitude": A[0], "latitude": A[1], "mode": "transit", "window": "wd08"}
    from fastapi.testclient import TestClient
    remote = TestClient(dev_app)                                  # client host "testclient"
    resp = remote.post(FIELD, json=pin)
    assert resp.status_code == 503 and resp.json()["reason"] == "development_only"
    assert remote.get("/api/transit/status").json() == {
        "available": False, "loading": False, "reason": "development_only",
        "whole_city": {"available": False, "reason": "not_configured"}}
    local = _local(dev_app)
    assert local.get("/api/transit/status").json()["development_only"] is True
    assert local.post(FIELD, json=pin).status_code == 200
    # Through a proxy on this machine is not this machine.
    proxied = local.post(FIELD, json=pin, headers={"X-Forwarded-For": "203.0.113.9"})
    assert proxied.status_code == 503
    # Nor through one that names the client with the deployment's own header.
    dev_app.state.settings = dev_app.state.settings.with_(client_ip_header="cf-connecting-ip")
    assert local.post(FIELD, json=pin, headers={"CF-Connecting-IP": "203.0.113.9"}).status_code == 503


def test_inside_london_but_out_of_reach_is_its_own_answer(mapbox, transit_client):
    data = transit_client.app.state.transit.data
    pin = next((C[0], C[1] + k * 25 / 111_320) for k in range(1, 40)
               if data.covered(C[0], C[1] + k * 25 / 111_320)
               and not data.reachable_from(C[0], C[1] + k * 25 / 111_320))
    resp = transit_client.post(FIELD, json={"longitude": pin[0], "latitude": pin[1], "mode": "transit"})
    assert resp.status_code == 422 and resp.json()["code"] == "no_transit_access"
    assert mapbox.count() == 0


def test_whole_minutes_are_for_development_builds_only(transit_client, dev_app):
    pin = {"longitude": A[0], "latitude": A[1], "mode": "transit", "window": "wd08"}
    assert transit_client.post(FIELD, json={**pin, "smooth": False}).json()["smoothed"] is True
    local = _local(dev_app)
    raw = local.post(FIELD, json={**pin, "smooth": False}).json()
    smooth = local.post(FIELD, json=pin).json()
    assert raw["smoothed"] is False and smooth["smoothed"] is True
    m, _ = _decode(raw)
    far = m[np.isfinite(m)]
    assert far.size and np.allclose(far[far > 31], np.round(far[far > 31]))   # beyond 2 km: whole minutes


def test_only_walking_replies_are_kept_raw(mapbox, client):
    """A cycling reply can be megabytes parsed; the field cache keeps the field it built."""
    for mode in ("cycling", "walking"):
        assert client.post(FIELD, json={"longitude": -0.1, "latitude": 51.5, "mode": mode}).status_code == 200
    kept = {key[2] for key in client.app.state.field_isochrones.cache._data}
    assert kept == {"walking"}
    assert client.post(FIELD, json={"longitude": -0.1, "latitude": 51.5, "mode": "cycling"}).status_code == 200
    assert mapbox.count("isochrone") == 4                   # the cycling field itself was cached


# ── Uncertain patches (King's Cross's platforms) ─────────────────────
PLATFORMS = (-150, -450, 150, -150)        # local metres around the pin (y south)


def _platform_isochrones(request: httpx.Request) -> httpx.Response:
    """The mock's circles, with the platforms cut out of every contour but the last."""
    from conftest import circle_isochrones
    lon, lat = map(float, request.url.path.rsplit("/", 1)[-1].split(","))
    params = request.url.params
    if "contours_meters" in params:
        values, metric, last, metres = [int(m) for m in params["contours_meters"].split(",")], "distance", 4000, 1.0
    else:
        values, metric, last, metres = [int(m) for m in params["contours_minutes"].split(",")], "time", 60, 0.004 * 111_320
    reply = circle_isochrones(lon, lat, values, metric)
    frame = F.LocalFrame(lon, lat)
    x0, y0, x1, y1 = PLATFORMS
    hole = [[float(v) for v in frame.from_local(x, y)] for x, y in ((x0, y0), (x0, y1), (x1, y1), (x1, y0), (x0, y0))]
    for f, v in zip(reply["features"], values):
        if v != last and v * metres > 600:
            f["geometry"]["coordinates"].append(hole)
    return httpx.Response(200, json=reply)


def _patches(body):
    shape = (body["ny"], body["nx"])
    mask, drawn = F.decode_uncertain(body.get("uncertain"), shape)
    minutes, _ = F.decode(body["minutes_b64"], shape)
    js, is_ = np.nonzero(mask)
    xs = body["x0"] + is_ * body["cell"]
    ys = body["y0"] + js * body["cell"]
    return mask, drawn, minutes, xs, ys


@pytest.mark.parametrize("mode", ["walking", "cycling", "driving"])
def test_the_platforms_come_back_as_an_uncertain_patch(mapbox, client, mode):
    mapbox.behaviour["isochrone"] = _platform_isochrones
    body = client.post(FIELD, json={"longitude": -0.1233, "latitude": 51.5308, "mode": mode}).json()
    assert mapbox.count("isochrone") == 2 and mapbox.count("matrix") == 0
    mask, drawn, minutes, xs, ys = _patches(body)
    assert mask.any()
    x0, y0, x1, y1 = PLATFORMS
    assert ((xs >= x0) & (xs <= x1) & (ys >= y0) & (ys <= y1)).all()   # only the platforms
    assert (minutes[mask] > 45).all()                    # the provider's estimate, kept
    assert (drawn[mask] < 15).all()                      # drawn between the ground around them


def test_without_patches_there_is_no_key(mapbox, client):
    body = client.post(FIELD, json={"longitude": -0.12, "latitude": 51.5, "mode": "walking"}).json()
    assert "uncertain" not in body


def test_transit_never_lets_a_patch_walk_beat_the_tables(mapbox, transit_client):
    mapbox.behaviour["isochrone"] = _platform_isochrones
    pin = {"longitude": A[0], "latitude": A[1]}
    walk = transit_client.post(FIELD, json={**pin, "mode": "walking"}).json()
    body = transit_client.post(FIELD, json={**pin, "mode": "transit", "window": "wd08"}).json()
    assert mapbox.count("isochrone") == 2 and mapbox.count("matrix") == 0
    wmask, _, wmin, wxs, wys = _patches(walk)
    assert wmask.any()
    mask, _, minutes, _, _ = _patches(body)
    for x, y, t in zip(wxs, wys, wmin[wmask]):
        j, i = round((y - body["y0"]) / body["cell"]), round((x - body["x0"]) / body["cell"])
        # Either the tables' own time (never the patch's 50 minutes) or the walk, still uncertain.
        assert (minutes[j, i] < t and not mask[j, i]) or (mask[j, i] and minutes[j, i] == pytest.approx(t, abs=0.1))
