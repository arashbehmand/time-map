"""
Bus and walking springs for the London time map's travel modes (#23 part 2).

Both take their times from the build's per-mode tables (#30): tt_wd13_bus.npy (buses, with walking
to and from them, or walking straight there within 30 min if that is faster) and tt_wd13_walk.npy
(walking only, up to 180 min), on the same points as the all-mode table. No routing runs here.

**A pair's time** follows the train rule (prep.station_pairs): each way, the fastest time from any
of one node's table points to any of the other's; both ways must have one (255 is no time); the
time is the mean of the two, floored at FLOOR_MIN. A station node's table points are its merged
stations; a cell's is its own point, none if the build masked it; a guard node has none.

**Walking springs** sit on the mesh's edges between two stations or cells (never a guard node):
walking has no routes, and these are the neighbours the mesh holds apart. At the layout's scale
(about 17 km/h door to door) a walk is slower than the map, so on the real map no walking spring
pulls; walking_links reports how far: each spring's rest length over its real length.

**Bus links** come from the build's prepared bus feed (bods.gtfs.zip; the rail feed has no buses
but rail replacement): bus trips (route_type 3 or 700-799) running on the build's weekday. These
route types don't tell a rail replacement bus from any other: BODS carries some (route "TUBE" on
build 2026-11-17), and they are kept like any bus the bus table routes. Each
stop belongs to its nearest station or cell (car.owners). A hop between stops one stop_sequence
apart, leaving the first inside the window, joins their two nodes; consecutive stops on one node
merge (the hop is dropped, counted as "same node"). Each pair of nodes is one link, however many
trips use it. A link is kept when the bus table has a time both ways and it is below the walking
table's: the bus table is the faster of a bus and a walk, so a time no faster than walking has no
bus in it. stop_times.txt is read as a stream, one trip at a time, and refused unless each trip's
rows are together (8.7 million rows on build 2026-11-17).

Standard library zip and csv, and numpy.
"""
from __future__ import annotations

import csv
import io
import itertools
import math
import zipfile
from collections import Counter
from datetime import date, time
from pathlib import Path

import numpy as np

from transit.layout import links as linkmod
from transit.layout.frame import to_local
from transit.layout.mesh import RING, Mesh
from transit.layout.prep import FLOOR_MIN, UNREACHABLE, RefusedInput

BUS_ROUTE_TYPES = {3}
BUS_ROUTE_TYPE_RANGES = ((700, 799),)
LEFT_OUT_MAX = 2000              # left-out pairs listed by name (all are counted)


def is_bus_type(route_type: int) -> bool:
    return route_type in BUS_ROUTE_TYPES or any(lo <= route_type <= hi for lo, hi in BUS_ROUTE_TYPE_RANGES)


def node_members(mesh: Mesh, pairs, cells: list[dict], pick, point_ok: np.ndarray) -> list[list[int]]:
    """The table points (points.csv idx) of every mesh node, in mesh order: stations (pairs' order),
    cells (cells[pick[k]]), guard nodes (none)."""
    out = [list(m) for m in pairs.members]
    for k in np.asarray(pick, dtype=np.int64).tolist():
        p = cells[k]
        i = int(p["idx"])
        out.append([i] if p.get("ok", "True") == "True" and bool(point_ok[i]) else [])
    out += [[] for _ in range(mesh.n - len(out))]
    if len(out) != mesh.n or (mesh.kinds[:len(pairs.members)] == RING).any():
        raise RefusedInput("the mesh's nodes are not the stations and cells given, in order")
    return out


def node_names(mesh: Mesh, pairs) -> list[str]:
    """A name for every mesh node: a station's label, "a point near <station>" for a cell."""
    out = list(pairs.labels)
    for i in range(pairs.n, mesh.n):
        near = int(np.argmin(np.hypot(*(pairs.xy - mesh.P[i]).T)))
        out.append(f"a point near {pairs.labels[near]}" if mesh.kinds[i] != RING else "a guard node")
    return out


def pair_minutes(table: np.ndarray, members: list[list[int]], ij: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """Minutes between each pair of nodes (NaN when none) and why not ("" when there is one): the
    module docstring's rule."""
    t = np.full(len(ij), np.nan)
    why = []
    for k, (i, j) in enumerate(np.asarray(ij, dtype=np.int64).reshape(-1, 2).tolist()):
        a, b = members[i], members[j]
        if not a or not b:
            why.append("a node with no table point")
            continue
        there = int(np.asarray(table[np.ix_(a, b)]).min())
        back = int(np.asarray(table[np.ix_(b, a)]).min())
        if there == UNREACHABLE or back == UNREACHABLE:
            why.append("no time both ways")
            continue
        t[k] = max((there + back) / 2.0, FLOOR_MIN)
        why.append("")
    return t, why


def _ratio(mesh, ij, rest_m) -> dict:
    real = np.hypot(*(mesh.P[ij[:, 0]] - mesh.P[ij[:, 1]]).T) if len(ij) else np.zeros(0)
    r = rest_m / np.maximum(real, 1e-9)
    return {"min": float(r.min()), "median": float(np.median(r))} if len(r) else {"min": None, "median": None}


def walk_links(mesh: Mesh, members: list[list[int]], walk: np.ndarray, s: float,
               names: list[str]) -> linkmod.Links:
    """Walking springs on the mesh's edges between stations and cells (module docstring)."""
    inner = (mesh.kinds[mesh.edges] != RING).all(axis=1)
    ij = mesh.edges[inner]
    t, why = pair_minutes(walk, members, ij)
    keep = np.isfinite(t)
    drops = Counter(w for w in why if w)
    left = [[names[int(i)], names[int(j)], w] for (i, j), w in zip(ij.tolist(), why) if w]
    arr, mins = ij[keep], t[keep]
    counts = {"edges": int(len(ij)), "links": int(keep.sum()), "dropped": dict(sorted(drops.items())),
              "left_out": left[:LEFT_OUT_MAX], "left_out_count": len(left),
              "rest_over_real": _ratio(mesh, arr, s * mins)}
    return linkmod.Links(arr, mins, s * mins, np.zeros(len(arr), dtype=np.int64), [[] for _ in range(len(arr))],
                         counts)


def _stop_times(z: zipfile.ZipFile, wanted):
    """(trip_id, [(stop_sequence, departure seconds or None, stop_id)]) for every trip in `wanted`,
    one trip at a time (blank lines skipped, the file closed when done); refused (linkmod.LinkError)
    when a trip's rows are not together, a row is shorter than the header, or a stop_sequence is not
    a whole number. A stop's departure is its departure_time, or its arrival_time when that is
    blank, as for the train links (links.train_links)."""
    names = {Path(n).name: n for n in z.namelist()}
    with z.open(names["stop_times.txt"]) as f:
        rd = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))
        head = [h.strip() for h in next(rd, [])]
        try:
            ti, si, qi = head.index("trip_id"), head.index("stop_id"), head.index("stop_sequence")
        except ValueError:
            raise linkmod.LinkError("stop_times.txt needs trip_id, stop_id and stop_sequence") from None
        di = head.index("departure_time") if "departure_time" in head else None
        ai = head.index("arrival_time") if "arrival_time" in head else None
        width = max(i for i in (ti, si, qi, di, ai) if i is not None) + 1

        def checked(rows):
            for line, r in enumerate(rows, start=2):
                if not r or (len(r) == 1 and not r[0].strip()):
                    continue
                if len(r) < width:
                    raise linkmod.LinkError(f"stop_times.txt line {line} has {len(r)} fields, the header {len(head)}")
                yield r

        seen = set()
        for tid, rows in itertools.groupby(checked(rd), key=lambda r: r[ti].strip()):
            if tid in seen:
                raise linkmod.LinkError(f"stop_times.txt is not grouped by trip: trip {tid} comes back")
            seen.add(tid)
            if tid not in wanted:
                for _ in rows:
                    pass
                continue
            out = []
            for r in rows:
                try:
                    seq = int(r[qi])
                except ValueError:
                    raise linkmod.LinkError(f"trip {tid} has a stop_sequence that is not a whole number") from None
                hms = (r[di].strip() if di is not None else "") or (r[ai].strip() if ai is not None else "")
                out.append((seq, linkmod.gtfs_seconds(hms), r[si].strip()))
            yield tid, out


def bus_links(feed_zip: Path, weekday: date, start: time, mesh: Mesh, members: list[list[int]],
              bus: np.ndarray, walk: np.ndarray, s: float, names: list[str],
              minutes: int = linkmod.WINDOW_MINUTES) -> linkmod.Links:
    """Bus links of one window from the prepared bus feed (module docstring)."""
    from transit.layout.car import owners

    lo = start.hour * 3600 + start.minute * 60 + start.second
    hi = lo + minutes * 60
    drops: Counter = Counter()
    with zipfile.ZipFile(feed_zip) as z:
        have = {Path(n).name for n in z.namelist()}
        for need in ("routes.txt", "trips.txt", "stops.txt", "stop_times.txt"):
            if need not in have:
                raise linkmod.LinkError(f"{Path(feed_zip).name} has no {need}")
        if "frequencies.txt" in have:
            raise linkmod.LinkError(f"{Path(feed_zip).name} has frequencies.txt: frequency-based trips are not read")
        route_name = {}
        for r in linkmod.gtfs_rows(z, "routes.txt"):
            try:
                rt = int(r.get("route_type", ""))
            except ValueError:
                continue
            if is_bus_type(rt):
                route_name[r["route_id"]] = r.get("route_short_name") or r.get("route_long_name") or r["route_id"]
        running = linkmod.active_services(z, weekday)
        trip_route = {r["trip_id"]: r["route_id"] for r in linkmod.gtfs_rows(z, "trips.txt")
                      if r.get("route_id") in route_name and r.get("service_id") in running}
        ids, lon, lat = [], [], []
        for r in linkmod.gtfs_rows(z, "stops.txt"):
            try:
                x, y = float(r.get("stop_lon", "")), float(r.get("stop_lat", ""))
            except ValueError:
                continue
            if math.isfinite(x) and math.isfinite(y):
                ids.append(r["stop_id"]); lon.append(x); lat.append(y)       # noqa: E702
        xy = to_local(np.array(lon), np.array(lat)) if ids else np.zeros((0, 2))
        own = owners(xy, mesh) if ids else np.zeros(0, dtype=np.int64)
        node_of = dict(zip(ids, own.tolist()))
        dist_of = dict(zip(ids, (np.hypot(*(xy - mesh.P[own]).T) if ids else np.zeros(0)).tolist()))
        used: set[str] = set()                     # the stops of the hops that make links

        per_pair: dict[tuple[int, int], dict] = {}
        hops = trips_with_link = trips_read = 0
        for tid, rows in _stop_times(z, trip_route):
            trips_read += 1
            rows.sort(key=lambda x: x[0])
            any_hop = False
            for (s0, d0, a), (s1, _, b) in zip(rows, rows[1:]):
                hops += 1
                if s1 - s0 != 1:
                    drops["stop_sequence gap"] += 1
                    continue
                if d0 is None:
                    drops["no departure time"] += 1
                    continue
                if not lo <= d0 < hi:
                    drops["outside the window"] += 1
                    continue
                na, nb = node_of.get(a), node_of.get(b)
                if na is None or nb is None:
                    drops["a stop with no position"] += 1
                    continue
                if na == nb:
                    drops["same node"] += 1
                    continue
                rec = per_pair.setdefault((min(na, nb), max(na, nb)), {"trips": 0, "routes": set()})
                used.update((a, b))
                rec["trips"] += 1
                rec["routes"].add(route_name[trip_route[tid]])
                any_hop = True
            trips_with_link += any_hop

    dist = np.array([dist_of[x] for x in sorted(used)])
    keys = sorted(per_pair)
    ij = np.array(keys, dtype=np.int64).reshape(-1, 2)
    tb, why = pair_minutes(bus, members, ij)
    tw, _ = pair_minutes(walk, members, ij)
    slower = np.isfinite(tb) & np.isfinite(tw) & (tb >= tw)
    reasons = [w or ("not faster than walking" if sl else "") for w, sl in zip(why, slower)]
    keep = np.array([not w for w in reasons], dtype=bool)
    link_drops = Counter(w for w in reasons if w)
    left = [[names[i], names[j], w] for (i, j), w in zip(keys, reasons) if w]
    arr, mins = ij[keep], tb[keep]
    kept = [k for k, ok in zip(keys, keep) if ok]
    linked = np.zeros(mesh.n, dtype=bool)
    linked[arr.ravel()] = True
    by_route = Counter(r for k in kept for r in per_pair[k]["routes"])
    counts = {"feed": Path(feed_zip).name, "weekday": weekday.isoformat(),
              "window": [f"{lo // 3600:02d}:{lo % 3600 // 60:02d}", f"{hi // 3600:02d}:{hi % 3600 // 60:02d}"],
              "bus_routes": len(route_name), "bus_trips_on_day": len(trip_route), "trips_read": trips_read,
              "trips_with_a_hop": trips_with_link, "hops_read": hops, "dropped": dict(sorted(drops.items())),
              "candidates": len(keys), "links": int(keep.sum()), "links_dropped": dict(sorted(link_drops.items())),
              "left_out": left[:LEFT_OUT_MAX], "left_out_count": len(left),
              "stops": len(ids), "stops_used": len(used),
              "stop_to_node_m": {"median": float(np.median(dist)) if len(dist) else None,
                                 "p90": float(np.percentile(dist, 90)) if len(dist) else None,
                                 "max": float(dist.max()) if len(dist) else None},
              "nodes_without_link": int((~linked & (mesh.kinds != RING)).sum()),
              "routes_with_a_link": len(by_route),
              "links_by_route_top": [[r, c] for r, c in by_route.most_common(10)],
              "rest_over_real": _ratio(mesh, arr, s * mins)}
    return linkmod.Links(arr, mins, s * mins, np.array([per_pair[k]["trips"] for k in kept], dtype=np.int64),
                         [sorted(per_pair[k]["routes"]) for k in kept], counts)


def by_route_pulls(lk: linkmod.Links, on: np.ndarray) -> list[str]:
    """The routes with the most pulling links (`on`: one bool per link)."""
    c: Counter = Counter()
    for k in np.where(on)[0].tolist():
        for r in lk.routes[k]:
            c[r] += 1
    return [r for r, _ in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]


__all__ = ["bus_links", "by_route_pulls", "node_members", "node_names", "pair_minutes", "walk_links"]
