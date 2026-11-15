"""
The batch job: python -m transit.layout --build <id> [--window wd13] [--out-dir DIR]

Reads transit/out/<build>/ (or $TRANSIT_OUT/<build>/), solves the λ sweep and writes, to
<repo>/transit/out/layout/<build>/<window>/ by default (git-ignored, beside the builds and never
inside one):

  layout.json   nodes, the frames (real map first, then every solved λ), metrics, triangles
  layout.js     the same, as `window.LAYOUT = …` for the viewer opened from disk
  index.html, viewer.js, viewer.css   the viewer (copied as committed)
  report.json, report.md, shepard.svg, pairs.npz

It exits 2, having written nothing, when the build, the checks file, the output folder or a solve
is refused; pass and fail verdicts are in the report, not the exit code.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import time
import tomllib
from datetime import date
from pathlib import Path

import numpy as np

from transit import settings
from transit.layout import measures, report
from transit.layout.prep import (
    BuildInput,
    Refused,
    RefusedInput,
    RefusedOutput,
    RefusedSolve,
    load_build,
    station_pairs,
)
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

__all__ = ["Refused", "check_out_dir", "default_out", "load_checks", "main", "run"]

REPO = Path(__file__).resolve().parents[2]
CHECKS = Path(__file__).with_name("checks.toml")
SHIPPED = ("static", "server")      # the app serves these, and the image copies them whole


def default_out(build: str, window: str) -> Path:
    return REPO / "transit" / "out" / "layout" / build / window


def check_out_dir(out: Path) -> None:
    """Everything the job writes is derived from the transit tables, so it must not reach git or
    the app: refuse a symlinked output file, a folder the app serves or ships, and a folder inside
    the repo where git would track any of the files written."""
    out = Path(out)
    for name in report.OUTPUT_FILES:
        if (out / name).is_symlink():
            raise RefusedOutput(f"{out / name} is a symlink; the job writes only plain files")
    real = out.resolve()
    try:
        rel = real.relative_to(REPO)
    except ValueError:
        return
    if rel.parts and rel.parts[0] in SHIPPED:
        raise RefusedOutput(f"{rel} is under {rel.parts[0]}/, which the app serves or ships")
    probes = [(rel / name).as_posix() for name in report.OUTPUT_FILES]
    try:
        r = subprocess.run(["git", "check-ignore", "--no-index", "--", *probes], cwd=REPO,
                           capture_output=True, text=True)
        ignored = set(r.stdout.splitlines())
    except FileNotFoundError:
        ignored = set()
    missing = [Path(p).name for p in probes if p not in ignored]
    if missing:
        raise RefusedOutput(f"{rel} is inside the repo and not git-ignored ({', '.join(missing)}); "
                            "everything the job writes is derived from the transit tables")


def load_checks(path: Path) -> tuple[dict, str]:
    """The hand-typed station ids, and the sha256 of the file they came from."""
    try:
        raw = Path(path).read_bytes()
        checks = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise RefusedInput(f"checks file {path}: {e}") from None

    def ids(v) -> bool:
        return isinstance(v, list) and all(isinstance(x, str) for x in v)

    wem, runs, labels = checks.get("wembley", {}), checks.get("metropolitan", []), checks.get("labels", {})
    ok = (isinstance(wem, dict) and all(isinstance(wem.get(k, ""), str) for k in ("station", "reference"))
          and isinstance(runs, list)
          and all(isinstance(r, dict) and isinstance(r.get("name"), str) and ids(r.get("stations")) for r in runs)
          and isinstance(labels, dict) and ids(labels.get("stations", [])))
    if not ok:
        raise RefusedInput(f"checks file {path} is not shaped like transit/layout/checks.toml")
    return checks, hashlib.sha256(raw).hexdigest()


def code_version() -> str | None:
    """HEAD, with -dirty when anything under transit/layout differs from it (staged, unstaged or
    untracked)."""
    try:
        head = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=REPO, capture_output=True,
                              text=True)
        if head.returncode:
            return None
        status = subprocess.run(["git", "status", "--porcelain", "--", "transit/layout"], cwd=REPO,
                                capture_output=True, text=True)
        return head.stdout.strip() + ("-dirty" if status.returncode or status.stdout.strip() else "")
    except FileNotFoundError:
        return None


def expired(valid_until: str | None, today: date | None = None) -> bool:
    try:
        return (today or date.today()) > date.fromisoformat(str(valid_until))
    except ValueError:
        return False


def _frame_metrics(pairs, X, A, tri, tri_full, tc, checks) -> tuple[dict, np.ndarray, list]:
    sig = sigma1(X, pairs)
    shift = np.hypot(*(X - A).T)
    wem = checks.get("wembley", {})
    lines = measures.line_order(pairs, X, checks.get("metropolitan", []), A=A)
    m = {"sigma1": sig, **tc(X),
         "folds": measures.fold_stats(tri, A, X), "folds_full": measures.fold_stats(tri_full, A, X),
         "wembley": measures.ratio_check(pairs, X, wem.get("station", ""), wem.get("reference", ""), sig),
         "met_breaks": report.count_breaks(lines),
         "shift_median_m": float(np.median(shift)), "shift_p90_m": float(np.percentile(shift, 90))}
    return m, node_stress(X, pairs), lines


def run(b: BuildInput, checks: dict, out: Path, lam_tildes=LAM_TILDES, tol: float = TOL,
        max_iter: int = MAX_ITER, log=print, checks_sha256: str | None = None) -> dict:
    t0 = time.monotonic()
    pairs = station_pairs(b)
    c = pairs.counts
    log(f"{c['stations_usable']} usable stations -> {pairs.n} nodes ({len(c['merges'])} merged, "
        f"{len(pairs.left_out)} groups left out), {c['pairs_used']} pairs, s = {pairs.s:.1f} m/min")
    k = measures.TC_K
    if pairs.n <= 3 * k + 1:
        raise RefusedInput(f"too few nodes ({pairs.n}) for neighbourhoods of {k}: it needs more than {3 * k + 1}")
    A = pairs.xy
    try:
        tri_full = measures.delaunay(A)
    except ValueError as e:
        raise RefusedInput(f"the real positions can't be triangulated: {e}") from None
    tri, peeled = measures.peel(A, tri_full)
    tc = measures.TrustContinuity(pairs.t, pairs.used, k=k)

    solves = sweep(pairs, lam_tildes, tol=tol, max_iter=max_iter)
    bad = [f["lam_tilde"] for f in solves if f["rose"] or not f["converged"] or not f["monotone"]]
    if bad:
        raise RefusedSolve(f"the solve at λ̃ = {', '.join(f'{v:.3g}' for v in bad)} did not converge or "
                           f"raised the objective (cap {max_iter} iterations); nothing was written")
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
                     "lam_unit": lam_unit(pairs), "tol": tol, "max_iter": max_iter, "tc_k": k,
                     "tc_seeds": measures.TC_SEEDS, "peel_min_deg": measures.PEEL_MIN_DEG,
                     "peel_edge_factor": measures.PEEL_EDGE_FACTOR, "line_tolerance": "1 map-minute (s metres)"}
    result = report.write(out, b, pairs, frames, tri, tri_full, peeled, checks, lines_at, restart, bins,
                          settings_used, code_version(), checks_sha256=checks_sha256,
                          expired=expired(b.manifest.get("valid_until")), seconds=time.monotonic() - t0)
    for line in result["summary"]:
        log(line)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m transit.layout",
                                 description="Lay out London's stations by travel time (issue #6, L-01).")
    ap.add_argument("--build", required=True, help="build id (YYYY-MM-DD) under $TRANSIT_OUT")
    ap.add_argument("--window", default="wd13", choices=sorted(settings.WINDOWS))
    ap.add_argument("--out-dir", type=Path, help="default: transit/out/layout/<build>/<window>/ in this repo")
    ap.add_argument("--checks", type=Path, default=CHECKS, help="hand-typed station ids (checks.toml)")
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
        run(b, checks, out, checks_sha256=checks_sha)
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    return 0
