"""A tiny transit build, shaped like transit/out/<build>/, for the provider and API tests.

Two stations 2.8 km apart east-west and two table cells; the second cell is masked (cut off).
Station A's walkshed has a small hole (a pond) 250 m east of it, so the field fills it in.
Cell C's walkshed lies 1.6 km north of the stations, so only C's row reaches it.
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
WINDOWS = {"wd08": ("weekday", "08:00"), "wd13": ("weekday", "13:00"),
           "wd19": ("weekday", "19:00"), "sa13": ("saturday", "13:00")}
WALK = 4000 / 60


def table(window: str = "wd08") -> np.ndarray:
    t = np.full((4, 4), 255, dtype=np.uint8)
    for i in range(3):
        t[i, i] = 0
    t[0, 1], t[1, 0] = 5, 6           # A ↔ B: a fast line
    t[0, 2], t[2, 0] = 12, 12         # A ↔ C
    t[1, 2], t[2, 1] = 9, 9           # B ↔ C
    if window == "sa13":
        t[0, 2] = t[1, 2] = 255       # on Saturday, nothing reaches C
    return t


def walkshed(point: tuple[float, float], radius: float, hole: tuple[float, float] | None = None):
    cells = disk_cells(point[0], point[1], 10, radius)
    if hole is not None:
        drop = set(disk_cells(hole[0], hole[1], 10, 60).tolist())
        cells = np.array([c for c in cells.tolist() if c not in drop], dtype=np.uint64)
    import h3
    lat, lon = np.array([h3.cell_to_latlng(h3.int_to_str(int(c))) for c in cells]).T
    minutes = haversine_m(point[0], point[1], lon, lat) / WALK
    return cells, minutes


def make_build(root: Path, *, cleared: bool = False, valid_until: date | None = None,
               complete: bool = True) -> Path:
    d = root / "build"
    d.mkdir(parents=True, exist_ok=True)
    rows = [(0, "station", "gbt:A", "ALPHA ROAD UND", *A, True),
            (1, "station", "gbt:B", "Bravo (London)", *B, True),
            (2, "cell", "88194ad000fffff", "", *C, True),
            (3, "cell", "88194ad001fffff", "", *D, False)]
    with open(d / "points.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["idx", "kind", "id", "name", "lon", "lat", "inside_gl", "snap_m", "ok",
                    "snap_lon", "snap_lat"])
        for idx, kind, pid, name, lon, lat, ok in rows:
            w.writerow([idx, kind, pid, name, lon, lat, True, 1.0, ok, lon, lat])
    np.save(d / "point_ok.npy", np.array([r[-1] for r in rows], dtype=bool))
    for win in WINDOWS:
        np.save(d / f"tt_{win}.npy", table(win))
        (d / f"tt_{win}.fingerprint.json").write_text(json.dumps({"complete": complete}))
    ws = Walksheds.build([
        (0, *walkshed(A, 900, hole=POND)),
        (1, *walkshed(B, 900)),
        (2, *walkshed(C, 400)),
    ])
    ws.save(d / "walksheds.npz")
    outputs = {}
    for name in [*(f"tt_{w}" for w in WINDOWS), "point_ok.npy", "walksheds.npz", "points.csv"]:
        file = f"{name}.npy" if name.startswith("tt_") else name
        data = (d / file).read_bytes()
        outputs[name] = {"file": file, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        if name.startswith("tt_"):
            outputs[name]["shape"] = [4, 4]
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
