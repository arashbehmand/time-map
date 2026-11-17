"""Whole city (#28): the full time map is served from a data folder, only under the transit
tables' own gate (server/transit/whole_city.py), and only if it was made from those tables."""
from __future__ import annotations

import ast
import gzip
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tiny_transit import make_build

import server.transit.whole_city as whole_city
from server.transit.whole_city import WholeCityUnavailable, load_dir

LAND = bytes(range(40))
SVG = '<svg xmlns="http://www.w3.org/2000/svg"><text>plot</text></svg>'


def make_city(root: Path, build: Path, win: str = "wd13", **over) -> Path:
    """One build's time-map folder (transit/out/timemap/<build>/) with one window, made from
    `build`'s tables unless `over` (the document's fields) says otherwise."""
    m = json.loads((build / "manifest.json").read_text())
    doc = {"schema": 2, "kind": "london-time-map", "build": m["build"], "window": win,
           "valid_until": m["valid_until"], "table_sha256": m["outputs"][f"tt_{win}"]["sha256"],
           "attribution": ["(c) OpenStreetMap contributors"],
           "land": {"file": "land.bin", "arrays": [{"name": "a", "dtype": "uint16", "offset": 0, "length": 20}]}}
    report = over.pop("report", True)
    land = over.pop("land_bytes", LAND)
    doc.update(over)
    d = root / "timemap" / m["build"] / win
    d.mkdir(parents=True, exist_ok=True)
    (d / "timemap.json").write_text(json.dumps(doc))
    if land is not None:
        (d / "land.bin").write_bytes(land)
    if report:
        (d / "report.md").write_text("# The report\n")
        (d / "shepard.svg").write_text(SVG)
    return d.parent


@pytest.fixture
def cleared(tmp_path):
    build = make_build(tmp_path, cleared=True)
    return build, make_city(tmp_path, build)


def _client(make_client, settings, build, city, **kw):
    return make_client(settings.with_(transit_dir=str(build), timemap_dir=str(city), transit_block_startup=True, **kw))


def _local(app):
    return TestClient(app, client=("127.0.0.1", 50000))


URL = "/api/whole-city/wd13/timemap.json"


def test_without_a_folder_nothing_is_served(client):
    assert client.get("/api/transit/status").json()["whole_city"] == {"available": False, "reason": "not_configured"}
    resp = client.get(URL)
    assert resp.status_code == 404 and resp.json()["code"] == "whole_city_unavailable"
    assert client.get("/whole-city/").status_code == 200        # the page still says why


def test_a_cleared_build_is_served(make_client, settings, cleared):
    c = _client(make_client, settings, *cleared)
    city = c.get("/api/transit/status").json()["whole_city"]
    assert city["available"] is True and city["build"] == "tiny" and city["report"] == {"wd13": True}
    assert city["windows"] == [{"id": "wd13", "label": "a weekday, 13:00–14:00", "date": "2026-12-09"}]
    assert city["service_dates"] == {"weekday": "2026-12-09", "saturday": "2026-12-12"}
    assert city["development_only"] is False
    resp = c.get(URL)
    assert resp.status_code == 200 and resp.json()["build"] == "tiny"
    assert resp.headers["cache-control"] == "no-cache" and resp.headers["x-content-type-options"] == "nosniff"
    assert c.get(URL, headers={"If-None-Match": resp.headers["etag"]}).status_code == 304
    land = c.get("/api/whole-city/wd13/land.bin")
    assert land.content == LAND and land.headers["content-type"] == "application/octet-stream"
    svg = c.get("/api/whole-city/wd13/shepard.svg")
    assert svg.headers["content-type"] == "image/svg+xml"
    assert svg.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'"
    md = c.get("/api/whole-city/wd13/report.md")
    assert md.headers["content-type"] == "text/plain; charset=utf-8" and md.text == "# The report\n"
    # A file the build doesn't have, or a window it doesn't: not found.
    assert c.get("/api/whole-city/wd13/panels.svg").status_code == 404
    assert c.get("/api/whole-city/wd08/timemap.json").status_code == 404


@pytest.mark.parametrize("path", ["/api/whole-city/wd13/manifest.json", "/api/whole-city/wd13/tt_wd13.npy",
                                  "/api/whole-city/xx/timemap.json", "/api/whole-city/wd13/..%2Ftimemap.json",
                                  "/api/whole-city/..%2F..%2Fbuild/manifest.json"])
def test_only_the_listed_files(make_client, settings, cleared, path):
    resp = _client(make_client, settings, *cleared).get(path)
    assert resp.status_code == 404
    assert "tmp" not in resp.text and "/" not in resp.json().get("detail", "").strip(".")


def test_big_files_go_out_gzipped_once(make_client, settings, tmp_path):
    """The gzip copy is made at load, not on every view; a client that doesn't take gzip gets the
    file as it is, under its own ETag."""
    build = make_build(tmp_path, cleared=True)
    big = bytes(range(256)) * 400                         # 100 kB that compresses
    city = make_city(tmp_path, build, land={"file": "land.bin", "arrays": [
        {"name": "a", "dtype": "uint8", "offset": 0, "length": len(big)}]}, land_bytes=big)
    c = _client(make_client, settings, build, city)
    gz = c.get("/api/whole-city/wd13/land.bin", headers={"Accept-Encoding": "gzip"})
    assert gz.headers["content-encoding"] == "gzip" and gz.content == big     # httpx decodes it
    assert "accept-encoding" in gz.headers["vary"].lower()
    assert int(gz.headers["content-length"]) < len(big) // 4
    plain = c.get("/api/whole-city/wd13/land.bin", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and plain.content == big
    assert plain.headers["etag"] != gz.headers["etag"]
    blob = c.app.state.whole_city.windows["wd13"].files["land.bin"]
    assert blob.gz is not None and gzip.decompress(blob.gz) == big


def test_a_list_or_weak_etag_still_gets_304(make_client, settings, cleared):
    c = _client(make_client, settings, *cleared)
    tag = c.get(URL, headers={"Accept-Encoding": "identity"}).headers["etag"]
    for header in (f'"other", {tag}', f"W/{tag}", "*"):
        assert c.get(URL, headers={"If-None-Match": header, "Accept-Encoding": "identity"}).status_code == 304
    assert c.get(URL, headers={"If-None-Match": '"other"', "Accept-Encoding": "identity"}).status_code == 200


def test_only_what_the_page_uses_is_served(make_client, settings, tmp_path):
    build = make_build(tmp_path, cleared=True)
    city = make_city(tmp_path, build)
    (city / "wd13" / "report.json").write_text("{}")
    (city / "wd13" / "panels.svg").write_text(SVG)
    c = _client(make_client, settings, build, city)
    assert c.get("/api/whole-city/wd13/report.json").status_code == 404
    assert c.get("/api/whole-city/wd13/panels.svg").status_code == 404
    assert set(c.app.state.whole_city.windows["wd13"].files) == {"timemap.json", "land.bin", "report.md", "shepard.svg"}


def test_a_window_made_from_other_tables_is_logged_once(make_client, settings, tmp_path, caplog):
    build = make_build(tmp_path, cleared=True)
    c = _client(make_client, settings, build, make_city(tmp_path, build, table_sha256="0" * 64))
    for _ in range(3):
        c.get("/api/transit/status")
        c.get(URL)
    lines = [r.getMessage() for r in caplog.records if "not served with tables" in r.getMessage()]
    assert lines == ["whole city: window wd13 not served with tables tiny: made from another table"]


def test_a_map_folder_without_tables_is_warned_about(make_client, settings, tmp_path, caplog):
    build = make_build(tmp_path, cleared=True)
    make_client(settings.with_(timemap_dir=str(make_city(tmp_path, build)), transit_block_startup=True))
    assert "TIMEMAP_DIR is set without TRANSIT_DIR" in caplog.text


def test_a_map_made_from_other_tables_is_refused(make_client, settings, tmp_path):
    build = make_build(tmp_path, cleared=True)
    city = make_city(tmp_path, build, table_sha256="0" * 64)
    c = _client(make_client, settings, build, city)
    assert c.get("/api/transit/status").json()["whole_city"] == {"available": False, "reason": "build_mismatch"}
    assert c.get(URL).status_code == 404


def test_an_old_map_is_still_served(make_client, settings, tmp_path):
    # Nothing expires (#30): past valid_until the page says services may have changed.
    build = make_build(tmp_path, cleared=True, valid_until=date.today() - timedelta(days=3))
    city = make_city(tmp_path, build, valid_until=(date.today() - timedelta(days=2)).isoformat())
    c = _client(make_client, settings, build, city)
    status = c.get("/api/transit/status").json()
    assert status["available"] is True and status["whole_city"]["available"] is True
    assert status["whole_city"]["valid_until"] == (date.today() - timedelta(days=2)).isoformat()
    for name in ("timemap.json", "land.bin", "report.md", "shepard.svg"):
        assert c.get(f"/api/whole-city/wd13/{name}").status_code == 200, name


def test_uncleared_tables_take_the_map_with_them(make_client, settings, tmp_path):
    build = make_build(tmp_path)
    c = _client(make_client, settings, build, make_city(tmp_path, build))
    assert c.get("/api/transit/status").json()["whole_city"] == {"available": False, "reason": "licences_not_cleared"}
    assert c.get(URL).status_code == 404


def test_the_override_serves_this_machine_only(make_client, settings, tmp_path):
    build = make_build(tmp_path)
    app = _client(make_client, settings, build, make_city(tmp_path, build), transit_allow_uncleared=True).app
    remote = TestClient(app)                                        # client host "testclient"
    assert remote.get("/api/transit/status").json()["whole_city"] == {"available": False, "reason": "development_only"}
    local = _local(app)
    assert local.get("/api/transit/status").json()["whole_city"]["development_only"] is True
    for name in ("timemap.json", "land.bin", "report.md", "shepard.svg"):
        assert local.get(f"/api/whole-city/wd13/{name}").status_code == 200, name
        assert remote.get(f"/api/whole-city/wd13/{name}").status_code == 404, name
        # Through a proxy on this machine is not this machine.
        proxied = local.get(f"/api/whole-city/wd13/{name}", headers={"X-Forwarded-For": "203.0.113.9"})
        assert proxied.status_code == 404, name


def test_the_override_is_refused_on_railway(make_client, settings, tmp_path, monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    build = make_build(tmp_path)
    app = _client(make_client, settings, build, make_city(tmp_path, build), transit_allow_uncleared=True).app
    local = _local(app)
    assert local.get("/api/transit/status").json()["whole_city"] == {"available": False,
                                                                    "reason": "override_refused_here"}
    assert local.get(URL).status_code == 404


def test_no_tables_no_map(make_client, settings, tmp_path):
    build = make_build(tmp_path, cleared=True)
    city = make_city(tmp_path, build)
    c = make_client(settings.with_(timemap_dir=str(city), transit_block_startup=True))
    assert c.get("/api/transit/status").json()["whole_city"] == {"available": False, "reason": "not_configured"}
    assert c.get(URL).status_code == 404


def test_without_the_report_the_page_links_none(make_client, settings, tmp_path):
    build = make_build(tmp_path, cleared=True)
    c = _client(make_client, settings, build, make_city(tmp_path, build, report=False))
    city = c.get("/api/transit/status").json()["whole_city"]
    assert city["available"] is True and city["report"] == {"wd13": False}


@pytest.mark.parametrize(("over", "why"), [
    ({"schema": 3}, "bad_doc"), ({"kind": "something-else"}, "bad_doc"), ({"window": "wd08"}, "bad_doc"),
    ({"valid_until": "soon"}, "bad_doc"), ({"table_sha256": "short"}, "bad_doc"),
    ({"land_bytes": None}, "missing"), ({"land_bytes": LAND[:10]}, "missing"),
])
def test_a_broken_window_is_left_out(tmp_path, caplog, over, why):
    build = make_build(tmp_path, cleared=True)
    city = make_city(tmp_path, build, **over)
    assert load_dir(city) == {}
    assert f"window wd13 left out ({why})" in caplog.text
    with pytest.raises(WholeCityUnavailable, match="missing"):
        whole_city.load_window(city / "wd08")


@pytest.mark.parametrize("root", ["[]", "null", '"text"', "3"])
def test_a_window_that_isnt_an_object_costs_only_itself(tmp_path, caplog, root):
    build = make_build(tmp_path, cleared=True)
    city = make_city(tmp_path, build)                     # wd13, good
    make_city(tmp_path, build, win="wd08")
    (city / "wd08" / "timemap.json").write_text(root)
    assert set(load_dir(city)) == {"wd13"}
    assert "window wd08 left out (bad_doc)" in caplog.text


def test_a_map_without_land_needs_no_land_file(tmp_path):
    build = make_build(tmp_path, cleared=True)
    city = make_city(tmp_path, build, land=None, land_bytes=None)
    assert set(load_dir(city)["wd13"].files) == {"timemap.json", "report.md", "shepard.svg"}


def test_a_folder_the_app_serves_is_refused(tmp_path):
    build = make_build(tmp_path, cleared=True)
    city = make_city(tmp_path / "static", build)
    with pytest.raises(WholeCityUnavailable, match="refused_here"):
        load_dir(city, repo=tmp_path)
    with pytest.raises(WholeCityUnavailable, match="missing"):
        load_dir(tmp_path / "nowhere", repo=tmp_path)


def test_the_holder_survives_anything(tmp_path, monkeypatch):
    holder = whole_city.WholeCityHolder(True)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(whole_city, "load_dir", boom)
    holder.load(str(tmp_path))
    assert holder.reason == "load_failed" and not holder.loading and holder.windows == {}


def test_the_loader_needs_nothing_the_image_doesnt_ship():
    tree = ast.parse(Path(whole_city.__file__).read_text())
    mods = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    mods += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not [m for m in mods if m and m.startswith("transit.layout")]


# ── travel modes (#23, L-03) ────────────────────────────────────────
def make_modes(city: Path, win: str = "wd13", **over) -> Path:
    """A modes/ folder beside the window's map, made with it unless `over` says otherwise."""
    d = city / win
    doc = json.loads((d / "timemap.json").read_text())
    doc.setdefault("nodes", {"kind": [0, 0, 1, 2]})
    (d / "timemap.json").write_text(json.dumps(doc))
    import hashlib
    bound = {"timemap_sha256": hashlib.sha256((d / "timemap.json").read_bytes()).hexdigest(),
             "land_sha256": hashlib.sha256((d / "land.bin").read_bytes()).hexdigest()}
    common = {"build": doc["build"], "window": win, "table_sha256": doc["table_sha256"], "nodes": 4, **bound, **over}
    md = d / "modes"
    md.mkdir(exist_ok=True)
    (md / "modes.json").write_text(json.dumps({"kind": "london-time-map-modes", **common}))
    for cid in ("car", "car-train"):
        (md / f"combo-{cid}.json").write_text(json.dumps({"kind": "london-time-map-combo", "id": cid, **common}))
    (md / "roads.bin").write_bytes(bytes(range(60)))
    (md / "report.md").write_text("# Travel modes\n")
    return city


MODES = "/api/whole-city/wd13/modes/modes.json"


def test_the_travel_modes_are_served_like_the_map(make_client, settings, cleared):
    build, city = cleared
    make_modes(city)
    c = _client(make_client, settings, build, city)
    assert c.get("/api/transit/status").json()["whole_city"]["modes"] == {"wd13": True}
    for name in ("modes.json", "combo-car.json", "combo-car-train.json", "roads.bin", "report.md"):
        resp = c.get(f"/api/whole-city/wd13/modes/{name}")
        assert resp.status_code == 200, name
        assert resp.headers["cache-control"] == "no-cache"
    assert c.get("/api/whole-city/wd13/modes/roads.bin").content == bytes(range(60))
    for bad in ("/api/whole-city/wd13/modes/car_links.json", "/api/whole-city/wd13/modes/..%2Ftimemap.json",
                "/api/whole-city/xx/modes/modes.json"):
        assert c.get(bad).status_code == 404, bad


def test_a_map_without_travel_modes_lists_none(make_client, settings, cleared):
    c = _client(make_client, settings, *cleared)
    assert "modes" not in c.get("/api/transit/status").json()["whole_city"]
    resp = c.get(MODES)
    assert resp.status_code == 404 and resp.json()["code"] == "whole_city_modes_unavailable"


def test_the_travel_modes_go_with_the_tables_gate(make_client, settings, tmp_path):
    build = make_build(tmp_path)                                  # licences not cleared
    city = make_modes(make_city(tmp_path, build))
    app = _client(make_client, settings, build, city, transit_allow_uncleared=True).app
    assert TestClient(app).get(MODES).status_code == 404         # not this machine: the tables' gate
    assert _local(app).get(MODES).status_code == 200


@pytest.mark.parametrize("over", [{"build": "other"}, {"table_sha256": "0" * 64}, {"nodes": 5}, {"window": "wd08"},
                                  {"timemap_sha256": "0" * 64}, {"land_sha256": "0" * 64}])
def test_travel_modes_made_with_another_map_are_left_out(tmp_path, caplog, over):
    build = make_build(tmp_path, cleared=True)
    city = make_modes(make_city(tmp_path, build), **over)
    cw = load_dir(city)["wd13"]
    assert cw.modes == {} and "travel modes left out" in caplog.text
    assert "timemap.json" in cw.files                          # the map itself still loads


def test_a_missing_travel_mode_file_leaves_them_all_out(tmp_path):
    build = make_build(tmp_path, cleared=True)
    city = make_modes(make_city(tmp_path, build))
    (city / "wd13" / "modes" / "roads.bin").unlink()
    assert load_dir(city)["wd13"].modes == {}


def test_travel_modes_made_with_an_earlier_map_are_left_out(tmp_path, caplog):
    """A time map rebuilt beside an old modes/ folder: the car's blends and land checks were made for
    the old timemap.json and land.bin, so the folder is not served with the new ones."""
    build = make_build(tmp_path, cleared=True)
    city = make_modes(make_city(tmp_path, build))
    assert load_dir(city)["wd13"].modes
    (city / "wd13" / "land.bin").write_bytes(LAND + b"x")
    assert load_dir(city)["wd13"].modes == {} and "travel modes left out" in caplog.text
