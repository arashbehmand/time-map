"""Every capped field, when oversized, gets a 4xx before any upstream call."""
import h3
import pytest

LON, LAT = -0.12, 51.5


def person(i, lon=LON, lat=LAT, mode="walking", **kw):
    return {"id": f"p{i}", "lon": lon + i * 0.001, "lat": lat, "mode": mode, **kw}


def solve_body(participants=None, **search):
    body = {"participants": participants or [person(0), person(1)]}
    if search:
        body["search"] = search
    return body


SOLVE_CASES = {
    "nine participants": solve_body([person(i) for i in range(9)]),
    "one participant": solve_body([person(0)]),
    "margin_km 11": solve_body(margin_km=11),
    "max_candidates 301": solve_body(max_candidates=301),
    "refine_top 11": solve_body(refine_top=11),
    "neighbor_ring 2": solve_body(neighbor_ring=2),
    "h3_resolution present": solve_body(h3_resolution=11),
    "id too long": solve_body([person(0, id="x" * 65), person(1)]),
    "weight 101": solve_body([person(0, weight=101), person(1)]),
    "max_minutes 241": solve_body([person(0, max_minutes=241), person(1)]),
    "nan lon": None,  # built below: JSON cannot carry NaN, so send raw text
    "walkers 40 km apart": solve_body([person(0), person(1, lon=LON + 0.58)]),
    "London and Paris walkers": solve_body([person(0), person(1, lon=2.35, lat=48.86)]),
    "London and Paris drivers": solve_body([person(0, mode="driving"), person(1, lon=2.35, lat=48.86,
                                                                               mode="driving")]),
}

MIXED_EIGHT = solve_body(
    [person(i, mode=m) for i, m in enumerate(["walking", "cycling", "driving"] * 2 + ["walking"] * 2)],
    max_candidates=300,
)

EXPECTED_CODE = {
    "walkers 40 km apart": "group_too_spread",
    "London and Paris walkers": "group_too_spread",
    "London and Paris drivers": "group_too_spread",
}


@pytest.mark.parametrize("name", list(SOLVE_CASES))
def test_oversized_solve_is_refused_before_upstream(mapbox, matrix_client, name):
    if name == "nan lon":
        resp = matrix_client.post(
            "/api/meetup/solve",
            content='{"participants":[{"id":"a","lon":NaN,"lat":51.5,"mode":"walking"},'
                    '{"id":"b","lon":0,"lat":51.5,"mode":"walking"}]}',
            headers={"content-type": "application/json"},
        )
    else:
        resp = matrix_client.post("/api/meetup/solve", json=SOLVE_CASES[name])
    assert 400 <= resp.status_code < 500, resp.text
    assert resp.json()["code"] == EXPECTED_CODE.get(name, "invalid_request")
    assert mapbox.count() == 0


def test_spread_refusal_is_plain_words(matrix_client):
    resp = matrix_client.post("/api/meetup/solve", json=SOLVE_CASES["London and Paris walkers"])
    detail = resp.json()["detail"]
    assert "walking" in detail and "km" in detail
    assert "51.5" not in detail and "2.35" not in detail


@pytest.mark.parametrize("body", [
    {"longitude": LON, "latitude": LAT, "travel_times": [5, 10, 15, 20, 25]},
    {"longitude": LON, "latitude": LAT, "travel_times": [61]},
    {"longitude": LON, "latitude": LAT, "travel_times": [0]},
    {"longitude": LON, "latitude": LAT, "travel_times": []},
    {"longitude": 181, "latitude": LAT},
    {"longitude": LON, "latitude": LAT, "profile": "driving-traffic"},
    {"longitude": LON, "latitude": LAT, "extra": 1},
])
@pytest.mark.parametrize("path", ["/api/isochrones", "/api/warp-params"])
def test_oversized_isochrone_requests(mapbox, client, path, body):
    resp = client.post(path, json=body)
    assert resp.status_code == 422, resp.text
    assert mapbox.count() == 0


def test_warp_params_rejects_polar_latitude(mapbox, client):
    resp = client.post("/api/warp-params", json={"longitude": 0, "latitude": 86})
    assert resp.status_code == 422
    assert mapbox.count() == 0


@pytest.mark.parametrize("path", [
    "/tiles/17/0/0", "/tiles/14/16384/0", "/tiles/2/0/4", "/tiles/-1/0/0",
    "/glyphs/Comic Sans/0-255.pbf",
    "/glyphs/Open Sans Regular,Arial Unicode MS Regular/1-256.pbf",
    "/glyphs/Open Sans Regular,Arial Unicode MS Regular/65536-65791.pbf",
    "/glyphs/Open Sans Regular,Arial Unicode MS Regular/0-255.json",
])
def test_bad_proxy_paths(mapbox, client, path):
    resp = client.get(path)
    assert 400 <= resp.status_code < 500
    assert mapbox.count() == 0


def test_body_limit_with_and_without_content_length(mapbox, client):
    big = b'{"participants": [' + b" " * (17 * 1024) + b"]}"
    resp = client.post("/api/meetup/solve", content=big, headers={"content-type": "application/json"})
    assert resp.status_code == 413
    assert resp.json()["code"] == "payload_too_large"

    def chunks():
        yield b'{"participants": ['
        for _ in range(20):
            yield b" " * 1024
        yield b"]}"

    resp = client.post("/api/meetup/solve", content=chunks(), headers={"content-type": "application/json"})
    assert resp.status_code == 413
    assert mapbox.count() == 0


@pytest.mark.parametrize("body", [
    {"longitude": LON, "latitude": LAT, "travel_times": [15, 30, 45, 60]},
    {"longitude": LON, "latitude": LAT, "travel_times": [10, 10, 20]},
])
def test_boundary_isochrones_pass_in_one_request(mapbox, client, body):
    resp = client.post("/api/isochrones", json=body)
    assert resp.status_code == 200, resp.text
    assert mapbox.count("isochrone") == 1


def test_boundary_solve_passes(mapbox, matrix_client):
    body = solve_body([person(i) for i in range(8)], margin_km=10)
    resp = matrix_client.post("/api/meetup/solve", json=body)
    assert resp.status_code == 200, resp.text
    diag = resp.json()["diagnostics"]
    assert diag["coarse_candidates"] <= 160 + 8
    assert diag["fine_candidates"] <= 160
    elements = sum(
        len(r.url.params["sources"].split(";")) * len(r.url.params["destinations"].split(";"))
        for r in mapbox.calls
    )
    assert elements <= diag["estimated_matrix_elements"]


def test_wide_driving_group_uses_a_coarse_resolution(mapbox, matrix_client):
    body = solve_body([person(0, mode="driving"), person(1, lon=LON + 1.2, mode="driving")])
    resp = matrix_client.post("/api/meetup/solve", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["diagnostics"]["coarse_candidates"] <= 160 + 2


def _matrix_elements(mapbox) -> int:
    return sum(
        len(r.url.params["sources"].split(";")) * len(r.url.params["destinations"].split(";"))
        for r in mapbox.calls if mapbox.api_of(r) == "matrix"
    )


def test_oversized_plan_shrinks_to_fit_instead_of_refusing(mapbox, matrix_client):
    resp = matrix_client.post("/api/meetup/solve", json=MIXED_EIGHT)
    assert resp.status_code == 200, resp.text
    assert resp.json()["diagnostics"]["estimated_matrix_elements"] <= 4000
    assert _matrix_elements(mapbox) <= 4000
    per_bucket = {}
    for r in mapbox.calls:
        per_bucket[r.url.path.split("/")[4]] = per_bucket.get(r.url.path.split("/")[4], 0) + 1
    assert sum(per_bucket.values()) <= 60


def test_driving_traffic_fits_its_30_per_minute_window(mapbox, matrix_client):
    body = solve_body([person(i, mode="driving-traffic") for i in range(8)])
    resp = matrix_client.post("/api/meetup/solve", json=body)
    assert resp.status_code == 200, resp.text
    assert mapbox.count("matrix") <= 30
    for r in mapbox.calls:
        coords = r.url.path.rsplit("/", 1)[-1].split(";")
        assert len(coords) <= 10


def test_unfittable_plan_is_refused_before_grid_disk(make_client, settings, mapbox, monkeypatch):
    def boom(*_a, **_kw):
        raise AssertionError("grid_disk called")

    c = make_client(settings.with_(router_provider="mapbox", max_solve_elements=30))
    monkeypatch.setattr(h3, "grid_disk", boom)
    resp = c.post("/api/meetup/solve", json=solve_body())
    assert resp.status_code == 422
    assert resp.json()["code"] == "search_too_expensive"
    assert mapbox.count() == 0


def test_tile_decoder_is_gone():
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    text = "".join(p.read_text() for p in (root / "server").rglob("*.py"))
    assert "mapbox_vector_tile" not in text
    assert "mapbox-vector-tile" not in (root / "requirements.txt").read_text()
