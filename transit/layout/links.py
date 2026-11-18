"""
Train links for the full London time map (#6, L-02): pairs of stations that a train serves one
after the other. The travel-time tables have no route topology, so it comes from the build's own
prepared GTFS feed (transit/data/work/<build>/gbt.gtfs.zip); only the topology does. Each
link's rest length is the layout's scale times the table's travel time between the two
stations, the same unit every other number in the report uses.

A hop is kept when:
- its trip runs on the build's weekday and is of a station route type (rail, tube, DLR, tram:
  settings.STATION_ROUTE_TYPES and its ranges), so rail-replacement buses, ferries and the
  cable car are left out;
- its two stops are consecutive in the trip (stop_sequence one apart), so a stop the feed's
  clip removed never joins the stations either side of it;
- the train leaves the first stop inside the table's window (13:00 to 14:00 for wd13);
- both stops are platforms (or stops) of stations in the layout, and two different nodes.
Each pair of nodes is one link, however many trains run between them. A link with no time
both ways in the table is dropped and counted, never given a length from elsewhere. A dropped
hop is counted under the first of these that applies: stop_sequence gap, no departure time,
outside the window, a stop not in the layout, same node.

Read with the standard library only (zipfile and csv), so the layout stays numpy only.
"""
from __future__ import annotations

import csv
import io
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path

import numpy as np

from transit import settings

WINDOW_MINUTES = settings.WINDOW_MINUTES


class LinkError(ValueError):
    """The feed can't be read as this module expects."""


def is_station_type(route_type: int) -> bool:
    """The same rule as transit.points.is_station_type (that module imports h3 and pandas)."""
    return route_type in settings.STATION_ROUTE_TYPES or any(
        lo <= route_type <= hi for lo, hi in settings.STATION_ROUTE_TYPE_RANGES)


def gtfs_rows(z: zipfile.ZipFile, name: str):
    names = {Path(n).name: n for n in z.namelist()}
    if name not in names:
        return None
    with z.open(names[name]) as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline="")):
            yield {(k or "").strip(): (v or "").strip() for k, v in row.items()}


def gtfs_seconds(hms: str) -> int | None:
    parts = hms.split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = (int(p) for p in parts)
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def active_services(z: zipfile.ZipFile, day: date) -> set[str]:
    """service_ids running on `day`: calendar, then calendar_dates' additions and removals."""
    ymd = day.strftime("%Y%m%d")
    col = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")[day.weekday()]
    on: set[str] = set()
    for r in gtfs_rows(z, "calendar.txt") or ():
        if r.get(col) == "1" and r.get("start_date", "") <= ymd <= r.get("end_date", ""):
            on.add(r["service_id"])
    for r in gtfs_rows(z, "calendar_dates.txt") or ():
        if r.get("date") != ymd:
            continue
        if r.get("exception_type") == "1":
            on.add(r["service_id"])
        elif r.get("exception_type") == "2":
            on.discard(r["service_id"])
    return on


@dataclass
class Links:
    pairs: np.ndarray                      # (K, 2) node indices, i < j, sorted
    minutes: np.ndarray                    # (K,) table time: mean of both directions, floored
    rest_m: np.ndarray                     # (K,) s · minutes
    trips: np.ndarray                      # (K,) trips with this hop in the window
    routes: list[list[str]] = field(default_factory=list)
    counts: dict = field(default_factory=dict)

    def has(self, i: int, j: int) -> bool:
        a, b = min(i, j), max(i, j)
        return bool(((self.pairs[:, 0] == a) & (self.pairs[:, 1] == b)).any()) if len(self.pairs) else False


def train_links(feed_zip: Path, weekday: date, start: time, pairs, feed_id: str = "gbt",
                minutes: int = WINDOW_MINUTES) -> Links:
    """The links of one window, from the prepared feed (see the module docstring)."""
    node_of = {mid: i for i, ids in enumerate(pairs.member_ids) for mid in ids}
    lo = start.hour * 3600 + start.minute * 60 + start.second
    hi = lo + minutes * 60
    drops: Counter = Counter()
    with zipfile.ZipFile(feed_zip) as z:
        names = {Path(n).name for n in z.namelist()}
        for need in ("routes.txt", "trips.txt", "stops.txt", "stop_times.txt"):
            if need not in names:
                raise LinkError(f"{Path(feed_zip).name} has no {need}")
        if "frequencies.txt" in names:
            raise LinkError(f"{Path(feed_zip).name} has frequencies.txt: frequency-based trips are not read")
        route_type = {}
        for r in gtfs_rows(z, "routes.txt"):
            try:
                route_type[r["route_id"]] = int(r.get("route_type", ""))
            except ValueError:
                continue
        running = active_services(z, weekday)
        trip_route = {}
        trips_on_day = 0
        for r in gtfs_rows(z, "trips.txt"):
            rt = route_type.get(r.get("route_id"))
            if rt is None or not is_station_type(rt) or r.get("service_id") not in running:
                continue
            trip_route[r["trip_id"]] = r["route_id"]
            trips_on_day += 1
        station_of = {}
        for r in gtfs_rows(z, "stops.txt"):
            station_of[r["stop_id"]] = r.get("parent_station") or r["stop_id"]
        stops_by_trip = defaultdict(list)
        for r in gtfs_rows(z, "stop_times.txt"):
            tid = r.get("trip_id")
            if tid not in trip_route:
                continue
            try:
                seq = int(r.get("stop_sequence", ""))
            except ValueError:
                raise LinkError(f"trip {tid} has a stop_sequence that is not a whole number") from None
            dep = gtfs_seconds(r.get("departure_time") or r.get("arrival_time") or "")
            stops_by_trip[tid].append((seq, dep, r.get("stop_id")))

    per_pair: dict[tuple[int, int], dict] = {}
    hops = 0
    trips_in_window = 0
    for tid, rows in stops_by_trip.items():
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
            na = node_of.get(f"{feed_id}:{station_of.get(a, a)}")
            nb = node_of.get(f"{feed_id}:{station_of.get(b, b)}")
            if na is None or nb is None:
                drops["a stop not in the layout"] += 1
                continue
            if na == nb:
                drops["same node"] += 1
                continue
            key = (min(na, nb), max(na, nb))
            rec = per_pair.setdefault(key, {"trips": 0, "routes": set()})
            rec["trips"] += 1
            rec["routes"].add(trip_route[tid])
            any_hop = True
        trips_in_window += any_hop

    keys = sorted(per_pair)
    kept, no_time = [], []
    for k in keys:
        (kept if pairs.used[k] else no_time).append(k)
    drops["no time both ways (links)"] = len(no_time)
    arr = np.array(kept, dtype=np.int64).reshape(-1, 2)
    mins = pairs.t[arr[:, 0], arr[:, 1]].astype(np.float64) if len(arr) else np.zeros(0)
    linked = np.zeros(pairs.n, dtype=bool)
    linked[arr.ravel()] = True
    route_counts = Counter(r for k in kept for r in per_pair[k]["routes"])
    counts = {"feed": Path(feed_zip).name, "weekday": weekday.isoformat(),
              "window": [f"{lo // 3600:02d}:{lo % 3600 // 60:02d}", f"{hi // 3600:02d}:{hi % 3600 // 60:02d}"],
              "station_trips_on_day": trips_on_day, "trips_with_a_link": trips_in_window, "hops_read": hops,
              "links": len(kept), "dropped": dict(sorted(drops.items())),
              "no_time_links": [[pairs.labels[i], pairs.labels[j]] for i, j in no_time],
              "nodes_without_link": [pairs.labels[i] for i in np.where(~linked)[0]],
              "links_by_route": dict(sorted(route_counts.items()))}
    return Links(arr, mins, pairs.s * mins, np.array([per_pair[k]["trips"] for k in kept], dtype=np.int64),
                 [sorted(per_pair[k]["routes"]) for k in kept], counts)
