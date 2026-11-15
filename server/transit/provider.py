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
from server.transit.venues import VENUE_KINDS, VENUE_WALK_MAX_MIN, WALK_DETOUR, VenueRow, load_venues
from server.warp.field import WALK_M_PER_MIN
from transit.tables import Walksheds, compose_row, decode_minutes, haversine_m

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
# The tables' error, as the median absolute minutes for the band of a trip's length: the
# owner's accepted lines (transit/settings.py ACCEPTED, 2026-11-13). The image ships only
# transit/tables.py, so they are copied here; a build's manifest records its own, which win.
TABLE_ERROR_MIN = {"le20": 3.0, "20to45": 7.0, "gt45": 7.0}


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
    pid: str = ""               # the point's id in points.csv


@dataclass(frozen=True)
class Place:
    """Somewhere to meet: one usable station, at the street point R5 measured its times from.
    Each is scored on its own column, so every time is to the point shown. Platforms with one
    name (Bank's Underground and DLR) stay separate places; the meetup's spacing rule keeps
    two of them from being listed together."""
    pid: str                    # the point's id in points.csv (a venue: its OSM id)
    name: str
    lon: float
    lat: float
    idx: int                    # table index (−1 for a venue)
    kind: str = "station"       # or a venue kind (server/transit/venues.py)


@dataclass(frozen=True, eq=False)
class VenueLinks:
    """One kind of venue and how each is reached: for venue v, the access points
    point[starts[v]:starts[v+1]] and the walk from each (minutes, at most VENUE_WALK_MAX_MIN)."""
    places: list[Place]
    point: np.ndarray           # int64 table indexes
    walk: np.ndarray            # float64 minutes
    starts: np.ndarray          # int64, one per venue
    lons: np.ndarray            # the venues' points, aligned with places
    lats: np.ndarray

    def minutes(self, row: np.ndarray) -> np.ndarray:
        """Minutes to each venue: the best over its access points of the table time plus the walk."""
        if not len(self.places):
            return np.zeros(0)
        return np.minimum.reduceat(np.asarray(row, dtype=np.float64)[self.point] + self.walk, self.starts)

    def walk_from(self, lon: float, lat: float, access: np.ndarray) -> np.ndarray:
        """
        Minutes on foot straight to each venue from a pin whose own access points are `access`
        (table indexes): the straight line times WALK_DETOUR at 4 km/h, for a venue that shares
        an access point with the pin and is within VENUE_WALK_MAX_MIN that way; inf for the
        rest. Without it, someone starting beside a pub would be timed to a station and back.
        Sharing an access point keeps the walk on the same side of a river or railway: the walk
        through that point is already a route between the two.
        """
        if not len(self.places) or not len(access):
            return np.full(len(self.places), np.inf)
        shared = np.logical_or.reduceat(np.isin(self.point, access), self.starts)
        walk = haversine_m(lon, lat, self.lons, self.lats) * WALK_DETOUR / WALK_M_PER_MIN
        return np.where(shared & (walk <= VENUE_WALK_MAX_MIN), walk, np.inf)


@dataclass(frozen=True, eq=False)
class Pool:
    """Somewhere to meet, of one kind: every station, then the venues of that kind. Scored
    together so the best station is there to compare the best venue with."""
    kind: str
    places: list[Place]
    is_venue: np.ndarray        # bool, aligned with places
    _place_idx: np.ndarray
    _venues: VenueLinks

    @property
    def venue_lons(self) -> np.ndarray:
        return self._venues.lons

    @property
    def venue_lats(self) -> np.ndarray:
        return self._venues.lats

    def minutes(self, row: np.ndarray, walk: np.ndarray | None = None) -> np.ndarray:
        """Minutes to every place from a table row; `walk` (from walk_from) is the straight
        walk to each venue, kept where it is quicker."""
        r = np.asarray(row, dtype=np.float64)
        venues = self._venues.minutes(r)
        if walk is not None:
            venues = np.minimum(venues, walk)
        return np.concatenate([r[self._place_idx], venues])

    def walk_from(self, lon: float, lat: float, access: np.ndarray) -> np.ndarray:
        return self._venues.walk_from(lon, lat, access)


class TransitData:
    """One loaded build. Construct with load(); everything here is read-only afterwards."""

    def __init__(self, directory: Path, manifest: dict, lon: np.ndarray, lat: np.ndarray,
                 kind: np.ndarray, names: list[str], ok: np.ndarray, walksheds: Walksheds,
                 development_only: bool, snap_lon: np.ndarray | None = None,
                 snap_lat: np.ndarray | None = None, ids: list[str] | None = None,
                 venues: list[VenueRow] | None = None):
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
        self.stations = [Station(int(i), display_name(names[i]), float(lon[i]), float(lat[i]),
                                 ids[i] if ids else str(i))
                         for i in np.flatnonzero((kind == "station") & ok)]
        self._station_xy = None
        self._build_places()
        self._tables: dict[str, np.ndarray] = {}
        self._build_egress()
        self._build_raster()
        self._build_coverage()
        try:
            self._build_venues(venues or [])
        except Exception as exc:  # noqa: BLE001 - venues are optional: never lose the tables to them
            log.warning("transit: venues left out (%s)", type(exc).__name__)
            self.venues, self._pools = {}, {}

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
        # Complete tables: a build folder has each table's fingerprint file; a published package
        # (transit.build's package stage) leaves those out, and its manifest's record of complete
        # tables stands in for them. Either way the sizes and hashes above pin the files to the
        # manifest.
        recorded = manifest.get("tables_fingerprints") or {}
        for w in WINDOWS:
            fp_path = d / f"tt_{w}.fingerprint.json"
            if not fp_path.exists():
                if w not in recorded:
                    raise TransitUnavailable("incomplete")
                continue
            try:
                fp = json.loads(fp_path.read_text())
            except (OSError, ValueError):
                raise TransitUnavailable("incomplete") from None
            if fp.get("complete") is not True:
                raise TransitUnavailable("incomplete")

        idx, kind, names, lon, lat, slon, slat, ids = [], [], [], [], [], [], [], []
        with open(d / "points.csv", newline="") as f:
            for row in csv.DictReader(f):
                idx.append(int(row["idx"]))
                ids.append(str(row.get("id") or row["idx"]))
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
        try:
            venues, why = load_venues(d, manifest, verify_hashes=verify_hashes)
        except Exception as exc:  # noqa: BLE001 - bad venue files must never stop the tables
            venues, why = None, f"venues_unreadable ({type(exc).__name__})"
        if why:
            log.warning("transit: venues left out (%s)", why)
        return cls(d, manifest, np.asarray(lon), np.asarray(lat), np.asarray(kind), names, ok,
                   walksheds, development_only, np.asarray(slon), np.asarray(slat), ids, venues)

    def table(self, window: str) -> np.ndarray:
        if window not in self._tables:
            t = np.load(self.directory / f"tt_{window}.npy", mmap_mode="r")
            if t.shape != (len(self.ok), len(self.ok)) or t.dtype != np.uint8:
                raise TransitUnavailable("bad_tables")
            self._tables[window] = t
        return self._tables[window]

    # ── precomputed indexes ──────────────────────────────────────────
    def _build_places(self) -> None:
        """One place per usable station, at its street point."""
        self.places = [Place(s.pid, s.name, float(self.snap_lon[s.idx]), float(self.snap_lat[s.idx]),
                             s.idx) for s in self.stations]
        self._place_idx = np.array([p.idx for p in self.places], dtype=np.int64)

    def place_minutes(self, row: np.ndarray) -> np.ndarray:
        """Minutes to every place (aligned with self.places): that station's own column."""
        return np.asarray(row, dtype=np.float64)[self._place_idx]

    def _build_venues(self, rows: list[VenueRow]) -> None:
        """Each venue's access points: the walksheds that reach its res-10 cell, else the nearest
        ring of cells (up to MAX_RING, RING_MINUTES a ring) that holds a walk of at most
        VENUE_WALK_MAX_MIN, ring minutes included, from a usable point. A venue with none is
        left out."""
        self.venues, self._pools = {}, {}
        found: dict[str, tuple[list, list, list, list]] = {k: ([], [], [], []) for k in VENUE_KINDS}
        for v in rows:
            pts, walks = [], []
            for cells, extra in self._rings(v.lon, v.lat):
                for cell in cells:
                    p, m = self.walksheds.near(cell)
                    w = m.astype(np.float64) + extra
                    keep = self.ok[p] & (w <= VENUE_WALK_MAX_MIN)
                    pts.append(p[keep].astype(np.int64))
                    walks.append(w[keep])
                if any(len(x) for x in pts):
                    break
            n = sum(len(x) for x in pts)
            if not n:
                continue
            places, point, walk, sizes = found[v.kind]
            places.append(Place(v.pid, v.name, v.lon, v.lat, -1, v.kind))
            point.extend(pts)
            walk.extend(walks)
            sizes.append(n)
        # A kind the file has is kept even when none of its venues is in reach: asked for, the
        # answer is then "none within a walk", not "not in these travel times".
        self.venues: dict[str, VenueLinks] = {}
        for kind in sorted({v.kind for v in rows}, key=VENUE_KINDS.index):
            places, point, walk, sizes = found[kind]
            starts = np.r_[0, np.cumsum(sizes)[:-1]].astype(np.int64) if sizes else np.zeros(0, np.int64)
            self.venues[kind] = VenueLinks(
                places, np.concatenate(point) if point else np.zeros(0, np.int64),
                np.concatenate(walk) if walk else np.zeros(0), starts,
                np.array([p.lon for p in places]), np.array([p.lat for p in places]))

    def pool(self, kind: str) -> Pool | None:
        """Every station, then the venues of this kind; None when the build has none."""
        if kind not in self.venues:
            return None
        if kind not in self._pools:
            links = self.venues[kind]
            self._pools[kind] = Pool(kind, self.places + links.places,
                                     np.r_[np.zeros(len(self.places), bool), np.ones(len(links.places), bool)],
                                     self._place_idx, links)
        return self._pools[kind]

    def tie_lines(self) -> dict[str, float]:
        """The tables' error by band (minutes): the build's accepted median lines when its
        manifest records them, else TABLE_ERROR_MIN."""
        lines = (self.manifest.get("settings") or {}).get("accepted_lines") or {}
        out = dict(TABLE_ERROR_MIN)
        for band, vals in (lines.get("band_lines") or {}).items():
            try:
                v = float(vals[0])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            if band in out and math.isfinite(v) and v >= 0:
                out[band] = v
        return out

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

    @staticmethod
    def _rings(lon: float, lat: float):
        """The point's res-10 cell, then each ring of cells around it up to MAX_RING, as
        ([cell ints], extra walking minutes)."""
        cell = h3.latlng_to_cell(lat, lon, H3_RES)
        for k in range(MAX_RING + 1):
            ring = [cell] if k == 0 else list(h3.grid_ring(cell, k))
            yield [h3.str_to_int(c) for c in ring], k * RING_MINUTES

    def _access(self, lon: float, lat: float) -> list[tuple[int, float]]:
        """[(res-10 cell, extra walking minutes)] whose walkshed reaches: the pin's own cell,
        or the nearest ring of cells (up to MAX_RING) that has one."""
        for cells, extra in self._rings(lon, lat):
            hits = [(c, extra) for c in cells if len(self.walksheds.near(c)[0])]
            if hits:
                return hits
        return []

    def access_points(self, lon: float, lat: float) -> np.ndarray:
        """The table points a journey from this pin can start at (as row() uses them)."""
        pts = [self.walksheds.near(c)[0] for c, _ in self._access(lon, lat)]
        own = self.station_near(lon, lat)
        if own is not None:
            pts.append(np.array([own]))
        return np.unique(np.concatenate(pts)).astype(np.int64) if pts else np.zeros(0, np.int64)

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
            "venues": {"kinds": {k: len(v.places) for k, v in self.venues.items()},
                       "walk_max_min": VENUE_WALK_MAX_MIN},
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
