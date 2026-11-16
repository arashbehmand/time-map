"""
The full London time map (#6, L-02): python -m transit.layout.timemap --build <id> [--window wd13]

A spring grid that can't fold. Every station and 300 points between them are joined by a
triangle mesh of soft springs; stations a train serves one after the other are joined by stiff
springs whose rest length is the scaled travel time between them; and every spring and triangle
resists collapse without limit, so no triangle can turn inside out. A slider moves from the real
map to the time map by making the train springs stronger against the grid (springs.py says how,
mesh.py why the map stays one to one). The Thames, parks, rail lines and borough outlines are
cut along the mesh and move with it (land.py), and the page draws them with W-01's renderer.

Reads, like the stations-only job, $TRANSIT_OUT/<build>/ (points.csv, point_ok.npy,
tt_<window>.npy, its fingerprint, manifest.json), and from $TRANSIT_DATA/work/<build>/ the
prepared rail feed gbt.gtfs.zip (which stations trains join) and the OSM clip london.osm.pbf (the
land), each checked against the manifest's sha256. Writes to
<repo>/transit/out/timemap/<build>/<window>/ (git-ignored):

  index.html, app.js, app.css, carry.js, mesh/*.js   the page (serve the folder; see the README)
  timemap.json, land.bin                             what the page draws
  report.md, report.json, shepard.svg, panels.svg   the report, beside the stations-only layout's

It exits 2, having written nothing, when an input, the checks file, the output folder, a solve,
the no-flip check on what would ship, or the page's size is refused. Pass and fail of the checks
are in the report, not the exit code.
"""
from __future__ import annotations

import argparse
import gzip
import heapq
import math
import shutil
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
    RefusedOutput,
    RefusedSolve,
    load_build,
    sha256,
    station_pairs,
)
from transit.layout.smacof import align_rigid, node_stress, sigma1

__all__ = ["OUTPUT_FILES", "Refused", "default_out", "main", "run"]

REPO = l01_job.REPO
PAGE = Path(__file__).with_name("timemap_page")
PAGE_FILES = ("index.html", "app.js", "app.css", "carry.js")
RENDERER = REPO / "static" / "shared" / "mesh"
RENDERER_FILES = ("renderer.js", "caps.js", "warp.js", "field.js", "frame.js", "schema-streets-v8.js")
DATA_FILES = ("timemap.json", "land.bin")
REPORT_FILES = ("report.json", "report.md", "shepard.svg", "panels.svg")
OUTPUT_FILES = (*PAGE_FILES, *(f"mesh/{n}" for n in RENDERER_FILES), *DATA_FILES, *REPORT_FILES)
MAX_GZIP_BYTES = 2_000_000
SEGMENT_STEP_M = 100.0
RESTART_SEEDS = (1, 2)        # the restart check: the sweep again from the real map nudged by
RESTART_JITTER_M = 0.5        # up to this much on every free node, once per seed


def default_out(build: str, window: str) -> Path:
    return REPO / "transit" / "out" / "timemap" / build / window


def check_out_dir(out: Path) -> None:
    out = Path(out)
    if (out / "mesh").is_symlink():
        raise RefusedOutput(f"{out / 'mesh'} is a symlink; the job writes only plain files")
    l01_job.check_out_dir(out, OUTPUT_FILES)


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
    free = ~mesh.fixed
    inner = mesh.kinds != meshmod.RING
    out = {"jitter_m": RESTART_JITTER_M, "seeds": list(RESTART_SEEDS), "ladders": [], "complete": True, "at": {}}
    rows = {k: [] for k in picks}

    def measure(k, X, kind, name):
        dm = springs.quantise(X * m.scale)
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
            out["complete"] = False
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


def _rho_short(r: float) -> str:
    return f"ρ = {r:.3g}"


# ── the job ─────────────────────────────────────────────────────────
def run(b: BuildInput, feed: Path, clip: Path | None, checks: dict, out: Path, *, rhos=springs.RHOS,
        tol: float = springs.TOL, max_iter: int = springs.MAX_ITER, log=print, checks_sha256: str | None = None,
        input_sha: dict | None = None, land_work: Path | None = None, restarts: bool = True) -> dict:
    from transit.layout import timemap_report as rep

    t0 = time.monotonic()
    pairs = station_pairs(b)
    k = measures.TC_K
    if pairs.n <= 3 * k + 1:
        raise RefusedInput(f"too few nodes ({pairs.n}) for neighbourhoods of {k}: it needs more than {3 * k + 1}")
    cells = [p for p in b.points if p["kind"] == "cell"]
    cell_xy = to_local(np.array([float(p["lon"]) for p in cells]), np.array([float(p["lat"]) for p in cells])) \
        if cells else np.zeros((0, 2))
    pick = meshmod.thin_cells(pairs.xy, cell_xy)
    try:
        mesh = meshmod.build_mesh(pairs.xy, cell_xy[pick])
    except ValueError as e:
        raise RefusedInput(f"the mesh can't be built from the real positions: {e}") from None
    log(f"{pairs.n} station nodes, {mesh.n_cell} cells, {mesh.n_ring} guard nodes: {len(mesh.tri)} triangles, "
        f"{len(mesh.edges)} grid springs")

    dep = departure(b)
    try:
        lk = linkmod.train_links(feed, dep.date(), dep.time(), pairs, feed_id=feed_prefix(pairs))
    except (linkmod.LinkError, OSError, KeyError) as e:
        raise RefusedInput(f"the train links can't be read from {Path(feed).name}: {e}") from None
    required_links(pairs, lk, checks)
    log(f"{len(lk.pairs)} train links ({lk.counts['station_trips_on_day']} station trips on {dep.date()})")

    m = springs.model(mesh, lk.pairs, lk.rest_m)
    frames = springs.sweep(m, rhos, tol=tol, max_iter=max_iter, log=log)
    # The sweep stops at the first step that doesn't converge or still folds; both refuse. A step
    # that failed on its way (at an in-between ρ) is named by the ρ it reached.
    bad = [f for f in frames if not (f["converged"] and f["monotone"])]
    if bad:
        where = ", ".join(f"{f['reached']:.3g}" + (f" (on the way to {f['rho']:.3g})" if f["reached"] != f["rho"] else "")
                          for f in bad)
        raise RefusedSolve(f"the solve at ρ = {where} did not converge (cap {max_iter} iterations, "
                           f"{springs.SUBDIVIDE} levels of sub-steps); nothing was written")
    folding = [f["rho"] for f in frames if not f["smooth_ok"]]
    if folding:
        raise RefusedSolve(f"the step to ρ = {folding[0]:.3g} still folds part way after {springs.MOTION_DEPTH} levels "
                           "of in-between positions; nothing was written")

    # What ships: whole decimetres. The no-flip check runs on exactly that, and on its Float32 image.
    for f in frames:
        ship(f, m, mesh)
    motion = motion_keyframes(m, mesh, frames, pairs.labels, log)

    n_st = pairs.n
    A = frames[0]["Xs"][:n_st]
    tc = measures.TrustContinuity(pairs.t, pairs.used, k=k)
    tri_l01_full = measures.delaunay(pairs.xy)
    tri_l01, _ = measures.peel(pairs.xy, tri_l01_full)
    l01 = l01_recompute(pairs, checks, tc, log)
    strict = l01["frames"][-1]["metrics"]["sigma1"]
    rl = ratio_list(checks)
    runs = checks.get("metropolitan", [])
    lines_at = {}
    prev = None
    inner = ~(mesh.kinds[mesh.tri] == meshmod.RING).any(axis=1)
    A0_in = mesh.A0[inner]
    for i, f in enumerate(frames):
        X = f["Xs"]
        st = X[:n_st]
        sig = sigma1(st, pairs)
        r = meshmod.area_ratios(mesh, X)[inner]
        stretch = meshmod.stretch(mesh, X)
        st_in = stretch[inner]
        lines_at[i] = measures.line_order(pairs, st, runs, A=A)
        lens = np.hypot(*(X[lk.pairs[:, 0]] - X[lk.pairs[:, 1]]).T) if len(lk.pairs) else np.zeros(0)
        past = np.maximum(lens / lk.rest_m - 1, 0) if len(lens) else np.zeros(1)   # how far past s·t
        shift = np.hypot(*(st - A).T)
        moved = np.hypot(*(X - prev).T)[mesh.kinds != meshmod.RING] if prev is not None else np.zeros(1)
        prev = X
        _, rot = align_rigid(st, A)
        f["stress"] = node_stress(st, pairs)
        f["metrics"] = {
            "sigma1": sig, **tc(st),
            "ratios": _ratios(pairs, st, rl, sig, strict),
            "met_breaks": l01_job.report.count_breaks(lines_at[i]),
            "folds": {"flipped": f["fold"]["flipped"], "triangles": int(len(mesh.tri)),
                      "area_share": float(A0_in[~(r > 0)].sum() / A0_in.sum())},
            "folds_l01": {"peeled": measures.fold_stats(tri_l01, pairs.xy, st),
                          "full": measures.fold_stats(tri_l01_full, pairs.xy, st)},
            "area_ratio_min": float(r.min()), "area_ratio_p1": float(np.percentile(r, 1)),
            "under_25pct": int((r < 0.25).sum()), "under_5pct": int((r < 0.05).sum()),
            "stretch_max": float(st_in.max()), "stretch_median": float(np.median(st_in)),
            "stretch_p90": float(np.percentile(st_in, 90)),
            "stretched_4_share": float(A0_in[st_in > 4].sum() / A0_in.sum()),
            "stretch_worst": meshmod.worst(mesh, stretch, pairs.labels, inner, largest=True),
            "squeezed": meshmod.squeezed(mesh, X, pairs.labels, mask=inner),
            "links_past_share": float((past > 0.01).mean()), "links_past_p90": float(np.percentile(past, 90)),
            "energy": springs.parts(m, X / m.scale, f["rho"]),
            "shift_median_m": float(np.median(shift)), "shift_p90_m": float(np.percentile(shift, 90)),
            "move_median_m": float(np.median(moved)), "rotation_deg": float(rot["rotation_deg"]),
            "iterations": f["iterations"], "gmax": f["gmax"], "substeps": f["substeps"], "pd": bool(f["pd"]),
        }

    wembley = wembley_analysis(pairs, mesh, lk, frames, checks, l01)
    picks = sorted({p for p in (wembley.get("pick"), len(frames) - 1) if p is not None})
    restart = restart_check(m, mesh, pairs, rhos, frames, picks, checks, tol, max_iter, log) if restarts else None
    routes = [{"name": r["name"], "nodes": nodes, **dict(zip(("c", "w"), meshmod.carry_route(mesh, nodes)))}
              for r in runs for nodes in [[i for i in (pairs.index_of(s) for s in r["stations"]) if i is not None]]
              if len(nodes) >= 2]
    land_doc = None
    if clip is not None:
        land_doc = carry_land(clip, mesh, land_work, log)
    result = rep.write(out, b=b, pairs=pairs, mesh=mesh, links=lk, model=m, frames=frames, cells=cells, pick=pick,
                       checks=checks, lines_at=lines_at, l01=l01, wembley=wembley, land=land_doc, routes=routes,
                       restart=restart, motion=motion, code=l01_job.code_version(), checks_sha256=checks_sha256,
                       input_sha=input_sha or {}, expired=l01_job.expired(b.manifest.get("valid_until")),
                       started=t0, log=log)
    for line in result["summary"]:
        log(line)
    return result


def ship(f: dict, m, mesh) -> None:
    """A solved layout as it ships, whole decimetres (f["dm"], f["Xs"]), refused unless it is one
    to one as shipped and in Float32."""
    f32 = lambda a: np.asarray(a).astype(np.float32).astype(np.float64)  # noqa: E731
    f["dm"] = springs.quantise(f["X"] * m.scale)
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


def copy_page(out: Path) -> None:
    (out / "mesh").mkdir(parents=True, exist_ok=True)
    for name in PAGE_FILES:
        shutil.copyfile(PAGE / name, out / name)
    for name in RENDERER_FILES:
        shutil.copyfile(RENDERER / name, out / "mesh" / name)


def page_static_bytes() -> int:
    return sum(gzip_size((PAGE / n).read_bytes()) for n in PAGE_FILES) + \
        sum(gzip_size((RENDERER / n).read_bytes()) for n in RENDERER_FILES)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m transit.layout.timemap",
                                 description="The full London time map: a spring grid that can't fold (#6, L-02).")
    ap.add_argument("--build", required=True, help="build id (YYYY-MM-DD) under $TRANSIT_OUT")
    ap.add_argument("--window", default="wd13", choices=sorted(settings.WINDOWS))
    ap.add_argument("--out-dir", type=Path, help="default: transit/out/timemap/<build>/<window>/ in this repo")
    ap.add_argument("--checks", type=Path, default=l01_job.CHECKS, help="hand-typed station ids (checks.toml)")
    ap.add_argument("--no-land", action="store_true", help="skip the OSM land (no osmium needed)")
    args = ap.parse_args(argv)
    try:
        date.fromisoformat(args.build)
    except ValueError:
        ap.error(f"--build must be a date (YYYY-MM-DD), not {args.build!r}")
    out = args.out_dir or default_out(args.build, args.window)
    try:
        check_out_dir(out)
        checks, checks_sha = load_checks(args.checks)
        b = load_build(settings.OUT / args.build, args.window)
        feed, clip, shas = find_inputs(b, settings.DATA, land=not args.no_land)
        run(b, feed, clip, checks, out, checks_sha256=checks_sha, input_sha=shas)
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
