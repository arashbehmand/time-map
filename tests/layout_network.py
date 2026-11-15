"""A synthetic transit build for the layout tests, shaped like transit/out/<build>/.

36 stations on a jittered 6 × 6 grid 1.5 km apart, joined to their neighbours on foot. An
express runs between the far corner G55 ("W") and G22 ("B"), 7 minutes one way and 8 the
other, standing in for the Metropolitan line. Around them, one case for each prep rule:

- T00 is 20 m from G00 (merged) and has its own fast link to G01;
- N50 is 60 m from G50 (kept apart);
- Z1 and Z2 are 300 m apart with 0 minutes between them (floored to 0.5);
- G02 -> G53 is 255 one way only (the pair is dropped);
- I1 and I2 reach only each other (an island, left out);
- X1 is not ok in points.csv, X2 is masked in point_ok.npy, and two rows are cells.

Every file is written into a temporary folder by the tests; no table is ever committed.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path

import numpy as np

BUILD = "2026-01-07"
WINDOW = "wd13"
LON0, LAT0 = -0.12, 51.50
M_PER_DEG = 111_320.0
WALK_M_PER_MIN = 4000 / 60
EXPRESS = ("syn:G55", "syn:G22", 7, 8)     # from, to, minutes there, minutes back
ONE_WAY = ("syn:G02", "syn:G53")          # 255 in this direction only

CHECKS_TOML = """
[wembley]
station = "syn:G55"
reference = "syn:G22"

[[metropolitan]]
name = "Express corridor"
stations = ["syn:G22", "syn:G33", "syn:G44", "syn:G55", "syn:NOPE"]

[labels]
stations = ["syn:G22", "syn:G55", "syn:NOPE"]
"""


def _stations() -> dict[str, tuple[float, float]]:
    """id -> (x east, y north) in metres."""
    rng = np.random.default_rng(7)
    st = {}
    for r in range(6):
        for c in range(6):
            jx, jy = rng.uniform(-150, 150, 2)
            st[f"syn:G{r}{c}"] = (c * 1500 + jx, r * 1500 + jy)
    gx, gy = st["syn:G00"]
    st["syn:T00"] = (gx + 20, gy)
    hx, hy = st["syn:G50"]
    st["syn:N50"] = (hx, hy + 60)
    zx, zy = st["syn:G11"]
    st["syn:Z1"] = (zx + 400, zy + 400)
    st["syn:Z2"] = (zx + 700, zy + 400)
    st["syn:I1"] = (20000, 3000)
    st["syn:I2"] = (20500, 3000)
    st["syn:X1"] = (3000, 9000)
    st["syn:X2"] = (3500, 9000)
    return st


def _walk(a, b) -> float:
    return math.dist(a, b) / WALK_M_PER_MIN + 1


def table_minutes(st: dict[str, tuple[float, float]], ids: list[str]) -> np.ndarray:
    """Whole-minute shortest paths (uint8, 255 = no time) between the stations in `ids`."""
    n = len(ids)
    at = {s: i for i, s in enumerate(ids)}
    D = np.full((n, n), np.inf)
    np.fill_diagonal(D, 0.0)

    def link(a, b, ab, ba=None):
        D[at[a], at[b]] = min(D[at[a], at[b]], ab)
        D[at[b], at[a]] = min(D[at[b], at[a]], ab if ba is None else ba)

    for r in range(6):
        for c in range(6):
            a = f"syn:G{r}{c}"
            for b in (f"syn:G{r}{c + 1}" if c < 5 else None, f"syn:G{r + 1}{c}" if r < 5 else None):
                if b:
                    link(a, b, _walk(st[a], st[b]))
    link("syn:T00", "syn:G01", 3)
    link("syn:N50", "syn:G50", 1)
    link("syn:Z1", "syn:G11", _walk(st["syn:Z1"], st["syn:G11"]))
    link("syn:Z1", "syn:Z2", 0)
    link("syn:I1", "syn:I2", 5)
    link("syn:X1", "syn:G52", _walk(st["syn:X1"], st["syn:G52"]))
    link("syn:X2", "syn:G53", _walk(st["syn:X2"], st["syn:G53"]))
    w, b, there, back = EXPRESS
    link(w, b, there, back)
    for k in range(n):
        D = np.minimum(D, D[:, k, None] + D[None, k, :])
    out = np.full((n, n), 255, dtype=np.uint8)
    fin = np.isfinite(D)
    rounded = np.floor(D[fin] + 0.5)
    vals = np.where(rounded <= 180, rounded, 255).astype(np.uint8)
    out[fin] = vals
    out[at[ONE_WAY[0]], at[ONE_WAY[1]]] = 255
    return out


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_build(root: Path, build: str = BUILD, table_hook=None, dtype=np.uint8, manifest_hook=None) -> Path:
    """Write the synthetic build into root/<build>/ and return that folder. `table_hook(t, at)`
    may change the table (as uint8, with `at` mapping ids to rows) before it is hashed;
    `manifest_hook(manifest)` may change the manifest before it is written."""
    out = Path(root) / build
    out.mkdir(parents=True, exist_ok=True)
    st = _stations()
    ids = sorted(st)
    cells = [("88194ad86dfffff", 1000.0, -2000.0), ("88194ad87dfffff", 2500.0, -2000.0)]
    rows = []
    for sid in ids:
        x, y = st[sid]
        rows.append({"kind": "station", "id": sid, "name": f"Syn {sid.split(':')[1]}", "x": x, "y": y,
                     "ok": sid != "syn:X1"})
    for cid, x, y in cells:
        rows.append({"kind": "cell", "id": cid, "name": "", "x": x, "y": y, "ok": True})
    n = len(rows)
    t = np.full((n, n), 255, dtype=np.uint8)
    np.fill_diagonal(t, 0)
    t[: len(ids), : len(ids)] = table_minutes(st, ids)
    at = {r["id"]: i for i, r in enumerate(rows)}
    if table_hook:
        table_hook(t, at)
    ok = np.ones(n, dtype=bool)
    ok[at["syn:X2"]] = False
    ok[at[cells[1][0]]] = False

    cos0 = math.cos(math.radians(LAT0))
    with open(out / "points.csv", "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["idx", "kind", "id", "name", "lon", "lat", "inside_gl", "snap_m", "ok", "snap_lon",
                     "snap_lat"])
        for i, r in enumerate(rows):
            lon = LON0 + r["x"] / (M_PER_DEG * cos0)
            lat = LAT0 + r["y"] / M_PER_DEG
            wr.writerow([i, r["kind"], r["id"], r["name"], f"{lon:.8f}", f"{lat:.8f}", True, 0.0,
                         r["ok"], f"{lon:.8f}", f"{lat:.8f}"])
    np.save(out / f"tt_{WINDOW}.npy", t.astype(dtype))
    np.save(out / "point_ok.npy", ok)
    table = out / f"tt_{WINDOW}.npy"
    inputs = {"points_sha256": _sha(out / "points.csv"), "departure": "2026-01-07T13:00:00"}
    manifest = {
        "build": build, "valid_until": "2026-02-01", "licences_cleared": False,
        "attribution": ["Synthetic test network"],
        "settings": {"r5": {"max_time_min": 180}},
        "points": {"total": n},
        "tables_fingerprints": {WINDOW: inputs},
        "outputs": {f"tt_{WINDOW}": {"file": table.name, "bytes": table.stat().st_size,
                                     "sha256": _sha(table), "shape": [n, n],
                                     "departure": "2026-01-07T13:00:00"},
                    "point_ok.npy": {"file": "point_ok.npy", "sha256": _sha(out / "point_ok.npy")}},
    }
    if manifest_hook:
        manifest_hook(manifest)
    (out / "manifest.json").write_text(json.dumps(manifest))
    (out / f"tt_{WINDOW}.fingerprint.json").write_text(json.dumps({"inputs": inputs, "complete": True}))
    return out


def write_checks(path: Path) -> Path:
    Path(path).write_text(CHECKS_TOML)
    return Path(path)
