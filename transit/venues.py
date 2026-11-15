"""
Meetup venues (#19): named pubs, bars, cafés, restaurants and parks from the build's own
OpenStreetMap clip, for the meetup to meet somewhere people can sit.

    python -m transit.venues --build <id>      # on its own, against any build that has its clip
    python -m transit.build venues --build <id> # the same, as a stage (`all` runs it)

osmium (already needed to make the clip) filters and exports the clip to GeoJSON lines; the
rest is shapely. Writes transit/out/<build>/venues.csv (id, kind, name, lon, lat) and
venues.json (the csv's size and sha256, the clip's sha256, counts and rules). The server
(server/transit/venues.py) reads them and works out which venues are within a 10-minute walk
of a station from the build's own walksheds, so nothing here depends on the tables.

Derived from OpenStreetMap under the Open Database License, like the tables: the credit
"© OpenStreetMap contributors" is already in every build's attribution.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

log = logging.getLogger("transit.venues")

# The kinds and the tag that makes each, in the order a feature with two kinds is read. The
# server keeps its own copy (server/transit/venues.py); tests/test_meetup_venues.py checks they agree.
KINDS = {"pub": ("amenity", "pub"), "bar": ("amenity", "bar"), "cafe": ("amenity", "cafe"),
         "restaurant": ("amenity", "restaurant"), "park": ("leisure", "park")}
PARK_MIN_M2 = 5000.0        # 0.5 ha
DEDUPE_M = 50.0             # same kind, same name, this close: one venue drawn twice
PARK_STATION_M = 800.0      # a park's pin is its edge nearest a station this close
NAME_MAX = 80
PRIVATE = {"private", "no"}
EARTH_R_M = 6371008.8
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_TYPES = {"n": "node", "w": "way", "r": "relation"}


@dataclass(frozen=True)
class Venue:
    id: str                 # node/123, way/456 or relation/789
    kind: str
    name: str
    lon: float
    lat: float

    def key(self) -> tuple:
        return (list(KINDS).index(self.kind), _id_key(self.id))


def _id_key(osm_id: str) -> tuple[int, int]:
    t, _, n = osm_id.partition("/")
    order = ("node", "way", "relation")
    return (order.index(t) if t in order else len(order), int(n) if n.isdigit() else 0)


def osm_id(raw) -> str:
    """osmium's --add-unique-id=type_id to an OSM id: 'n123' → 'node/123'. An area is 'a' and
    twice its way's id ('a20' → 'way/10'), or twice its relation's plus one ('a41' →
    'relation/20')."""
    raw = str(raw)
    head, digits = raw[:1], raw[1:]
    if not digits.isdigit():
        return raw
    if head == "a":
        n = int(digits)
        return f"way/{n // 2}" if n % 2 == 0 else f"relation/{n // 2}"
    return f"{_TYPES[head]}/{digits}" if head in _TYPES else raw


def clean_name(raw) -> str:
    name = " ".join(_CONTROL.sub(" ", str(raw)).split())
    return name[:NAME_MAX].rstrip()


def kind_of(tags: dict) -> str | None:
    for kind, (key, value) in KINDS.items():
        if tags.get(key) == value:
            return kind
    return None


def parse_lines(lines: Iterable[str]) -> Iterable[dict]:
    """GeoJSON text sequence (RFC 8142, as osmium writes it): one feature a line, each line
    possibly starting with the record separator."""
    for line in lines:
        line = line.lstrip("\x1e").strip()
        if line:
            yield json.loads(line)


# ── geometry, in metres around a point ──────────────────────────────
class _Frame:
    """Equirectangular metres around (lon0, lat0): within a park's few kilometres, its areas
    and distances are well under 1% out, and no projection library is needed."""

    def __init__(self, lon0: float, lat0: float):
        self.lon0, self.lat0 = lon0, lat0
        self.kx = math.radians(1) * EARTH_R_M * math.cos(math.radians(lat0))
        self.ky = math.radians(1) * EARTH_R_M

    def fwd(self, geom):
        import shapely
        return shapely.transform(geom, lambda c: _xy(c, self.lon0, self.lat0, self.kx, self.ky))

    def back(self, x: float, y: float) -> tuple[float, float]:
        return self.lon0 + x / self.kx, self.lat0 + y / self.ky


def _xy(c, lon0, lat0, kx, ky):
    out = c.copy()
    out[:, 0] = (c[:, 0] - lon0) * kx
    out[:, 1] = (c[:, 1] - lat0) * ky
    return out


def park_point(poly, stations: list[tuple[float, float]]) -> tuple[float, float] | None:
    """
    Where to meet in a park (lon/lat polygon), or None when it is under PARK_MIN_M2: the
    closest station within PARK_STATION_M itself when it stands in the park, else the point of
    the park's edge nearest that station (where people arrive), else a point inside the park.
    """
    import numpy as np
    import shapely
    from shapely.ops import nearest_points

    c = poly.centroid
    frame = _Frame(c.x, c.y)
    local = frame.fwd(poly)
    if local.area < PARK_MIN_M2:
        return None
    q = local.representative_point()
    if stations:
        xy = np.asarray(stations, dtype=np.float64)
        pts = shapely.points((xy[:, 0] - frame.lon0) * frame.kx, (xy[:, 1] - frame.lat0) * frame.ky)
        d = shapely.distance(local, pts)
        i = int(np.argmin(d))
        if d[i] <= PARK_STATION_M:
            q = pts[i] if d[i] == 0 else nearest_points(local.boundary, pts[i])[0]
    return frame.back(q.x, q.y)


def features_to_venues(features: Iterable[dict], stations: list[tuple[float, float]]) -> list[Venue]:
    """
    Venues from GeoJSON features (properties = OSM tags, id = osmium's type_id): named, not
    private, of a kind in KINDS. A pub, bar, café or restaurant drawn as an area is pinned inside
    it; a park of at least PARK_MIN_M2 at park_point. One drawn twice (same kind and name within
    DEDUPE_M) is kept once, under the lowest id. Sorted by kind, then id.
    """
    from shapely.geometry import shape

    found: list[Venue] = []
    for f in features:
        tags = f.get("properties") or {}
        kind = kind_of(tags)
        name = clean_name(tags.get("name", ""))
        if kind is None or not name or tags.get("access") in PRIVATE:
            continue
        geom = f.get("geometry") or {}
        gtype = geom.get("type")
        try:
            g = shape(geom)
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
        if g.is_empty:
            continue
        if kind == "park":
            if gtype not in ("Polygon", "MultiPolygon"):
                continue            # a park drawn as a point or a line has no area to check
            at = park_point(g, stations)
            if at is None:
                continue
        elif gtype == "Point":
            at = (g.x, g.y)
        elif gtype in ("Polygon", "MultiPolygon"):
            p = g.point_on_surface()
            at = (p.x, p.y)
        else:
            continue
        found.append(Venue(osm_id(f.get("id", "")), kind, name, round(at[0], 7), round(at[1], 7)))
    return dedupe(found)


def dedupe(venues: list[Venue], gap_m: float = DEDUPE_M) -> list[Venue]:
    kept: dict[tuple[str, str], list[Venue]] = {}
    out = []
    for v in sorted(venues, key=Venue.key):
        same = kept.setdefault((v.kind, v.name.casefold()), [])
        if any(_metres(v, k) <= gap_m for k in same):
            continue
        same.append(v)
        out.append(v)
    return sorted(out, key=Venue.key)


def _metres(a: Venue, b: Venue) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dl = math.radians(b.lon - a.lon)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(h))


# ── files ───────────────────────────────────────────────────────────
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stations_of(out_dir: Path) -> list[tuple[float, float]]:
    """The usable stations' points from the build's points.csv (and point_ok.npy if there)."""
    ok = None
    if (out_dir / "point_ok.npy").exists():
        import numpy as np
        ok = np.load(out_dir / "point_ok.npy")
    out = []
    with open(out_dir / "points.csv", newline="") as f:
        for i, r in enumerate(csv.DictReader(f)):
            if r["kind"] == "station" and (ok[i] if ok is not None else str(r.get("ok")).lower() == "true"):
                out.append((float(r["lon"]), float(r["lat"])))
    return out


def extract(clip: Path, work: Path) -> Path:
    """The clip's candidate features as GeoJSON lines (osmium tags-filter, then export)."""
    work.mkdir(parents=True, exist_ok=True)
    filtered = work / "venues.osm.pbf"
    lines = work / "venues.geojsonseq"
    amenities = ",".join(v for k, v in KINDS.values() if k == "amenity")
    subprocess.run(["osmium", "tags-filter", str(clip), f"nwr/amenity={amenities}", "nwr/leisure=park",
                    "-o", str(filtered), "--overwrite"], check=True)
    subprocess.run(["osmium", "export", str(filtered), "-f", "geojsonseq", "--geometry-types=point,polygon",
                    "--add-unique-id=type_id", "-o", str(lines), "--overwrite"], check=True)
    return lines


def write(out_dir: Path, venues: list[Venue], meta: dict) -> dict:
    path = out_dir / "venues.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "kind", "name", "lon", "lat"])
        for v in venues:
            w.writerow([v.id, v.kind, v.name, v.lon, v.lat])
    counts = {k: sum(v.kind == k for v in venues) for k in KINDS}
    info = {"schema": 1, "csv": {"file": path.name, "bytes": path.stat().st_size, "sha256": _sha256(path)},
            "counts": counts, "kinds": {k: f"{t}={v}" for k, (t, v) in KINDS.items()},
            "rules": {"park_min_m2": PARK_MIN_M2, "dedupe_m": DEDUPE_M, "park_station_m": PARK_STATION_M,
                      "name_max": NAME_MAX, "named_only": True, "access_left_out": sorted(PRIVATE)},
            **meta}
    (out_dir / "venues.json").write_text(json.dumps(info, indent=2))
    return info


def run(out_dir: Path, work: Path, clip: Path) -> dict:
    """Extract, read and write a build's venues. Returns venues.json's contents."""
    if not clip.exists():
        raise SystemExit(f"{clip} is missing: run `python -m transit.build prep --build {out_dir.name}` first")
    if not (out_dir / "points.csv").exists():
        raise SystemExit(f"{out_dir / 'points.csv'} is missing: run the points stage first")
    lines = extract(clip, work)
    with open(lines, encoding="utf-8") as f:
        venues = features_to_venues(parse_lines(f), stations_of(out_dir))
    prep = out_dir / "prep.json"
    as_of = json.loads(prep.read_text()).get("osm", {}).get("data_as_of") if prep.exists() else None
    osmium = subprocess.run(["osmium", "--version"], capture_output=True, text=True).stdout.splitlines()
    info = write(out_dir, venues, {"osm_clip_sha256": _sha256(clip), "osm_data_as_of": as_of,
                                   "osmium": osmium[0] if osmium else None})
    log.info("venues: %s", ", ".join(f"{n} {k}" for k, n in info["counts"].items()))
    return info


def main(argv: list[str] | None = None) -> int:
    from transit import settings

    p = argparse.ArgumentParser(prog="python -m transit.venues", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--build", required=True, help="build id (YYYY-MM-DD)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    run(settings.OUT / args.build, settings.WORK / args.build, settings.WORK / args.build / "london.osm.pbf")
    return 0


if __name__ == "__main__":
    sys.exit(main())
