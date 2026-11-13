"""
The London transit tables at runtime (#5, W-02): load one build of transit/ (T-01, #14) and
compose travel times from any pin to every place, without Java, R5 or the network.

    time(pin → place) = min over access points k near the pin and access points j near the
                        place of  walk(pin → k) + T[k, j] + walk(j → place)

which is #1's formula (transit.tables.compose_row, then the walksheds once more for the last
walk). Access points are stations and table cells; a table cell's row carries its buses.
Places are H3 res-10 cells, mapped onto the field's grid through a raster of cell indexes
built once at load.

Serving these times is publishing derived output (docs/transit-sources.md). So a build is
served only if its manifest says `licences_cleared: true` (the owner's licences), or for local
development with TRANSIT_ALLOW_UNCLEARED=1: to this machine only, and never on Railway.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import h3
import numpy as np
import shapely
from shapely.geometry import mapping

from server.localgrid import Grid, LocalFrame, mercator, metres_per_unit
from server.warp.field import WALK_M_PER_MIN
from transit.tables import Walksheds, compose_row, decode_minutes

log = logging.getLogger("time_map.transit")

LONDON = ZoneInfo("Europe/London")
WINDOWS = ("wd08", "wd13", "wd19", "sa13")
H3_RES = 10
RASTER_M = 50.0                 # raster pixel, metres (at the build's mean latitude)
RING_MINUTES = 2.0              # one res-10 ring further, on foot (about 131 m at 4 km/h)
MAX_RING = 3
STATION_SNAP_M = 100.0          # a pin this close to a station starts from its own row
STATION_WALK_M = 150.0          # around a station, the last steps are walked straight
FILL_PASSES = 3                 # 100 m grid: gaps up to about 300 m from a known node
COVERAGE_BUFFER_M = 1200.0
COVERAGE_SIMPLIFY_M = 300.0
GRID_MAX_HALF = 800                 # cells either side of the pin: the whole coverage box fits


class TransitUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def london_today() -> date:
    return datetime.now(LONDON).date()


def window_label(spec: dict) -> str:
    """'a weekday, 08:00–09:00' from {'day': 'weekday', 'start': '08:00', 'minutes': 60}."""
    h, m = map(int, spec["start"].split(":"))
    end = h * 60 + m + int(spec.get("minutes", 60))
    day = spec["day"] if spec["day"] == "weekday" else spec["day"].capitalize()
    return f"a {day}, {spec['start']}–{end // 60 % 24:02d}:{end % 60:02d}"


_SUFFIXES = (" Underground Station", " DLR Station", " Tram Stop", " Underground", " Und",
             " DLR", " Dlr", " LRT", " Lrt", " LT", " Lt", " Station")


def display_name(raw: str) -> str:
    """'WEMBLEY PARK UND' → 'Wembley Park'; 'Abbey Wood (London)' → 'Abbey Wood';
    'Bond Street (Elizabeth line)' stays as it is."""
    name = " ".join(raw.split())
    if name.isupper():
        name = " ".join(w[:1].upper() + w[1:].lower() for w in name.split(" "))
        name = "-".join(p[:1].upper() + p[1:] for p in name.split("-"))
    for suffix in _SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
            break
    return name.removesuffix(" (London)").strip()


@dataclass(frozen=True)
class Station:
    idx: int
    name: str
    lon: float
    lat: float


class TransitData:
    """One loaded build. Construct with load(); everything here is read-only afterwards."""

    def __init__(self, directory: Path, manifest: dict, lon: np.ndarray, lat: np.ndarray,
                 kind: np.ndarray, names: list[str], ok: np.ndarray, walksheds: Walksheds,
                 development_only: bool, snap_lon: np.ndarray | None = None,
                 snap_lat: np.ndarray | None = None):
        self.directory = directory
        self.manifest = manifest
        self.build = str(manifest.get("build", ""))
        self.valid_until = date.fromisoformat(manifest["valid_until"])
        self.windows = {w: manifest["windows"][w] for w in WINDOWS}
        self.development_only = development_only
        self.lon, self.lat, self.ok = lon, lat, ok
        # Where each point's times were measured from: R5 snaps it to the street network.
        self.snap_lon = lon if snap_lon is None else snap_lon
        self.snap_lat = lat if snap_lat is None else snap_lat
        self.is_station = kind == "station"
        self.walksheds = walksheds
        self.stations = [Station(int(i), display_name(names[i]), float(lon[i]), float(lat[i]))
                         for i in np.flatnonzero((kind == "station") & ok)]
        self._station_xy = None
        self._tables: dict[str, np.ndarray] = {}
        self._build_egress()
        self._build_raster()
        self._build_coverage()

    # ── loading ──────────────────────────────────────────────────────
    @classmethod
    def load(cls, directory: str | Path, *, allow_uncleared: bool = False,
             on_railway: bool = False, today: date | None = None,
             verify_hashes: bool = True) -> TransitData:
        d = Path(directory)
        try:
            manifest = json.loads((d / "manifest.json").read_text())
        except (OSError, ValueError):
            raise TransitUnavailable("missing_manifest") from None
        if manifest.get("schema") != 1 or not all(w in manifest.get("windows", {}) for w in WINDOWS):
            raise TransitUnavailable("bad_manifest")
        cleared = manifest.get("licences_cleared") is True
        development_only = False
        if not cleared:
            if not allow_uncleared:
                raise TransitUnavailable("licences_not_cleared")
            if on_railway:
                raise TransitUnavailable("override_refused_here")
            development_only = True
        try:
            valid_until = date.fromisoformat(manifest["valid_until"])
        except (KeyError, ValueError):
            raise TransitUnavailable("bad_manifest") from None
        if valid_until < (today or london_today()):
            raise TransitUnavailable("expired")
        outputs = manifest.get("outputs", {})
        for name in [*(f"tt_{w}" for w in WINDOWS), "point_ok.npy", "walksheds.npz", "points.csv"]:
            out = outputs.get(name)
            if not out:
                raise TransitUnavailable("bad_manifest")
            path = d / out["file"]
            if not path.is_file() or path.stat().st_size != out["bytes"]:
                raise TransitUnavailable("files_differ")
            if verify_hashes and _sha256(path) != out["sha256"]:
                raise TransitUnavailable("files_differ")
        for w in WINDOWS:
            try:
                fp = json.loads((d / f"tt_{w}.fingerprint.json").read_text())
            except (OSError, ValueError):
                raise TransitUnavailable("incomplete") from None
            if fp.get("complete") is not True:
                raise TransitUnavailable("incomplete")

        idx, kind, names, lon, lat, slon, slat = [], [], [], [], [], [], []
        with open(d / "points.csv", newline="") as f:
            for row in csv.DictReader(f):
                idx.append(int(row["idx"]))
                kind.append(row["kind"])
                names.append(row["name"])
                lon.append(float(row["lon"]))
                lat.append(float(row["lat"]))
                slon.append(_float_or(row.get("snap_lon"), lon[-1]))
                slat.append(_float_or(row.get("snap_lat"), lat[-1]))
        n = len(idx)
        if idx != list(range(n)):
            raise TransitUnavailable("bad_points")
        ok = np.load(d / "point_ok.npy")
        if ok.shape != (n,) or ok.dtype != bool:
            raise TransitUnavailable("bad_points")
        for w in WINDOWS:
            shape = tuple(outputs[f"tt_{w}"].get("shape", ()))
            if shape != (n, n):
                raise TransitUnavailable("bad_tables")
        walksheds = Walksheds.load(d / "walksheds.npz")
        if len(walksheds.points) and (walksheds.points.min() < 0 or walksheds.points.max() >= n):
            raise TransitUnavailable("bad_walksheds")
        return cls(d, manifest, np.asarray(lon), np.asarray(lat), np.asarray(kind), names, ok,
                   walksheds, development_only, np.asarray(slon), np.asarray(slat))

    def table(self, window: str) -> np.ndarray:
        if window not in self._tables:
            t = np.load(self.directory / f"tt_{window}.npy", mmap_mode="r")
            if t.shape != (len(self.ok), len(self.ok)) or t.dtype != np.uint8:
                raise TransitUnavailable("bad_tables")
            self._tables[window] = t
        return self._tables[window]

    # ── precomputed indexes ──────────────────────────────────────────
    def _build_egress(self) -> None:
        """Walkshed entries sorted by cell, so the last walk is one minimum.reduceat."""
        ws = self.walksheds
        rows = np.repeat(np.arange(len(ws.points)), np.diff(ws.indptr))
        order = np.argsort(ws.cells, kind="stable")
        cells = ws.cells[order]
        starts = np.flatnonzero(np.r_[True, cells[1:] != cells[:-1]]) if len(cells) else np.zeros(0, int)
        self.cells = cells[starts]                              # unique res-10 cells, sorted
        self._eg_starts = starts
        self._eg_point = ws.points[rows[order]].astype(np.int64)  # table index of the access point
        self._eg_minutes = ws.minutes[order].astype(np.float64)

    def _build_raster(self) -> None:
        """A Web Mercator raster of indexes into self.cells (−1: no walkshed cell there)."""
        if not len(self.cells):
            self._r = None
            return
        # Walkshed cells lie within 2 km of a usable point (stations reach 2 km, cells 1 km).
        lon, lat = self.lon[self.ok], self.lat[self.ok]
        mid = float(np.mean(lat))
        step = RASTER_M / metres_per_unit(mid)
        mx, my = mercator(np.array([lon.min(), lon.max()]), np.array([lat.max(), lat.min()]))
        pad = 2500 / metres_per_unit(mid)
        mx0, my0 = float(mx[0]) - pad, float(my[0]) - pad
        nx = int(math.ceil((float(mx[1]) + pad - mx0) / step)) + 1
        ny = int(math.ceil((float(my[1]) + pad - my0) / step)) + 1
        px = mx0 + step * np.arange(nx)
        py = my0 + step * np.arange(ny)
        lons = px * 360 - 180
        lats = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * py))))
        raster = np.empty((ny, nx), dtype=np.int32)
        for j, la in enumerate(lats.tolist()):
            ids = np.fromiter((h3.str_to_int(h3.latlng_to_cell(la, lo, H3_RES)) for lo in lons.tolist()),
                              dtype=np.uint64, count=nx)
            pos = np.searchsorted(self.cells, ids)
            pos = np.minimum(pos, len(self.cells) - 1)
            raster[j] = np.where(self.cells[pos] == ids, pos, -1)
        self._r = (mx0, my0, step, raster)

    def _build_coverage(self) -> None:
        """Where transit times are known: usable points, buffered and simplified (lon/lat)."""
        frame = LocalFrame(float(np.mean(self.lon[self.ok])), float(np.mean(self.lat[self.ok])))
        x, y = frame.to_local(self.lon[self.ok], self.lat[self.ok])
        area = shapely.union_all(shapely.buffer(shapely.points(x, y), COVERAGE_BUFFER_M, quad_segs=4))
        area = shapely.simplify(area, COVERAGE_SIMPLIFY_M)
        self.coverage_local_frame = frame
        self.coverage_local = area
        shapely.prepare(self.coverage_local)

        def back(coords: np.ndarray) -> np.ndarray:
            lon, lat = frame.from_local(coords[:, 0], coords[:, 1])
            return np.column_stack([np.round(lon, 5), np.round(lat, 5)])

        self.coverage = mapping(shapely.transform(area, back))
        lon0, lat0, lon1, lat1 = shapely.bounds(shapely.transform(area, back))
        self.coverage_bbox = (float(lon0), float(lat0), float(lon1), float(lat1))

    # ── queries ──────────────────────────────────────────────────────
    def valid_on(self, today: date | None = None) -> bool:
        return self.valid_until >= (today or london_today())

    def covered(self, lon: float, lat: float) -> bool:
        """Inside the area the tables cover: the same polygon the page enables the chip by."""
        x, y = self.coverage_local_frame.to_local(lon, lat)
        return bool(shapely.contains_xy(self.coverage_local, float(x), float(y)))

    def reachable_from(self, lon: float, lat: float) -> bool:
        """A journey can start here: a walkshed reaches the pin's cell (or a ring near it), or
        the pin is at a station."""
        return self.station_near(lon, lat) is not None or bool(self._access(lon, lat))

    def _access(self, lon: float, lat: float) -> list[tuple[int, float]]:
        """[(res-10 cell, extra walking minutes)] whose walkshed reaches: the pin's own cell,
        or the nearest ring of cells (up to MAX_RING) that has one."""
        cell = h3.latlng_to_cell(lat, lon, H3_RES)
        for k in range(MAX_RING + 1):
            ring = [cell] if k == 0 else list(h3.grid_ring(cell, k))
            hits = [(h3.str_to_int(c), k * RING_MINUTES) for c in ring
                    if len(self.walksheds.near(h3.str_to_int(c))[0])]
            if hits:
                return hits
        return []

    def station_near(self, lon: float, lat: float) -> int | None:
        if self._station_xy is None:
            self._station_xy = np.array([[s.lon, s.lat] for s in self.stations]) if self.stations else np.zeros((0, 2))
        if not len(self._station_xy):
            return None
        dx = (self._station_xy[:, 0] - lon) * 111_320 * math.cos(math.radians(lat))
        dy = (self._station_xy[:, 1] - lat) * 110_574
        d = np.hypot(dx, dy)
        i = int(np.argmin(d))
        return self.stations[i].idx if d[i] <= STATION_SNAP_M else None

    def row(self, lon: float, lat: float, window: str) -> np.ndarray:
        """Minutes from the pin to every table point (inf: none within the tables' limit)."""
        table = self.table(window)
        best = np.full(table.shape[1], np.inf)
        # The ring offset is a walk to the nearest cell with a walkshed: it belongs to the
        # cell access only, never to the row of a station the pin stands at.
        for cell, extra in self._access(lon, lat):
            best = np.minimum(best, compose_row(table, self.walksheds, cell) + extra)
        origin_point = self.station_near(lon, lat)
        if origin_point is not None:
            best = np.minimum(best, decode_minutes(table[origin_point]))
        best[~self.ok] = np.inf
        return best

    def cell_minutes(self, row: np.ndarray) -> np.ndarray:
        """Minutes to every walkshed cell (aligned with self.cells): the last walk."""
        vals = row[self._eg_point] + self._eg_minutes
        return np.minimum.reduceat(vals, self._eg_starts) if len(vals) else np.zeros(0)

    def grid_for(self, frame: LocalFrame, cell: float) -> Grid:
        """The whole coverage box, wherever the pin is in it (from Heathrow the tables reach
        56 km east)."""
        lon0, lat0, lon1, lat1 = self.coverage_bbox
        xs, ys = frame.to_local(np.array([lon0, lon1]), np.array([lat1, lat0]))
        return Grid.covering(cell, float(xs[0]), float(ys[0]), float(xs[1]), float(ys[1]),
                             max_half=GRID_MAX_HALF)

    def field(self, lon: float, lat: float, window: str, frame: LocalFrame,
              grid: Grid) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(minutes, filled, row) on the grid: whole minutes from the tables, NaN where no
        walkshed cell is known, inf where the tables have no time; gaps up to about 300 m
        filled from their neighbours and flagged."""
        row = self.row(lon, lat, window)
        per_cell = self.cell_minutes(row)
        X, Y = grid.mesh()
        mx0, my0 = frame.m0
        mx = mx0 + X / frame.m
        my = my0 + Y / frame.m
        out = np.full(X.shape, np.nan)
        if self._r is not None:
            rx0, ry0, step, raster = self._r
            ix = np.rint((mx - rx0) / step).astype(np.int64)
            iy = np.rint((my - ry0) / step).astype(np.int64)
            inside = (ix >= 0) & (iy >= 0) & (ix < raster.shape[1]) & (iy < raster.shape[0])
            ci = np.full(X.shape, -1, dtype=np.int64)
            ci[inside] = raster[iy[inside], ix[inside]]
            known = ci >= 0
            out[known] = per_cell[ci[known]]
        # A place that is itself a table point ends there: its own time, with no walk from a
        # nearby access point (compose_pair's dest_point), at the street point R5 measured it
        # from. A station is a place to be, so its node takes its time even over water (a
        # station on a bridge); a table cell only lowers a node that already holds a time.
        # Several points can share a node: np.fmin.at / np.minimum.at keep the quickest.
        px, py = frame.to_local(self.snap_lon[self.ok], self.snap_lat[self.ok])
        pi = np.rint((np.asarray(px) - grid.x0) / grid.cell).astype(np.int64)
        pj = np.rint((np.asarray(py) - grid.y0) / grid.cell).astype(np.int64)
        own = row[self.ok]
        station = self.is_station[self.ok]
        on = (pi >= 0) & (pj >= 0) & (pi < grid.nx) & (pj < grid.ny) & np.isfinite(own)
        cell_ok = on & ~station
        cell_ok[cell_ok] = np.isfinite(out[pj[cell_ok], pi[cell_ok]])
        st_ok = on & station
        np.minimum.at(out, (pj[cell_ok], pi[cell_ok]), own[cell_ok])
        np.fmin.at(out, (pj[st_ok], pi[st_ok]), own[st_ok])
        # And the last few steps from a station, straight at 4 km/h: within STATION_WALK_M the
        # walkshed's whole minutes from the nearest cell centre are coarser than the walk. It
        # only lowers nodes that already hold a time: water and places no walkshed reaches stay
        # as they are, so this never paints reach across a river.
        st = np.array([s.idx for s in self.stations], dtype=np.int64)
        if len(st):
            sx, sy = frame.to_local(self.snap_lon[st], self.snap_lat[st])
            sx, sy, smin = np.asarray(sx), np.asarray(sy), row[st]
            reach = int(np.ceil(STATION_WALK_M / grid.cell))
            ci = np.rint((sx - grid.x0) / grid.cell).astype(np.int64)
            cj = np.rint((sy - grid.y0) / grid.cell).astype(np.int64)
            known = np.isfinite(out)
            for dj in range(-reach, reach + 1):
                for di in range(-reach, reach + 1):
                    ni, nj = ci + di, cj + dj
                    ok = (ni >= 0) & (nj >= 0) & (ni < grid.nx) & (nj < grid.ny) & np.isfinite(smin)
                    d = np.hypot(grid.x0 + ni * grid.cell - sx, grid.y0 + nj * grid.cell - sy)
                    ok &= d <= STATION_WALK_M
                    ok[ok] = known[nj[ok], ni[ok]]
                    np.minimum.at(out, (nj[ok], ni[ok]), smin[ok] + d[ok] / WALK_M_PER_MIN)
        filled = np.zeros(X.shape, dtype=bool)
        for _ in range(FILL_PASSES):
            gap = np.isnan(out)
            vals = np.where(np.isfinite(out), out, 0.0)
            w = np.isfinite(out).astype(np.float64)
            num = np.zeros(X.shape)
            den = np.zeros(X.shape)
            for dj, di in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                num += _roll0(vals * w, dj, di)
                den += _roll0(w, dj, di)
            take = gap & (den > 0)
            out[take] = num[take] / den[take]
            filled |= take
        return out, filled, row

    def status(self) -> dict:
        return {
            "available": True,
            "build": self.build,
            "valid_until": self.valid_until.isoformat(),
            "windows": [{"id": w, "label": window_label(self.windows[w])} for w in WINDOWS],
            "coverage": self.coverage,
            "credits": credits(self.manifest),
            "development_only": self.development_only,
        }


def credits(manifest: dict) -> list[str]:
    seen, out = set(), []
    for c in manifest.get("attribution", []):
        c = str(c).replace("(c)", "©")
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _roll0(a: np.ndarray, dj: int, di: int) -> np.ndarray:
    """a shifted by (dj, di) rows and columns, with zeros (not wrap-around) at the edges."""
    out = np.zeros_like(a)
    H, W = a.shape
    out[max(dj, 0):H + min(dj, 0), max(di, 0):W + min(di, 0)] = \
        a[max(-dj, 0):H + min(-dj, 0), max(-di, 0):W + min(-di, 0)]
    return out


def _float_or(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class TransitHolder:
    """What the app keeps: the loaded build, or why there is none."""

    def __init__(self, configured: bool):
        self.configured = configured
        self.loading = configured
        self.data: TransitData | None = None
        self.reason = "loading" if configured else "not_configured"

    def load(self, directory: str, *, allow_uncleared: bool, on_railway: bool) -> None:
        try:
            self.data = TransitData.load(directory, allow_uncleared=allow_uncleared, on_railway=on_railway)
            self.reason = ""
            if self.data.development_only:
                log.warning("transit: serving tables whose licences are not cleared "
                            "(TRANSIT_ALLOW_UNCLEARED=1): local development only")
            log.info("transit: build %s loaded, valid until %s", self.data.build, self.data.valid_until)
        except TransitUnavailable as exc:
            self.reason = exc.reason
            log.warning("transit: not available (%s)", exc.reason)
        except Exception as exc:  # noqa: BLE001 - a bad build must never stop the app
            self.reason = "load_failed"
            log.warning("transit: load failed (%s)", type(exc).__name__)
        finally:
            self.loading = False

    def usable(self, today: date | None = None, *, local: bool = False) -> TransitData | None:
        """The build, if it may be served to this request: unexpired, and, if its licences
        are not cleared (the development override), only to a request from this machine."""
        if self.data is not None and not self.data.valid_on(today):
            self.reason = "expired"
            return None
        if self.data is not None and self.data.development_only and not local:
            return None
        return self.data

    def reason_for(self, local: bool) -> str:
        if self.data is not None and self.data.development_only and not local and self.data.valid_on():
            return "development_only"
        return self.reason or "unavailable"

    def status(self, *, local: bool = False) -> dict:
        data = self.usable(local=local)
        if data is not None:
            return {**data.status(), "loading": False, "reason": ""}
        return {"available": False, "loading": self.loading, "reason": self.reason_for(local)}
