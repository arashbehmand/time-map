"""
Fit how R5's times are costed to TfL Journey Planner, on a gate sample's cached replies (no
new TfL calls).

R5 takes a ride as costing only its 1-min boarding allowance, and on this feed getting into
a station, out of it and between its platforms costs almost nothing (a station's stops share
one coordinate). Journey Planner allows real time for all of these. Two levers are fitted:
extra length on the street links of rail, tube and tram stops (transit.r5.station_access),
and a per-ride / per-change cost. The quick look showed the gap grows with the number of
rides and changes (walk only +1 min; one ride -2; one change -4; two or more -5).

The model, computed from R5:

    time = min( W,  min over k of  T_k + ride_cost * k + change_cost * (k - 1) )

W is the walk-only time and T_k the fastest time with at most k rides (R5's ride limit; R5
keeps the walk-only option in it). This is an upper bound, not the exact costed time, for two
reasons: T_k is already a median over the window, and the minimum of medians is at least the
median of the per-minute minimum; and a trip needing more rides than a listed limit is charged
the next limit up (with limits 1-5 and 8, a 6- or 7-ride trip pays for 8).

    python -m transit.calibrate --build <id> [--report gate/a-provisional/report.json]

Writes out/<build>/calibrate.json and .md. Calibrating on the same pairs that score it shows
how well the model can fit, not an independent pass: a fresh sample would confirm it.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import copy
import itertools
import json
import logging
import math
import time
from datetime import timedelta

import numpy as np

from transit import settings
from transit.tables import band_of, error_stats

log = logging.getLogger("transit.calibrate")

RIDE_LIMITS = (1, 2, 3, 4, 5, 8)
SPEEDS = (4.0,)
RIDE_COSTS = (0, 1)
CHANGE_COSTS = (0,)
STATION_ACCESS_M = (0, 150, 200, 250, 300, 350)


def with_rides(task, rides: int):
    """
    A copy of the task with at most `rides` rides. R5 insists the walk-only option stays, so
    T_k = min(W, transit with <= k rides). That keeps walking right: when walking wins,
    T_k + costs > W and the minimum is W anyway.
    """
    req = copy.copy(task)
    req._regional_task.maxRides = rides
    return req


def times_from(tn, origin, dests_lonlat, departure, speed: float) -> tuple[np.ndarray, np.ndarray]:
    """(W, T) from one origin: W walk-only minutes (n,), T by ride limit (k, n)."""
    import jpype
    import shapely

    from transit import r5

    m = r5.r5py()
    cfg = settings.R5
    dl = np.asarray(dests_lonlat, dtype=float)
    gdf = r5._gdf(range(len(dl)), dl[:, 0], dl[:, 1])
    ttc = jpype.JClass("com.conveyal.r5.analyst.TravelTimeComputer")

    def run(task) -> np.ndarray:
        req = copy.copy(task)
        req.origin = shapely.Point(*origin)
        v = np.asarray(ttc(req, tn).computeTravelTimes().travelTimes.getValues()[0], dtype=float)
        v[v >= 2**31 - 1] = np.inf
        return v

    walk = m.RegionalTask(tn, origin=None, destinations=gdf, departure=departure,
                          departure_time_window=timedelta(minutes=1), percentiles=[50],
                          transport_modes=[m.TransportMode.WALK], access_modes=[m.TransportMode.WALK],
                          egress_modes=[m.TransportMode.WALK],
                          max_time=timedelta(minutes=cfg["max_time_walking_min"]),
                          max_time_walking=timedelta(minutes=cfg["max_time_walking_min"]),
                          speed_walking=speed)
    base = m.RegionalTask(tn, origin=None, destinations=gdf, departure=departure,
                          departure_time_window=timedelta(minutes=cfg["departure_time_window_min"]),
                          percentiles=list(cfg["percentiles"]),
                          transport_modes=[m.TransportMode.TRANSIT], access_modes=[m.TransportMode.WALK],
                          egress_modes=[m.TransportMode.WALK],
                          max_time=timedelta(minutes=cfg["max_time_min"]),
                          max_time_walking=timedelta(minutes=cfg["max_time_walking_min"]),
                          speed_walking=speed, max_public_transport_rides=max(RIDE_LIMITS))
    W = run(walk)
    T = np.vstack([run(with_rides(base, k)) for k in RIDE_LIMITS])
    return W, T


def costed(W: np.ndarray, T: np.ndarray, ride_cost: float, change_cost: float,
           ride_limits=RIDE_LIMITS) -> np.ndarray:
    """min(W, min_k T_k + ride_cost * k + change_cost * (k - 1)) element-wise."""
    k = np.asarray(ride_limits, dtype=float)[:, None]
    return np.minimum(W, (T + ride_cost * k + change_cost * (k - 1)).min(axis=0))


def load_pairs(build, report_path) -> list[dict]:
    """Pairs with a finite TfL median from a gate report, with their window and coordinates."""
    from transit.build import load_points

    pts = load_points(build)
    lon, lat = pts["snap_lon"].to_numpy(float), pts["snap_lat"].to_numpy(float)
    rep = json.loads(report_path.read_text())
    out = []
    for w, d in rep["bands"].items():
        for p in d["pairs"]:
            if p.get("tfl") is None or not math.isfinite(p["tfl"]):
                continue
            out.append({"window": w, "origin": tuple(p["origin"]),
                        "dest": (float(lon[p["dest"]]), float(lat[p["dest"]])), "tfl": float(p["tfl"])})
    return out


def score(values: np.ndarray, tfl: np.ndarray, windows: list[str]) -> dict:
    """Band statistics per window and overall; `fit` is the worst ratio to a pass line."""
    cells, worst = {}, 0.0
    for w in sorted(set(windows)):
        for band in settings.BANDS:
            sel = [i for i, (wi, f) in enumerate(zip(windows, tfl)) if wi == w and band_of(f) == band]
            errs = [abs(values[i] - tfl[i]) if math.isfinite(values[i]) else math.inf for i in sel]
            st = error_stats(errs)
            st["median_signed"] = float(np.median([values[i] - tfl[i] for i in sel
                                                   if math.isfinite(values[i])])) if sel else math.nan
            line = settings.BAND_LINES[band]
            st["passes"] = st["n"] > 0 and st["median"] <= line[0] and st["p90"] <= line[1]
            cells[f"{w}/{band}"] = st
            if st["n"]:
                worst = max(worst, st["median"] / line[0], st["p90"] / line[1])
    return {"cells": cells, "fit": worst,
            "cells_passing": sum(1 for c in cells.values() if c["passes"])}


def main(argv=None) -> int:
    from transit.build import Build, network

    p = argparse.ArgumentParser(prog="python -m transit.calibrate", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--build", required=True)
    p.add_argument("--report", default="gate/a-provisional/report.json")
    p.add_argument("--threads", type=int, default=settings.MATRIX_THREADS)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    b = Build(args.build)
    pairs = load_pairs(b, b.out / args.report)
    tn = network(b)
    tfl = np.array([q["tfl"] for q in pairs])
    windows = [q["window"] for q in pairs]
    from transit.r5 import station_access

    started = time.monotonic()
    raw = {}
    for access_m in STATION_ACCESS_M:
        station_access(tn, access_m)
        for speed in SPEEDS:
            def one(q, speed=speed):
                return times_from(tn, q["origin"], [q["dest"]], b.window_start(q["window"]), speed)

            with cf.ThreadPoolExecutor(max_workers=args.threads) as pool:
                res = list(pool.map(one, pairs))
            raw[(access_m, speed)] = (np.array([r[0][0] for r in res]),
                                      np.stack([r[1][:, 0] for r in res], axis=1))
            log.info("station access %d m, %.1f km/h: %d pairs in %.0f s", access_m, speed, len(pairs),
                     time.monotonic() - started)
    station_access(tn, 0)

    results = []
    for (access_m, speed), rc, cc in itertools.product(raw, RIDE_COSTS, CHANGE_COSTS):
        W, T = raw[(access_m, speed)]
        s = score(costed(W, T, rc, cc), tfl, windows)
        results.append({"station_access_m": access_m, "speed_walking_kmh": speed, "ride_cost_min": rc,
                        "change_cost_min": cc, "fit": s["fit"], "cells_passing": s["cells_passing"],
                        "cells": s["cells"]})
    results.sort(key=lambda r: (r["fit"], -r["cells_passing"]))
    base = next(r for r in results if r["station_access_m"] == 0 and r["speed_walking_kmh"] == 4.0
                and r["ride_cost_min"] == 0 and r["change_cost_min"] == 0)
    out = {"report": args.report, "pairs": len(pairs), "ride_limits": list(RIDE_LIMITS),
           "current": base, "best": results[:10],
           "note": "fitted and scored on the same pairs: a fresh sample would confirm"}
    (b.out / "calibrate.json").write_text(json.dumps(out, indent=2, default=str))
    lines = [f"# Calibration on {args.report} ({len(pairs)} pairs, cached TfL replies)", "",
             "Fit = the worst ratio of a cell's median or p90 to its pass line (<= 1 passes).", "",
             "| Station access (m) | Walk km/h | Per ride | Per change | Fit | Cells passing (of 12) |",
             "|---|---|---|---|---|---|"]
    for r in [base] + results[:12]:
        lines.append(f"| {r['station_access_m']} | {r['speed_walking_kmh']} | {r['ride_cost_min']} | "
                     f"{r['change_cost_min']} | {r['fit']:.2f} | {r['cells_passing']} |")
    best = results[0]
    lines += ["", f"## Best: +{best['station_access_m']} m station access, {best['speed_walking_kmh']} km/h, "
              f"+{best['ride_cost_min']} min a ride, +{best['change_cost_min']} min a change", "",
              "| Cell | n | median / p90 | signed | passes |", "|---|---|---|---|---|"]
    for k, c in best["cells"].items():
        lines.append(f"| {k} | {c['n']} | {c['median']:.1f} / {c['p90']:.1f} | {c['median_signed']:+.1f} | "
                     f"{'yes' if c['passes'] else 'no'} |")
    (b.out / "calibrate.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
