"""
The one command that rebuilds the London transit tables.

    python -m transit.build all                 # every stage, in order
    python -m transit.build <stage> [<stage> ...] [options]   # some stages, in one process

Stages: fetch, dates, prep, check, network, points, journeys, tables, walksheds, manifest,
package. `all` runs them in order and skips a stage whose inputs and settings have not
changed since it last ran (use --force to redo it). The gate (python -m transit.gate) and
publishing (publish) are run on purpose, never by `all`.

Outputs go to transit/out/<build>/, downloads and work files to transit/data/ (git-ignored).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import tarfile
import time
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from transit import settings

log = logging.getLogger("transit.build")

STAGES = ("fetch", "dates", "prep", "check", "network", "points", "journeys", "tables",
          "walksheds", "manifest", "package")
FEEDS = {"gbt": ("gb_transit", "gtfs-rail-and-tfl.zip", None),
         "bods": ("bods", "itm_london_gtfs.zip", [3])}


# ── paths and small helpers ─────────────────────────────────────────
class Build:
    def __init__(self, build: str):
        try:
            self.day = date.fromisoformat(build)
        except ValueError:
            raise SystemExit(f"--build must be a date (YYYY-MM-DD), not {build!r}") from None
        self.id = build
        self.raw = settings.RAW / build
        self.work = settings.WORK / build
        self.out = settings.OUT / build
        for p in (self.raw, self.work, self.out):
            p.mkdir(parents=True, exist_ok=True)

    def raw_feed(self, fid: str) -> Path:
        folder, name, _ = FEEDS[fid]
        return self.raw / folder / name

    def prepped(self, fid: str) -> Path:
        return self.work / f"{fid}.gtfs.zip"

    @property
    def osm_clip(self) -> Path:
        return self.work / "london.osm.pbf"

    def read_json(self, name: str) -> dict:
        p = self.out / name
        if not p.exists():
            raise SystemExit(f"{p} is missing: run the stage that writes it first")
        return json.loads(p.read_text())

    def write_json(self, name: str, data) -> Path:
        p = self.out / name
        p.write_text(json.dumps(data, indent=2, default=str))
        return p

    def dates(self) -> tuple[date, date]:
        d = self.read_json("dates.json")
        return date.fromisoformat(d["weekday"]), date.fromisoformat(d["saturday"])

    def window_start(self, window: str) -> datetime:
        wd, sat = self.dates()
        kind, t = settings.WINDOWS[window]
        return datetime.combine(wd if kind == "weekday" else sat, t)


# The code each stage runs (besides build.py): a change to it re-runs the stage in `all`.
STAGE_CODE = {
    "fetch": ["fetch.py", "sources.toml"], "dates": ["feeds.py", "tables.py"],
    "prep": ["feeds.py", "points.py"], "check": ["feeds.py", "sources.toml"],
    "network": ["r5.py"], "points": ["points.py", "r5.py"],
    "journeys": ["r5.py", "tfl.py", "calibrate.py", "cases.toml"],
    "tables": ["r5.py", "tables.py", "calibrate.py"], "walksheds": ["r5.py", "tables.py"],
    "manifest": ["manifest.py"], "package": [],
}


def code_hash(files: list[str]) -> str:
    h = hashlib.sha256()
    for name in ["build.py", "settings.py", *files]:
        h.update(name.encode() + b"\0" + (settings.ROOT / name).read_bytes())
    return h.hexdigest()[:16]


def sha256(path: Path) -> str:
    from transit.fetch import sha256_file

    return sha256_file(path)


def digest(paths: list[Path], extra: dict | None = None, stage: str | None = None) -> str:
    h = hashlib.sha256()
    for p in paths:
        st = p.stat()
        h.update(f"{p.name}:{st.st_size}:{st.st_mtime_ns};".encode())
    h.update(json.dumps(settings.snapshot(), sort_keys=True).encode())
    if stage is not None:
        h.update(code_hash(STAGE_CODE[stage]).encode())
    if extra:
        h.update(json.dumps(extra, sort_keys=True, default=str).encode())
    return h.hexdigest()


def stage_done(b: Build, name: str, key: str, outputs: list[Path]) -> bool:
    rec = b.out / "stages" / f"{name}.json"
    if not rec.exists() or not all(o.exists() for o in outputs):
        return False
    return json.loads(rec.read_text()).get("key") == key


def mark_done(b: Build, name: str, key: str, started: float, info: dict | None = None) -> None:
    (b.out / "stages").mkdir(exist_ok=True)
    (b.out / "stages" / f"{name}.json").write_text(json.dumps(
        {"key": key, "seconds": round(time.monotonic() - started, 1),
         "finished_at": datetime.now().isoformat(timespec="seconds"), **(info or {})},
        indent=2, default=str))


def load_feeds(b: Build, tables=None) -> dict[str, dict]:
    from transit.feeds import TABLES, read_gtfs

    return {fid: read_gtfs(b.prepped(fid), tables or TABLES) for fid in FEEDS}


def sources() -> dict:
    with open(settings.SOURCES_TOML, "rb") as f:
        return tomllib.load(f)


def cases() -> dict:
    with open(settings.CASES_TOML, "rb") as f:
        return tomllib.load(f)


def app_key() -> str | None:
    """TFL_APP_KEY from the environment, else from the repo's .env (python-dotenv is pinned)."""
    if not os.environ.get("TFL_APP_KEY"):
        env = settings.ROOT.parent / ".env"
        try:
            from dotenv import load_dotenv
        except ImportError:
            if env.exists():
                log.warning("python-dotenv is not installed, so .env is not read: "
                            "pip install -r transit/requirements.txt, or export TFL_APP_KEY")
        else:
            load_dotenv(env)
    return os.environ.get("TFL_APP_KEY") or None


# ── stages ──────────────────────────────────────────────────────────
def stage_fetch(b: Build, args) -> None:
    from transit.fetch import fetch

    # A clip made from another polygon or extract has to be remade from England.
    fetch(b.id, gb_transit_tag=args.gb_transit_tag, with_tfl_txc=args.with_tfl_txc,
          need_england=not clip_is_current(b))


CORE_AGENCIES = r"(?i)london underground|docklands|london trams|london overground|elizabeth line"


def stage_dates(b: Build, args) -> None:
    """
    Pick the Wednesday and Saturday the build and the gate share. A week qualifies when every
    feed keeps at least 85% of its usual trips on both days and the core TfL operators run; of
    those, the week with the fewest closures wins (large operators with no trips that day, plus
    TfL's planned closures on Saturday), earliest on ties.
    """
    from transit.feeds import agency_of_trips, read_gtfs, trips_by_agency_on
    from transit.tables import candidate_weeks

    if (b.out / "dates.json").exists() and not args.redate:
        # The gate's cached TfL replies are for these dates: never re-pick them by accident.
        log.info("keeping the service dates in dates.json (--redate picks new ones)")
        return
    today = date.fromisoformat(args.today) if args.today else date.today()
    bh_raw = json.loads((b.raw / "bank-holidays.json").read_text())
    bank = {date.fromisoformat(e["date"]) for e in bh_raw["england-and-wales"]["events"]}
    feeds = {fid: read_gtfs(b.raw_feed(fid), ("agency", "routes", "trips", "calendar", "calendar_dates"))
             for fid in FEEDS}
    # Timetable changes are on Sundays and the week runs Wednesday to Saturday, so a week never
    # straddles one; the next change only ends the tables' validity (see stage_manifest).
    weeks = candidate_weeks(today, today, today + timedelta(days=120), bank, settings.DATE_LEAD_DAYS,
                            None, settings.WEEKDAY, count=settings.CANDIDATE_WEEKS)
    days = sorted({d for w in weeks for d in w})
    on = {fid: trips_by_agency_on(feed, days) for fid, feed in feeds.items()}
    big = {}
    for fid, feed in feeds.items():
        counts = agency_of_trips(feed).groupby("agency_name").size()
        big[fid] = [a for a in counts[counts >= 500].index if "replacement" not in a.lower()]

    import re

    def day_report(d: date) -> dict:
        same = [x for x in days if (x.weekday() == 5) == (d.weekday() == 5)]
        rep = {"ok": True, "missing_operators": [], "trips": {}}
        for fid in FEEDS:
            total = int(on[fid][d].sum())
            usual = max(int(on[fid][x].sum()) for x in same)
            rep["trips"][fid] = total
            if total < 0.85 * usual:
                rep["ok"] = False
            for a in big[fid]:
                if on[fid][d].get(a, 0) == 0:
                    rep["missing_operators"].append(a)
                    if re.search(CORE_AGENCIES, a):
                        rep["ok"] = False
        return rep

    reports = {d.isoformat(): day_report(d) for d in days}
    good = [w for w in weeks if reports[w[0].isoformat()]["ok"] and reports[w[1].isoformat()]["ok"]]
    if not good:
        b.write_json("dates-rejected.json", reports)
        raise SystemExit("no candidate week keeps every feed running: see dates-rejected.json")
    tfl_closures: dict[str, int] = {}
    if not args.no_tfl:
        try:
            tfl_closures = asyncio.run(_saturday_closures([w[1] for w in good]))
        except Exception as exc:  # noqa: BLE001 - closures only rank the weeks
            log.warning("could not read TfL planned closures (%s); ranking by the feeds only",
                        type(exc).__name__)

    def score(w) -> int:
        feed = sum(len(reports[d.isoformat()]["missing_operators"]) for d in w)
        return feed + tfl_closures.get(w[1].isoformat(), 0)

    wd, sat = min(good, key=lambda w: (score(w), w[0]))
    b.write_json("dates.json", {
        "weekday": wd.isoformat(), "saturday": sat.isoformat(), "picked_on": today.isoformat(),
        "candidates": [{"weekday": w.isoformat(), "saturday": s.isoformat(), "score": score((w, s)),
                        "weekday_report": reports[w.isoformat()],
                        "saturday_report": reports[s.isoformat()],
                        "tfl_saturday_closures": tfl_closures.get(s.isoformat())}
                       for w, s in good],
        "rule": "weeks >= 10 days ahead, no bank holiday; every feed at >= 85% of its usual trips "
                "and the core TfL operators running; fewest closures (operators with no trips, TfL "
                "planned closures on Saturday)"})
    log.info("service dates: %s and %s", wd, sat)


async def _saturday_closures(saturdays: list[date]) -> dict[str, int]:
    from transit.tfl import JourneyPlanner

    async with JourneyPlanner(app_key(), rate_per_min=settings.TFL_RATE_PER_MIN_ANON, in_flight=2) as jp:
        lines = await jp.lines("tube,overground,elizabeth-line,dlr,tram")
        ids = [ln["id"] for ln in lines]
        out = {}
        for d in saturdays:
            out[d.isoformat()] = await jp.planned_closures(ids, datetime.combine(d, datetime.min.time()))
        return out


def stage_prep(b: Build, args) -> None:
    from transit.feeds import prep_feed
    from transit.points import buffered, load_region

    region = load_region()
    clip = buffered(region, settings.BUFFER_OSM_M)
    wd, sat = b.dates()
    info: dict = {"feeds": {}}
    for fid, (_, _, rtypes) in FEEDS.items():
        info["feeds"][fid] = prep_feed(b.raw_feed(fid), b.prepped(fid), [wd, sat], clip, fid, rtypes)

    england = b.raw / "osm" / "england-latest.osm.pbf"
    stamp = osm_timestamp(england)
    poly_text, identity = clip_identity(b)
    clip_json = b.work / "clip.json"
    if b.osm_clip.exists() and clip_json.exists() and json.loads(clip_json.read_text()) != identity:
        if not england.exists():
            raise SystemExit("the OSM clip was made from another polygon or extract, and the England "
                             "file was deleted: run fetch again")
        b.osm_clip.unlink()
    if not b.osm_clip.exists():
        poly = b.work / "clip.geojson"
        poly.write_text(poly_text)
        subprocess.run(["osmium", "extract", "-p", str(poly), "-s", "smart", "--overwrite",
                        "-o", str(b.osm_clip), str(england)], check=True)
    clip_json.write_text(json.dumps(identity, indent=2))
    info["osm"] = {"file": b.osm_clip.name, "bytes": b.osm_clip.stat().st_size,
                   "data_as_of": stamp,
                   "osmium": subprocess.run(["osmium", "--version"], capture_output=True,
                                            text=True).stdout.splitlines()[0]}
    if england.exists() and not args.keep_england:
        england.unlink()
        log.info("deleted the England PBF (its sha256 and md5 stay in its .meta.json)")
    b.write_json("prep.json", info)


def clip_identity(b: Build) -> tuple[str, dict]:
    """The clip polygon (GeoJSON text) and what the clip must have been made from."""
    import shapely.geometry

    from transit.points import buffered, load_region

    clip = buffered(load_region(), settings.BUFFER_OSM_M)
    poly_text = json.dumps({"type": "Feature", "properties": {},
                            "geometry": shapely.geometry.mapping(clip)})
    england_meta = b.raw / "osm" / "england-latest.osm.pbf.meta.json"
    # The clip is remade when its polygon or the England extract it came from changes.
    return poly_text, {"polygon_sha256": hashlib.sha256(poly_text.encode()).hexdigest(),
                       "england_sha256": json.loads(england_meta.read_text()).get("sha256")
                       if england_meta.exists() else None}


def clip_is_current(b: Build) -> bool:
    """True when the OSM clip exists and was made from the current polygon and extract."""
    clip_json = b.work / "clip.json"
    return b.osm_clip.exists() and clip_json.exists() and \
        json.loads(clip_json.read_text()) == clip_identity(b)[1]


def osm_timestamp(england: Path) -> str | None:
    """
    When the OSM data was cut: the replication timestamp in the England file's header
    (osmium extract drops it), else the date in Geofabrik's dated file name (england-YYMMDD).
    """
    import re

    if england.exists():
        fi = json.loads(subprocess.run(["osmium", "fileinfo", "-j", str(england)], check=True,
                                       capture_output=True, text=True).stdout)
        ts = fi.get("header", {}).get("option", {}).get("osmosis_replication_timestamp")
        if ts:
            return ts
    meta = england.with_name(england.name + ".meta.json")
    if meta.exists():
        m = re.search(r"-(\d{2})(\d{2})(\d{2})\.osm\.pbf", json.loads(meta.read_text()).get("final_url", ""))
        if m:
            return f"20{m.group(1)}-{m.group(2)}-{m.group(3)} (Geofabrik file date)"
    return None


def stage_check(b: Build, args) -> int:
    from transit.feeds import check_feeds, report_markdown

    src = sources()
    feeds = load_feeds(b)
    wd, sat = b.dates()
    tfl_lines = None
    if not args.no_tfl:
        try:
            tfl_lines = asyncio.run(_tfl_lines())
        except Exception as exc:  # noqa: BLE001
            log.warning("TfL line list unavailable (%s); skipping that check", type(exc).__name__)
    report = check_feeds(feeds, src["sentinel"], [wd, sat], src["bus_count"]["min_routes"], tfl_lines)
    # gb-transit's TfL part is its own kept copy; nothing else says how old it is.
    kept = json.loads((b.raw / "gb_transit" / "feed-meta.json").read_text()).get("tfl_timetables_kept_on")
    if kept:
        age = (b.day - date.fromisoformat(kept)).days
        report["tfl_copy_age_days"] = age
        if age > settings.TFL_COPY_MAX_AGE_DAYS and not args.allow_stale_tfl:
            report["failures"].append(
                f"gb-transit's copy of TfL's timetables is {age} days old (kept {kept}); convert TfL's "
                f"own zip, or pass --allow-stale-tfl")
    b.write_json("feeds-check.json", report)
    (b.out / "feeds-check.md").write_text(report_markdown(report))
    if report["failures"]:
        log.error("feed check: %d problems, see %s", len(report["failures"]), b.out / "feeds-check.md")
        return 0 if args.allow_missing else 1
    log.info("feed check passed")
    return 0


async def _tfl_lines() -> dict[str, list[str]]:
    from transit.tfl import JourneyPlanner

    out: dict[str, list[str]] = {}
    async with JourneyPlanner(app_key(), rate_per_min=settings.TFL_RATE_PER_MIN_ANON, in_flight=2) as jp:
        for mode in ("tube", "dlr", "overground", "elizabeth-line", "tram", "cable-car", "bus"):
            out[mode] = [ln["name"] for ln in await jp.lines(mode)]
    return out


_network = None


def network(b: Build):
    """The R5 network (built once per process; r5py caches it on disk between processes)."""
    global _network
    if _network is None:
        from transit.r5 import build_network, station_access

        _network, _ = build_network(b.osm_clip, [b.prepped(f) for f in FEEDS], b.out / "network.json")
        if settings.R5["station_access_m"]:
            station_access(_network, settings.R5["station_access_m"])
    return _network


def stage_network(b: Build, args) -> None:
    network(b)


def stage_points(b: Build, args) -> None:
    from transit.points import buffer_gaps, build_points, load_region
    from transit.r5 import snap_points

    feeds = load_feeds(b, ("agency", "stops", "routes", "trips", "stop_times", "calendar_dates"))
    pts, counts = build_points(feeds, list(b.dates()), load_region())
    slon, slat, dist = snap_points(network(b), pts)
    pts["snap_lon"], pts["snap_lat"], pts["snap_m"] = slon, slat, np.round(dist, 1)
    pts["ok"] = np.isfinite(slon)
    pts.to_csv(b.out / "points.csv", index=False)
    np.save(b.out / "point_ok.npy", pts["ok"].to_numpy())
    gaps = buffer_gaps(pts, feeds)
    masked = pts[~pts["ok"]]
    b.write_json("points.json", {**counts, "masked": int(len(masked)),
                                 "masked_points": masked[["idx", "kind", "id", "name", "lon", "lat"]]
                                 .to_dict("records"),
                                 "snap_m_p50": float(np.nanmedian(dist)),
                                 "snap_m_p95": float(np.nanpercentile(dist, 95)),
                                 "buffer_gaps": gaps})
    log.info("points: %s, %d masked", counts, len(masked))


def load_points(b: Build):
    import pandas as pd

    pts = pd.read_csv(b.out / "points.csv", dtype={"id": str, "name": str}, keep_default_na=False,
                      na_values={"snap_lon": [""], "snap_lat": [""], "snap_m": [""]})
    pts["ok"] = pts["ok"].astype(str).str.lower() == "true"
    pts["inside_gl"] = pts["inside_gl"].astype(str).str.lower() == "true"
    return pts


def usable_points(b: Build) -> np.ndarray:
    """Points the tables cover: snapped, and not cut off from the network (point_ok.npy)."""
    path = b.out / "point_ok.npy"
    return np.load(path) if path.exists() else load_points(b)["ok"].to_numpy()


def stage_journeys(b: Build, args) -> None:
    """The go/no-go: three journeys, the interchange probe and the walking-speed check."""
    import re

    from transit import r5
    from transit.tfl import JourneyPlanner, TflError

    tn = network(b)
    c = cases()
    key = app_key()
    # About 50 Journey Planner calls: without a key they go anonymously, slowly.
    result: dict = {"journeys": [], "probe": [], "walk_check": [], "tfl": True,
                    "tfl_key": bool(key)}

    def r5_median(o, d, window, modes=None) -> float:
        if not modes and settings.R5["ride_cost_min"]:
            # The same costing as the tables: walk, or k rides plus the per-ride cost.
            from transit.calibrate import costed, times_from

            W, T = times_from(tn, o, [d], b.window_start(window), settings.R5["speed_walking_kmh"])
            return float(costed(W, T, settings.R5["ride_cost_min"], 0.0)[0])
        task = r5.regional_task(tn, r5._gdf(["d"], [d[0]], [d[1]]), b.window_start(window))
        if modes:
            task.transport_modes = [r5._mode(x) for x in modes]
        return float(r5.one_origin(tn, task, o[0], o[1])[0])

    async def tfl_part():
        rate = settings.TFL_RATE_PER_MIN if key else settings.TFL_RATE_PER_MIN_ANON
        async with JourneyPlanner(key, rate_per_min=rate, in_flight=4) as jp:
            for j in result["journeys"] + result["probe"]:
                start = b.window_start("wd08")
                try:
                    # Station to station by NaPTAN id: the comparison the go/no-go means.
                    med, js = await jp.window(j.get("tfl_from") or tuple(j["from"]),
                                              j.get("tfl_to") or tuple(j["to"]), start)
                    j["tfl_wd08"] = med
                    j["tfl_departures"] = [x.minutes for x in js]
                    j["tfl_0830_legs"] = js[3].legs
                    # The same journey from the coordinates, as recorded first (address walks).
                    j["tfl_wd08_from_coordinates"], _ = await jp.window(tuple(j["from"]), tuple(j["to"]), start)
                except TflError as exc:
                    j["tfl_error"] = str(exc)
            for w in result["walk_check"]:
                try:
                    w["tfl_walk"] = await jp.walk(tuple(w["from"]), tuple(w["to"]),
                                                  b.window_start("wd13"))
                except TflError as exc:
                    w["tfl_error"] = str(exc)

    for kind, items in (("journeys", c["journey"]), ("probe", c["probe"])):
        for j in items:
            row = {"name": j["name"], "from": j["from"], "to": j["to"],
                   "tfl_from": j.get("tfl_from"), "tfl_to": j.get("tfl_to")}
            row["r5"] = {w: r5_median(j["from"], j["to"], w) for w in settings.WINDOWS}
            if kind == "journeys":
                # Which kind of line R5 takes: the same median with only the expected mode
                # (tube, or rail) allowed. R5's itinerary search, which would name the line,
                # runs out of a 10 GB heap on this network.
                if j.get("expect_mode"):
                    row["r5_wd08_expected_mode_only"] = r5_median(j["from"], j["to"], "wd08", [j["expect_mode"]])
                    row["r5_wd08_uncosted"] = r5_median(j["from"], j["to"], "wd08", ["TRANSIT"])
                    row["expect_mode"] = j["expect_mode"]
                at = b.window_start("wd08").replace(minute=30)
                row["r5_0830"] = []
                if settings.R5_ITINERARIES:
                    try:
                        row["r5_0830"] = r5.itineraries(tn, tuple(j["from"]), tuple(j["to"]), at)[:3]
                    except Exception as exc:  # noqa: BLE001 - Java errors arrive as many types
                        log.warning("itinerary for %s failed: %s", j["name"], type(exc).__name__)
                        row["r5_0830_error"] = f"{type(exc).__name__}: {exc}"[:300]
                row["expect_lines"] = j["expect_lines"]
            result[kind].append(row)
    for w in c["walk_check"]:
        r5_walk = r5.walk_minutes(tn, [tuple(w["from"])], [tuple(w["to"])], b.window_start("wd13"),
                                  max_time_min=60)[0, 0]
        result["walk_check"].append({"name": w["name"], "from": w["from"], "to": w["to"],
                                     "r5_walk": float(r5_walk)})
    asyncio.run(tfl_part())

    # Line families from R5's legs: route ids and agency ids, matched against the expectation.
    routes = {}
    for fid in FEEDS:
        from transit.feeds import read_gtfs
        r = read_gtfs(b.prepped(fid), ("routes", "agency"))
        names = r["routes"].set_index("route_id")
        for rid, row in names.iterrows():
            routes[rid] = f"{row.get('agency_id', '')} {row.get('route_short_name', '')} {row.get('route_long_name', '')}"
    for j in result["journeys"]:
        legs = j["r5_0830"][0]["legs"] if j["r5_0830"] else []
        text = " ".join(routes.get(leg["route_id"] or "", "") for leg in legs)
        j["r5_0830_lines"] = text.strip()
        if text.strip():
            j["line_family_ok"] = bool(re.search(j["expect_lines"], text))
        elif "r5_wd08_expected_mode_only" in j:
            # A mode check, not a line check: the uncosted median with only the expected mode
            # equals the uncosted all-mode one (tube covers both the Metropolitan and Jubilee).
            mo, allm = j["r5_wd08_expected_mode_only"], j.get("r5_wd08_uncosted", j["r5"]["wd08"])
            j["line_family_ok"] = bool(np.isfinite(mo) and np.isfinite(allm) and mo <= allm)
            j["line_check"] = "mode only (line not verified)"
            j["r5_0830_lines"] = f"(no itinerary; {j['expect_mode']} only gives the same time)" \
                if j["line_family_ok"] else f"(no itinerary; {j['expect_mode']} only is slower or none)"
        if "tfl_wd08" in j:
            j["delta_wd08"] = j["r5"]["wd08"] - j["tfl_wd08"]
            j["within_tolerance"] = abs(j["delta_wd08"]) <= settings.JOURNEY_TOLERANCE_MIN
    for w in result["walk_check"]:
        if w.get("tfl_walk"):
            w["implied_speed_kmh"] = settings.R5["speed_walking_kmh"] * w["r5_walk"] / w["tfl_walk"]
    b.write_json("journeys.json", result)
    (b.out / "gonogo.md").write_text(gonogo_markdown(b, result))


def gonogo_markdown(b: Build, result: dict) -> str:
    net = json.loads((b.out / "network.json").read_text()) if (b.out / "network.json").exists() else {}
    lines = [f"# Go/no-go, build {b.id}", "",
             f"R5 costed as the tables: +{settings.R5['station_access_m']:.0f} m station access, "
             f"+{settings.R5['ride_cost_min']:.0f} min a ride.", ""]
    gb = net.get("peak_rss_bytes", 0) / 1e9
    heap = net.get("max_heap_bytes", 0) / 1e9
    lines += [f"- Network: {net.get('status')} in {net.get('seconds', 0) / 60:.1f} min, "
              f"peak RSS {gb:.1f} GB, JVM heap {heap:.1f} GB, {net.get('r5_jar')}, "
              f"Java {net.get('java_version')}", ""]
    lines += ["Station to station: TfL by NaPTAN station id, R5 from the station's coordinate. "
              "Minutes from the requested departure (the first wait counts), median over the "
              "window (R5: every minute 08:00-08:59; TfL: 08:00, 08:10, ... 08:50).", "",
              "| Journey | R5 wd08 | TfL wd08 | Δ | TfL from coordinates | R5 08:30 lines | Mode matches (line not checked) | "
              "R5 wd13 | wd19 | sa13 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for j in result["journeys"]:
        r = j["r5"]
        tfl = j.get("tfl_wd08")
        delta = j.get("delta_wd08")
        coords = j.get("tfl_wd08_from_coordinates")
        lines.append(f"| {j['name']} | {r['wd08']:.0f} | {'' if tfl is None else f'{tfl:.1f}'} | "
                     f"{'' if delta is None else f'{delta:+.1f}'} | "
                     f"{'' if coords is None else f'{coords:.1f}'} | {j.get('r5_0830_lines', '')} | "
                     f"{'yes' if j.get('line_family_ok') else 'NO'} | {r['wd13']:.0f} | {r['wd19']:.0f} | "
                     f"{r['sa13']:.0f} |")
    lines += ["", "## Interchange probe (minutes)", "", "| Pair | R5 wd08 | TfL wd08 | Δ |", "|---|---|---|---|"]
    for j in result["probe"]:
        tfl = j.get("tfl_wd08")
        d = "" if tfl is None else f"{j['r5']['wd08'] - tfl:+.1f}"
        lines.append(f"| {j['name']} | {j['r5']['wd08']:.0f} | {'' if tfl is None else f'{tfl:.1f}'} | {d} |")
    lines += ["", "## Walking speed", "", "| Walk | R5 (4.0 km/h) | TfL Average | implied km/h |",
              "|---|---|---|---|"]
    for w in result["walk_check"]:
        speed = w.get("implied_speed_kmh")
        lines.append(f"| {w['name']} | {w['r5_walk']:.1f} | {w.get('tfl_walk', '')} | "
                     f"{'' if speed is None else f'{speed:.2f}'} |")
    if not result.get("tfl_key"):
        lines += ["", "TfL Journey Planner was called without a key (about 50 calls, 30 a minute)."]
    return "\n".join(lines) + "\n"


def table_fingerprint(b: Build, window: str | None) -> dict:
    """Everything a table depends on. A table made from anything else is rebuilt, not resumed."""
    return {"settings": settings.snapshot(), "code": code_hash(STAGE_CODE["tables"]),
            "departure": b.window_start(window).isoformat() if window else None,
            "points_sha256": sha256(b.out / "points.csv"),
            "prepared_sha256": {f: sha256(b.prepped(f)) for f in FEEDS},
            "osm_clip_sha256": sha256(b.osm_clip)}


def stage_tables(b: Build, args):
    from transit.r5 import fingerprint_path, table_window
    from transit.tables import isolated_points

    pts = load_points(b)
    ok = pts["ok"].to_numpy()
    windows = [args.window] if args.window else list(settings.WINDOWS)
    info = {}
    for w in windows:
        info[w] = table_window(network(b), pts, ok, b.window_start(w), b.out / f"tt_{w}.npy",
                               threads=args.threads, limit_origins=args.limit_origins,
                               fingerprint=table_fingerprint(b, w), force=args.force)
    prev = json.loads((b.out / "tables.json").read_text()) if (b.out / "tables.json").exists() else {}
    prev.update(info)
    complete = all(json.loads(fingerprint_path(b.out / f"tt_{w}.npy").read_text()).get("complete")
                   for w in settings.WINDOWS if fingerprint_path(b.out / f"tt_{w}.npy").exists()) and \
        all((b.out / f"tt_{w}.npy").exists() for w in settings.WINDOWS)
    if complete:
        # Points cut off from the network (a street fragment) in every window are masked like
        # unsnapped ones: their rows and columns become 255, and point_ok.npy says so.
        tables = {w: np.load(b.out / f"tt_{w}.npy", mmap_mode="r+") for w in settings.WINDOWS}
        iso = set.intersection(*(set(isolated_points(t, ok, settings.ISOLATED_SHARE).tolist())
                                 for t in tables.values()))
        iso = np.array(sorted(iso), dtype=int)
        for t in tables.values():
            if len(iso):
                t[iso, :] = settings.UNREACHABLE
                t[:, iso] = settings.UNREACHABLE
            t.flush()
        usable = ok.copy()
        usable[iso] = False
        np.save(b.out / "point_ok.npy", usable)
        pj = b.read_json("points.json")
        pj["isolated"] = int(len(iso))
        pj["isolated_points"] = pts.loc[iso, ["idx", "kind", "id", "name", "lon", "lat", "inside_gl",
                                             "snap_m"]].to_dict("records")
        b.write_json("points.json", pj)
        for w, t in tables.items():
            prev.setdefault(w, {})["share_255"] = float((np.asarray(t) == settings.UNREACHABLE).mean())
        prev["isolated_points_masked"] = int(len(iso))
        log.info("tables complete; %d points cut off from the network masked", len(iso))
    b.write_json("tables.json", prev)
    return None if complete else "incomplete"


def stage_walksheds(b: Build, args) -> None:
    from transit.r5 import walksheds

    pts = load_points(b)
    ws, info = walksheds(network(b), pts, usable_points(b), b.window_start("wd13"), threads=args.threads)
    ws.save(b.out / "walksheds.npz")
    b.write_json("walksheds.json", info)


def attribution() -> list[str]:
    seen, out = set(), []
    for src in sources().values():
        if isinstance(src, dict):
            for a in src.get("attribution", []):
                if a not in seen:
                    seen.add(a)
                    out.append(a)
    return out


def stage_manifest(b: Build, args) -> None:
    from transit import manifest
    from transit.feeds import read_gtfs
    from transit.r5 import fingerprint_path
    from transit.tables import next_change

    wd, sat = b.dates()
    fps = {}
    for w in settings.WINDOWS:
        fp = fingerprint_path(b.out / f"tt_{w}.npy")
        if not fp.exists() or not json.loads(fp.read_text()).get("complete"):
            raise SystemExit(f"tt_{w}.npy is missing or incomplete: run the tables stage to the end first")
        fps[w] = json.loads(fp.read_text())["inputs"]
    # Each table must have been built from the current settings, code, points, feeds and clip
    # (re-running prep and then one window would otherwise mix inputs).
    now = table_fingerprint(b, None)
    for w, f in fps.items():
        want = {**now, "departure": b.window_start(w).isoformat()}
        differ = sorted(k for k in set(want) | set(f) if want.get(k) != f.get(k))
        if differ:
            raise SystemExit(f"tt_{w}.npy was built from other inputs than the current ones "
                             f"({', '.join(differ)}): run the tables stage again")
    built_with = [f["settings"] for f in fps.values()]
    srcs = json.loads((b.raw / "sources.json").read_text())
    prep = b.read_json("prep.json")
    inputs = {"raw": srcs["files"], "gb_transit_release": srcs.get("gb_transit_release"),
              "prepared": {fid: {**prep["feeds"][fid], **manifest.output_entry(b.prepped(fid))}
                           for fid in FEEDS},
              "osm_clip": {**prep["osm"], **manifest.output_entry(b.osm_clip)},
              "region": manifest.output_entry(settings.REGION_GEOJSON)}
    gbmeta = json.loads((b.raw / "gb_transit" / "feed-meta.json").read_text())
    inputs["timetable_as_of"] = {
        "national_rail": f"{gbmeta.get('feed_version')} (gb-transit build {gbmeta.get('built')})",
        "tfl_tube_dlr_tram_river_cable": gbmeta.get("tfl_timetables_kept_on"),
        "bods_buses": srcs["files"]["bods/itm_london_gtfs.zip"].get("fetched_at"),
        "osm": prep["osm"].get("data_as_of")}
    feed_ends = []
    raw_cal = read_gtfs(b.raw_feed("bods"), ("calendar",)).get("calendar")
    if raw_cal is not None and len(raw_cal):
        feed_ends.append(datetime.strptime(raw_cal["end_date"].max(), "%Y%m%d").date())
    feed_ends.append(datetime.strptime(gbmeta["feed_end_date"], "%Y%m%d").date())
    # The tables follow the timetable of their service dates: they go stale at the next
    # National Rail timetable change.
    change = next_change(settings.TIMETABLE_CHANGES, sat)
    if change is not None:
        feed_ends.append(change - timedelta(days=1))
    outputs = {}
    for w in settings.WINDOWS:
        p = b.out / f"tt_{w}.npy"
        if p.exists():
            outputs[f"tt_{w}"] = manifest.output_entry(p, shape=list(np.load(p, mmap_mode="r").shape),
                                                       departure=b.window_start(w).isoformat())
    for name in ("point_ok.npy", "walksheds.npz", "points.csv"):
        if (b.out / name).exists():
            outputs[name] = manifest.output_entry(b.out / name)
    stages = {}
    for f in sorted((b.out / "stages").glob("*.json")):
        stages[f.stem] = json.loads(f.read_text())
    for name in ("tables.json", "walksheds.json"):
        if (b.out / name).exists():
            stages[name[:-5] + "_run"] = json.loads((b.out / name).read_text())
    m = manifest.build_manifest(
        b.id, {"weekday": wd.isoformat(), "saturday": sat.isoformat()}, inputs,
        b.read_json("points.json") | {"file": "points.csv"}, outputs,
        b.read_json("network.json"), stages, feed_ends, attribution(), table_settings=built_with[0])
    m["tables_fingerprints"] = fps
    m["licences_cleared"] = licences_cleared()
    manifest.write(b.out / "manifest.json", m)
    log.info("manifest written; valid until %s", m["valid_until"])


LICENCE_TEXT = """\
London transit tables (time-map), build {build}

The travel-time tables, walksheds and points in this archive are derived from
OpenStreetMap and are made available under the Open Database License 1.0
(https://opendatacommons.org/licenses/odbl/1-0/). (c) OpenStreetMap contributors.

They are also derived from the sources below; their credits must be kept:

{credits}
"""

LICENCE_PENDING = """\
London transit tables (time-map), build {build}

NOT LICENSED FOR PUBLICATION. The rail timetable's licence (Rail Settlement Plan) and
TfL's terms for derived data have not been read (docs/transit-sources.md), so no rights
are granted over these tables, walksheds or points. Do not share this archive.

They are derived from the sources below; their credits must be kept:

{credits}
"""


def licences_cleared() -> bool:
    return bool(sources().get("publication", {}).get("licences_cleared", False))


def stage_package(b: Build, args) -> None:
    files = [b.out / f"tt_{w}.npy" for w in settings.WINDOWS] + \
        [b.out / n for n in ("point_ok.npy", "points.csv", "walksheds.npz", "manifest.json")]
    missing = [f.name for f in files if not f.exists()]
    if missing:
        raise SystemExit(f"cannot package: missing {missing}")
    from transit.r5 import fingerprint_path

    m = b.read_json("manifest.json")
    if "tables_fingerprints" not in m:
        raise SystemExit("cannot package: the manifest has no complete-table record; run the manifest stage")
    # The files must still be the ones the manifest describes: a table rebuilt (or cut short by
    # a pilot) after the manifest was written is refused.
    for w in settings.WINDOWS:
        fp_path = fingerprint_path(b.out / f"tt_{w}.npy")
        fp = json.loads(fp_path.read_text()) if fp_path.exists() else {}
        if not fp.get("complete") or fp.get("inputs") != m["tables_fingerprints"].get(w):
            raise SystemExit(f"cannot package: tt_{w}.npy changed after the manifest was written; "
                             "run the manifest stage again")
    changed = [e["file"] for e in m["outputs"].values() if sha256(b.out / e["file"]) != e["sha256"]]
    if changed:
        raise SystemExit(f"cannot package: {changed} changed after the manifest was written; "
                         "run the manifest stage again")
    lic = b.out / "LICENSE-DATA.txt"
    text = LICENCE_TEXT if licences_cleared() else LICENCE_PENDING
    lic.write_text(text.format(build=b.id, credits="\n".join(f"- {a}" for a in attribution())))
    tar = b.out / f"transit-london-{b.id}.tar.xz"
    with tarfile.open(tar, "w:xz") as t:
        for f in files + [lic]:
            t.add(f, arcname=f"transit-london-{b.id}/{f.name}")
    log.info("packaged %s (%.1f MB)", tar.name, tar.stat().st_size / 1e6)


def stage_publish(b: Build, args) -> None:
    """A draft GitHub release. Only when the licences allow it and the owner said yes."""
    if not licences_cleared():
        raise SystemExit("publish refused: sources.toml [publication] licences_cleared is false (the rail "
                         "and TfL derived-data terms have not been read; docs/transit-sources.md)")
    if not args.confirm:
        raise SystemExit("publish needs --confirm: the licences in docs/transit-sources.md must allow "
                         "publishing derived tables, and the owner must agree")
    tar = b.out / f"transit-london-{b.id}.tar.xz"
    notes = b.out / "release-notes.md"
    notes.write_text(f"London transit tables, build {b.id}. See manifest.json.\n\n" +
                     "\n".join(f"- {a}" for a in attribution()) + "\n")
    subprocess.run(["gh", "release", "create", f"transit-london-{b.id}", "--draft",
                    "--title", f"London transit tables {b.id}", "--notes-file", str(notes),
                    str(tar), str(b.out / "manifest.json")], check=True)


def stage_check_keys(b: Build, args) -> None:
    from transit.tfl import key_ok

    key = app_key()
    if not key:
        raise SystemExit("TFL_APP_KEY is not set (see transit/README.md, 'Accounts and keys')")
    print("TfL key OK" if key_ok(key) else "TfL refused the key: check it on api-portal.tfl.gov.uk")


RUNNERS = {"fetch": stage_fetch, "dates": stage_dates, "prep": stage_prep, "check": stage_check,
           "network": stage_network, "points": stage_points, "journeys": stage_journeys,
           "tables": stage_tables, "walksheds": stage_walksheds, "manifest": stage_manifest,
           "package": stage_package}


# Options that change what a stage does: they are part of its key, so `all --redate` re-picks
# the dates and `all --with-tfl-txc` downloads TfL's zip even when nothing else changed.
STAGE_OPTIONS = {
    "fetch": ("gb_transit_tag", "with_tfl_txc"), "dates": ("redate", "today", "no_tfl"),
    "prep": ("keep_england",), "check": ("no_tfl", "allow_missing", "allow_stale_tfl"),
}


def stage_options(name: str, args) -> dict:
    return {o: getattr(args, o, None) for o in STAGE_OPTIONS.get(name, ())}


def stage_inputs(b: Build, name: str) -> tuple[list[Path], list[Path]]:
    """(inputs, outputs) per stage, for skipping unchanged stages in `all`."""
    prepped = [b.prepped(f) for f in FEEDS]
    raw = [b.raw_feed(f) for f in FEEDS]
    io = {
        "fetch": ([], [b.raw / "sources.json"]),
        "dates": (raw, [b.out / "dates.json"]),
        "prep": (raw + [b.out / "dates.json", settings.REGION_GEOJSON],
                 prepped + [b.osm_clip, b.out / "prep.json"]),
        "check": (prepped, [b.out / "feeds-check.json"]),
        "network": (prepped + [b.osm_clip], [b.out / "network.json"]),
        "points": (prepped + [b.osm_clip, b.out / "dates.json", settings.REGION_GEOJSON],
                   [b.out / "points.csv"]),
        "journeys": (prepped + [b.osm_clip, b.out / "dates.json"], [b.out / "journeys.json"]),
        "tables": (prepped + [b.osm_clip, b.out / "dates.json", b.out / "points.csv"],
                   [b.out / f"tt_{w}.npy" for w in settings.WINDOWS]),
        "walksheds": (prepped + [b.osm_clip, b.out / "points.csv", b.out / "point_ok.npy"],
                      [b.out / "walksheds.npz"]),
        "manifest": ([b.out / f"tt_{w}.npy" for w in settings.WINDOWS] + [b.out / "walksheds.npz"],
                     [b.out / "manifest.json"]),
        "package": ([b.out / "manifest.json"], [b.out / f"transit-london-{b.id}.tar.xz"]),
    }
    return io[name]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m transit.build", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("stage", nargs="+", choices=STAGES + ("all", "publish", "check-keys"),
                   help="one or more stages (run in one process, so the network loads once)")
    p.add_argument("--build", default=date.today().isoformat(), help="build id (default: today)")
    p.add_argument("--force", action="store_true", help="redo stages even if nothing changed")
    p.add_argument("--today", help="pretend today is this date when picking service dates")
    p.add_argument("--gb-transit-tag", help="gb-transit release tag (default: latest)")
    p.add_argument("--with-tfl-txc", action="store_true",
                   help="also download TfL's TransXChange zip (UK2GTFS check, bus fallback)")
    p.add_argument("--keep-england", action="store_true", help="keep the England PBF after clipping")
    p.add_argument("--no-tfl", action="store_true", help="skip TfL API calls in dates and check")
    p.add_argument("--allow-missing", action="store_true", help="do not fail on feed-check problems")
    p.add_argument("--window", choices=list(settings.WINDOWS), help="tables: only this window")
    p.add_argument("--limit-origins", type=int, help="tables: stop after this many origins (pilot)")
    p.add_argument("--threads", type=int, default=settings.MATRIX_THREADS)
    p.add_argument("--confirm", action="store_true", help="publish: the licences allow it")
    p.add_argument("--redate", action="store_true", help="dates: pick new service dates (the gate's "
                   "cached replies are for the old ones)")
    p.add_argument("--allow-stale-tfl", action="store_true",
                   help="check: accept a gb-transit TfL copy older than TFL_COPY_MAX_AGE_DAYS")
    p.add_argument("--max-memory", help="JVM heap for R5, e.g. 12G (default TRANSIT_MAX_MEMORY or 10G)")
    args = p.parse_args(argv)
    if args.max_memory:
        settings.JVM_MAX_MEMORY = args.max_memory

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # its INFO lines carry full URLs
    b = Build(args.build)

    if "publish" in args.stage:
        stage_publish(b, args)
        return 0
    if "check-keys" in args.stage:
        stage_check_keys(b, args)
        return 0
    run_all = "all" in args.stage
    names = STAGES if run_all else tuple(s for s in STAGES if s in args.stage)
    for name in names:
        inputs, outputs = stage_inputs(b, name)
        extra = {"stage": name, "options": stage_options(name, args)}
        key = digest([i for i in inputs if i.exists()], extra, stage=name)
        if run_all and not args.force and stage_done(b, name, key, outputs):
            log.info("skip %s (unchanged)", name)
            continue
        started = time.monotonic()
        log.info("stage %s", name)
        rc = RUNNERS[name](b, args)
        if rc == "incomplete":
            log.warning("stage %s is incomplete (a pilot?): not marked done", name)
            continue
        if rc:
            return rc
        key = digest([i for i in inputs if i.exists()], extra, stage=name)
        mark_done(b, name, key, started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
