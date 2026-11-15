"""100 random solve requests against a misbehaving Matrix API: never an unhandled error,
and never a degraded answer without a note."""
import logging
import random
import zlib
from collections import Counter

import httpx
from conftest import assert_clean, matrix_reply

import server.meetup.solver as solver

ALLOWED = {200, 422, 429, 502, 503, 504}
FAULTS = ["ok", "nulls", "poison", "all_noroute", "malformed_once", "malformed_always",
          "429_once", "500_once", "500_always", "timeout_once", "far_snaps"]


def _key(lon, lat):
    return zlib.crc32(f"{lon:.6f},{lat:.6f}".encode())


class Faulty:
    """One request's Matrix behaviour; records what it did."""

    def __init__(self, fault, rng):
        self.fault = fault
        self.rng = rng
        self.calls = 0
        self.no_route_replies = 0
        self.bad_replies = 0

    def __call__(self, request):
        self.calls += 1
        coords = [tuple(map(float, c.split(","))) for c in request.url.path.rsplit("/", 1)[-1].split(";")]
        dst = [int(i) for i in request.url.params["destinations"].split(";")]
        f = self.fault
        if f == "all_noroute" or (f == "poison" and any(_key(*coords[d]) % 29 == 0 for d in dst)):
            self.no_route_replies += 1
            return httpx.Response(200, json={"code": "NoRoute", "message": "No route found"})
        if (f == "malformed_once" and self.calls == 1) or f == "malformed_always":
            self.bad_replies += 1
            body = matrix_reply(request)
            body["durations"][0] = body["durations"][0][:-1]
            return httpx.Response(200, json=body)
        if f == "429_once" and self.calls == 1:
            return httpx.Response(429, headers={"retry-after": "1"})
        if (f == "500_once" and self.calls == 1) or f == "500_always":
            return httpx.Response(500, text="upstream trouble")
        if f == "timeout_once" and self.calls == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        body = matrix_reply(request)
        if f == "nulls":
            for row in body["durations"]:
                for j in range(len(row)):
                    if self.rng.random() < 0.05:
                        row[j] = None
        if f == "far_snaps":
            for w in body["destinations"]:
                if _key(*w["location"]) % 4 == 0:
                    w["location"] = [w["location"][0], w["location"][1] + 0.004]  # ~450 m
        return httpx.Response(200, json=body)


def _body(rng):
    if rng.random() < 0.1:  # an invalid request now and then
        return rng.choice([
            {"participants": [{"id": "a", "lon": 0, "lat": 51.5, "mode": "walking"}]},
            {"participants": [{"id": "a", "lon": -0.1, "lat": 51.5, "mode": "walking"},
                              {"id": "b", "lon": 2.35, "lat": 48.86, "mode": "walking"}]},
            {"participants": [{"id": "a", "lon": -0.1, "lat": 51.5, "mode": "flying"},
                              {"id": "b", "lon": -0.1, "lat": 51.6, "mode": "walking"}]},
        ])
    n = rng.randint(2, 8)
    modes = ["walking", "cycling", "driving", "driving-traffic"]
    spread = rng.choice([0.01, 0.03, 0.06])
    return {
        "objective": rng.choice(["minimax", "sum"]),
        "participants": [
            {"id": f"p{i}", "lon": -0.12 + rng.uniform(-spread, spread) * 1.6,
             "lat": 51.51 + rng.uniform(-spread, spread), "mode": rng.choice(modes)}
            for i in range(n)
        ],
    }


def test_random_requests_never_fail_unhandled_and_never_hide_trouble(mapbox, matrix_client, clock, caplog):
    caplog.set_level(logging.DEBUG)
    rng = random.Random(11)
    seen = {}
    for n in range(100):
        fault = FAULTS[n % len(FAULTS)]
        behaviour = Faulty(fault, rng)
        mapbox.behaviour["matrix"] = behaviour
        clock.advance(61)  # a fresh rate window: the limiter is not what this test is about
        resp = matrix_client.post("/api/meetup/solve", json=_body(rng))
        body = resp.json()
        assert resp.status_code in ALLOWED, (fault, resp.status_code, body)
        assert body.get("code") != "internal_error", fault
        assert_clean(resp.text)
        seen[(fault, resp.status_code)] = seen.get((fault, resp.status_code), 0) + 1
        if resp.status_code != 200:
            continue
        diag, notes = body["diagnostics"], body["notes"]
        assert fault not in ("all_noroute", "malformed_always", "500_always"), (fault, diag)
        # A place left unchecked in the stages the answer came from is always said.
        if diag["dropped_no_route"] or diag["unchecked_for_budget"]:
            assert solver.NOTE_UNCHECKED in notes, (fault, diag)
        # NoRoute here always means a point that fails: it is dropped or left unchecked in
        # the answer's stages (and said), or it lost the closer search (and that is said).
        if behaviour.no_route_replies:
            assert diag["dropped_no_route"] + diag["unchecked_for_budget"] > 0 or \
                solver.NOTE_FINE_SKIPPED in notes, (fault, diag)
            assert solver.NOTE_NO_ROUTE in notes, (fault, diag)
        # An answer after a bad reply used the reply asked for again, and says so.
        if behaviour.bad_replies:
            assert diag["retries"] >= 1 and diag["bad_replies"] >= 1, (fault, diag)
            assert solver.NOTE_BAD_REPLY in notes, (fault, diag)
    assert_clean(caplog.text)
    # The run exercised both answers and refusals.
    assert any(code == 200 for _, code in seen) and any(code != 200 for _, code in seen)


def _station_body(rng, data):
    """A group with someone on public transport: pins at stations, a few outside the tables'
    area (Brighton) or far from every station, the rest on any road mode."""
    n = rng.randint(2, 8)
    people = []
    for i in range(n):
        s = data.stations[rng.randrange(len(data.stations))]
        lon, lat = s.lon + rng.uniform(-0.003, 0.003), s.lat + rng.uniform(-0.002, 0.002)
        mode = "transit" if i == 0 or rng.random() < 0.5 else \
            rng.choice(["walking", "cycling", "driving", "driving-traffic"])
        if rng.random() < 0.05:
            lon, lat = -0.137, 50.82
        people.append({"id": f"p{i}", "lon": lon, "lat": lat, "mode": mode})
    return {"participants": people, "window": rng.choice([None, "wd08", "wd13", "wd19", "sa13"])}


def test_random_station_requests_never_fail_unhandled_and_never_hide_trouble(
        make_client, settings, mapbox, clock, tmp_path, caplog):
    from tiny_transit import make_random_build

    import server.meetup.stations as st
    build = make_random_build(tmp_path, 40, seed=8)
    client = make_client(settings.with_(router_provider="mapbox", transit_dir=str(build),
                                        transit_block_startup=True))
    data = client.app.state.transit.data
    caplog.set_level(logging.DEBUG)
    rng = random.Random(12)
    seen = {}
    for n in range(100):
        fault = FAULTS[n % len(FAULTS)]
        behaviour = Faulty(fault, rng)
        mapbox.behaviour["matrix"] = behaviour
        mapbox.calls.clear()
        clock.advance(61)
        body = _station_body(rng, data)
        resp = client.post("/api/meetup/solve", json=body)
        out = resp.json()
        assert resp.status_code in ALLOWED, (fault, resp.status_code, out)
        assert out.get("code") != "internal_error", fault
        assert_clean(resp.text)
        road = sum(1 for p in body["participants"] if p["mode"] != "transit")
        assert mapbox.count("matrix") <= 2 * road, (fault, mapbox.count("matrix"), road)
        # Each road person is asked about alone, and in at most two requests.
        starts = Counter()
        for call in mapbox.calls:
            if mapbox.api_of(call) == "matrix":
                coords = call.url.path.rsplit("/", 1)[-1].split(";")
                sources = call.url.params["sources"].split(";")
                assert len(sources) == 1, (fault, sources)
                starts[coords[int(sources[0])]] += 1
        assert all(v <= 2 for v in starts.values()), (fault, starts)
        seen[(fault, resp.status_code)] = seen.get((fault, resp.status_code), 0) + 1
        if resp.status_code != 200:
            continue
        diag, notes = out["diagnostics"], out["notes"]
        assert diag["path"] == "stations" and out["best"]["kind"] == "station"
        assert out["window"] == (body["window"] or "wd19") and notes[0].startswith("Public transport")
        if diag["dropped_no_route"] or diag["unchecked_for_budget"]:
            assert solver.NOTE_UNCHECKED in notes, (fault, diag)
        if behaviour.no_route_replies:
            assert diag["no_route_replies"] >= 1 and solver.NOTE_NO_ROUTE in notes, (fault, diag)
        if behaviour.bad_replies:
            assert diag["retries"] >= 1 and solver.NOTE_BAD_REPLY in notes, (fault, diag)
        if road and diag["checked_by_road"] < diag["places"]:
            assert st.note_shortlist(diag["checked_by_road"]) in notes
    assert_clean(caplog.text)
    assert any(code == 200 for _, code in seen) and any(code != 200 for _, code in seen)
