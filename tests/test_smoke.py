"""One happy-path request per endpoint, with Mapbox mocked."""
import pytest


@pytest.mark.parametrize("path", ["/", "/manifest.json", "/sw.js", "/meetup/", "/warp/", "/api/meetup/health"])
def test_pages(client, path):
    assert client.get(path).status_code == 200


def test_isochrones(client):
    resp = client.post("/api/isochrones", json={"longitude": -0.12, "latitude": 51.5, "travel_times": [10, 20]})
    assert resp.status_code == 200
    assert [f["properties"]["contour"] for f in resp.json()["features"]] == [10, 20]


def test_warp_params(client):
    resp = client.post("/api/warp-params", json={"longitude": -0.12, "latitude": 51.5})
    assert resp.status_code == 200, resp.text
    assert resp.json()["k"] == 4


def test_solve_euclidean(client):
    resp = client.post("/api/meetup/solve", json={"participants": [
        {"id": "a", "lon": -0.12, "lat": 51.5, "mode": "walking"},
        {"id": "b", "lon": -0.10, "lat": 51.51, "mode": "cycling"},
    ]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["best"]["times"].keys() == {"a", "b"}


def test_solve_matrix(mapbox, matrix_client):
    resp = matrix_client.post("/api/meetup/solve", json={"participants": [
        {"id": "a", "lon": -0.12, "lat": 51.5, "mode": "walking"},
        {"id": "b", "lon": -0.10, "lat": 51.51, "mode": "walking"},
    ]})
    assert resp.status_code == 200, resp.text
    assert mapbox.count("matrix") > 0


def test_tile(client):
    resp = client.get("/tiles/14/8185/5448")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-protobuf"


def test_glyph(client):
    resp = client.get("/glyphs/Open Sans Regular,Arial Unicode MS Regular/0-255.pbf")
    assert resp.status_code == 200
