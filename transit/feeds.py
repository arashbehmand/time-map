"""
GTFS preparation and checks, in pandas (no R, no Java).

prep: keep only the services that run on the build's two service dates, drop stop_times
outside Greater London + 5 km (trips keep their inside stops at their real times), drop
shapes and transfers, write calendar_dates for exactly those dates and give each feed its own
feed_id. R5 loads every trip whatever its date, so this is what keeps memory down.

check: the feed audit the issue asks for. Missing lines (sentinels), doubled service
(duplicate trips inside one operator, the same segment at the same minute from two
operators), timezones and calendars.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("transit.feeds")

TABLES = ("agency", "stops", "routes", "trips", "stop_times", "calendar", "calendar_dates",
          "frequencies", "feed_info")
WEEKDAY_COLS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
BUS_TYPES = {3, 700, 701, 702, 703, 704, 705, 706, 707, 708, 709, 710, 711, 712, 713, 714, 715, 716}


# ── reading ─────────────────────────────────────────────────────────
def read_gtfs(path: Path, tables: tuple[str, ...] = TABLES) -> dict[str, pd.DataFrame]:
    """Read GTFS tables as strings (missing tables are left out). Handles a nested folder."""
    out: dict[str, pd.DataFrame] = {}
    with zipfile.ZipFile(path) as z:
        names = {Path(n).name: n for n in z.namelist() if n.endswith(".txt")}
        for t in tables:
            name = names.get(f"{t}.txt")
            if name is None:
                continue
            with z.open(name) as f:
                df = pd.read_csv(f, dtype=str, keep_default_na=False, encoding="utf-8-sig")
            df.columns = [c.strip() for c in df.columns]
            out[t] = df
    return out


STOP_TIME_COLS = ("trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence",
                  "pickup_type", "drop_off_type", "timepoint")


def read_stop_times(path: Path, trips: set[str], stops: set[str] | None = None,
                    chunksize: int = 1_000_000) -> tuple[pd.DataFrame, int]:
    """
    stop_times for these trips (and stops), read in chunks with only the columns R5 needs, so
    a 1.5 GB stop_times.txt never sits in memory whole. Returns (rows kept, rows read).
    """
    parts, seen = [], 0
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if Path(n).name == "stop_times.txt")
        with z.open(name) as f:
            for chunk in pd.read_csv(f, dtype=str, keep_default_na=False, encoding="utf-8-sig",
                                     usecols=lambda c: c.strip() in STOP_TIME_COLS, chunksize=chunksize):
                chunk.columns = [c.strip() for c in chunk.columns]
                seen += len(chunk)
                keep = chunk["trip_id"].isin(trips)
                if stops is not None:
                    keep &= chunk["stop_id"].isin(stops)
                parts.append(chunk[keep])
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=list(STOP_TIME_COLS))
    return out, seen


def to_seconds(s: pd.Series) -> pd.Series:
    """GTFS times (may pass 24:00:00, may be empty) to seconds, NaN when empty."""
    parts = s.str.strip().str.split(":", expand=True)
    if parts.shape[1] < 3:
        return pd.Series(np.nan, index=s.index)
    h = pd.to_numeric(parts[0], errors="coerce")
    m = pd.to_numeric(parts[1], errors="coerce")
    sec = pd.to_numeric(parts[2], errors="coerce")
    return h * 3600 + m * 60 + sec


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def active_services(feed: dict[str, pd.DataFrame], dates: list[date]) -> dict[date, set[str]]:
    """service_ids running on each date (calendar plus calendar_dates exceptions)."""
    cal = feed.get("calendar")
    cd = feed.get("calendar_dates")
    out: dict[date, set[str]] = {}
    for d in dates:
        ymd = _ymd(d)
        on: set[str] = set()
        if cal is not None and len(cal):
            col = WEEKDAY_COLS[d.weekday()]
            mask = (cal[col] == "1") & (cal["start_date"] <= ymd) & (cal["end_date"] >= ymd)
            on = set(cal.loc[mask, "service_id"])
        if cd is not None and len(cd):
            day = cd[cd["date"] == ymd]
            on |= set(day.loc[day["exception_type"] == "1", "service_id"])
            on -= set(day.loc[day["exception_type"] == "2", "service_id"])
        out[d] = on
    return out


def agency_of_trips(feed: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """trip_id, route_id, service_id, agency_id, agency_name, route_type, route names."""
    routes = feed["routes"].copy()
    if "agency_id" not in routes.columns or (routes["agency_id"] == "").all():
        only = feed["agency"]["agency_id"].iloc[0] if "agency_id" in feed["agency"] else ""
        routes["agency_id"] = only
    agency = feed["agency"]
    if "agency_id" not in agency.columns:
        agency = agency.assign(agency_id=routes["agency_id"].iloc[0] if len(routes) else "")
    r = routes.merge(agency[["agency_id", "agency_name"]], on="agency_id", how="left")
    for c in ("route_short_name", "route_long_name"):
        if c not in r.columns:
            r[c] = ""
    r["route_type"] = pd.to_numeric(r["route_type"], errors="coerce").fillna(-1).astype(int)
    t = feed["trips"][["trip_id", "route_id", "service_id"]]
    return t.merge(r[["route_id", "agency_id", "agency_name", "route_type", "route_short_name",
                      "route_long_name"]], on="route_id", how="left")


def trips_by_agency_on(feed: dict[str, pd.DataFrame], dates: list[date]) -> dict[date, pd.Series]:
    """Number of trips per agency_name running on each date."""
    t = agency_of_trips(feed)
    act = active_services(feed, dates)
    return {d: t[t["service_id"].isin(act[d])].groupby("agency_name").size() for d in dates}


# ── prep ────────────────────────────────────────────────────────────
def prep_feed(src: Path, dst: Path, dates: list[date], polygon, feed_id: str,
              route_types: list[int] | None = None) -> dict:
    """Write a GTFS zip with only what runs on `dates` inside `polygon` (shapely, lon/lat)."""
    import shapely

    feed = read_gtfs(src, tuple(t for t in TABLES if t != "stop_times"))
    before = {"trips": len(feed["trips"]), "stops": len(feed["stops"])}
    act = active_services(feed, dates)
    services = set().union(*act.values())

    routes = feed["routes"]
    if route_types is not None:
        rt = pd.to_numeric(routes["route_type"], errors="coerce")
        routes = routes[rt.isin(route_types)]
    trips = feed["trips"]
    trips = trips[trips["service_id"].isin(services) & trips["route_id"].isin(routes["route_id"])]

    stops = feed["stops"]
    lon = pd.to_numeric(stops["stop_lon"], errors="coerce").to_numpy()
    lat = pd.to_numeric(stops["stop_lat"], errors="coerce").to_numpy()
    good = np.isfinite(lon) & np.isfinite(lat)
    inside = np.zeros(len(stops), dtype=bool)
    inside[good] = shapely.contains_xy(polygon, lon[good], lat[good])
    inside_ids = set(stops.loc[inside, "stop_id"])

    st, before["stop_times"] = read_stop_times(src, set(trips["trip_id"]), inside_ids)
    # A trip must start and end at a stop with times: trim untimed stops at either end.
    st["_seq"] = pd.to_numeric(st["stop_sequence"], errors="coerce")
    st = st.sort_values(["trip_id", "_seq"])
    timed = (st["arrival_time"].str.strip() != "") & (st["departure_time"].str.strip() != "")
    seq_timed = st["_seq"].where(timed)
    first = seq_timed.groupby(st["trip_id"]).transform("min")
    last = seq_timed.groupby(st["trip_id"]).transform("max")
    st = st[(st["_seq"] >= first) & (st["_seq"] <= last)]
    counts = st.groupby("trip_id").size()
    keep_trips = set(counts[counts >= 2].index)
    st = st[st["trip_id"].isin(keep_trips)].drop(columns="_seq")
    trips = trips[trips["trip_id"].isin(keep_trips)]
    if "shape_id" in trips.columns:
        trips = trips.drop(columns="shape_id")

    # Doubled trips: on each date, a trip with the same route, the same stops and the same
    # first departure as another is a second copy (overlapping timetable versions, seen in
    # BODS, whose later times can differ a little). Keep the newer version: the trip whose
    # service starts latest (calendar start_date, else its first calendar_dates day), then the
    # lowest trip_id. Each trip then runs on the dates it was kept for.
    pattern = st.groupby("trip_id", sort=False)["stop_id"].agg("|".join)
    first_dep = st.groupby("trip_id", sort=False)["departure_time"].first()
    starts = service_starts(feed)
    tt = trips.set_index("trip_id")[["route_id", "service_id"]].join(pattern.rename("pat")) \
        .join(first_dep.rename("first"))
    tt["start"] = tt["service_id"].map(starts).fillna("")
    tt = tt.reset_index().sort_values(["start", "trip_id"], ascending=[False, True]).set_index("trip_id")
    kept_on: dict[str, list[str]] = {}
    doubled = {}
    for d in dates:
        on = tt[tt["service_id"].isin(act[d])]
        dup = on.duplicated(subset=["route_id", "pat", "first"], keep="first")
        doubled[d.isoformat()] = int(dup.sum())
        for trip in on.index[~dup.to_numpy()]:
            kept_on.setdefault(trip, []).append(_ymd(d))
    trips = trips[trips["trip_id"].isin(kept_on)].copy()
    trips["service_id"] = trips["service_id"] + "@" + trips["trip_id"].map(lambda t: "+".join(kept_on[t]))
    st = st[st["trip_id"].isin(kept_on)]

    used = set(st["stop_id"])
    parents = set()
    if "parent_station" in stops.columns:
        parents = set(stops.loc[stops["stop_id"].isin(used), "parent_station"]) - {""}
    stops_out = stops[stops["stop_id"].isin(used | parents)].copy()
    if "parent_station" in stops_out.columns:
        known = set(stops_out["stop_id"])
        stops_out.loc[~stops_out["parent_station"].isin(known), "parent_station"] = ""
    routes_out = routes[routes["route_id"].isin(set(trips["route_id"]))]
    agency = feed["agency"]
    if "agency_id" in agency.columns and "agency_id" in routes_out.columns:
        agency = agency[agency["agency_id"].isin(set(routes_out["agency_id"])) | (agency["agency_id"] == "")]

    cd_rows = [(sid, ymd, "1") for sid in sorted(set(trips["service_id"]))
               for ymd in sid.rsplit("@", 1)[1].split("+")]
    cal_dates = pd.DataFrame(cd_rows, columns=["service_id", "date", "exception_type"])

    tables = {"agency": agency, "stops": stops_out, "routes": routes_out, "trips": trips,
              "stop_times": st, "calendar_dates": cal_dates}
    if "frequencies" in feed:
        tables["frequencies"] = feed["frequencies"][feed["frequencies"]["trip_id"].isin(kept_on)]
    info = feed.get("feed_info")
    version = info["feed_version"].iloc[0] if info is not None and "feed_version" in info and len(info) else ""
    tables["feed_info"] = pd.DataFrame([{
        "feed_publisher_name": "time-map transit build (prepared from " + src.name + ")",
        "feed_publisher_url": "https://github.com/arashbehmand/time-map",
        "feed_lang": "en", "feed_version": version, "feed_id": feed_id,
        "feed_start_date": _ymd(min(dates)), "feed_end_date": _ymd(max(dates)),
    }])
    write_gtfs(dst, tables)
    after = {"trips": len(trips), "stop_times": len(st), "stops": len(stops_out)}
    kept_days = trips["service_id"].str.rsplit("@", n=1).str[1].str.split("+")
    trips_per_date = {}
    for d in dates:
        ymd = _ymd(d)
        trips_per_date[d.isoformat()] = int(sum(ymd in days for days in kept_days))
    log.info("prep %s: trips %d -> %d, stop_times %d -> %d", feed_id, before["trips"],
             after["trips"], before["stop_times"], after["stop_times"])
    return {"feed_id": feed_id, "source": src.name, "feed_version": version, "before": before,
            "after": after, "trips_per_date": trips_per_date, "doubled_trips_removed": doubled}


def service_starts(feed: dict[str, pd.DataFrame]) -> dict[str, str]:
    """service_id -> the first day it runs (calendar start_date, else its first added date)."""
    out: dict[str, str] = {}
    cd = feed.get("calendar_dates")
    if cd is not None and len(cd):
        added = cd[cd["exception_type"] == "1"].groupby("service_id")["date"].min()
        out.update(added.to_dict())
    cal = feed.get("calendar")
    if cal is not None and len(cal):
        out.update(dict(zip(cal["service_id"], cal["start_date"])))
    return out


def write_gtfs(path: Path, tables: dict[str, pd.DataFrame]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, df in tables.items():
            buf = io.StringIO()
            df.to_csv(buf, index=False)
            z.writestr(f"{name}.txt", buf.getvalue())
    tmp.replace(path)


# ── checks ──────────────────────────────────────────────────────────
def _stop_names(feed) -> pd.Series:
    return feed["stops"].set_index("stop_id")["stop_name"]


def sentinel_hits(feeds: dict[str, dict], sentinel: dict, dates: list[date]) -> dict:
    """Trips per date that match one sentinel, over all feeds."""
    hits = {d.isoformat(): 0 for d in dates}
    for feed in feeds.values():
        t = agency_of_trips(feed)
        if "agency" in sentinel:
            t = t[t["agency_name"].str.contains(sentinel["agency"], regex=True, na=False)]
        if "route_type" in sentinel:
            t = t[t["route_type"] == int(sentinel["route_type"])]
        if "route" in sentinel:
            pat = sentinel["route"]
            m = t["route_short_name"].str.contains(pat, regex=True, na=False) | \
                t["route_long_name"].str.contains(pat, regex=True, na=False)
            t = t[m]
        if "stops" in sentinel and len(t):
            names = _stop_names(feed)
            st = feed["stop_times"]
            st = st[st["trip_id"].isin(set(t["trip_id"]))]
            nm = st["stop_id"].map(names).fillna("")
            a = set(st.loc[nm.str.contains(sentinel["stops"][0], regex=True), "trip_id"])
            b = set(st.loc[nm.str.contains(sentinel["stops"][1], regex=True), "trip_id"])
            t = t[t["trip_id"].isin(a & b)]
        act = active_services(feed, dates)
        for d in dates:
            hits[d.isoformat()] += int(t["service_id"].isin(act[d]).sum())
    return hits


def duplicate_trips(feed: dict, d: date, tolerance_s: int = 0) -> pd.DataFrame:
    """
    Per route: trips on date d that repeat another trip of the same route with the same stop
    pattern, starting within tolerance_s (default: the same second). Overlapping timetable
    versions show up here. (Real services can be a minute apart: the Victoria line is.)
    """
    t = agency_of_trips(feed)
    act = active_services(feed, [d])[d]
    t = t[t["service_id"].isin(act)]
    st = feed["stop_times"]
    st = st[st["trip_id"].isin(set(t["trip_id"]))].copy()
    st["_seq"] = pd.to_numeric(st["stop_sequence"], errors="coerce")
    st = st.sort_values(["trip_id", "_seq"])
    pattern = st.groupby("trip_id")["stop_id"].agg("|".join)
    first_dep = to_seconds(st.groupby("trip_id")["departure_time"].first())
    df = t.set_index("trip_id")[["route_id", "agency_name", "route_short_name"]].join(
        pattern.rename("pattern")).join(first_dep.rename("dep")).dropna(subset=["dep"])
    df = df.sort_values(["route_id", "pattern", "dep"])
    gap = df.groupby(["route_id", "pattern"])["dep"].diff()
    df["dup"] = gap <= tolerance_s
    out = df.groupby(["route_id", "agency_name", "route_short_name"]).agg(
        trips=("dup", "size"), duplicates=("dup", "sum")).reset_index()
    out["share"] = out["duplicates"] / out["trips"]
    return out[out["duplicates"] > 0].sort_values("duplicates", ascending=False)


def segment_keys(feed: dict, d: date, place_res: int = 9) -> pd.DataFrame:
    """
    Non-bus runs of three consecutive stations on date d: (agency, a, b, c, departure minute
    at each).
    A place is the H3 cell of the stop's station (its parent when it has one). Single
    segments are not enough: a Victoria line train and a Great Northern train can both run
    Highbury -> Finsbury Park in the same minute on different tracks; and on shared track a
    Bakerloo and a Lioness train can leave Queen's Park in the same minute. Two separate
    trains cannot match to the minute at three stations in a row; one train listed twice does.
    """
    import h3

    t = agency_of_trips(feed)
    t = t[~t["route_type"].isin(BUS_TYPES)]
    act = active_services(feed, [d])[d]
    t = t[t["service_id"].isin(act)]
    st = feed["stop_times"]
    st = st[st["trip_id"].isin(set(t["trip_id"]))].copy()
    cols = ["agency", "a", "b", "c", "minute", "mb", "mc"]
    if not len(st):
        return pd.DataFrame(columns=cols)
    stops = feed["stops"].set_index("stop_id")
    parent = stops["parent_station"] if "parent_station" in stops.columns else pd.Series("", stops.index)
    lons = pd.to_numeric(stops["stop_lon"], errors="coerce")
    lats = pd.to_numeric(stops["stop_lat"], errors="coerce")
    place_of: dict[str, str] = {}
    for s in st["stop_id"].unique():
        station = parent.get(s) or s
        lo, la = lons.get(station, np.nan), lats.get(station, np.nan)
        place_of[s] = h3.latlng_to_cell(la, lo, place_res) if np.isfinite(la) and np.isfinite(lo) else ""
    st["place"] = st["stop_id"].map(place_of)
    st["_seq"] = pd.to_numeric(st["stop_sequence"], errors="coerce")
    st = st.sort_values(["trip_id", "_seq"])
    st["dep"] = to_seconds(st["departure_time"])
    g = st.groupby("trip_id")["place"]
    st["b"] = g.shift(-1)
    st["c"] = g.shift(-2)
    gd = st.groupby("trip_id")["dep"]
    st["db"] = gd.shift(-1)
    st["dc"] = gd.shift(-2)
    seg = st.dropna(subset=["b", "c", "dep", "db", "dc"])
    seg = seg[(seg["place"] != "") & (seg["b"] != "") & (seg["c"] != "") &
              (seg["place"] != seg["b"]) & (seg["b"] != seg["c"])]
    agency = seg["trip_id"].map(t.set_index("trip_id")["agency_name"])
    return pd.DataFrame({"agency": agency.to_numpy(), "a": seg["place"].to_numpy(),
                         "b": seg["b"].to_numpy(), "c": seg["c"].to_numpy(),
                         "minute": (seg["dep"] // 60).astype(int).to_numpy(),
                         "mb": (seg["db"] // 60).astype(int).to_numpy(),
                         "mc": (seg["dc"] // 60).astype(int).to_numpy()})


def doubled_segments(segments: pd.DataFrame) -> pd.DataFrame:
    """Runs of three stations served to the minute at each by two different operators."""
    base = segments.drop_duplicates()
    both = base.merge(base, on=["a", "b", "c", "minute", "mb", "mc"], suffixes=("", "_other"))
    both = both[both["agency"] < both["agency_other"]]
    if not len(both):
        return pd.DataFrame(columns=["agency", "agency_other", "segments"])
    return both.groupby(["agency", "agency_other"]).size().rename("segments").reset_index() \
        .sort_values("segments", ascending=False)


def check_feeds(feeds: dict[str, dict], sentinels: list[dict], dates: list[date],
                bus_min_routes: int, tfl_lines: dict[str, list[str]] | None = None) -> dict:
    """The feed audit. `feeds` maps feed_id -> GTFS tables (already prepared)."""
    report: dict = {"dates": [d.isoformat() for d in dates], "feeds": {}, "failures": []}
    for fid, feed in feeds.items():
        tz = sorted(set(feed["agency"]["agency_timezone"]))
        per_date = trips_by_agency_on(feed, dates)
        rtypes = pd.to_numeric(feed["routes"]["route_type"], errors="coerce").value_counts()
        report["feeds"][fid] = {
            "timezones": tz,
            "routes_by_type": {str(int(k)): int(v) for k, v in rtypes.items()},
            "trips_by_agency": {d.isoformat(): {k: int(v) for k, v in s.items()}
                                for d, s in per_date.items()},
        }
        if tz != ["Europe/London"]:
            report["failures"].append(f"{fid}: agency_timezone is {tz}, not Europe/London")
        for d, s in per_date.items():
            if s.sum() == 0:
                report["failures"].append(f"{fid}: no trips on {d.isoformat()}")

    report["sentinels"] = []
    for s in sentinels:
        hits = sentinel_hits(feeds, s, dates)
        # "days" limits a line to weekdays or Saturdays (the Waterloo & City runs Mon-Fri).
        need = [d for d in dates if s.get("days") is None
                or (s["days"] == "weekday") == (d.weekday() < 5)]
        # min_trips catches an operator the clip has cut to a token service.
        ok = all(hits[d.isoformat()] >= int(s.get("min_trips", 1)) for d in need)
        report["sentinels"].append({"name": s["name"], "trips": hits, "ok": ok})
        if not ok:
            report["failures"].append(f"missing line: {s['name']} {hits}")

    bus_routes = 0
    bus_names: set[str] = set()
    for feed in feeds.values():
        r = feed["routes"]
        rt = pd.to_numeric(r["route_type"], errors="coerce")
        bus = r[rt == 3]
        bus_routes += len(bus)
        bus_names |= set(bus["route_short_name"].str.strip().str.upper())
    report["bus_routes"] = bus_routes
    if bus_routes < bus_min_routes:
        report["failures"].append(f"only {bus_routes} bus routes (expected at least {bus_min_routes})")

    report["duplicates"] = {}
    report["doubled"] = {}
    for d in dates:
        dups = []
        segs = []
        for fid, feed in feeds.items():
            dd = duplicate_trips(feed, d)
            dd.insert(0, "feed", fid)
            dups.append(dd)
            segs.append(segment_keys(feed, d))
        dup = pd.concat(dups, ignore_index=True)
        report["duplicates"][d.isoformat()] = dup.head(50).to_dict("records")
        # Copies a minute or two apart are not removed (real services can be that close), so
        # report them for buses, where overlapping timetable versions occur.
        near = []
        for fid, feed in feeds.items():
            nd = duplicate_trips(feed, d, tolerance_s=120)
            nd = nd[nd["route_id"].isin(set(feed["routes"].loc[
                pd.to_numeric(feed["routes"]["route_type"], errors="coerce").isin(BUS_TYPES), "route_id"]))]
            nd.insert(0, "feed", fid)
            near.append(nd)
        nd = pd.concat(near, ignore_index=True)
        report.setdefault("bus_near_duplicates", {})[d.isoformat()] = {
            "routes": int(len(nd)), "trips": int(nd["duplicates"].sum()) if len(nd) else 0,
            "top": nd.head(20).to_dict("records")}
        bad = dup[(dup["share"] > 0.02) & (dup["duplicates"] >= 3)]
        for row in bad.itertuples():
            report["failures"].append(
                f"doubled trips {d.isoformat()}: {row.feed} {row.agency_name} {row.route_short_name} "
                f"{row.duplicates}/{row.trips}")
        dbl = doubled_segments(pd.concat(segs, ignore_index=True))
        report["doubled"][d.isoformat()] = dbl.head(50).to_dict("records")
        for row in dbl.itertuples():
            if row.segments >= 20:
                report["failures"].append(
                    f"doubled service {d.isoformat()}: {row.agency} and {row.agency_other} share "
                    f"{row.segments} segment departures")

    if tfl_lines is not None:
        report["tfl_lines"] = tfl_line_check(feeds, tfl_lines, bus_names)
        for mode, missing in report["tfl_lines"]["missing"].items():
            if mode == "bus":
                share = report["tfl_lines"]["bus_matched_share"]
                if share < 0.95:
                    report["failures"].append(f"only {share:.0%} of TfL's bus lines are in the bus feed")
            elif missing:
                report["failures"].append(f"TfL {mode} lines not in any feed: {missing}")
    return report


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower().replace("&", "and"))


def tfl_line_check(feeds: dict[str, dict], tfl_lines: dict[str, list[str]],
                   bus_names: set[str]) -> dict:
    """Every line TfL lists has a route with that name; buses by route number."""
    names: set[str] = set()
    for feed in feeds.values():
        r = feed["routes"]
        for c in ("route_short_name", "route_long_name"):
            if c in r.columns:
                names |= {_norm(x) for x in r[c]}
        names |= {_norm(x) for x in feed["agency"]["agency_name"]}
    missing: dict[str, list[str]] = defaultdict(list)
    bus_total = bus_hit = 0
    for mode, lines in tfl_lines.items():
        for line in lines:
            if mode == "bus":
                bus_total += 1
                if line.strip().upper() in bus_names:
                    bus_hit += 1
                else:
                    missing["bus"].append(line)
            elif not any(_norm(line) in n or n == _norm(line) for n in names if n):
                missing[mode].append(line)
    return {"missing": dict(missing), "bus_total": bus_total,
            "bus_matched_share": (bus_hit / bus_total) if bus_total else 1.0}


def report_markdown(report: dict) -> str:
    lines = [f"# Feed check ({', '.join(report['dates'])})", ""]
    lines.append("**Result: " + ("FAIL" if report["failures"] else "pass") + "**")
    lines.append("")
    for f in report["failures"]:
        lines.append(f"- {f}")
    lines += ["", "## Feeds", ""]
    for fid, f in report["feeds"].items():
        lines.append(f"### {fid}")
        lines.append(f"- timezones: {f['timezones']}")
        lines.append(f"- routes by type: {f['routes_by_type']}")
        for d, agencies in f["trips_by_agency"].items():
            top = ", ".join(f"{k} {v}" for k, v in sorted(agencies.items(), key=lambda kv: -kv[1]))
            lines.append(f"- trips on {d}: {top}")
        lines.append("")
    lines += ["## Sentinel lines", "", "| Line | " + " | ".join(report["dates"]) + " | OK |",
              "|---|" + "---|" * len(report["dates"]) + "---|"]
    for s in report["sentinels"]:
        lines.append(f"| {s['name']} | " + " | ".join(str(s["trips"][d]) for d in report["dates"])
                     + f" | {'yes' if s['ok'] else 'NO'} |")
    lines += ["", f"Bus routes: {report['bus_routes']}", ""]
    if "tfl_lines" in report:
        tl = report["tfl_lines"]
        lines.append(f"TfL bus lines matched: {tl['bus_matched_share']:.1%} of {tl['bus_total']}")
        for mode, miss in tl["missing"].items():
            lines.append(f"- not found ({mode}): {', '.join(miss[:40])}" + (" ..." if len(miss) > 40 else ""))
        lines.append("")
    for d, nd in report.get("bus_near_duplicates", {}).items():
        lines.append(f"Bus trips within 2 min of another on the same route and stops, {d}: "
                     f"{nd['trips']} on {nd['routes']} routes (reported, not removed)")
    lines.append("")
    for d in report["dates"]:
        lines += [f"## Doubled service on {d}", ""]
        for r in report["doubled"][d][:20]:
            lines.append(f"- {r['agency']} / {r['agency_other']}: {r['segments']} segment departures")
        lines.append("")
        lines += [f"## Duplicate trips on {d} (top 20)", ""]
        for r in report["duplicates"][d][:20]:
            lines.append(f"- {r['feed']} {r['agency_name']} {r['route_short_name']}: "
                         f"{r['duplicates']}/{r['trips']}")
        lines.append("")
    return "\n".join(lines)
