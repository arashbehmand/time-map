"""
The full London time map (#6, L-02): python -m transit.layout.timemap --build <id> [--window wd13]

A spring grid that can't fold. Every station and 300 points between them are joined by a
triangle mesh of soft springs; stations a train serves one after the other are joined by stiff
springs whose rest length is the scaled travel time between them; and every spring and triangle
resists collapse without limit, so no triangle can turn inside out. A slider moves from the real
map to the time map by making the train springs stronger against the grid (springs.py says how,
mesh.py why the map stays one to one). The Thames, parks, rail lines and borough outlines are
cut along the mesh and move with it (land.py), and the app's Whole city page (static/whole-city/,
#28) draws them with W-01's renderer.

Reads, like the stations-only job, $TRANSIT_OUT/<build>/ (points.csv, point_ok.npy,
tt_<window>.npy, its fingerprint, manifest.json), and from $TRANSIT_DATA/work/<build>/ the
prepared rail feed gbt.gtfs.zip (which stations trains join) and the OSM clip london.osm.pbf (the
land), each checked against the manifest's sha256. Writes to
<repo>/transit/out/timemap/<build>/<window>/ (git-ignored):

  timemap.json, land.bin                             what the page draws
  report.md, report.json, shepard.svg, panels.svg   the report, beside the stations-only layout's

The app serves them from TIMEMAP_DIR=transit/out/timemap/<build>, only while the tables they were
made from may be served (server/transit/whole_city.py). The page itself is the app's code, so the
size check counts it with the data: what a visitor downloads.

It exits 2, having written nothing, when an input, the checks file, the output folder, a solve,
the no-flip check on what would ship, or the page's size is refused. Pass and fail of the checks
are in the report, not the exit code.
"""
from __future__ import annotations

import argparse
import gzip
import heapq
import itertools
import math
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np

from transit import settings
from transit.layout import job as l01_job
from transit.layout import links as linkmod
from transit.layout import measures, springs
from transit.layout import mesh as meshmod
from transit.layout.frame import to_local
from transit.layout.prep import (
    BuildInput,
    Refused,
    RefusedInput,
    RefusedSolve,
    load_build,
    load_mode_table,
    sha256,
    station_pairs,
)
from transit.layout.smacof import align_rigid, node_stress, sigma1

__all__ = ["OUTPUT_FILES", "Refused", "default_out", "main", "run"]

REPO = l01_job.REPO
# The page that draws the output: the app's Whole city tool, and the shared modules it loads.
# Not written by the job; counted in the page's size.
PAGE = REPO / "static" / "whole-city"
PAGE_FILES = ("index.html", "app.js", "carry.js", "style.css")
SHARED = REPO / "static" / "shared"
SHARED_FILES = ("ui.css", "handoff.js", "offline.js", "tools.js", "transit.js")
RENDERER = SHARED / "mesh"
RENDERER_FILES = ("renderer.js", "caps.js", "warp.js", "field.js", "frame.js", "schema-streets-v8.js")
DATA_FILES = ("timemap.json", "land.bin")
REPORT_FILES = ("report.json", "report.md", "shepard.svg", "panels.svg")
OUTPUT_FILES = (*DATA_FILES, *REPORT_FILES)
# The travel modes (#23), with --modes car bus walk (any of them): served like the map, each
# combination fetched the first time it is switched on. Train and tube is always a mode; train alone
# is the time map itself (timemap.json), every other combination of the modes a file of its own.
MODES_DIR = "modes"
MODE_IDS = ("bus", "car", "train", "walk")
EXTRA_MODES = ("bus", "car", "walk")          # what --modes takes


def combo_id(modes) -> str:
    return "-".join(sorted(modes))


def combos_of(chosen) -> list[tuple[str, ...]]:
    """Every combination of train and the chosen modes but train alone, by size, then by name."""
    base = sorted({"train", *chosen})
    return [c for r in range(1, len(base) + 1) for c in itertools.combinations(base, r) if c != ("train",)]


def combo_file(cid: str) -> str:
    return f"combo-{cid}.json"


MODES_FIXED = ("modes.json", "roads.bin", "report.md", "report.json")
# Every file a run with any modes may write: what the output checks probe, and what a run removes
# when it doesn't write it (a run without the car leaves no roads.bin behind, and so on).
MODES_FILES = tuple(f"{MODES_DIR}/{n}" for n in (*MODES_FIXED, *(combo_file(combo_id(c))
                                                                 for c in combos_of(EXTRA_MODES))))
MAX_GZIP_BYTES = 2_000_000
SEGMENT_STEP_M = 100.0
RESTART_SEEDS = (1, 2)        # the restart check: the sweep again from the real map nudged by
RESTART_JITTER_M = 0.5        # up to this much on every free node, once per seed
RESTART_FLOOR_M = 0.01        # a nudged run of a combination that is the real map throughout, stopped
                              # this close to the real map, has returned (restart_check)


def default_out(build: str, window: str) -> Path:
    return REPO / "transit" / "out" / "timemap" / build / window


def check_out_dir(out: Path, modes: bool = False) -> None:
    l01_job.check_out_dir(Path(out), OUTPUT_FILES + (MODES_FILES if modes else ()))


# ── inputs ──────────────────────────────────────────────────────────
def load_checks(path: Path) -> tuple[dict, str]:
    """L-01's checks file, plus its [[ratio]] and [[required_link]] tables."""
    checks, sha = l01_job.load_checks(path)
    ratios, req = checks.get("ratio", []), checks.get("required_link", [])
    ok = (isinstance(ratios, list) and all(isinstance(r, dict) and all(isinstance(r.get(k), str)
                                                                         for k in ("name", "station", "reference"))
                                           for r in ratios)
          and isinstance(req, list) and all(isinstance(r, dict) and isinstance(r.get("a"), str)
                                             and isinstance(r.get("b"), str) for r in req))
    if not ok:
        raise RefusedInput(f"checks file {path}: [[ratio]] needs name, station and reference; "
                           "[[required_link]] needs a and b")
    spots = checks.get("car_spot", [])          # pairs faster by car (#23, L-03): the travel modes' spot check
    if not (isinstance(spots, list) and all(isinstance(r, dict) and all(isinstance(r.get(k), str)
                                                                         for k in ("name", "a", "b")) for r in spots)):
        raise RefusedInput(f"checks file {path}: [[car_spot]] needs name, a and b")
    return checks, sha


def ratio_list(checks: dict) -> list[dict]:
    """Every station pair judged by L-01's ratio rule: Wembley Park first."""
    wem = checks.get("wembley", {})
    out = []
    if wem.get("station") and wem.get("reference"):
        out.append({"key": "wembley_park", "name": "Wembley Park to Baker Street", "station": wem["station"],
                    "reference": wem["reference"]})
    for k, r in enumerate(checks.get("ratio", [])):
        out.append({"key": f"ratio_{k + 1}", "name": r["name"], "station": r["station"], "reference": r["reference"]})
    return out


def _prepared_sha(manifest: dict, key: str) -> str | None:
    inputs = manifest.get("inputs", {})
    if key == "gbt":
        return (inputs.get("prepared", {}).get("gbt") or {}).get("sha256")
    return (inputs.get("osm_clip") or {}).get("sha256")


def find_inputs(b: BuildInput, data: Path, land: bool) -> tuple[Path, Path | None, dict]:
    """The prepared rail feed and the OSM clip of build b under data/work/<build>/, checked
    against b's manifest."""
    work = Path(data) / "work" / b.dir.name
    feed = work / "gbt.gtfs.zip"
    found = {}
    for key, path, need in (("gbt", feed, True), ("osm_clip", work / "london.osm.pbf", land)):
        if not need:
            continue
        want = _prepared_sha(b.manifest, key)
        if not path.is_file():
            raise RefusedInput(f"{path} is missing (TRANSIT_DATA points at {data})")
        if not want:
            raise RefusedInput(f"manifest.json records no sha256 for {path.name}")
        got = sha256(path)
        if got != want:
            raise RefusedInput(f"{path.name} is not the file manifest.json describes (sha256 differs)")
        found[key] = got
    return feed, (work / "london.osm.pbf") if land else None, found


def bus_feed_of(b: BuildInput, data: Path) -> Path:
    """The prepared bus feed of build b (data/work/<build>/bods.gtfs.zip), checked against b's
    manifest."""
    path = Path(data) / "work" / b.dir.name / "bods.gtfs.zip"
    want = (b.manifest.get("inputs", {}).get("prepared", {}).get("bods") or {}).get("sha256")
    if not path.is_file():
        raise RefusedInput(f"{path} is missing (TRANSIT_DATA points at {data})")
    if not want:
        raise RefusedInput(f"manifest.json records no sha256 for {path.name}")
    if sha256(path) != want:
        raise RefusedInput(f"{path.name} is not the file manifest.json describes (sha256 differs)")
    return path


def departure(b: BuildInput) -> datetime:
    dep = b.departure
    try:
        return datetime.fromisoformat(str(dep))
    except ValueError:
        raise RefusedInput(f"manifest.json gives tt_{b.window} no departure time ({dep!r})") from None


# ── per-position measures ───────────────────────────────────────────
def _shortest_chain(n: int, links: np.ndarray, length: np.ndarray, a: int, b: int) -> tuple[float, list[int]]:
    adj = [[] for _ in range(n)]
    for (i, j), w in zip(links.tolist(), length.tolist()):
        adj[i].append((j, w))
        adj[j].append((i, w))
    dist = [math.inf] * n
    prev = [-1] * n
    dist[a] = 0.0
    q = [(0.0, a)]
    while q:
        d, i = heapq.heappop(q)
        if d > dist[i]:
            continue
        if i == b:
            break
        for j, w in adj[i]:
            if d + w < dist[j]:
                dist[j], prev[j] = d + w, i
                heapq.heappush(q, (dist[j], j))
    if not math.isfinite(dist[b]):
        return math.inf, []
    path = [b]
    while path[-1] != a:
        path.append(prev[path[-1]])
    return dist[b], path[::-1]


def _ratios(pairs, st, checks_list, sig, strict):
    out = {}
    for r in checks_list:
        c = measures.ratio_check(pairs, st, r["station"], r["reference"], sig)
        if "deviation" in c and strict is not None:
            c["strict_tolerance"] = strict
            c["strict_status"] = "pass" if abs(c["deviation"]) <= strict else "fail"
        out[r["key"]] = {**c, "name": r["name"], "key": r["key"]}
    return out


def feed_prefix(pairs) -> str:
    """The prefix points.csv gives the layout's stations ("gbt" in "gbt:910GLONDNBS"): the feed whose
    stop ids the train links are read with."""
    prefixes = sorted({mid.split(":", 1)[0] for ids in pairs.member_ids for mid in ids if ":" in mid})
    if len(prefixes) != 1:
        raise RefusedInput(f"the layout's stations come from {len(prefixes)} feeds ({', '.join(prefixes) or 'none'}); "
                           "the train links are read from one")
    return prefixes[0]


def required_links(pairs, lk, checks) -> None:
    """Every [[required_link]] must name two stations of this layout and be a train link: a missing
    one means stops were joined to stations wrongly (or the checks file names the wrong station)."""
    missing = []
    for r in checks.get("required_link", []):
        i, j = pairs.index_of(r["a"]), pairs.index_of(r["b"])
        absent = [sid for sid, v in ((r["a"], i), (r["b"], j)) if v is None]
        if absent:
            raise RefusedInput(f"[[required_link]] {r['a']} – {r['b']}: {', '.join(absent)} is not a station of this "
                               "build's layout")
        if not lk.has(i, j):
            missing.append(f"{pairs.labels[i]}–{pairs.labels[j]}")
    if missing:
        raise RefusedInput(f"the feed gives no train link {', '.join(missing)}: stops are joined to stations wrongly")


def l01_recompute(pairs, checks, tc, log) -> dict:
    """The stations-only layout (L-01) solved again here from the same pairs, so both reports
    come from the same tables and code, and the [[ratio]] rows can be scored on it too. A solve
    L-01's own job would refuse (not converged, or the objective rose) stops this job as well."""
    from transit.layout.smacof import sweep as l01_sweep

    A = pairs.xy
    tri_full = measures.delaunay(A)
    tri, _ = measures.peel(A, tri_full)
    solves = l01_sweep(pairs)
    bad = [f["lam_tilde"] for f in solves if f["rose"] or not f["converged"] or not f["monotone"]]
    if bad:
        raise RefusedSolve(f"the stations-only layout, solved again for the comparison, did not converge or raised its "
                           f"objective at λ̃ = {', '.join(f'{v:.3g}' for v in bad)}; nothing was written")
    frames = [{"lam_tilde": None, "X": A, "converged": True}] + solves
    rl = ratio_list(checks)
    out = []
    for f in frames:
        m, _, _ = l01_job._frame_metrics(pairs, f["X"], A, tri, tri_full, tc, checks)
        m["ratios"] = _ratios(pairs, f["X"], rl, m["sigma1"], None)
        out.append({"lam_tilde": f["lam_tilde"], "converged": bool(f.get("converged", True)), "metrics": m})
    log(f"L-01 solved again: {len(solves)} values of λ")
    return {"frames": out, "time_map_X": frames[-1]["X"], "triangles": int(len(tri)),
            "triangles_full": int(len(tri_full))}


def _wembley_pick(frames) -> tuple[list[int], int, list[float]] | None:
    res = [f["metrics"]["ratios"].get("wembley_park", {}) for f in frames]
    if not all("deviation" in r for r in res):
        return None
    passes = [k for k, r in enumerate(res) if r["status"] == "pass"]
    gap = [max(0.0, abs(r["deviation"]) - r["tolerance"]) for r in res]
    return passes, (passes[0] if passes else int(np.argmin(gap))), gap


def restart_check(m, mesh, pairs, rhos, frames, picks, checks, tol, max_iter, log) -> dict:
    """Whether the positions in `picks` depend on how the continuation got there. Two kinds of run:
    the same sweep from the real map nudged by RESTART_JITTER_M on every free node (seeded), which
    tests the start; and the sweep through other ladders of ρ (every other step, and straight from
    the real map in one step), which tests the step size. Each run is compared with the shipped
    layout on every node. It never stops the job."""
    runs = checks.get("metropolitan", [])
    wem = checks.get("wembley", {})
    free = ~m.fixed                      # the mesh's free nodes, and the car's twins when it has them
    inner = mesh.kinds != meshmod.RING
    out = {"jitter_m": RESTART_JITTER_M, "seeds": list(RESTART_SEEDS), "ladders": [], "complete": True, "at": {},
           "floor": []}
    rows = {k: [] for k in picks}
    # A combination that is the real map at every shipped position (no spring pulls: walking or bus
    # alone, #23) has one lowest-energy layout, the real map. From a nudged start the solver gets
    # back to within a fraction of a millimetre of it and then stops: there the energy is at its
    # floating-point floor (it even reads below zero), so no step can show a gain. Such a run goes on
    # position by position from where it stopped (_floor_run) and is scored on what each solve gives.
    quiet = all(np.array_equal(f["dm"], frames[0]["dm"]) for f in frames)

    def measure(k, X, kind, name):
        dm = springs.quantise(X[:m.n_mesh] * m.scale)
        shift = np.hypot(*((dm - frames[k]["dm"]) / 10.0).T)
        shift[~inner] = 0
        far = int(np.argmax(shift))
        Xs = dm[:pairs.n] / 10.0
        sig = sigma1(Xs, pairs)
        w = measures.ratio_check(pairs, Xs, wem.get("station", ""), wem.get("reference", ""), sig)
        rows[k].append({"kind": kind, "name": name, "sigma1": sig, "wembley_km": w.get("map_m", 0.0) / 1000,
                        "wembley_ratio": w.get("ratio_to_median"), "wembley_status": w.get("status"),
                        "met_breaks": l01_job.report.count_breaks(measures.line_order(pairs, Xs, runs,
                                                                                      A=frames[0]["Xs"][:pairs.n])),
                        "shift_max_m": float(shift.max()),
                        "shift_at": pairs.labels[far] if far < pairs.n and shift.max() > 0 else None})

    for seed in RESTART_SEEDS:
        jitter = RESTART_JITTER_M
        rng = np.random.default_rng(seed)
        nudge = rng.uniform(-1, 1, (int(free.sum()), 2))
        while True:
            X0 = m.P.copy()
            X0[free] += nudge * jitter / m.scale
            if (measures.signed_areas(X0, m.tri) > 0).all():
                break
            jitter /= 2
        res = springs.sweep(m, rhos, tol=tol, max_iter=max_iter, X0=X0)
        if len(res) < len(frames) or not all(f["converged"] for f in res):
            floor = _floor_run(m, rhos, res, tol, max_iter) if quiet else None
            if floor is None:
                out["complete"] = False
                continue
            out["floor"].append({"seed": seed, "stopped_at_rho": res[-1]["rho"], "gmax_max": floor["gmax_max"],
                                 "from_real_m": floor["from_real_m"]})
            for k in picks:
                measure(k, floor["X"][k], "nudged", f"seed {seed}, up to {jitter:g} m (each position solved from the "
                        f"last; stopped at most {floor['from_real_m'] * 1000:.1f} mm from the real map, at the "
                        "solver's rounding floor)")
            continue
        for k in picks:
            measure(k, res[k]["X"], "nudged", f"seed {seed}, up to {jitter:g} m")
    # Other steps: every other ρ (keeping the picks), and each pick straight from the real map.
    pick_rhos = {frames[k]["rho"]: k for k in picks}
    ladders = [("every other step", sorted({0.0, *rhos[2::2], *pick_rhos}))]
    ladders += [(f"one step to {_rho_short(r)}", [0.0, r]) for r in pick_rhos if r > 0]
    for name, lad in ladders:
        res = springs.sweep(m, lad, tol=tol, max_iter=max_iter, smooth=False)
        out["ladders"].append(name)
        if len(res) < len(lad) or not all(f["converged"] for f in res):
            out["complete"] = False
            continue
        for f in res:
            if f["rho"] in pick_rhos:
                measure(pick_rhos[f["rho"]], f["X"], "ladder", name)
    for k in picks:
        km = [frames[k]["metrics"]["ratios"].get("wembley_park", {}).get("map_m", 0.0) / 1000] + \
             [r["wembley_km"] for r in rows[k]]
        worst = max(rows[k], key=lambda r: r["shift_max_m"], default=None)
        out["at"][k] = {"rho": frames[k]["rho"], "runs": rows[k], "wembley_km_spread": max(km) - min(km),
                        "statuses": sorted({r["wembley_status"] for r in rows[k]}
                                           | {frames[k]["metrics"]["ratios"].get("wembley_park", {}).get("status")}
                                           - {None}),
                        "shift_max_m": worst["shift_max_m"] if worst else 0.0,
                        "shift_at": worst["shift_at"] if worst else None, "shift_run": worst["name"] if worst else None}
    log("restart check: " + "; ".join(f"ρ = {v['rho']:.3g}: no node more than {v['shift_max_m']:.1f} m from the "
                                      "shipped layout" for v in out["at"].values()))
    return out


def _floor_run(m, rhos, res, tol, max_iter) -> dict | None:
    """A nudged sweep of a combination that is the real map at every shipped position stopped short
    (res: its frames up to the one that didn't converge). Carry on from where it stopped, solving at
    each position of the ladder in turn from the last, and accept the run only if every position
    either converges or stops within RESTART_FLOOR_M of the real map on every row (the car's twins
    too) without running out of iterations: the solver's rounding floor, not another layout.
    Returns {X: {position: layout}, from_real_m, gmax_max}, or None."""
    X = res[-1]["X"] if res else None
    if X is None:
        return None
    got, worst, gmax = {}, 0.0, 0.0
    for k, r in enumerate(rhos):
        if k < len(res) - 1 and res[k]["converged"]:
            got[k] = res[k]["X"]
            continue
        s = springs.solve(m, X, r, tol=tol, max_iter=max_iter)
        X = s.X
        dev = float(np.abs(X - m.P).max()) * m.scale
        if not math.isfinite(dev) or (not s.converged and (dev >= RESTART_FLOOR_M or s.iterations >= max_iter)):
            return None
        got[k], worst, gmax = X, max(worst, dev), max(gmax, s.gmax)
    return {"X": got, "from_real_m": worst, "gmax_max": gmax}


def _rho_short(r: float) -> str:
    return f"ρ = {r:.3g}"


# ── the job ─────────────────────────────────────────────────────────
def run(b: BuildInput, feed: Path, clip: Path | None, checks: dict, out: Path, *, rhos=springs.RHOS,
        tol: float = springs.TOL, max_iter: int = springs.MAX_ITER, log=print, checks_sha256: str | None = None,
        input_sha: dict | None = None, land_work: Path | None = None, restarts: bool = True,
        car: tuple | None = None, modes=(), bus_feed: Path | None = None, tables: dict | None = None) -> dict:
    """The job. With travel modes (#23), every combination of train and `modes` too, written to
    modes/: the car from `car` (car_links.json, roads.json and car_report.json, transit/layout/car.py),
    bus from `bus_feed` (the prepared bus feed) and the bus and walking tables in `tables`
    ({kind: (table, sha256)}, prep.load_mode_table), walking from the walking table."""
    from transit.layout import modelinks
    from transit.layout import timemap_report as rep

    t0 = time.monotonic()
    chosen = set(modes) | ({"car"} if car is not None else set())
    if chosen - set(EXTRA_MODES):
        raise RefusedInput(f"no travel mode {', '.join(sorted(chosen - set(EXTRA_MODES)))}")
    if chosen & {"bus", "walk"}:
        if b.window != settings.CAR_WINDOW:
            raise RefusedInput(f"bus and walking times exist for weekday 13:00-14:00 only: build them for --window "
                               f"{settings.CAR_WINDOW}, not {b.window}")
        need = {"walk"} | ({"bus"} if "bus" in chosen else set())
        if not tables or need - set(tables):
            raise RefusedInput(f"{' and '.join(sorted(need))} need their tables (prep.load_mode_table)")
        if "bus" in chosen and bus_feed is None:
            raise RefusedInput("bus needs the build's prepared bus feed (bods.gtfs.zip)")
    if "car" in chosen and car is None:
        raise RefusedInput("car needs the car job's files (python -m transit.layout.car)")
    pairs = station_pairs(b)
    k = measures.TC_K
    if pairs.n <= 3 * k + 1:
        raise RefusedInput(f"too few nodes ({pairs.n}) for neighbourhoods of {k}: it needs more than {3 * k + 1}")
    mesh, cells, pick = mesh_of(b, pairs)
    if isinstance(car, (str, Path)):             # the car job's folder: refused unless made on this mesh
        from transit.layout import car as carmod
        carmod.check_window(b.window)
        car = carmod.load(b.manifest.get("build"), b.window, mesh, Path(car), table_sha256=b.table_sha256)
    log(f"{pairs.n} station nodes, {mesh.n_cell} cells, {mesh.n_ring} guard nodes: {len(mesh.tri)} triangles, "
        f"{len(mesh.edges)} grid springs")

    dep = departure(b)
    names = modelinks.node_names(mesh, pairs)
    plain = {}
    if chosen & {"bus", "walk"}:
        members = modelinks.node_members(mesh, pairs, cells, pick, b.point_ok)
        if "walk" in chosen:
            plain["walk"] = modelinks.walk_links(mesh, members, tables["walk"][0], pairs.s, names)
            c = plain["walk"].counts
            log(f"walking: {c['links']} springs on {c['edges']} mesh edges between stations and cells "
                f"({c['left_out_count']} left out)")
        if "bus" in chosen:
            try:
                plain["bus"] = modelinks.bus_links(bus_feed, dep.date(), dep.time(), mesh, members, tables["bus"][0],
                                                   tables["walk"][0], pairs.s, names)
            except (linkmod.LinkError, OSError, KeyError, IndexError) as e:
                raise RefusedInput(f"the bus links can't be read from {Path(bus_feed).name}: {e}") from None
            c = plain["bus"].counts
            log(f"bus: {c['links']} springs from {c['candidates']} pairs of nodes consecutive on {c['trips_with_a_hop']} "
                f"bus trips ({c['left_out_count']} left out: " + ", ".join(f"{v} {w}" for w, v in
                                                                          c["links_dropped"].items()) + ")")
    try:
        lk = linkmod.train_links(feed, dep.date(), dep.time(), pairs, feed_id=feed_prefix(pairs))
    except (linkmod.LinkError, OSError, KeyError) as e:
        raise RefusedInput(f"the train links can't be read from {Path(feed).name}: {e}") from None
    required_links(pairs, lk, checks)
    log(f"{len(lk.pairs)} train links ({lk.counts['station_trips_on_day']} station trips on {dep.date()})")

    m = springs.model(mesh, lk.pairs, lk.rest_m)
    frames, motion = solve_combo(m, mesh, pairs, rhos, tol, max_iter, log)

    n_st = pairs.n
    A = frames[0]["Xs"][:n_st]
    tc = measures.TrustContinuity(pairs.t, pairs.used, k=k)
    l01 = l01_recompute(pairs, checks, tc, log)
    ctx = measure_context(pairs, mesh, checks, tc, l01, A)
    lines_at = measure_frames(frames, m, mesh, pairs, lk, ctx)

    wembley = wembley_analysis(pairs, mesh, lk, frames, checks, l01)
    picks = sorted({p for p in (wembley.get("pick"), len(frames) - 1) if p is not None})
    restart = restart_check(m, mesh, pairs, rhos, frames, picks, checks, tol, max_iter, log) if restarts else None
    runs = checks.get("metropolitan", [])
    routes = [{"name": r["name"], "nodes": nodes, **dict(zip(("c", "w"), meshmod.carry_route(mesh, nodes)))}
              for r in runs for nodes in [[i for i in (pairs.index_of(s) for s in r["stations"]) if i is not None]]
              if len(nodes) >= 2]

    modes_doc = None
    if chosen:
        sources = {k: {"table_sha256": v[1]} for k, v in (tables or {}).items()}
        if "bus" in chosen:
            sources["bus"]["feed_sha256"] = sha256(Path(bus_feed))
        if "walk" in chosen:
            r5 = (b.manifest.get("settings") or {}).get("r5") or {}
            sources["walk"]["speed_kmh"] = float(r5.get("speed_walking_kmh", settings.R5["speed_walking_kmh"]))
        modes_doc = solve_modes(chosen, mesh, pairs, lk, m, frames, motion, wembley, checks, ctx, rhos, tol, max_iter,
                                restarts, log, car=car, plain=plain, names=names, train_restart=restart,
                                tables=sources, departure=b.departure)
    land_doc = None
    if clip is not None:
        land_doc = carry_land(clip, mesh, land_work, log)
    result = rep.write(out, b=b, pairs=pairs, mesh=mesh, links=lk, model=m, frames=frames, cells=cells, pick=pick,
                       checks=checks, lines_at=lines_at, l01=l01, wembley=wembley, land=land_doc, routes=routes,
                       restart=restart, motion=motion, code=l01_job.code_version(), checks_sha256=checks_sha256,
                       input_sha=input_sha or {}, expired=l01_job.expired(b.manifest.get("valid_until")),
                       started=t0, log=log, modes=modes_doc)
    for line in result["summary"]:
        log(line)
    return result


def solve_combo(m, mesh, pairs, rhos, tol, max_iter, log, name: str | None = None) -> tuple[list[dict], dict]:
    """One combination of travel modes solved over the ρ ladder from the real map, refused unless
    every position converges and every blend the page draws holds: the shipped positions (whole
    decimetres, checked as shipped and in Float32) and the keyframes between them."""
    what = f"{name}: " if name else ""
    frames = springs.sweep(m, rhos, tol=tol, max_iter=max_iter, log=log)
    # The sweep stops at the first step that doesn't converge or still folds; both refuse. A step
    # that failed on its way (at an in-between ρ) is named by the ρ it reached.
    bad = [f for f in frames if not (f["converged"] and f["monotone"])]
    if bad:
        where = ", ".join(f"{f['reached']:.3g}" + (f" (on the way to {f['rho']:.3g})" if f["reached"] != f["rho"] else "")
                          for f in bad)
        raise RefusedSolve(f"{what}the solve at ρ = {where} did not converge (cap {max_iter} iterations, "
                           f"{springs.SUBDIVIDE} levels of sub-steps); nothing was written")
    folding = [f["rho"] for f in frames if not f["smooth_ok"]]
    if folding:
        raise RefusedSolve(f"{what}the step to ρ = {folding[0]:.3g} still folds part way after {springs.MOTION_DEPTH} "
                           "levels of in-between positions; nothing was written")
    # What ships: whole decimetres. The no-flip check runs on exactly that, and on its Float32 image.
    for f in frames:
        ship(f, m, mesh)
    return frames, motion_keyframes(m, mesh, frames, pairs.labels, log)


def measure_context(pairs, mesh, checks, tc, l01, A) -> dict:
    """What every combination's measures share."""
    tri_l01_full = measures.delaunay(pairs.xy)
    tri_l01, _ = measures.peel(pairs.xy, tri_l01_full)
    inner = ~(mesh.kinds[mesh.tri] == meshmod.RING).any(axis=1)
    return {"tc": tc, "strict": l01["frames"][-1]["metrics"]["sigma1"], "rl": ratio_list(checks),
            "runs": checks.get("metropolitan", []), "tri_l01": tri_l01, "tri_l01_full": tri_l01_full,
            "inner": inner, "A": A}


def measure_frames(frames, m, mesh, pairs, lk, ctx) -> dict:
    """Each position's measures (frame["metrics"], frame["stress"]); returns the Metropolitan line's
    order at each. `lk` is the train links (None when the combination has no trains)."""
    n_st = pairs.n
    A, tc, inner = ctx["A"], ctx["tc"], ctx["inner"]
    A0_in = mesh.A0[inner]
    lines_at = {}
    prev = None
    for i, f in enumerate(frames):
        X = f["Xs"]
        st = X[:n_st]
        sig = sigma1(st, pairs)
        r = meshmod.area_ratios(mesh, X)[inner]
        stretch = meshmod.stretch(mesh, X)
        st_in = stretch[inner]
        lines_at[i] = measures.line_order(pairs, st, ctx["runs"], A=A)
        if lk is not None and len(lk.pairs):
            lens = np.hypot(*(X[lk.pairs[:, 0]] - X[lk.pairs[:, 1]]).T)
            past = np.maximum(lens / lk.rest_m - 1, 0)                     # how far past s·t
        else:
            past = np.zeros(1)
        shift = np.hypot(*(st - A).T)
        moved = np.hypot(*(X - prev).T)[mesh.kinds != meshmod.RING] if prev is not None else np.zeros(1)
        prev = X
        _, rot = align_rigid(st, A)
        Xe = f["X"].copy()                       # the energy on what ships, the twins where they were solved
        Xe[:m.n_mesh] = X / m.scale
        f["stress"] = node_stress(st, pairs)
        f["metrics"] = {
            "sigma1": sig, **tc(st),
            "ratios": _ratios(pairs, st, ctx["rl"], sig, ctx["strict"]),
            "met_breaks": l01_job.report.count_breaks(lines_at[i]),
            "folds": {"flipped": f["fold"]["flipped"], "triangles": int(len(mesh.tri)),
                      "area_share": float(A0_in[~(r > 0)].sum() / A0_in.sum())},
            "folds_l01": {"peeled": measures.fold_stats(ctx["tri_l01"], pairs.xy, st),
                          "full": measures.fold_stats(ctx["tri_l01_full"], pairs.xy, st)},
            "area_ratio_min": float(r.min()), "area_ratio_p1": float(np.percentile(r, 1)),
            "under_25pct": int((r < 0.25).sum()), "under_5pct": int((r < 0.05).sum()),
            "stretch_max": float(st_in.max()), "stretch_median": float(np.median(st_in)),
            "stretch_p90": float(np.percentile(st_in, 90)),
            "stretched_4_share": float(A0_in[st_in > 4].sum() / A0_in.sum()),
            "stretch_worst": meshmod.worst(mesh, stretch, pairs.labels, inner, largest=True),
            "squeezed": meshmod.squeezed(mesh, X, pairs.labels, mask=inner),
            "links_past_share": float((past > 0.01).mean()), "links_past_p90": float(np.percentile(past, 90)),
            "energy": springs.parts(m, Xe, f["rho"]),
            "shift_median_m": float(np.median(shift)), "shift_p90_m": float(np.percentile(shift, 90)),
            "move_median_m": float(np.median(moved)), "rotation_deg": float(rot["rotation_deg"]),
            "iterations": f["iterations"], "gmax": f["gmax"], "substeps": f["substeps"], "pd": bool(f["pd"]),
        }
    return lines_at


# ── travel modes (#23, L-03) ────────────────────────────────────────
PULL_PAST = 0.01             # a spring "pulls" when it is stretched more than this share past its rest


def car_springs(car_links: dict, s: float) -> tuple[springs.CarSprings, list[dict]]:
    """The car's road layer from car_links.json: twins on every linked node, a road spring per link
    at s·drive minutes, access springs at s·overhead/2."""
    links = car_links["links"]
    twins = np.array(car_links["twins"], dtype=np.int64)
    at = {int(v): k for k, v in enumerate(twins.tolist())}
    hops = np.array([[at[lk["a"]], at[lk["b"]]] for lk in links], dtype=np.int64).reshape(-1, 2)
    rest = np.array([s * float(lk["minutes"]) for lk in links])
    return springs.CarSprings(twins, hops, rest, s * float(car_links["overhead_min"]) / 2), links


def pulls(m, X: np.ndarray, labels: list[str], car_links: list[dict] | None, plain: dict | None = None) -> dict:
    """Per travel mode: how many of its springs pull at X (normalised, every row) and which pull
    hardest (train and walking: the pairs of places, `labels` naming every mesh node; car: the
    roads; bus: the routes, from `plain`'s bus links). `n` counts springs more than PULL_PAST past
    their rest length; `taut` every spring at or past it, which is every spring the solve feels.
    Bus and walking, which barely pull, name their taut springs; train and car their pulling ones."""
    from transit.layout.modelinks import by_route_pulls
    out = {}
    for f in m.families:
        if not len(f.links):
            continue
        d = X[f.links[:, 0]] - X[f.links[:, 1]]
        past = np.hypot(d[:, 0], d[:, 1]) / f.rest - 1
        on = past > PULL_PAST
        taut = past >= 0
        named = taut if f.name in ("bus", "walk") else on
        order = [int(i) for i in np.argsort(-past) if named[i]]
        rec = {"n": int(on.sum()), "of": int(len(past)), "share": float(on.mean()), "taut": int(taut.sum()),
               "past_max": float(past.max()), "past_p90": float(np.percentile(np.maximum(past, 0), 90))}
        if f.name in ("train", "walk"):
            rec["top"] = [f"{labels[int(f.links[i, 0])]} – {labels[int(f.links[i, 1])]}" for i in order[:3]]
        elif f.name == "bus" and plain and plain.get("bus") is not None:
            rec["top"] = by_route_pulls(plain["bus"], taut)
            rec["top_links"] = [f"{labels[int(f.links[i, 0])]} – {labels[int(f.links[i, 1])]}" for i in order[:3]]
        elif f.name == "car" and car_links is not None:
            # Where the car pulls: the roads with the most length of pulling links, not the few
            # most-stretched hops (often short minor roads).
            by_road: dict[str, float] = {}
            for i in order:
                for road in car_links[i]["roads"][:1]:
                    by_road[road] = by_road.get(road, 0.0) + float(car_links[i]["road_m"])
            rec["top"] = [r for r, _ in sorted(by_road.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
            rec["top_links"] = [f"{car_links[i]['a_name']} – {car_links[i]['b_name']}" for i in order[:3]]
        out[f.name] = rec
    return out


def cross_flags(mesh, frames_a: list[dict], frames_b: list[dict]) -> list[dict | None]:
    """Whether the straight blend between two combinations at each solved position keeps every
    triangle's area positive all the way (exactly, on whole decimetres: mesh.blend_min_area), and
    its smallest area ratio. Position 0 is the real map in both: no blend."""
    out = [None]
    for fa, fb in zip(frames_a[1:], frames_b[1:]):
        lo = float((meshmod.blend_min_area(mesh.tri, fa["dm"], fb["dm"]) / (200 * mesh.A0)).min())
        out.append({"rho": fa["rho"], "ok": bool(lo > 0), "min_area_ratio": lo})
    return out


def _car_minutes(n: int, car_links: list[dict], overhead: float) -> np.ndarray:
    """Door-to-door car minutes between every two nodes along the car links (overhead once a trip;
    inf where no road joins them)."""
    D = np.full((n, n), np.inf)
    adj = [[] for _ in range(n)]
    for lk in car_links:
        adj[lk["a"]].append((lk["b"], lk["minutes"]))
        adj[lk["b"]].append((lk["a"], lk["minutes"]))
    for s in sorted({x for lk in car_links for x in (lk["a"], lk["b"])}):
        dist = {s: 0.0}
        q = [(0.0, s)]
        while q:
            d, i = heapq.heappop(q)
            if d > dist.get(i, math.inf):
                continue
            for j, w in adj[i]:
                if d + w < dist.get(j, math.inf):
                    dist[j] = d + w
                    heapq.heappush(q, (d + w, j))
        for j, d in dist.items():
            D[s, j] = d + overhead if j != s else 0.0
    return D


SPOT_MIN_M = 3000.0          # the spot check's pair is at least this far apart on the real map


def spot_check(pairs, mesh, car_links: list[dict], overhead: float, combos: dict, picks: list[int],
               checks: dict) -> list[dict]:
    """Pairs faster by car than by train (the issue's spot check): the station pair at least
    SPOT_MIN_M apart with the largest table time minus car time, and every [[car_spot]] row of the
    checks file. Each with its real distance, its car bound (s × car minutes) and its map distance
    with train only, car only and car + train at `picks` (positions). Status: "pass" when the pair is
    faster by car and car + train ends closer than train only at the end, "fail" when faster by car
    and not closer, "not faster by car" (no claim either way), "no time" when the table or the roads
    give none, "absent". The control: how much car + train shrinks every station pair at least
    SPOT_MIN_M apart (the median ratio to train only), so a pair that only shrinks as much as the area
    around it reads as such."""
    n = pairs.n
    D = _car_minutes(mesh.n, car_links, overhead)[:n, :n]
    real = np.hypot(*(pairs.xy[:, None, :] - pairs.xy[None, :, :]).transpose(2, 0, 1))
    gain = np.where(pairs.used & np.isfinite(D) & (real >= SPOT_MIN_M), np.nan_to_num(pairs.t, nan=0.0) - D, -np.inf)
    rows = []
    if np.isfinite(gain).any() and gain.max() > 0:
        i, j = np.unravel_index(int(np.argmax(gain)), gain.shape)
        rows.append({"name": "Biggest gain by car", "a": int(min(i, j)), "b": int(max(i, j)), "kind": "auto"})
    for r in checks.get("car_spot", []):
        i, j = pairs.index_of(r["a"]), pairs.index_of(r["b"])
        if i is None or j is None:
            rows.append({"name": r["name"], "absent": [x for x, v in ((r["a"], i), (r["b"], j)) if v is None],
                         "kind": "checks"})
            continue
        rows.append({"name": r["name"], "a": i, "b": j, "kind": "checks"})
    end = max(picks)
    iu = np.triu_indices(n, 1)
    far = (real[iu] >= SPOT_MIN_M) & pairs.used[iu]

    def dist(cid, k):
        X = combos[cid][k]["Xs"][:n]
        return np.hypot(*(X[iu[0]] - X[iu[1]]).T)
    control = {k: float(np.median(dist("car-train", k)[far] / dist("train", k)[far])) if far.any() else None
               for k in picks}
    out = []
    for r in rows:
        if "absent" in r:
            out.append({**r, "status": "absent"})
            continue
        a, b = r["a"], r["b"]
        tt = float(pairs.t[a, b]) if pairs.used[a, b] else None
        cm = float(D[a, b]) if math.isfinite(D[a, b]) else None
        rec = {**r, "a_name": pairs.labels[a], "b_name": pairs.labels[b], "real_km": float(real[a, b]) / 1000,
               "train_table_min": tt, "car_min": cm, "car_bound_km": pairs.s * cm / 1000 if cm is not None else None,
               "train_bound_km": pairs.s * tt / 1000 if tt is not None else None, "at": {}, "control": control}
        for k in picks:
            rec["at"][k] = {cid: float(np.hypot(*(fr[k]["Xs"][a] - fr[k]["Xs"][b]))) / 1000
                            for cid, fr in combos.items()}
        closer = rec["at"][end]["car-train"] < rec["at"][end]["train"]
        rec["ratio_end"] = rec["at"][end]["car-train"] / rec["at"][end]["train"] if rec["at"][end]["train"] else None
        if tt is None or cm is None:
            rec["status"] = "no time"
        elif cm >= tt:
            rec["status"] = "not faster by car"
        else:
            rec["status"] = "pass" if closer else "fail"
        rec["closer_at"] = [k for k in picks if rec["at"][k]["car-train"] < rec["at"][k]["train"]]
        out.append(rec)
    return out


def combo_rows(mesh, pairs, combos: dict, k: int, names: list[str] | None = None) -> dict:
    """The issue's comparison at position k, per combination: node shift against the real map and
    against train only (every station and cell), σ₁ against the all-mode tables, Wembley Park, the
    thinnest triangle. `names`: every mesh node's (modelinks.node_names)."""
    from transit.layout.modelinks import node_names
    names = names or node_names(mesh, pairs)
    inner = mesh.kinds != meshmod.RING
    real = combos["train"][0]["Xs"]
    train = combos["train"][k]["Xs"]
    out = {}
    for cid, fr in combos.items():
        X = fr[k]["Xs"]
        mt = fr[k]["metrics"]
        s_real = np.hypot(*(X - real).T)[inner]
        s_train = np.hypot(*(X - train).T)[inner]
        far = int(np.where(inner)[0][int(np.argmax(s_real))])
        out[cid] = {"rho": fr[k]["rho"], "shift_real_median_m": float(np.median(s_real)),
                    "shift_real_max_m": float(s_real.max()),
                    "shift_real_max_at": names[far],
                    "shift_train_median_m": float(np.median(s_train)), "shift_train_max_m": float(s_train.max()),
                    "sigma1": mt["sigma1"], "wembley": mt["ratios"].get("wembley_park", {}),
                    "stretch_max": mt["stretch_max"], "stretch_worst": mt["stretch_worst"],
                    "area_ratio_min": mt["area_ratio_min"], "flipped": mt["folds"]["flipped"]}
    return out


def one_apart(a: str, b: str) -> bool:
    """Whether two combinations differ by exactly one mode (a toggle switches one into the other)."""
    return len(set(a.split("-")) ^ set(b.split("-"))) == 1


def solve_modes(chosen, mesh, pairs, lk, m_train, frames, motion, wembley, checks, ctx, rhos, tol, max_iter,
                restarts, log, *, car=None, plain=None, names=None, train_restart=None, tables=None,
                departure=None) -> dict:
    """Every combination of train and the chosen modes, beside train alone: each solved over the
    same ρ ladder, measured, its restart check run, and the straight blends between combinations
    that differ by one mode checked at every solved position. `plain`: the bus and walking links
    (modelinks), by mode."""
    plain = plain or {}
    names = names or list(pairs.labels)
    cs = links = car_links_doc = roads_doc = car_report = None
    if "car" in chosen:
        car_links_doc, roads_doc, car_report = car
        if abs(float(car_links_doc["s_m_per_min"]) - pairs.s) > 1e-6 * pairs.s:
            raise RefusedInput("the car links were built at another scale than this layout's: run "
                               "python -m transit.layout.car again")
        cs, links = car_springs(car_links_doc, pairs.s)
        log(f"car: {len(links)} road springs on {len(cs.twins)} twins, {car_links_doc['overhead_min']:g} min overhead "
            "once a trip")
    combos = {"train": frames}
    motions = {"train": motion}
    models = {"train": m_train}
    restart = {"train": train_restart} if train_restart else {}
    lines = {}
    for modes in combos_of(chosen):
        cid = combo_id(modes)
        extra = [(k, plain[k].pairs, plain[k].rest_m) for k in ("bus", "walk") if k in modes]
        mc = springs.model(mesh, lk.pairs if "train" in modes else [], lk.rest_m if "train" in modes else [],
                           car=cs if "car" in modes else None, extra=extra)
        log(f"── {cid} ──")
        fr, mo = solve_combo(mc, mesh, pairs, rhos, tol, max_iter, log, name=cid)
        lines[cid] = measure_frames(fr, mc, mesh, pairs, lk if "train" in modes else None, ctx)
        combos[cid], motions[cid], models[cid] = fr, mo, mc
    for cid, fr in combos.items():
        for f in fr:
            f["pulls"] = pulls(models[cid], f["X"], names, links, plain)
    picks = {}
    for cid, fr in combos.items():
        got = _wembley_pick(fr)
        picks[cid] = sorted({p for p in ((got[1] if got else None), len(fr) - 1) if p})     # never the real map
        if cid != "train" and restarts:
            log(f"restart check, {cid}:")
            restart[cid] = restart_check(models[cid], mesh, pairs, rhos, fr, picks[cid], checks, tol, max_iter, log)
    cross = {f"{a}|{b}": cross_flags(mesh, combos[a], combos[b])
             for a, b in itertools.combinations(list(combos), 2) if one_apart(a, b)}
    for key, flags in cross.items():
        bad = [f["rho"] for f in flags if f and not f["ok"]]
        log(f"switch {key}: straight at {sum(1 for f in flags if f and f['ok'])} of {len(flags) - 1} positions"
            + (f"; through the real map at ρ = {', '.join(f'{r:.3g}' for r in bad)}" if bad else ""))
    wpick = wembley.get("pick")
    cmp_at = sorted({p for p in (wpick, len(frames) - 1) if p is not None})
    spots = spot_check(pairs, mesh, links, float(car_links_doc["overhead_min"]), combos, cmp_at, checks) \
        if "car" in chosen else []
    return {"chosen": sorted(chosen), "combos": combos, "motions": motions, "models": models, "restart": restart,
            "cross": cross, "picks": picks, "compare": {k: combo_rows(mesh, pairs, combos, k, names) for k in cmp_at},
            "compare_at": cmp_at, "spots": spots, "car_links": car_links_doc, "roads": roads_doc,
            "car_report": car_report, "lines": lines, "plain": plain, "tables": tables or {}, "departure": departure,
            "names": names}




def mesh_of(b: BuildInput, pairs) -> tuple:
    """The mesh of a build's station nodes and its thinned cells (the car job builds the same one):
    (mesh, the build's cells, the picked cells' indices into them)."""
    cells = [p for p in b.points if p["kind"] == "cell"]
    cell_xy = to_local(np.array([float(p["lon"]) for p in cells]), np.array([float(p["lat"]) for p in cells])) \
        if cells else np.zeros((0, 2))
    pick = meshmod.thin_cells(pairs.xy, cell_xy)
    try:
        mesh = meshmod.build_mesh(pairs.xy, cell_xy[pick])
    except ValueError as e:
        raise RefusedInput(f"the mesh can't be built from the real positions: {e}") from None
    return mesh, cells, pick


def ship(f: dict, m, mesh) -> None:
    """A solved layout as it ships, whole decimetres (f["dm"], f["Xs"]), refused unless it is one
    to one as shipped and in Float32."""
    f32 = lambda a: np.asarray(a).astype(np.float32).astype(np.float64)  # noqa: E731
    f["dm"] = springs.quantise(f["X"][:m.n_mesh] * m.scale)           # the car's twins never ship
    f["Xs"] = f["dm"] / 10.0
    for label, X, P in (("as shipped", f["Xs"], mesh.P), ("in Float32", f32(f["Xs"]), f32(mesh.P))):
        fc = meshmod.fold_check(mesh, X, P)
        if not fc["ok"]:
            raise RefusedSolve(f"ρ = {f['rho']:.3g} {label}: {fc['flipped']} flipped triangles, ring moved "
                               f"{fc['ring_moved_m']:.2f} m, finite {fc['finite']}; nothing was written")
    f["fold"] = fc


def motion_keyframes(m, mesh, frames, labels, log) -> dict:
    """The page moves smoothly from one position to the next by blending neighbouring keyframes in
    a straight line. The sweep kept its steps small enough for that (springs.sweep, smooth): where a
    blend would have folded it solved positions in between. Here they become keyframes, at
    fractional slider steps (by log ρ between the two solved positions they sit between), each
    shipped and checked like a position, and every blend between neighbours is checked again,
    exactly (mesh.blend_ok), before anything is written."""
    keys = [{"rho": frames[0]["rho"], "at": 0.0, "main": 0, "dm": frames[0]["dm"]}]

    def add(e, at):
        if not (e["converged"] and e["monotone"]):
            raise RefusedSolve(f"the in-between position ρ = {e['rho']:.3g} did not converge; nothing was written")
        k = {"rho": e["rho"], "X": e["X"], "main": None, "at": round(at, 6), "settling": bool(e.get("settling"))}
        ship(k, m, mesh)
        keys.append(k)

    for i in range(1, len(frames)):
        a, b = frames[i - 1]["rho"], frames[i]["rho"]

        def at_of(rho, i=i, a=a, b=b):
            return (i - 1) + (math.log(rho / a) / math.log(b / a) if a > 0 else rho / b)

        between, n = frames[i]["between"], 0
        while n < len(between):
            e = between[n]
            if not e.get("settling"):
                add(e, at_of(e["rho"]))
                n += 1
                continue
            # Where the map settles after a snap: all at one ρ, so spread evenly over the slider
            # between the keyframe before and the solved position they lead to (at that ρ), in order.
            run = [e]
            while n + len(run) < len(between) and between[n + len(run)].get("settling"):
                run.append(between[n + len(run)])
            lo, hi = keys[-1]["at"], at_of(e["rho"])
            for r, x in enumerate(run):
                add(x, lo + (hi - lo) * (r + 1) / (len(run) + 1))
            n += len(run)
        keys.append({"rho": b, "at": float(i), "main": i, "dm": frames[i]["dm"]})
    if any(not q["at"] > p["at"] for p, q in zip(keys, keys[1:])):
        raise RefusedSolve("the keyframes are out of order along the slider; nothing was written")
    worst = math.inf
    for a, b in zip(keys, keys[1:]):
        lo = float((meshmod.blend_min_area(mesh.tri, a["dm"], b["dm"]) / (200 * mesh.A0)).min())
        if not lo > 0:
            raise RefusedSolve(f"the motion from ρ = {a['rho']:.3g} to ρ = {b['rho']:.3g} folds; nothing was written")
        worst = min(worst, lo)
    inserted = sum(k["main"] is None and not k.get("settling") for k in keys)
    settling = sum(bool(k.get("settling")) for k in keys)
    # Each jump: from the keyframe before a settling run to the solved position after it, the
    # station that moves furthest and how far.
    snaps, j = [], 1
    while j < len(keys):
        if not keys[j].get("settling"):
            j += 1
            continue
        e = j
        while keys[e].get("settling"):
            e += 1
        before, after = keys[j - 1], keys[e]
        move = np.hypot(*((after["dm"] - before["dm"])[:mesh.n_station] / 10.0).T)
        far = int(np.argmax(move))
        snaps.append({"rho": after["rho"], "from": before["rho"], "station": labels[far], "move_m": float(move[far]),
                      "at_from": before["at"], "at_to": after["at"]})
        j = e
    log(f"motion: {len(keys) - 1} blends, every one unfolded all the way; {inserted} in-between positions, "
        f"{settling} settling step{'' if settling == 1 else 's'}"
        + "".join(f"; a jump from ρ = {x['from']:.4g} to {x['rho']:.4g} ({x['move_m']:.0f} m)" for x in snaps))
    return {"keys": keys, "inserted": inserted, "settling": settling, "snaps": snaps, "blends": len(keys) - 1,
            "min_area_ratio_along_blends": worst}


def _bend_deg(X, a, b, c) -> float:
    """The turn at b going a → b → c on X: 0 straight on, 180 straight back."""
    u, v = X[b] - X[a], X[c] - X[b]
    nu, nv = float(np.hypot(*u)), float(np.hypot(*v))
    if nu == 0 or nv == 0:
        return 0.0
    return math.degrees(math.acos(max(-1.0, min(1.0, float(u @ v) / (nu * nv)))))


def wembley_analysis(pairs, mesh, lk, frames, checks, l01) -> dict:
    """Where Wembley Park first passes (or comes closest), and what shapes it there and at the end."""
    wem = checks.get("wembley", {})
    i_w, i_b = pairs.index_of(wem.get("station", "")), pairs.index_of(wem.get("reference", ""))
    got = _wembley_pick(frames)
    if i_w is None or i_b is None or got is None:
        return {"status": "absent"}
    passes, pick, gap = got
    end = len(frames) - 1
    chain_m, path = _shortest_chain(pairs.n, lk.pairs, lk.rest_m, i_b, i_w)
    seg = np.linspace(0, 1, max(2, int(np.hypot(*(mesh.P[i_w] - mesh.P[i_b])) // SEGMENT_STEP_M) + 1))
    which, _ = meshmod.locate(mesh.P, mesh.tri, mesh.P[i_b] + seg[:, None] * (mesh.P[i_w] - mesh.P[i_b]))
    crossed = sorted({int(t) for t in which if t >= 0})

    def at(k):
        X = frames[k]["Xs"]
        hops = []
        for a, c in zip(path, path[1:]):
            j = int(np.where(((lk.pairs[:, 0] == min(a, c)) & (lk.pairs[:, 1] == max(a, c))))[0][0])
            hops.append({"from": pairs.labels[a], "to": pairs.labels[c], "minutes": float(lk.minutes[j]),
                         "rest_m": float(lk.rest_m[j]), "map_m": float(np.hypot(*(X[a] - X[c]))),
                         "real_m": float(np.hypot(*(mesh.P[a] - mesh.P[c])))})
        bends = [{"at": pairs.labels[path[q]], "deg": _bend_deg(X, path[q - 1], path[q], path[q + 1])}
                 for q in range(1, len(path) - 1)]
        r = meshmod.area_ratios(mesh, X)[crossed] if crossed else np.zeros(0)
        sh = meshmod.stretch(mesh, X)[crossed] if crossed else np.zeros(0)
        return {"rho": frames[k]["rho"], "hops": hops, "bends": bends,
                "chord_m": float(np.hypot(*(X[i_w] - X[i_b]))),
                "corridor_area_min": float(r.min()) if len(r) else None,
                "corridor_area_median": float(np.median(r)) if len(r) else None,
                "corridor_stretch_max": float(sh.max()) if len(sh) else None}

    others = []
    for row in checks.get("ratio", []):
        i, j = pairs.index_of(row["station"]), pairs.index_of(row["reference"])
        if i is None or j is None:
            continue
        others.append({"name": row["name"], "linked_to_reference": lk.has(i, j),
                       "links": [pairs.labels[q] for q in sorted(set(lk.pairs[(lk.pairs == i).any(axis=1)].ravel()) - {i})],
                       "to_reference_km": [float(np.hypot(*(frames[k]["Xs"][i] - frames[k]["Xs"][j]))) / 1000
                                           for k in (0, pick, end)],
                       "to_wembley_park_km": [float(np.hypot(*(frames[k]["Xs"][i] - frames[k]["Xs"][i_w]))) / 1000
                                              for k in (0, pick, end)],
                       "rest_km": float(pairs.s * pairs.t[i, j]) / 1000 if pairs.used[i, j] else None})
    res = [f["metrics"]["ratios"]["wembley_park"] for f in frames]
    l01_res = [f["metrics"]["wembley"] for f in l01["frames"]]
    l01_pass = [k for k, r in enumerate(l01_res) if r.get("status") == "pass"]
    return {"status": "pass" if passes else "fail", "passes": passes, "first_pass": passes[0] if passes else None,
            "pick": pick, "pick_is_pass": bool(passes), "gap": gap[pick],
            "direct_rest_m": float(pairs.s * pairs.t[i_w, i_b]),
            "chain": {"metres": chain_m, "stations": [pairs.labels[v] for v in path]},
            "corridor_triangles": len(crossed), "at_pick": at(pick), "at_end": at(end), "others": others,
            "end_trend": [float(r["ratio_to_median"]) for r in res[-3:]],
            "l01_first_pass": l01_pass[0] if l01_pass else None}


def carry_land(clip: Path, mesh, work: Path | None, log) -> dict:
    """The clip's land read once; `rung(k)` cuts it at land.LADDER[k] (the report takes the first
    rung whose page fits), and `coarse` is the panels' copy (water and borough outlines)."""
    from transit.layout import land

    with tempfile.TemporaryDirectory() as tmp:
        lines = land.extract(clip, Path(work or tmp))
        with open(lines, encoding="utf-8") as f:
            layers = land.classify(land.parse_lines(f))
    return land_from_layers(layers, mesh, log)


def land_from_layers(layers, mesh, log) -> dict:
    import shapely

    from transit.layout import land

    land.check_boroughs(layers)
    hull = shapely.MultiPoint(mesh.P[mesh.kinds != meshmod.RING]).convex_hull

    def rung(k: int):
        tol_m, min_ha = land.LADDER[k]
        return land.carry(land.prepare(layers, hull, tol_m, min_ha), mesh.P, mesh.tri, tol_m, min_ha)

    coarse_prep = land.prepare(layers, hull, *land.COARSE)
    coarse = land.carry({k: v for k, v in coarse_prep.items() if k in ("water", "boundary")}, mesh.P, mesh.tri,
                        *land.COARSE)
    log(f"land: {layers.counts['water']} water, {layers.counts['park']} park, {layers.counts['rail']} rail "
        f"features, {len(layers.boroughs)} boroughs")
    return {"rung": rung, "n_rungs": len(land.LADDER), "coarse": coarse, "source_counts": layers.counts}


def gzip_size(data: bytes) -> int:
    return len(gzip.compress(data, mtime=0))


def page_static_bytes() -> int:
    """The app's Whole city page and what it loads, gzipped: the part of a view the job doesn't
    write."""
    return sum(gzip_size((d / n).read_bytes())
               for d, names in ((PAGE, PAGE_FILES), (SHARED, SHARED_FILES), (RENDERER, RENDERER_FILES))
               for n in names)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m transit.layout.timemap",
                                 description="The full London time map: a spring grid that can't fold (#6, L-02).")
    ap.add_argument("--build", required=True, help="build id (YYYY-MM-DD) under $TRANSIT_OUT")
    ap.add_argument("--window", default="wd13", choices=sorted(settings.WINDOWS))
    ap.add_argument("--out-dir", type=Path, help="default: transit/out/timemap/<build>/<window>/ in this repo")
    ap.add_argument("--checks", type=Path, default=l01_job.CHECKS, help="hand-typed station ids (checks.toml)")
    ap.add_argument("--no-land", action="store_true", help="skip the OSM land (no osmium needed)")
    ap.add_argument("--modes", nargs="+", choices=list(EXTRA_MODES),
                    help="the travel modes too (#23): every combination of train and tube with these (car from "
                    "python -m transit.layout.car, bus and walking from the build's bus-only and walking-only "
                    f"tables), written to modes/ (weekday 13:00 only, --window {settings.CAR_WINDOW})")
    ap.add_argument("--car-dir", type=Path, help="the car job's output (default: transit/out/car/<build>/<window>/)")
    args = ap.parse_args(argv)
    try:
        date.fromisoformat(args.build)
    except ValueError:
        ap.error(f"--build must be a date (YYYY-MM-DD), not {args.build!r}")
    out = args.out_dir or default_out(args.build, args.window)
    try:
        chosen = set(args.modes or ())
        # Every travel mode is weekday 13:00-14:00 only: refused before anything is read.
        if "car" in chosen:
            from transit.layout import car as carmod
            carmod.check_window(args.window)
        if chosen & {"bus", "walk"} and args.window != settings.CAR_WINDOW:
            raise RefusedInput(f"bus and walking times exist for weekday 13:00-14:00 only: build them for "
                               f"--window {settings.CAR_WINDOW}, not {args.window}")
        check_out_dir(out, modes=bool(args.modes))
        checks, checks_sha = load_checks(args.checks)
        b = load_build(settings.OUT / args.build, args.window)
        feed, clip, shas = find_inputs(b, settings.DATA, land=not args.no_land)
        car = bus_feed = None
        tables = {}
        if "car" in chosen:
            car = args.car_dir or carmod.default_out(args.build, args.window)
        if chosen & {"bus", "walk"}:
            for kind in ("bus", "walk") if "bus" in chosen else ("walk",):
                tables[kind] = load_mode_table(b, kind)
            if "bus" in chosen:
                bus_feed = bus_feed_of(b, settings.DATA)
        run(b, feed, clip, checks, out, checks_sha256=checks_sha, input_sha=shas, car=car, modes=chosen,
            bus_feed=bus_feed, tables=tables)
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
