"""
Download every input once per build, with sha256, size, Last-Modified and the URL it came
from. A file whose recorded sha256 still matches is not downloaded again. Files behind a
login can be placed in the raw folder by hand; they are hashed the same way.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from transit import settings

log = logging.getLogger("transit.fetch")

GITHUB_API = "https://api.github.com/repos/{repo}/releases"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_sources() -> dict:
    with open(settings.SOURCES_TOML, "rb") as f:
        return tomllib.load(f)


def _meta_path(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def download(url: str, dest: Path, headers: dict | None = None, params: dict | None = None,
             redact: tuple[str, ...] = (), identity: dict | None = None) -> dict:
    """
    Stream url to dest. Skips the download when dest exists, its recorded sha256 matches and it
    came from the same URL and `identity` (e.g. a release tag); a file recorded as something
    else is refused, never relabelled. `redact` lists query parameters that must not be written
    to the metadata (keys).
    """
    import httpx

    meta_file = _meta_path(dest)
    if dest.exists() and meta_file.exists():
        meta = json.loads(meta_file.read_text())
        same = meta.get("url") == url and all(meta.get(k) == v for k, v in (identity or {}).items())
        if not same:
            raise SystemExit(f"{dest} was downloaded from {meta.get('url')} "
                             f"({ {k: meta.get(k) for k in (identity or {})} }), not {url} {identity or ''}: "
                             f"use a new build id, or delete the file")
        if meta.get("sha256") == sha256_file(dest):
            log.info("have %s", dest.name)
            return meta
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = {"User-Agent": settings.USER_AGENT, **(headers or {})}
    log.info("download %s", url)
    with httpx.stream("GET", url, headers=h, params=params, follow_redirects=True,
                      timeout=httpx.Timeout(60, read=300)) as resp:
        resp.raise_for_status()
        with open(part, "wb") as f:
            for chunk in resp.iter_bytes(1 << 20):
                f.write(chunk)
        # The query string can carry a key: never write it down.
        final_url = str(resp.url).split("?")[0] if redact else str(resp.url)
        meta = {
            "url": url,
            "final_url": final_url,
            "last_modified": resp.headers.get("last-modified"),
            "content_disposition": resp.headers.get("content-disposition"),
            "etag": resp.headers.get("etag"),
        }
    os.replace(part, dest)
    meta.update({"file": dest.name, "size": dest.stat().st_size, "sha256": sha256_file(dest),
                 "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **(identity or {})})
    meta_file.write_text(json.dumps(meta, indent=2))
    return meta


def record_local(path: Path, note: str) -> dict:
    """Metadata for a file placed by hand."""
    meta = {"file": path.name, "url": None, "note": note, "size": path.stat().st_size,
            "sha256": sha256_file(path),
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _meta_path(path).write_text(json.dumps(meta, indent=2))
    return meta


def github_release(repo: str, tag: str | None) -> dict:
    import httpx

    url = GITHUB_API.format(repo=repo) + (f"/tags/{tag}" if tag else "/latest")
    resp = httpx.get(url, headers={"User-Agent": settings.USER_AGENT,
                                   "Accept": "application/vnd.github+json"}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def check_disk(path: Path, min_free_gb: float) -> float:
    path.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(path).free / 1e9
    if free < min_free_gb:
        raise SystemExit(f"Only {free:.1f} GB free under {path}; the build needs {min_free_gb} GB.")
    return free


def fetch(build: str, gb_transit_tag: str | None = None, with_tfl_txc: bool = False) -> dict:
    """Download the inputs for one build into data/raw/<build>/ and write sources.json."""
    raw = settings.RAW / build
    check_disk(settings.DATA, settings.MIN_FREE_GB)
    src = load_sources()
    out: dict = {"build": build, "files": {}}

    # gb-transit: National Rail + TfL tube/DLR/tram/river/cable car, as GTFS.
    gb = src["gb_transit"]
    release = github_release(gb["repo"], gb_transit_tag)
    assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
    missing = [a for a in gb["assets"] if a not in assets]
    if missing:
        raise SystemExit(f"gb-transit release {release.get('tag_name')} lacks {missing}")
    for name in gb["assets"]:
        # The release identity is stored with the download and checked on reuse.
        out["files"][f"gb_transit/{name}"] = download(
            assets[name], raw / "gb_transit" / name,
            identity={"release_tag": release.get("tag_name"),
                      "release_published_at": release.get("published_at")})
    out["gb_transit_release"] = {"tag": release.get("tag_name"),
                                 "published_at": release.get("published_at"),
                                 "notes": (release.get("body") or "")[:2000]}

    # BODS London-region GTFS (buses). Downloads without a login; a file placed by hand
    # (bods/itm_london_gtfs.zip) is used as it is.
    bods = src["bods"]
    bods_dest = raw / "bods" / bods["file"]
    if bods_dest.exists() and not _meta_path(bods_dest).exists():
        out["files"]["bods/" + bods["file"]] = record_local(bods_dest, "placed by hand")
    else:
        params = {"api_key": os.environ["BODS_API_KEY"]} if os.environ.get("BODS_API_KEY") else None
        out["files"]["bods/" + bods["file"]] = download(bods["url"], bods_dest, params=params,
                                                         redact=("api_key",))

    # OSM: Geofabrik England, checked against its .md5.
    osm = src["osm"]
    pbf = raw / "osm" / osm["file"]
    md5 = download(osm["url"] + ".md5", raw / "osm" / (osm["file"] + ".md5"))
    clipped = settings.WORK / build / "london.osm.pbf"
    if clipped.exists() and not pbf.exists():
        log.info("England PBF already clipped and deleted; keeping its recorded metadata")
        out["files"]["osm/" + osm["file"]] = json.loads(_meta_path(pbf).read_text())
    else:
        meta = download(osm["url"], pbf)
        expected = (raw / "osm" / (osm["file"] + ".md5")).read_text().split()[0]
        meta["md5"] = md5_file(pbf)
        if meta["md5"] != expected:
            raise SystemExit("England PBF md5 does not match Geofabrik's .md5 file")
        _meta_path(pbf).write_text(json.dumps(meta, indent=2))
        out["files"]["osm/" + osm["file"]] = meta
    out["files"]["osm/" + osm["file"] + ".md5"] = md5

    # England and Wales bank holidays.
    out["files"]["bank-holidays.json"] = download(src["bank_holidays"]["url"],
                                                  raw / "bank-holidays.json")

    # TfL's own TransXChange timetables: for the UK2GTFS check and the bus fallback.
    if with_tfl_txc:
        tfl = src["tfl_txc"]
        out["files"]["tfl/" + tfl["file"]] = download(tfl["url"], raw / "tfl" / tfl["file"])

    (raw / "sources.json").write_text(json.dumps(out, indent=2))
    return out
