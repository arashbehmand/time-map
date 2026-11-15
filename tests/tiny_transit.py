"""A tiny transit build, shaped like transit/out/<build>/, for the provider and API tests.

Two stations 2.8 km apart east-west and two table cells; the second cell is masked (cut off).
Station A's walkshed has a small hole (a pond) 250 m east of it, so the field fills it in;
station B's has one 120 m north of it, inside the 150 m the last steps from a station are
walked straight. Cell C's walkshed lies 1.6 km north of the stations, so only C's row reaches
it. `twin` adds a station E 5 m from B, with its own minutes from A.

write_venues adds venue files (venues.csv and venues.json, #19) to any build.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from transit.tables import Walksheds, cell_int, disk_cells, haversine_m

A = (-0.1200, 51.5000)          # station "A" (written in capitals, as gb-transit does)
B = (-0.0800, 51.5000)          # station "B"
C = (-0.1000, 51.5150)          # table cell
D = (-0.0600, 51.5300)          # table cell, masked
POND = (-0.11640, 51.5000)      # 250 m east of A
POND_B = (-0.0800, 51.50108)    # 120 m north of B
E = (-0.07993, 51.50001)        # 5 m from B
WINDOWS = {"wd08": ("weekday", "08:00"), "wd13": ("weekday", "13:00"),
           "wd19": ("weekday", "19:00"), "sa13": ("saturday", "13:00")}
WALK = 4000 / 60


def table(window: str = "wd08", twin: int | None = None) -> np.ndarray:
    n = 5 if twin is not None else 4
    t = np.full((n, n), 255, dtype=np.uint8)
    for i in range(3):
        t[i, i] = 0
    if twin is not None:
        t[4, 4] = 0
        t[0, 4], t[4, 0] = twin, twin     # A ↔ E
        t[4, 1] = t[1, 4] = 0
    t[0, 1], t[1, 0] = 5, 6           # A ↔ B: a fast line
    t[0, 2], t[2, 0] = 12, 12         # A ↔ C
    t[1, 2], t[2, 1] = 9, 9           # B ↔ C
    if window == "sa13":
        t[0, 2] = t[1, 2] = 255       # on Saturday, nothing reaches C
    return t


def walkshed(point: tuple[float, float], radius: float, hole: tuple[float, float] | None = None,
             hole_radius: float = 60):
    cells = disk_cells(point[0], point[1], 10, radius)
    if hole is not None:
        drop = set(disk_cells(hole[0], hole[1], 10, hole_radius).tolist())
        cells = np.array([c for c in cells.tolist() if c not in drop], dtype=np.uint64)
    import h3
    lat, lon = np.array([h3.cell_to_latlng(h3.int_to_str(int(c))) for c in cells]).T
    minutes = haversine_m(point[0], point[1], lon, lat) / WALK
    return cells, minutes


def make_build(root: Path, *, cleared: bool = False, valid_until: date | None = None,
               complete: bool = True, twin: int | None = None) -> Path:
    d = root / "build"
    d.mkdir(parents=True, exist_ok=True)
    rows = [(0, "station", "gbt:A", "ALPHA ROAD UND", *A, True),
            (1, "station", "gbt:B", "Bravo (London)", *B, True),
            (2, "cell", "88194ad000fffff", "", *C, True),
            (3, "cell", "88194ad001fffff", "", *D, False)]
    if twin is not None:
        rows.append((4, "station", "gbt:E", "ECHO UND", *E, True))
    n = len(rows)
    with open(d / "points.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["idx", "kind", "id", "name", "lon", "lat", "inside_gl", "snap_m", "ok",
                    "snap_lon", "snap_lat"])
        for idx, kind, pid, name, lon, lat, ok in rows:
            w.writerow([idx, kind, pid, name, lon, lat, True, 1.0, ok, lon, lat])
    np.save(d / "point_ok.npy", np.array([r[-1] for r in rows], dtype=bool))
    for win in WINDOWS:
        np.save(d / f"tt_{win}.npy", table(win, twin))
        (d / f"tt_{win}.fingerprint.json").write_text(json.dumps({"complete": complete}))
    ws = Walksheds.build([
        (0, *walkshed(A, 900, hole=POND)),
        (1, *walkshed(B, 900, hole=POND_B, hole_radius=70)),
        (2, *walkshed(C, 400)),
        *([(4, *walkshed(E, 900, hole=POND_B, hole_radius=70))] if twin is not None else []),
    ])
    ws.save(d / "walksheds.npz")
    outputs = {}
    for name in [*(f"tt_{w}" for w in WINDOWS), "point_ok.npy", "walksheds.npz", "points.csv"]:
        file = f"{name}.npy" if name.startswith("tt_") else name
        data = (d / file).read_bytes()
        outputs[name] = {"file": file, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        if name.startswith("tt_"):
            outputs[name]["shape"] = [n, n]
    manifest = {
        "schema": 1, "build": "tiny", "timezone": "Europe/London",
        "service_dates": {"weekday": "2026-12-09", "saturday": "2026-12-12"},
        "windows": {w: {"day": day, "start": start, "minutes": 60} for w, (day, start) in WINDOWS.items()},
        "valid_until": (valid_until or date.today() + timedelta(days=30)).isoformat(),
        "outputs": outputs,
        "attribution": ["Powered by TfL Open Data", "(c) OpenStreetMap contributors",
                        "Powered by TfL Open Data"],
        "licences_cleared": cleared,
    }
    (d / "manifest.json").write_text(json.dumps(manifest))
    return d


def cell_of(lon: float, lat: float) -> int:
    return cell_int(lon, lat, 10)


# ── random builds, for the meetup station tests ─────────────────────
CENTRE = (-0.10, 51.51)
NAMES = ["ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT", "GOLF", "HOTEL", "INDIA",
         "JULIET", "KILO", "LIMA", "MIKE", "NOVEMBER", "OSCAR", "PAPA", "QUEBEC", "ROMEO",
         "SIERRA", "TANGO", "UNIFORM", "VICTOR", "WHISKEY", "XRAY", "YANKEE", "ZULU"]


def make_random_build(root: Path, n_stations: int = 12, seed: int = 0, *, spread_m: float = 2500,
                      unreachable: float = 0.05, cleared: bool = True, n_venues: int = 0) -> Path:
    """
    A cleared build of n_stations stations scattered within spread_m of CENTRE, with random
    asymmetric minutes per window (some 255) and a 700 m walkshed each. Every fourth station
    has a twin of the same name 100-300 m away (one place with two platforms); one in seven is
    masked. n_venues venues of random kinds are scattered over 1.4 x spread_m, so some are
    beyond every walkshed. Returns the build directory.
    """
    rng = np.random.default_rng(seed)
    d = root / f"rand-{seed}"
    d.mkdir(parents=True, exist_ok=True)
    m_lat = 1 / 111_195
    m_lon = m_lat / np.cos(np.radians(CENTRE[1]))
    pts: list[tuple[str, float, float, bool]] = []
    k = 0
    while len(pts) < n_stations:
        name = NAMES[k % len(NAMES)] + ("" if k < len(NAMES) else f" {k // len(NAMES)}") + " UND"
        dx, dy = rng.uniform(-spread_m, spread_m, 2)
        lon, lat = CENTRE[0] + dx * m_lon, CENTRE[1] + dy * m_lat
        ok = rng.random() > 1 / 7 or not pts
        pts.append((name, lon, lat, bool(ok)))
        if k % 4 == 0 and len(pts) < n_stations:
            off = rng.uniform(100, 300)
            pts.append((name, lon + off * m_lon, lat, True))
        k += 1
    n = len(pts)
    with open(d / "points.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["idx", "kind", "id", "name", "lon", "lat", "inside_gl", "snap_m", "ok",
                    "snap_lon", "snap_lat"])
        for i, (name, lon, lat, ok) in enumerate(pts):
            w.writerow([i, "station", f"gbt:S{i}", name, lon, lat, True, 1.0, ok, lon, lat])
    np.save(d / "point_ok.npy", np.array([p[3] for p in pts], dtype=bool))
    for win in WINDOWS:
        t = rng.integers(2, 60, size=(n, n)).astype(np.uint8)
        t[rng.random((n, n)) < unreachable] = 255
        np.fill_diagonal(t, 0)
        np.save(d / f"tt_{win}.npy", t)
        (d / f"tt_{win}.fingerprint.json").write_text(json.dumps({"complete": True}))
    Walksheds.build([(i, *walkshed((p[1], p[2]), 700)) for i, p in enumerate(pts)]).save(d / "walksheds.npz")
    outputs = {}
    for name in [*(f"tt_{w}" for w in WINDOWS), "point_ok.npy", "walksheds.npz", "points.csv"]:
        file = f"{name}.npy" if name.startswith("tt_") else name
        data = (d / file).read_bytes()
        outputs[name] = {"file": file, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        if name.startswith("tt_"):
            outputs[name]["shape"] = [n, n]
    manifest = {
        "schema": 1, "build": f"rand-{seed}", "timezone": "Europe/London",
        "service_dates": {"weekday": "2026-12-09", "saturday": "2026-12-12"},
        "windows": {w: {"day": day, "start": start, "minutes": 60} for w, (day, start) in WINDOWS.items()},
        "valid_until": (date.today() + timedelta(days=30)).isoformat(),
        "outputs": outputs,
        "attribution": ["Powered by TfL Open Data", "(c) OpenStreetMap contributors"],
        "licences_cleared": cleared,
    }
    (d / "manifest.json").write_text(json.dumps(manifest))
    if n_venues:
        write_venues(d, random_venues(n_venues, seed, 1.4 * spread_m))
    return d


# ── venues ──────────────────────────────────────────────────────────
VENUE_KINDS = ("pub", "bar", "cafe", "restaurant", "park")
CLIP_SHA = "c1" * 32


def random_venues(n: int, seed: int = 0, spread_m: float = 2500) -> list[tuple]:
    """n (id, kind, name, lon, lat) around CENTRE, sorted as transit/venues.py writes them."""
    rng = np.random.default_rng(seed + 1000)
    m_lat = 1 / 111_195
    m_lon = m_lat / np.cos(np.radians(CENTRE[1]))
    out = []
    for i in range(n):
        dx, dy = rng.uniform(-spread_m, spread_m, 2)
        kind = VENUE_KINDS[int(rng.integers(len(VENUE_KINDS)))]
        out.append((f"node/{i + 1}", kind, f"The {kind.title()} {i + 1}",
                    CENTRE[0] + dx * m_lon, CENTRE[1] + dy * m_lat))
    return sorted(out, key=lambda v: (VENUE_KINDS.index(v[1]), int(v[0].split("/")[1])))


def write_venues(d: Path, rows: list[tuple], *, listed: bool = False) -> Path:
    """
    venues.csv from (id, kind, name, lon, lat) rows, recorded the way a build does it: in
    venues.json with its sha256 and the OSM clip's, which the manifest's inputs carry too; and,
    with `listed` (the manifest stage), in the manifest's outputs as well.
    """
    path = d / "venues.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "kind", "name", "lon", "lat"])
        w.writerows(rows)
    data = path.read_bytes()
    entry = {"file": "venues.csv", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    manifest = json.loads((d / "manifest.json").read_text())
    manifest.setdefault("inputs", {})["osm_clip"] = {"file": "london.osm.pbf", "sha256": CLIP_SHA}
    (d / "venues.json").write_text(json.dumps({"schema": 1, "csv": entry, "osm_clip_sha256": CLIP_SHA}))
    if listed:
        manifest["outputs"]["venues.csv"] = entry
    (d / "manifest.json").write_text(json.dumps(manifest))
    return path
