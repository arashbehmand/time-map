"""
The car for the London time map's travel modes (#23, L-03): Traffic4cast's speeds read from a
synthetic zip (pyarrow, the transit environment's; skipped without it), and the main roads, their
speeds and the car links on synthetic OpenStreetMap ways (no osmium, no network).
"""
from __future__ import annotations

import io
import json
import math
import zipfile
from datetime import date

import numpy as np
import pytest
from layout_network import BUILD
from timemap_network import CAR_KPH, PRIMARY_ID0, TRUNK_ID0, car_docs, car_inputs, lonlat, write_timemap_build

from transit import settings
from transit.layout import car as carmod
from transit.layout import carspeed
from transit.layout import mesh as meshmod
from transit.layout.frame import to_local
from transit.layout.prep import RefusedInput


# ── the speeds ──────────────────────────────────────────────────────
def _speed_zip(path, days: dict[str, list[tuple]], columns=settings.T4C_COLUMNS):
    """A LONDON_2022.zip-shaped file: one parquet per day, rows (u, v, t, kph)."""
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")

    with zipfile.ZipFile(path, "w") as z:
        for d, rows in days.items():
            cols = {"u": [r[0] for r in rows], "v": [r[1] for r in rows], "day": [d] * len(rows),
                    "t": [r[2] for r in rows], "volume_class": [5] * len(rows),
                    "median_speed_kph": [float(r[3]) for r in rows], "free_flow_kph": [50.0] * len(rows)}
            buf = io.BytesIO()
            pq.write_table(pa.table({k: cols[k] for k in columns}), buf)
            z.writestr(f"speed_classes/london/speed_classes_{d}.parquet", buf.getvalue())
        z.writestr("movie/london/2019-07-02_london_8ch.h5", b"not read")
    return path


def test_the_days_used_are_weekdays_outside_holidays_and_christmas():
    assert carspeed.day_rule(date(2019, 7, 2)) is None                      # a Tuesday
    assert carspeed.day_rule(date(2019, 7, 6)) == "weekend"
    assert carspeed.day_rule(date(2019, 8, 26)) == "bank holiday"
    assert carspeed.day_rule(date(2019, 12, 23)) == "Christmas weeks"
    assert carspeed.day_rule(date(2020, 1, 3)) == "Christmas weeks"
    assert carspeed.day_rule(date(2020, 1, 6)) is None


def test_speeds_are_the_median_of_the_13_to_14_bins_on_the_days_used(tmp_path):
    z = _speed_zip(tmp_path / "LONDON_2022.zip", {
        "2019-07-02": [(1, 2, 52, 10), (1, 2, 53, 20), (1, 2, 60, 99), (2, 1, 55, 30), (3, 4, 54, 0.5)],
        "2019-07-03": [(1, 2, 54, 40)],
        "2019-07-06": [(1, 2, 52, 99)],                                    # a Saturday
        "2019-08-26": [(1, 2, 52, 99)],                                    # a bank holiday
    })
    cols, meta = carspeed.aggregate(z, log=lambda *a: None)
    got = {(int(u), int(v)): (float(k), int(n)) for u, v, k, n in zip(cols["u"], cols["v"], cols["median_kph"],
                                                                       cols["rows"])}
    assert got == {(1, 2): (20.0, 3), (2, 1): (30.0, 1)}       # 10, 20, 40; the 13:00 bins only; 0.5 km/h left out
    assert meta["days_used"] == ["2019-07-02", "2019-07-03"]
    assert meta["days_skipped"] == {"2019-07-06": "weekend", "2019-08-26": "bank holiday"}
    assert meta["rows_under_min_kph"] == 1 and meta["edges"] == 2 and len(meta["zip_sha256"]) == 64
    assert "non-commercial" in meta["licence"]
    out = tmp_path / "cache"
    carspeed.write(cols, meta, out)
    again, meta2 = carspeed.load(z, out)
    assert np.array_equal(again["median_kph"], cols["median_kph"]) and meta2["edges"] == 2


def test_days_from_the_covid_period_and_other_columns_are_refused(tmp_path):
    late = _speed_zip(tmp_path / "late.zip", {"2019-07-02": [(1, 2, 52, 10)], "2020-03-02": [(1, 2, 52, 10)]})
    with pytest.raises(RefusedInput, match="covid"):
        carspeed.aggregate(late, log=lambda *a: None)
    other = _speed_zip(tmp_path / "other.zip", {"2019-07-02": [(1, 2, 52, 10)]},
                       columns=("u", "v", "day", "t", "median_speed_kph"))
    with pytest.raises(RefusedInput, match="columns"):
        carspeed.aggregate(other, log=lambda *a: None)
    with pytest.raises(RefusedInput, match="LONDON_2022.zip"):
        carspeed.aggregate(tmp_path / "missing.zip")


def test_cached_speeds_made_from_another_zip_are_refused(tmp_path):
    z = _speed_zip(tmp_path / "LONDON_2022.zip", {"2019-07-02": [(1, 2, 52, 10)]})
    cols, meta = carspeed.aggregate(z, log=lambda *a: None)
    carspeed.write(cols, meta, tmp_path / "cache")
    _speed_zip(z, {"2019-07-02": [(1, 2, 52, 10), (3, 4, 53, 12)]})          # rewritten: another size
    with pytest.raises(RefusedInput, match="another"):
        carspeed.load(z, tmp_path / "cache")
    assert carspeed.cache_dir(tmp_path / "data" / "raw" / "t4c22" / "x.zip") == tmp_path / "data" / "work" / "t4c22"


def test_the_median_per_edge_is_exact():
    u = np.array([5, 1, 1, 1, 5, 1])
    v = np.array([6, 2, 2, 2, 6, 2])
    s = np.array([7.0, 3.0, 1.0, 2.0, 9.0, 10.0])
    uu, vv, med, n = carspeed.group_median(u, v, s)
    assert uu.tolist() == [1, 5] and vv.tolist() == [2, 6] and med.tolist() == [2.5, 8.0] and n.tolist() == [4, 2]


# ── the roads ───────────────────────────────────────────────────────
def test_opl_is_read_with_its_escapes():
    lines = ["n1 v1 dV c0 t2020-01-01T00:00:00Z i0 u T x-0.1 y51.5",
             "n2 v1 dV c0 t2020-01-01T00:00:00Z i0 u Tname=A%20%B x-0.2 y51.6",
             "w9 v3 dV c0 t2020-01-01T00:00:00Z i0 u Thighway=primary,name=Ballards%20%Lane,ref=A598 Nn1,n2"]
    nodes, ways = carmod.parse_opl(lines)
    assert nodes == {1: (-0.1, 51.5), 2: (-0.2, 51.6)}
    assert ways[0].id == 9 and ways[0].nodes == [1, 2] and ways[0].tags["name"] == "Ballards Lane"


def test_one_way_rules():
    assert carmod.directions({"highway": "primary"}) == (True, True)
    assert carmod.directions({"highway": "primary", "oneway": "yes"}) == (True, False)
    assert carmod.directions({"highway": "primary", "oneway": "-1"}) == (False, True)
    assert carmod.directions({"highway": "primary", "junction": "roundabout"}) == (True, False)
    assert carmod.directions({"highway": "motorway"}) == (True, False)
    assert carmod.directions({"highway": "motorway", "oneway": "no"}) == (True, True)
    assert carmod.is_car_road({"highway": "trunk"}) and not carmod.is_car_road({"highway": "trunk_link"})
    assert not carmod.is_car_road({"highway": "primary", "motor_vehicle": "no"})
    assert not carmod.is_car_road({"highway": "primary", "access": "no", "bicycle": "yes"})
    assert carmod.is_car_road({"highway": "primary", "access": "no", "motor_vehicle": "yes"})
    assert not carmod.is_car_road({"highway": "secondary"})


def test_speeds_land_on_our_roads_by_node_id_then_by_geometry():
    nodes, ways, speeds, t4c_xy = car_inputs()
    g = carmod.road_graph(nodes, ways)
    sp = carmod.place_speeds(g, speeds, t4c_xy)
    c = sp["counts"]
    assert c["t4c_synthetic"] == 1 and c["t4c_vanished"] == 1
    cls = g.cls
    trunk = cls == "trunk"
    # Traffic4cast's edges span two of ours: every trunk edge gets the speed, by node id.
    assert (sp["source"][trunk] == "id").all() and np.allclose(sp["speed"][trunk], CAR_KPH["trunk"])
    # The primary's last 750 m (three edges each way) have no speed: filled with the class's median.
    prim = cls == "primary"
    assert (sp["source"][prim] == "filled").sum() == 6 and np.allclose(sp["speed"][prim], CAR_KPH["primary"])
    cov = carmod.coverage(g, sp["source"])
    assert cov["trunk"]["before"] == 1.0 and 0.8 < cov["primary"]["after"] < 1.0
    # A chord from an edge that didn't land by id lends its speed by geometry.
    one = {k: v[:0] for k, v in speeds.items()}
    a, b = PRIMARY_ID0, PRIMARY_ID0 + 1
    nodes2 = dict(t4c_xy)
    nodes2[77] = lonlat(4505, -500)                                        # an old node beside ours
    nodes2[78] = lonlat(4505, -250)
    one = {"u": np.array([77]), "v": np.array([78]), "median_kph": np.array([11.0])}
    sp2 = carmod.place_speeds(g, one, nodes2)
    e = int(np.where((g.ea == g.index(a)) & (g.eb == g.index(b)))[0][0])
    assert sp2["source"][e] == "geometry" and sp2["speed"][e] == 11.0
    back = int(np.where((g.ea == g.index(b)) & (g.eb == g.index(a)))[0][0])
    assert sp2["source"][back] != "geometry"                                # the other way round: not parallel


def _grid_mesh():
    """Stations on the synthetic build's 6 × 6 grid (no jitter), in the local frame like the roads."""
    ll = np.array([lonlat(c * 1500.0, r * 1500.0) for r in range(6) for c in range(6)])
    return meshmod.build_mesh(to_local(ll[:, 0], ll[:, 1]), np.zeros((0, 2)))


def test_car_links_join_consecutive_nodes_along_a_road_and_cutting_finer_changes_nothing():
    nodes, ways, speeds, t4c_xy = car_inputs()
    mesh = _grid_mesh()
    g = carmod.road_graph(nodes, ways)
    sp = carmod.place_speeds(g, speeds, t4c_xy)
    labels = [f"S{i}" for i in range(36)]
    res = carmod.car_links(g, sp, mesh, 180, labels)
    pairs = {(lk["a"], lk["b"]) for lk in res["links"]}
    # The trunk runs along row 4 (y = 6000 north; the mesh's y points south: row 4 is node 24 to 29).
    row4 = {tuple(sorted((24 + c, 25 + c))) for c in range(5)}
    assert row4 <= pairs
    for lk in res["links"]:
        if (lk["a"], lk["b"]) in row4:
            assert lk["minutes"] == pytest.approx(1500 / 1000 / CAR_KPH["trunk"] * 60, rel=0.05)
            assert lk["roads"][0] == "A1" and lk["measured_share"] == 1.0
    old = settings.CAR_PIECE_M
    try:
        settings.CAR_PIECE_M = 5.0
        fine = carmod.car_links(g, sp, mesh, 180, labels)
    finally:
        settings.CAR_PIECE_M = old
    assert {(lk["a"], lk["b"]) for lk in fine["links"]} == pairs


def test_a_link_with_too_little_road_measured_is_dropped_and_said_why():
    nodes, ways, speeds, t4c_xy = car_inputs()
    mesh = _grid_mesh()
    g = carmod.road_graph(nodes, ways)
    sp = carmod.place_speeds(g, {k: v[:0] for k, v in speeds.items()}, t4c_xy)
    assert not (sp["source"] == "id").any()
    res = carmod.car_links(g, sp, mesh, 180, None)
    assert not res["links"] and res["dropped"] and all("measured" in d["why"] or "no way" in d["why"]
                                                       for d in res["dropped"])


def test_thames_crossings_are_listed_linked_or_with_the_reason():
    import shapely

    nodes, ways, speeds, t4c_xy = car_inputs()
    w = carmod.Way
    # The river crosses the trunk between x = 2000 and 2500 and the primary near y = 2000.
    nodes[90_001], nodes[90_002] = lonlat(2250, 7500), lonlat(2250, -1000)
    river = w(5, [90_001, 90_002], {"waterway": "river", "name": "River Thames"})
    bridge = [k for k, x in enumerate(range(-500, 8001, 250)) if x in (2000, 2250, 2500)]
    trunk = ways[0]
    a, b, c = (trunk.nodes[k] for k in bridge)
    ways = [w(1, trunk.nodes[:bridge[0] + 1], trunk.tags), w(11, [a, b, c], {**trunk.tags, "bridge": "yes",
                                                                             "name": "Syn Bridge"}),
            w(12, trunk.nodes[bridge[-1]:], trunk.tags), ways[1], river]
    nodes[91_001], nodes[91_002] = lonlat(2000, 1000), lonlat(2500, 1000)
    ways.append(w(13, [91_001, 91_002], {"highway": "cycleway", "disused:highway": "primary", "bridge": "yes",
                                         "motor_vehicle": "no", "name": "Closed Bridge"}))
    nodes[92_001], nodes[92_002] = lonlat(2000, 3000), lonlat(2500, 3000)
    ways.append(w(14, [92_001, 92_002], {"highway": "secondary", "bridge": "yes", "name": "Side Bridge"}))
    mesh = _grid_mesh()
    x0, y0 = to_local(*lonlat(-3000, 9000))
    x1, y1 = to_local(*lonlat(11000, -3000))
    region = shapely.box(x0, y0, x1, y1)                                   # local metres, y south
    doc = carmod.build(nodes, ways, t4c_xy, speeds, mesh, 180, [f"S{i}" for i in range(36)], region, log=lambda *a: None)
    by = {x["name"]: x for x in doc["bridges"]}
    assert by["Syn Bridge"]["status"] == "linked" and by["Syn Bridge"]["links"]
    assert by["Closed Bridge"]["status"] == "closed to cars"
    assert by["Side Bridge"]["status"] == "not a main road" and "secondary" in by["Side Bridge"]["why"]


def test_the_car_documents_name_their_mesh_and_a_mismatch_is_refused(tmp_path):
    build_dir, _ = write_timemap_build(tmp_path)
    links, roads, report = car_docs(build_dir)
    assert links["mesh_sha256"] == roads["mesh_sha256"] == report["mesh_sha256"]
    assert links["overhead_min"] == settings.CAR_OVERHEAD_MIN and links["twins"] and links["links"]
    assert all(lk["a"] in links["twins"] and lk["b"] in links["twins"] for lk in links["links"])
    d = tmp_path / "car"
    d.mkdir()
    for name, doc in zip(carmod.OUT_FILES, (links, roads, report)):
        (d / name).write_text(json.dumps(doc))
    mesh = _grid_mesh()
    with pytest.raises(RefusedInput, match="another mesh"):
        carmod.load(BUILD, "wd13", mesh, d)
    with pytest.raises(RefusedInput, match="no car links"):
        carmod.load(BUILD, "wd13", mesh, tmp_path / "nowhere")


def test_the_closed_tidal_thames_is_inside_greater_london():
    import shapely

    region = carmod.london_polygon()
    westminster_bridge = shapely.Point(*to_local(-0.1218, 51.5008))
    assert region.contains(westminster_bridge)
    assert math.isfinite(region.area) and region.area > 1.5e9                    # about 1,570 km²
    assert TRUNK_ID0 != PRIMARY_ID0
