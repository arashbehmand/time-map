"""100 random solve requests against a misbehaving Matrix API: never an unhandled error,
and never a degraded answer without a note."""
import logging
import random
import zlib

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
            assert notes, (fault, diag)
        # An answer after a bad reply used the reply asked for again, and says so.
        if behaviour.bad_replies:
            assert diag["retries"] >= 1 and diag["bad_replies"] >= 1, (fault, diag)
            assert solver.NOTE_BAD_REPLY in notes, (fault, diag)
    assert_clean(caplog.text)
    # The run exercised both answers and refusals.
    assert any(code == 200 for _, code in seen) and any(code != 200 for _, code in seen)
