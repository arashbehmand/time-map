"""The synthetic build for the full London time map's tests (#6, L-02): tests/layout_network.py's
36 stations plus a grid of cells, a tiny prepared rail feed and a little OpenStreetMap land, all
written into a temporary folder. No Java, no tables, no osmium.

The feed (feed id "syn", so its parent stations are the layout's "syn:G22" and so on):
- a subway along row 2, both ways, at 13:05 and 13:20: five links;
- the express G55 -> G22 at 13:10 (the layout's stand-in for the Metropolitan line);
- a rail trip down column 5 whose third stop is missing (stop_sequence 1, 2, 4): G05-G15 is a
  link, G15-G35 is a gap and is never joined;
- a trip along row 3 at 09:00 (outside the window), one on a service that doesn't run that day,
  a rail-replacement bus (714) and a ferry (4) along row 4: none of them makes a link;
- G00 -> T00 (merged into one node), G01 -> X9 (no such station), and G02 -> G53, whose table
  time is one way only (a link with no time both ways);
- G30 -> G31 with no times at its first stop (no departure time).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import zipfile
from pathlib import Path

from layout_network import BUILD, CHECKS_TOML, LAT0, LON0, M_PER_DEG, write_build
from layout_network import WINDOW as WINDOW_ID

FEED_ID = "syn"
DAY = "20260107"                 # the synthetic build's weekday (manifest departure 2026-01-07T13:00)
N_CELLS = 150

CHECKS_TOML_L02 = CHECKS_TOML + """
[[ratio]]
name = "Syn G44 to Syn G22"
station = "syn:G44"
reference = "syn:G22"

[[required_link]]
a = "syn:G55"
b = "syn:G22"
"""

EXPECTED_LINKS = {("G20", "G21"), ("G21", "G22"), ("G22", "G23"), ("G23", "G24"), ("G24", "G25"),
                  ("G22", "G55"), ("G05", "G15")}


def _trip(tid, route, service, stops, start="13:05:00", seqs=None):
    h, m, s = (int(v) for v in start.split(":"))
    t0 = h * 3600 + m * 60 + s
    rows = []
    for k, stop in enumerate(stops):
        t = t0 + 120 * k
        hms = f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"
        rows.append([tid, hms, hms, stop, (seqs[k] if seqs else k + 1)])
    return {"trip": [route, service, tid], "rows": rows}


def gtfs_tables() -> dict[str, list[list]]:
    row2 = [f"G2{c}" for c in range(6)]
    trips = [
        _trip("sub_out", "SUB", "WD", [f"{s}P" for s in row2]),
        _trip("sub_back", "SUB", "WD", [f"{s}P" for s in reversed(row2)], start="13:20:00"),
        _trip("express", "RAIL", "WD", ["G55P", "G22P"], start="13:10:00"),
        _trip("gap", "RAIL", "WD", ["G05P", "G15P", "G35P"], seqs=[1, 2, 4]),
        _trip("morning", "RAIL", "WD", [f"G3{c}P" for c in range(4)], start="09:00:00"),
        _trip("off", "RAIL", "OFF", [f"G1{c}P" for c in range(4)]),
        _trip("bus", "BUS", "WD", [f"G4{c}P" for c in range(4)]),
        _trip("ferry", "FERRY", "WD", [f"G4{c}P" for c in range(3)]),
        _trip("merged", "RAIL", "WD", ["G00P", "T00P"]),
        _trip("absent", "RAIL", "WD", ["G01P", "X9P"]),
        _trip("oneway", "RAIL", "WD", ["G02P", "G53P"]),
        _trip("untimed", "RAIL", "WD", ["G30P", "G31P"]),
    ]
    trips[-1]["rows"][0][1] = trips[-1]["rows"][0][2] = ""
    stations = sorted({r[3][:-1] for t in trips for r in t["rows"]})
    stops = [["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station"]]
    for s in stations:
        stops.append([s, s, "51.5", "-0.1", "1", ""])
        stops.append([f"{s}P", f"{s} platform", "51.5", "-0.1", "0", s])
    return {
        "agency.txt": [["agency_id", "agency_name", "agency_url", "agency_timezone"],
                       ["A", "Synthetic", "https://example.invalid", "Europe/London"]],
        "routes.txt": [["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"],
                       ["SUB", "A", "S", "Subway", "1"], ["RAIL", "A", "R", "Rail", "2"],
                       ["BUS", "A", "B", "Rail replacement", "714"], ["FERRY", "A", "F", "Ferry", "4"]],
        "trips.txt": [["route_id", "service_id", "trip_id"]] + [t["trip"] for t in trips],
        "stop_times.txt": [["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"]]
        + [r for t in trips for r in t["rows"]],
        "stops.txt": stops,
        "calendar_dates.txt": [["service_id", "date", "exception_type"], ["WD", DAY, "1"], ["OFF", "20260108", "1"]],
    }


def write_gtfs(path: Path, tables: dict | None = None, extra: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, rows in {**(tables or gtfs_tables()), **(extra or {})}.items():
            buf = io.StringIO()
            csv.writer(buf, lineterminator="\n").writerows(rows)
            z.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), buf.getvalue())
    return path


# ── land ────────────────────────────────────────────────────────────
def lonlat(x: float, y: float) -> list[float]:
    """layout_network's metres (x east, y north) to lon/lat."""
    cos0 = math.cos(math.radians(LAT0))
    return [LON0 + x / (M_PER_DEG * cos0), LAT0 + y / M_PER_DEG]


def _ring(pts):
    ring = [lonlat(x, y) for x, y in pts]
    return ring + [ring[0]]


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def land_features(boroughs: int = 33) -> list[dict]:
    """A river band, a park with a pond cut out of it, a main line, a tube tunnel, a siding (left
    out) and `boroughs` borough squares tiling the stations (codes E09000001 on)."""
    feats = [
        {"type": "Feature", "properties": {"natural": "water", "name": "River Syn"},
         "geometry": {"type": "Polygon", "coordinates": [_ring(_rect(-1500, 3000, 9000, 3300))]}},
        {"type": "Feature", "properties": {"leisure": "park", "name": "Syn Park"},
         "geometry": {"type": "Polygon", "coordinates": [_ring(_rect(4500, 4500, 6000, 6000)),
                                                          _ring(_rect(5000, 5000, 5500, 5500))[::-1]]}},
        {"type": "Feature", "properties": {"railway": "rail"},
         "geometry": {"type": "LineString", "coordinates": [lonlat(0, 1000), lonlat(7500, 1100)]}},
        {"type": "Feature", "properties": {"railway": "subway", "tunnel": "yes"},
         "geometry": {"type": "LineString", "coordinates": [lonlat(0, 6000), lonlat(7500, 6500)]}},
        {"type": "Feature", "properties": {"railway": "rail", "service": "siding"},
         "geometry": {"type": "LineString", "coordinates": [lonlat(0, 2000), lonlat(3000, 2000)]}},
    ]
    cols = 11
    for k in range(boroughs):
        r, c = divmod(k, cols)
        x0, y0 = c * 7500 / cols, r * 2500
        feats.append({"type": "Feature", "properties": {"boundary": "administrative", "admin_level": "8",
                                                        "ref:gss": f"E090000{k + 1:02d}"},
                      "geometry": {"type": "Polygon",
                                   "coordinates": [_ring(_rect(x0, y0, x0 + 7500 / cols, y0 + 2500))]}})
    return feats


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_timemap_build(tmp: Path, feed_hook=None) -> tuple[Path, Path]:
    """The build under tmp/out/<build>/ and its prepared feed and (stand-in) clip under
    tmp/data/work/<build>/, the manifest recording both. Returns (build dir, data dir)."""
    data = Path(tmp) / "data"
    work = data / "work" / BUILD
    feed = write_gtfs(work / "gbt.gtfs.zip")
    if feed_hook:
        feed_hook(feed)
    clip = work / "london.osm.pbf"
    clip.write_bytes(b"not a real OSM file: the tests hand the land over directly")

    def hook(manifest):
        manifest["inputs"] = {"prepared": {"gbt": {"file": feed.name, "sha256": _sha(feed)}},
                              "osm_clip": {"file": clip.name, "sha256": _sha(clip)}}

    build_dir = write_build(Path(tmp) / "out", manifest_hook=hook, n_cells=N_CELLS)
    return build_dir, data


def write_checks_l02(path: Path) -> Path:
    Path(path).write_text(CHECKS_TOML_L02)
    return Path(path)


def manifest_of(build_dir: Path) -> dict:
    return json.loads((Path(build_dir) / "manifest.json").read_text())


# ── car (#23, L-03) ─────────────────────────────────────────────────
TRUNK_ID0, PRIMARY_ID0 = 10_000, 20_000
CAR_KPH = {"trunk": 48.0, "primary": 30.0}


def car_inputs():
    """OpenStreetMap-like main roads over the synthetic stations, and Traffic4cast-like speeds on
    them: (nodes {id: (lon, lat)}, ways [car.Way], speeds {u, v, median_kph}, t4c_xy).

    - "A1" (trunk) along row 4, both ways, a node every 250 m; Traffic4cast's edges span two of our
      edges each (simplified between junctions), so they land on our road by the node-id path rule;
    - "A2" (primary) up column 3, crossing A1 at a shared node; its last 750 m have no speed (filled
      from the class's median measured speed);
    - an edge with a synthetic node id, and one whose node the clip no longer has: neither is used.
    """
    from transit.layout.car import Way

    xs = list(range(-500, 8001, 250))
    nodes, ways = {}, []
    trunk = [TRUNK_ID0 + k for k in range(len(xs))]
    for nid, x in zip(trunk, xs):
        nodes[nid] = tuple(lonlat(x, 6000))
    junction = trunk[xs.index(4500)]
    primary = [junction if y == 6000 else PRIMARY_ID0 + k for k, y in enumerate(xs)]
    for nid, y in zip(primary, xs):
        nodes.setdefault(nid, tuple(lonlat(4500, y)))
    ways.append(Way(1, trunk, {"highway": "trunk", "ref": "A1", "name": "Syn Way"}))
    ways.append(Way(2, primary, {"highway": "primary", "ref": "A2", "name": "Syn Road"}))
    u, v, kph = [], [], []
    for a, b in zip(trunk[::2], trunk[2::2]):
        u += [a, b]; v += [b, a]; kph += [CAR_KPH["trunk"]] * 2          # noqa: E702
    for a, b in zip(primary[:-3], primary[1:-3]):
        u += [a, b]; v += [b, a]; kph += [CAR_KPH["primary"]] * 2        # noqa: E702
    u += [1 << 41, trunk[0]]; v += [trunk[1], 999_999]; kph += [50.0, 50.0]   # noqa: E702
    import numpy as np
    speeds = {"u": np.array(u, dtype=np.int64), "v": np.array(v, dtype=np.int64), "median_kph": np.array(kph)}
    return nodes, ways, speeds, dict(nodes)


def car_docs(build_dir, window=WINDOW_ID):
    """car_links.json, roads.json and car_report.json for the synthetic build, from car_inputs()."""
    from transit.layout import car as carmod
    from transit.layout import timemap
    from transit.layout.prep import load_build, station_pairs

    b = load_build(build_dir, window)
    pairs = station_pairs(b)
    mesh, _, _ = timemap.mesh_of(b, pairs)
    nodes, ways, speeds, t4c_xy = car_inputs()
    doc = carmod.build(nodes, ways, t4c_xy, speeds, mesh, b.max_time, pairs.labels, None, log=lambda *a: None)
    smeta = {"days_used": ["2019-07-02", "2019-07-03"], "days_skipped": {}, "bins": [52, 53, 54, 55],
             "rules": {"min_kph": 1.0}, "rows_read": 10, "rows_in_window": 8, "rows_under_min_kph": 0,
             "rows_kept": 8, "volume_class_rows": {"5": 8}, "zip_sha256": "0" * 64, "edges": len(speeds["u"])}
    return carmod.documents(doc, b, pairs, mesh, smeta)
