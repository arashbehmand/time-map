"""
The batch job: python -m transit.layout --build <id> [--window wd13] [--out-dir DIR]

Reads transit/out/<build>/ (or $TRANSIT_OUT/<build>/), solves the λ sweep and writes, to
<repo>/transit/out/<build>/layout/<window>/ by default (git-ignored):

  layout.json   nodes, the frames (real map first, then every solved λ), metrics, triangles
  layout.js     the same, as `window.LAYOUT = …` for the viewer opened from disk
  index.html, viewer.js, viewer.css   the viewer (copied as committed)
  report.json, report.md, shepard.svg, pairs.npz

It exits 2 when the build or the output folder is refused; pass and fail verdicts are in the
report, not the exit code.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import tomllib
from datetime import date
from pathlib import Path

import numpy as np

from transit import settings
from transit.layout import measures, report
from transit.layout.prep import BuildInput, RefusedInput, load_build, station_pairs
from transit.layout.smacof import (
    LAM_TILDES,
    MAX_ITER,
    TOL,
    lam_unit,
    node_stress,
    restart_check,
    sigma1,
    sweep,
)

REPO = Path(__file__).resolve().parents[2]
CHECKS = Path(__file__).with_name("checks.toml")


def default_out(build: str, window: str) -> Path:
    return REPO / "transit" / "out" / build / "layout" / window


def check_out_dir(out: Path) -> None:
    """Refuse a folder inside the repo that git would track: nothing derived may reach git."""
    out = Path(out).resolve()
    try:
        rel = out.relative_to(REPO)
    except ValueError:
        return
    probe = (rel / "layout.json").as_posix()
    try:
        r = subprocess.run(["git", "check-ignore", "-q", "--no-index", probe], cwd=REPO,
                           capture_output=True)
        ignored = r.returncode == 0
    except FileNotFoundError:
        ignored = False
    if not ignored:
        raise RefusedInput(f"{rel} is inside the repo and not git-ignored; the layout is derived "
                           "from tables that are not licensed for publication")


def code_version() -> str | None:
    try:
        head = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=REPO, capture_output=True,
                              text=True)
        if head.returncode:
            return None
        dirty = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "transit/layout"], cwd=REPO,
                               capture_output=True).returncode
        return head.stdout.strip() + ("-dirty" if dirty else "")
    except FileNotFoundError:
        return None


def _frame_metrics(pairs, X, A, tri, tri_full, tc, checks) -> tuple[dict, np.ndarray, list]:
    sig = sigma1(X, pairs)
    shift = np.hypot(*(X - A).T)
    wem = checks.get("wembley", {})
    lines = measures.line_order(pairs, X, checks.get("metropolitan", []))
    m = {"sigma1": sig, **tc(X),
         "folds": measures.fold_stats(tri, A, X), "folds_full": measures.fold_stats(tri_full, A, X),
         "wembley": measures.ratio_check(pairs, X, wem.get("station", ""), wem.get("reference", ""), sig),
         "met_backtracks": sum(len(r["backtracks"]) for r in lines),
         "shift_median_m": float(np.median(shift)), "shift_p90_m": float(np.percentile(shift, 90))}
    return m, node_stress(X, pairs), lines


def run(b: BuildInput, checks: dict, out: Path, lam_tildes=LAM_TILDES, tol: float = TOL,
        max_iter: int = MAX_ITER, log=print) -> dict:
    t0 = time.monotonic()
    pairs = station_pairs(b)
    c = pairs.counts
    log(f"{c['stations_usable']} usable stations -> {pairs.n} nodes ({len(c['merges'])} merged, "
        f"{len(pairs.left_out)} groups left out), {c['pairs_used']} pairs, s = {pairs.s:.1f} m/min")
    A = pairs.xy
    tri_full = measures.delaunay(A)
    tri, peeled = measures.peel(A, tri_full)
    tc = measures.TrustContinuity(pairs.t, pairs.used)

    solves = sweep(pairs, lam_tildes, tol=tol, max_iter=max_iter)
    log(f"solved {len(solves)} values of λ in {time.monotonic() - t0:.0f} s")
    frames = [{"lam_tilde": None, "lam": None, "X": A, "iterations": 0, "converged": True,
               "rose": False, "monotone": True, "rotation_deg": 0.0, "reflection_fits_better": False}]
    frames += solves
    lines_at = {}
    for i, f in enumerate(frames):
        f["metrics"], f["stress"], lines = _frame_metrics(pairs, f["X"], A, tri, tri_full, tc, checks)
        if i in (0, len(frames) - 1):
            lines_at[i] = lines
    restart = restart_check(pairs, frames[-1]["metrics"]["sigma1"], tol=tol, max_iter=max_iter)
    bins = {"real": measures.shepard_bins(pairs, A), "time": measures.shepard_bins(pairs, frames[-1]["X"])}
    settings_used = {"merge_m": 50.0, "floor_min": 0.5, "weight": "t^-2", "lam_tildes": list(lam_tildes),
                     "lam_unit": lam_unit(pairs), "tol": tol, "max_iter": max_iter, "tc_k": measures.TC_K,
                     "tc_seeds": measures.TC_SEEDS, "peel_min_deg": measures.PEEL_MIN_DEG,
                     "peel_edge_factor": measures.PEEL_EDGE_FACTOR}
    result = report.write(out, b, pairs, frames, tri, tri_full, peeled, checks, lines_at, restart, bins,
                          settings_used, code_version(), seconds=time.monotonic() - t0)
    for line in result["summary"]:
        log(line)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m transit.layout",
                                 description="Lay out London's stations by travel time (issue #6, L-01).")
    ap.add_argument("--build", required=True, help="build id (YYYY-MM-DD) under $TRANSIT_OUT")
    ap.add_argument("--window", default="wd13", choices=sorted(settings.WINDOWS))
    ap.add_argument("--out-dir", type=Path, help="default: transit/out/<build>/layout/<window>/ in this repo")
    ap.add_argument("--checks", type=Path, default=CHECKS, help="hand-typed station ids (checks.toml)")
    args = ap.parse_args(argv)
    try:
        date.fromisoformat(args.build)
    except ValueError:
        ap.error(f"--build must be a date (YYYY-MM-DD), not {args.build!r}")
    out = args.out_dir or default_out(args.build, args.window)
    try:
        check_out_dir(out)
        b = load_build(settings.OUT / args.build, args.window)
        checks = tomllib.loads(Path(args.checks).read_text())
        run(b, checks, out)
    except RefusedInput as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    return 0
