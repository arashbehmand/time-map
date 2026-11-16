"""One happy-path request per endpoint, with Mapbox mocked."""
import pytest


@pytest.mark.parametrize("path", ["/", "/manifest.json", "/sw.js", "/meet-up/", "/from-here/", "/whole-city/",
                                  "/api/meetup/health"])
def test_pages(client, path):
    assert client.get(path).status_code == 200


# Old links (#28): the tools' first paths still work, with the rest of the path and the query.
# The browser keeps a #fragment (a meetup share link's group) across the redirect itself.
@pytest.mark.parametrize(("old", "new"), [
    ("/warp", "/from-here/"), ("/warp/", "/from-here/"), ("/warp/?mode=transit&window=wd08", "/from-here/?mode=transit&window=wd08"),
    ("/warp/app.js?polar=1", "/from-here/app.js?polar=1"), ("/warp/lab/", "/from-here/lab/"),
    ("/meetup", "/meet-up/"), ("/meetup/", "/meet-up/"), ("/meetup/?x=1", "/meet-up/?x=1"),
    ("/meetup/style.css", "/meet-up/style.css"),
])
def test_old_paths_redirect(client, old, new):
    for method in (client.get, client.head):
        resp = method(old, follow_redirects=False)
        assert resp.status_code == 308, old
        assert resp.headers["location"] == new
    assert client.get(old).status_code == 200


def test_a_query_named_rest_stays_in_the_query(client):
    resp = client.get("/warp?rest=lab/", follow_redirects=False)
    assert resp.status_code == 308 and resp.headers["location"] == "/from-here/?rest=lab/"


def test_isochrones(client):
    resp = client.post("/api/isochrones", json={"longitude": -0.12, "latitude": 51.5, "travel_times": [10, 20]})
    assert resp.status_code == 200
    assert [f["properties"]["contour"] for f in resp.json()["features"]] == [10, 20]


def test_travel_time_field(client):
    resp = client.post("/api/travel-time-field", json={"longitude": -0.12, "latitude": 51.5})
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "walking"


def test_warp_params_is_gone(client):
    assert client.post("/api/warp-params", json={"longitude": -0.12, "latitude": 51.5}).status_code == 404


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
