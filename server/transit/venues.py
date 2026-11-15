"""
Meetup venues (issue #19): named pubs, bars, cafés, restaurants and parks from OpenStreetMap,
extracted offline next to a transit build (transit/venues.py writes venues.csv and
venues.json). This module only reads them: numpy and the standard library, nothing from the
build's own environment.

A build without venue files is normal: the meetup then meets at stations only. Venue files that
don't check out are left out, with the reason logged, and never stop the tables from loading.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("time_map.transit")

# The kinds and the OSM tag that makes each, in the order a feature with two kinds is read.
# transit/venues.py keeps its own copy (the image ships no build code); a test checks they agree.
KIND_TAGS = {"pub": ("amenity", "pub"), "bar": ("amenity", "bar"), "cafe": ("amenity", "cafe"),
             "restaurant": ("amenity", "restaurant"), "park": ("leisure", "park")}
VENUE_KINDS = tuple(KIND_TAGS)
# The longest walk from a station or table cell to a venue (minutes at 4 km/h, about 670 m).
VENUE_WALK_MAX_MIN = 10.0
# A straight walk from a pin to a venue near it is this much longer on the streets: typical
# urban route directness is 1.2 to 1.4 times the straight line.
WALK_DETOUR = 1.3
NAME_MAX = 80
WORD = {"pub": "pub", "bar": "bar", "cafe": "café", "restaurant": "restaurant", "park": "park"}
PLURAL = {"pub": "pubs", "bar": "bars", "cafe": "cafés", "restaurant": "restaurants", "park": "parks"}

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass(frozen=True)
class VenueRow:
    pid: str            # the OSM id: node/123, way/456 or relation/789
    kind: str
    name: str
    lon: float
    lat: float


def clean_name(raw: str) -> str:
    """Control characters out, spaces collapsed, at most NAME_MAX characters."""
    name = " ".join(_CONTROL.sub(" ", str(raw)).split())
    return name[:NAME_MAX].rstrip()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check(path: Path, entry: dict, verify_hashes: bool) -> str:
    """'' when the file matches its recorded size and sha256, else why not."""
    if not isinstance(entry, dict) or path.stat().st_size != entry.get("bytes"):
        return "venues_differ"
    if verify_hashes and _sha256(path) != entry.get("sha256"):
        return "venues_differ"
    return ""


def load_venues(directory: Path, manifest: dict, *,
                verify_hashes: bool = True) -> tuple[list[VenueRow] | None, str]:
    """
    (venues, reason): the build's venues, or None and why not ('' when there are simply no
    venue files). venues.csv is trusted when venues.json records its size and sha256 and was
    made from the same OSM clip as the tables (the manifest's inputs.osm_clip.sha256), so
    venues never come from other map data than the walksheds they are joined to. A manifest
    that lists venues.csv from an earlier run doesn't stop a newer extract of the same clip:
    venues.json is what vouches for the file, and the difference is logged.
    """
    d = Path(directory)
    path = d / "venues.csv"
    listed = (manifest.get("outputs") or {}).get("venues.csv")
    if not path.is_file():
        return None, "venues_missing" if listed else ""
    try:
        meta = json.loads((d / "venues.json").read_text())
    except (OSError, ValueError):
        return None, "venues_unrecorded"
    clip = ((manifest.get("inputs") or {}).get("osm_clip") or {}).get("sha256")
    if not clip or meta.get("osm_clip_sha256") != clip:
        return None, "venues_from_another_clip"
    reason = _check(path, meta.get("csv") or {}, verify_hashes)
    if reason:
        return None, reason
    if listed and _check(path, listed, verify_hashes):
        log.warning("venues: venues.csv differs from the manifest's record; venues.json vouches for it")
    rows, bad = [], 0
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                kind, name = r["kind"], clean_name(r["name"])
                lon, lat = float(r["lon"]), float(r["lat"])
            except (KeyError, TypeError, ValueError):
                bad += 1
                continue
            if kind not in KIND_TAGS or not name or not str(r.get("id") or "") \
                    or not (math.isfinite(lon) and math.isfinite(lat)) \
                    or not (-180 <= lon <= 180 and -90 <= lat <= 90):
                bad += 1
                continue
            rows.append(VenueRow(str(r["id"]), kind, name, lon, lat))
    if bad:
        log.warning("venues: %d rows left out (bad kind, name or point)", bad)
    return rows, ""
