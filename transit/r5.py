"""
Everything that runs R5 (through r5py). r5py is imported only here, inside functions, after
the JVM heap, JAVA_HOME and the cache folder are set: r5py starts the JVM on import and reads
`--max-memory` from the command line.

Matrices call R5's TravelTimeComputer once per origin, as r5py's TravelTimeMatrix does, but
on a thread pool over the one network and straight into a uint8 memmap (r5py loops origins
one at a time and builds a DataFrame per origin).
"""
from __future__ import annotations

import concurrent.futures as cf
import copy
import json
import logging
import os
import resource
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from transit import settings
from transit.tables import UNREACHABLE, encode_minutes, haversine_m

log = logging.getLogger("transit.r5")
MAX_INT32 = 2**31 - 1
_r5py = None


def r5py():
    """Import r5py with this build's heap, JDK and cache folder."""
    global _r5py
    if _r5py is not None:
        return _r5py
    settings.CACHE.mkdir(parents=True, exist_ok=True)
    os.environ["XDG_CACHE_HOME"] = str(settings.CACHE)
    home = settings.jvm_home()
    if home:
        os.environ["JAVA_HOME"] = home
    if "--max-memory" not in sys.argv:
        sys.argv += ["--max-memory", settings.JVM_MAX_MEMORY]
    import r5py as module

    _r5py = module
    return module


def java_info() -> dict:
    import jpype

    rt = jpype.JClass("java.lang.Runtime").getRuntime()
    system = jpype.JClass("java.lang.System")
    return {"java_version": str(system.getProperty("java.version")),
            "java_vendor": str(system.getProperty("java.vendor")),
            "max_heap_bytes": int(rt.maxMemory())}


def peak_rss_bytes() -> int:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(r if sys.platform == "darwin" else r * 1024)   # macOS reports bytes, Linux KiB


def _mode(name: str):
    return getattr(r5py().TransportMode, name)


# ── network ─────────────────────────────────────────────────────────
def build_network(osm: Path, gtfs: list[Path], out_json: Path):
    """Build (or load from r5py's cache) the network; record time, memory and warnings."""
    started = time.monotonic()
    timer = threading.Timer(settings.NETWORK_TIMEOUT_S, _timeout, args=(out_json, started))
    timer.daemon = True
    timer.start()
    done = threading.Event()

    def heartbeat() -> None:
        # R5 builds silently: say every minute that it is still going, and how big it is.
        while not done.wait(60):
            log.info("still building the network: %.0f min, peak RSS %.1f GB",
                     (time.monotonic() - started) / 60, peak_rss_bytes() / 1e9)

    threading.Thread(target=heartbeat, daemon=True).start()
    m = r5py()
    cached_before = _cached_networks()
    try:
        tn = m.TransportNetwork(str(osm), [str(p) for p in gtfs])
    finally:
        timer.cancel()
        done.set()
    elapsed = time.monotonic() - started
    # r5py saves a network to its cache right after building it: a new or newer cache file
    # means this was a cold build; otherwise it was loaded from the cache.
    cold = _cached_networks() != cached_before
    info = {"status": "built" if cold else "loaded from r5py's cache", "osm": osm.name,
            "gtfs": [p.name for p in gtfs], "seconds": round(elapsed, 1),
            "peak_rss_bytes": peak_rss_bytes(), "r5py_version": m.__version__,
            "timezone": str(tn.timezone), **java_info(),
            "at": datetime.now().isoformat(timespec="seconds")}
    try:
        from r5py.util.classpath import R5_JAR_URL
        info["r5_jar"] = Path(R5_JAR_URL).name
    except ImportError:
        pass
    out_json.parent.mkdir(parents=True, exist_ok=True)
    # network.json keeps the cold build (the go/no-go numbers); cache loads go to their own log.
    if cold or not out_json.exists():
        out_json.write_text(json.dumps(info, indent=2))
    with open(out_json.with_name("network-loads.jsonl"), "a") as f:
        f.write(json.dumps(info) + "\n")
    log.info("network %s in %.0f s, peak RSS %.1f GB", "built" if cold else "loaded", elapsed,
             info["peak_rss_bytes"] / 1e9)
    return tn, info


def _cached_networks() -> dict[str, float]:
    return {p.name: p.stat().st_mtime for p in (settings.CACHE / "r5py").glob("*.transport_network")}


def _timeout(out_json: Path, started: float) -> None:
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps({"status": "timeout", "seconds": time.monotonic() - started,
                                    "peak_rss_bytes": peak_rss_bytes()}, indent=2))
    log.error("network build passed %d s: stopping (no-go)", settings.NETWORK_TIMEOUT_S)
    os._exit(3)


# ── station access (experimental, off by default) ───────────────────
# Original link-edge lengths, per network (an edge index means nothing in another network).
_link_lengths: dict[int, dict[int, int]] = {}
_R5_JAR = "r5-v7.5.1-r5py-all.jar"


def _is_station_route_type(rt: int) -> bool:
    return rt in settings.STATION_ROUTE_TYPES or any(lo <= rt <= hi for lo, hi in settings.STATION_ROUTE_TYPE_RANGES)


def station_access(tn, metres: float, transfers: bool = False) -> dict:
    """
    Time getting into and out of stations. The feed puts every platform of a station at one
    coordinate, which R5 links to the nearest street, so getting from the street to a platform
    costs it almost nothing. Each stop has its own link edge to the street (R5 gives a stop a
    vertex of its own); this lengthens the link edges of rail, tube and tram stops by `metres`.

    Access (the street search from an origin) sees the new lengths directly. Egress uses R5's
    stop-to-vertex distance tables, which are built with the network, so they are rebuilt here,
    and the linkage cache is replaced so no egress table from the old lengths survives. Changes
    between platforms keep R5's own precomputed transfers unless `transfers` is set, which
    recomputes them (a change then pays twice). Lengths are set from the originals, so calling
    it again replaces the earlier value. This edits private R5 state: it refuses any R5 jar but
    the one it was written for, and checks its own effect.

    Experimental: with this on (300 m, plus +1 min a ride), the quick look fitted Journey
    Planner's convention of walking from the nearest street address, not real station access
    (docs/transit-spike.md).
    """
    import jpype
    from r5py.util.classpath import R5_JAR_URL

    if Path(R5_JAR_URL).name != _R5_JAR:
        raise RuntimeError(f"station_access was written for {_R5_JAR}, not {Path(R5_JAR_URL).name}")
    started = time.monotonic()
    jtn = tn._transport_network
    tl, sl = jtn.transitLayer, jtn.streetLayer
    originals = _link_lengths.setdefault(id(tn), {})
    station_pattern = [_is_station_route_type(int(tl.routes.get(tl.tripPatterns.get(p).routeIndex).route_type))
                       for p in range(tl.tripPatterns.size())]
    cur = sl.edgeStore.getCursor()
    add = int(round(metres * 1000))
    stops, probe = 0, None
    for s in range(tl.getStopCount()):
        pats = tl.patternsForStop.get(s)
        if not any(station_pattern[pats.get(i)] for i in range(pats.size())):
            continue
        v = tl.streetVertexForStop.get(s)
        if v < 0:
            continue
        stops += 1
        # R5 stores one length per edge pair (there and back): set each pair once.
        pairs = set()
        for lst in (sl.outgoingEdges.get(v), sl.incomingEdges.get(v)):
            pairs |= {lst.get(i) // 2 for i in range(lst.size())}
        for pair in pairs:
            cur.seek(pair * 2)
            if pair not in originals:
                originals[pair] = cur.getLengthMm()
            cur.setLengthMm(originals[pair] + add)
            probe = probe or pair
    if probe is not None:
        cur.seek(probe * 2)
        if cur.getLengthMm() != originals[probe] + add:
            raise RuntimeError("station_access: a link edge did not take its new length")
    # Egress: rebuild the stop-to-vertex distance tables and drop cached linkages.
    tl.buildDistanceTables(None)
    jtn.linkageCache = jpype.JClass("com.conveyal.r5.analyst.LinkageCache")()
    if transfers:
        # findTransfers() only works on stops that have none yet, so clear them all first.
        tl.streetTransfers.clear()
        loader_cls = jpype.JClass("com.conveyal.r5.transit.GtfsTransferLoader")
        config = jpype.JClass("com.conveyal.r5.analyst.cluster.TransportNetworkConfig$TransferConfig").OSM_ONLY
        jpype.JClass("com.conveyal.r5.transit.TransferFinder")(jtn, loader_cls(tl, config)).findTransfers()
        if tl.streetTransfers.size() != tl.getStopCount():
            raise RuntimeError("R5 did not recompute the transfers for every stop")
    info = {"metres": metres, "station_stops": stops, "transfers_recomputed": transfers,
            "egress_tables_rebuilt": True, "seconds": round(time.monotonic() - started, 1)}
    log.info("station access: +%.0f m on the street links of %d rail/tube/tram stops (%.0f s)",
             metres, stops, info["seconds"])
    return info


# ── snapping ────────────────────────────────────────────────────────
def snap_points(tn, points) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(snapped lon, snapped lat, distance m); NaN where no street within the radius."""
    import geopandas as gpd
    import shapely

    lon = points["lon"].to_numpy(float)
    lat = points["lat"].to_numpy(float)
    slon = np.full(len(points), np.nan)
    slat = np.full(len(points), np.nan)
    for kind, radius in (("station", settings.SNAP_RADIUS_STATION_M),
                         ("cell", settings.SNAP_RADIUS_CELL_M)):
        sel = (points["kind"] == kind).to_numpy()
        if not sel.any():
            continue
        gs = gpd.GeoSeries(shapely.points(lon[sel], lat[sel]), crs="EPSG:4326")
        snapped = tn.snap_to_network(gs, radius=radius, street_mode=_mode("WALK"))
        empty = snapped.is_empty.to_numpy()
        sx = np.where(empty, np.nan, snapped.x.where(~snapped.is_empty).to_numpy(float))
        sy = np.where(empty, np.nan, snapped.y.where(~snapped.is_empty).to_numpy(float))
        slon[sel], slat[sel] = sx, sy
    dist = np.where(np.isfinite(slon), haversine_m(lon, lat, slon, slat), np.nan)
    return slon, slat, dist


# ── matrices ────────────────────────────────────────────────────────
def _gdf(ids, lon, lat):
    import geopandas as gpd
    import shapely

    return gpd.GeoDataFrame({"id": [str(i) for i in ids]},
                            geometry=shapely.points(np.asarray(lon), np.asarray(lat)), crs="EPSG:4326")


def regional_task(tn, destinations, departure: datetime, walking_only: bool = False,
                  max_time_min: int | None = None):
    """An r5py RegionalTask with every setting given explicitly."""
    m = r5py()
    cfg = settings.R5
    if walking_only:
        modes, access = [_mode("WALK")], [_mode("WALK")]
    else:
        modes = [_mode(x) for x in cfg["transport_modes"]]
        access = [_mode(x) for x in cfg["access_modes"]]
    egress = access if walking_only else [_mode(x) for x in cfg["egress_modes"]]
    max_time = max_time_min if max_time_min is not None else cfg["max_time_min"]
    window = 1 if walking_only else cfg["departure_time_window_min"]
    return m.RegionalTask(
        tn, origin=None, destinations=destinations, departure=departure,
        departure_time_window=timedelta(minutes=window), percentiles=list(cfg["percentiles"]),
        transport_modes=modes, access_modes=access, egress_modes=egress,
        max_time=timedelta(minutes=max_time),
        max_time_walking=timedelta(minutes=max_time if walking_only else cfg["max_time_walking_min"]),
        speed_walking=cfg["speed_walking_kmh"],
        max_public_transport_rides=cfg["max_public_transport_rides"],
    )


def one_origin(tn, task, lon: float, lat: float) -> np.ndarray:
    """Median minutes from one origin to the task's destinations (inf: no time)."""
    import jpype
    import shapely

    ttc_cls = jpype.JClass("com.conveyal.r5.analyst.TravelTimeComputer")
    req = copy.copy(task)
    req.origin = shapely.Point(lon, lat)
    res = ttc_cls(req, tn).computeTravelTimes()
    vals = np.asarray(res.travelTimes.getValues()[0], dtype=np.float64)
    vals[vals >= MAX_INT32] = np.inf
    return vals


def fingerprint_path(out_path: Path) -> Path:
    return out_path.with_suffix(".fingerprint.json")


def table_window(tn, points, ok: np.ndarray, departure: datetime, out_path: Path,
                 threads: int = settings.MATRIX_THREADS, limit_origins: int | None = None,
                 fingerprint: dict | None = None, force: bool = False) -> dict:
    """
    Fill out_path (N x N uint8 .npy memmap) for one window. Rows already listed in the
    done-list next to it are skipped, so a stopped build resumes where it was, but only when
    the table's fingerprint (settings, points, feeds, network, departure, code) is the one
    given: otherwise, or with `force`, the table starts again. The fingerprint file records
    whether every usable origin is done; manifest and package refuse an incomplete table.
    """
    n = len(points)
    done_path = out_path.with_suffix(".done")
    fp_path = fingerprint_path(out_path)
    old = json.loads(fp_path.read_text()) if fp_path.exists() else None
    if out_path.exists() and (force or old is None or old.get("inputs") != fingerprint):
        log.info("%s: %s, starting it again", out_path.name,
                 "--force" if force else "made from other inputs or settings")
        for p in (out_path, done_path, fp_path):
            p.unlink(missing_ok=True)
    if out_path.exists():
        table = np.load(out_path, mmap_mode="r+")
        if table.shape != (n, n):
            raise SystemExit(f"{out_path} has shape {table.shape}, expected {(n, n)}")
    else:
        # A new table: no checkpoint from an earlier one may survive (a deleted .npy with its
        # .done left behind would otherwise skip rows that are not in the new file).
        done_path.unlink(missing_ok=True)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        table = np.lib.format.open_memmap(out_path, mode="w+", dtype=np.uint8, shape=(n, n))
        table[:] = UNREACHABLE
        usable = np.where(ok)[0]
        table[usable, usable] = 0          # a masked point stays 255 everywhere, its diagonal too
        table.flush()
        fp_path.write_text(json.dumps({"inputs": fingerprint, "complete": False}, indent=2, default=str))
    done = set(int(x) for x in done_path.read_text().split()) if done_path.exists() else set()

    lon = points["snap_lon"].to_numpy(float)
    lat = points["snap_lat"].to_numpy(float)
    dest_idx = np.where(ok)[0]
    task = regional_task(tn, _gdf(points["idx"].to_numpy()[dest_idx], lon[dest_idx], lat[dest_idx]),
                         departure)
    todo = [int(i) for i in np.where(ok)[0] if int(i) not in done]
    if limit_origins is not None:
        todo = todo[:limit_origins]
    started = time.monotonic()
    lock = threading.Lock()
    finished = 0

    ride_cost = settings.R5.get("ride_cost_min", 0.0)
    if ride_cost:
        from transit.calibrate import RIDE_LIMITS, costed, with_rides

        limited = [with_rides(task, k) for k in RIDE_LIMITS]
        walk_task = regional_task(tn, _gdf(points["idx"].to_numpy()[dest_idx], lon[dest_idx], lat[dest_idx]),
                                  departure, walking_only=True,
                                  max_time_min=settings.R5["max_time_walking_min"])

    def run(i: int) -> tuple[int, np.ndarray]:
        if not ride_cost:
            return i, one_origin(tn, task, lon[i], lat[i])
        T = np.vstack([one_origin(tn, t, lon[i], lat[i]) for t in limited])
        return i, costed(one_origin(tn, walk_task, lon[i], lat[i]), T, ride_cost, 0.0)

    with open(done_path, "a") as done_file, cf.ThreadPoolExecutor(max_workers=threads) as pool:
        for k in range(0, len(todo), settings.ORIGIN_CHUNK):
            chunk = todo[k:k + settings.ORIGIN_CHUNK]
            for i, vals in pool.map(run, chunk):
                row = np.full(n, UNREACHABLE, dtype=np.uint8)
                row[dest_idx] = encode_minutes(vals, settings.MAX_MINUTES)
                row[i] = 0
                with lock:
                    table[i] = row
                    finished += 1
            table.flush()
            done_file.write("\n".join(str(i) for i in chunk) + "\n")
            done_file.flush()
            rate = (time.monotonic() - started) / max(1, finished)
            log.info("%s: %d/%d origins, %.2f s per origin, ~%.0f min left", out_path.name,
                     len(done) + finished, int(ok.sum()), rate, rate * (len(todo) - finished) / 60)
    elapsed = time.monotonic() - started
    done_now = set(int(x) for x in done_path.read_text().split()) if done_path.exists() else set()
    complete = set(int(i) for i in np.where(ok)[0]) <= done_now
    fp = json.loads(fp_path.read_text())
    fp.update({"complete": complete, "origins_done": len(done_now), "origins_usable": int(ok.sum())})
    fp_path.write_text(json.dumps(fp, indent=2, default=str))
    return {"origins_computed": finished, "seconds": round(elapsed, 1),
            "seconds_per_origin": round(elapsed / max(1, finished), 3), "threads": threads,
            "peak_rss_bytes": peak_rss_bytes(), "complete": complete}


def walk_minutes(tn, origins_lonlat: list[tuple[float, float]], dests_lonlat, departure: datetime,
                 max_time_min: int = settings.WALKSHED_MAX_MIN) -> np.ndarray:
    """Walking-only minutes, origins x destinations (inf: no path within max_time_min)."""
    dl = np.asarray(dests_lonlat, dtype=float)
    task = regional_task(tn, _gdf(range(len(dl)), dl[:, 0], dl[:, 1]), departure,
                         walking_only=True, max_time_min=max_time_min)
    return np.vstack([one_origin(tn, task, lo, la) for lo, la in origins_lonlat]) \
        if origins_lonlat else np.zeros((0, len(dl)))


def walksheds(tn, points, ok: np.ndarray, departure: datetime, threads: int = settings.MATRIX_THREADS):
    """
    Access points and the res-10 cells reached from them on foot, as Walksheds: every usable
    station to the cells within 2 km, and every usable table cell to the cells within 1 km (a
    cell's table row carries its bus access). Cell centres are snapped like table points; one
    with no street within WALKSHED_SNAP_M (water, large parks) is left out, not linked to a far
    bank. The same walk serves access and egress.
    """
    import geopandas as gpd
    import h3
    import shapely

    from transit.tables import Walksheds, disk_cells

    kinds = points["kind"].to_numpy()
    groups = [("station", settings.WALKSHED_RADIUS_M), ("cell", settings.CELL_ACCESS_RADIUS_M)]
    rows = []
    started = time.monotonic()
    snapped_cache: dict[int, tuple[float, float] | None] = {}
    lock = threading.Lock()
    batch = settings.WALKSHED_STATION_BATCH
    info = {}

    def snapped(cells: np.ndarray) -> dict[int, tuple[float, float] | None]:
        with lock:
            missing = [int(c) for c in cells if int(c) not in snapped_cache]
        if missing:
            ll = np.array([h3.cell_to_latlng(h3.int_to_str(c)) for c in missing])
            gs = gpd.GeoSeries(shapely.points(ll[:, 1], ll[:, 0]), crs="EPSG:4326")
            sn = tn.snap_to_network(gs, radius=settings.WALKSHED_SNAP_M, street_mode=_mode("WALK"))
            with lock:
                for c, g in zip(missing, sn):
                    snapped_cache[c] = None if g.is_empty else (float(g.x), float(g.y))
        with lock:
            return {int(c): snapped_cache[int(c)] for c in cells}

    for kind, radius in groups:
        sel_pts = points[(kinds == kind) & ok]
        ids = sel_pts["idx"].to_numpy()
        lons = sel_pts["snap_lon"].to_numpy(float)
        lats = sel_pts["snap_lat"].to_numpy(float)

        def run_batch(b: int, lons=lons, lats=lats, ids=ids, radius=radius):
            sel = slice(b, b + batch)
            disks = [disk_cells(lo, la, settings.H3_RES_WALK, radius) for lo, la in zip(lons[sel], lats[sel])]
            union = np.unique(np.concatenate(disks))
            sn = snapped(union)
            keep = np.array([sn[int(c)] is not None for c in union])
            cells_kept = union[keep]
            if not len(cells_kept):
                return [(int(ids[b + k]), d, np.full(len(d), np.inf)) for k, d in enumerate(disks)]
            ll = np.array([sn[int(c)] for c in cells_kept])
            task = regional_task(tn, _gdf(range(len(cells_kept)), ll[:, 0], ll[:, 1]), departure,
                                 walking_only=True, max_time_min=settings.WALKSHED_MAX_MIN)
            out = []
            for k, (lo, la) in enumerate(zip(lons[sel], lats[sel])):
                vals = one_origin(tn, task, lo, la)
                full = np.full(len(union), np.inf)
                full[keep] = vals
                out.append((int(ids[b + k]), disks[k], full[np.searchsorted(union, disks[k])]))
            return out

        done = 0
        with cf.ThreadPoolExecutor(max_workers=threads) as pool:
            for part in pool.map(run_batch, range(0, len(sel_pts), batch)):
                rows.extend(part)
                done += len(part)
                if done % (batch * 20) < batch:
                    log.info("walksheds (%s): %d/%d", kind, done, len(sel_pts))
        info[kind + "s"] = int(len(sel_pts))
    ws = Walksheds.build(sorted(rows, key=lambda r: r[0]))
    dropped = sum(1 for v in snapped_cache.values() if v is None)
    info.update({"entries": int(len(ws.cells)), "cells_checked": len(snapped_cache),
                 "cells_without_street": dropped, "seconds": round(time.monotonic() - started, 1)})
    log.info("walksheds: %s", info)
    return ws, info


# ── itineraries ─────────────────────────────────────────────────────
def itineraries(tn, origin: tuple[float, float], dest: tuple[float, float], departure: datetime,
                max_time_min: int = 75, rides: int = 3, rail_only: bool = True) -> list[dict]:
    """
    R5's itinerary options for one departure (legs with mode, route, minutes). R5's itinerary
    search is multi-criteria and keeps far more partial journeys than the table search: over
    London's buses it ran out of a 10 GB heap even at 90 min and 4 rides. It is only used to
    name the line a journey takes, so by default it searches rail, tube and tram only.
    """
    m = r5py()
    cfg = settings.R5
    modes = ([_mode("RAIL"), _mode("SUBWAY"), _mode("TRAM")] if rail_only
             else [_mode(x) for x in cfg["transport_modes"]])
    di = m.DetailedItineraries(
        tn, origins=_gdf(["o"], [origin[0]], [origin[1]]), destinations=_gdf(["d"], [dest[0]], [dest[1]]),
        departure=departure, departure_time_window=timedelta(minutes=1),
        transport_modes=modes,
        access_modes=[_mode(x) for x in cfg["access_modes"]],
        egress_modes=[_mode(x) for x in cfg["egress_modes"]],
        max_time=timedelta(minutes=max_time_min),
        max_time_walking=timedelta(minutes=cfg["max_time_walking_min"]),
        speed_walking=cfg["speed_walking_kmh"],
        max_public_transport_rides=rides,
    )
    options = []
    for opt, legs in di.groupby("option"):
        total = float(legs["travel_time"].sum().total_seconds() / 60 + legs["wait_time"].sum().total_seconds() / 60)
        options.append({"option": int(opt), "minutes": round(total, 1), "legs": [
            {"mode": str(r.transport_mode), "route_id": r.route_id, "agency_id": r.agency_id,
             "feed": r.feed, "start_stop_id": r.start_stop_id, "end_stop_id": r.end_stop_id,
             "minutes": round(r.travel_time.total_seconds() / 60, 1),
             "wait": round(r.wait_time.total_seconds() / 60, 1)}
            for r in legs.sort_values("segment").itertuples()]})
    return sorted(options, key=lambda o: o["minutes"])
