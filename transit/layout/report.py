"""
Writes one build's layout outputs: layout.json (+ layout.js for the viewer), report.json,
report.md, shepard.svg, pairs.npz and a copy of the viewer. All of it is derived from the transit
tables, so it goes only to a folder git ignores and the app doesn't ship (job.check_out_dir),
whatever the build's licence says: that is #6 L-01's rule.
"""
from __future__ import annotations

import gzip
import json
import math
import shutil
from pathlib import Path

import numpy as np

from transit.layout.frame import ORIGIN
from transit.layout.prep import RefusedOutput

VIEWER = Path(__file__).with_name("viewer")
VIEWER_FILES = ("index.html", "viewer.js", "viewer.css")
OUTPUT_FILES = ("layout.json", "layout.js", "report.json", "report.md", "shepard.svg", "pairs.npz",
                *VIEWER_FILES)
MAX_GZIP_BYTES = 2_000_000
TC_PASS = 0.9
FOLD_AREA_LINE = 0.01


def _r(v, sig=6):
    """Floats to `sig` significant digits (NaN and inf to null), through dicts and lists."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, float):
        return float(f"{v:.{sig}g}") if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _r(x, sig) for k, x in v.items()}
    if isinstance(v, list):
        return [_r(x, sig) for x in v]
    return v


def count_breaks(lines: list[dict]) -> int:
    """Line-order breaks, counted once per (kind, station) across runs that share stations."""
    return len({(b["kind"], b["station"]) for r in lines for b in r["breaks"]})


def _free(f: dict, key: str = "folds") -> bool:
    m = f["metrics"][key]
    return m["folded"] == 0 and m["boundary_crossings"] == 0


def fold_run(frames: list[dict], key: str = "folds") -> dict:
    """How far the map stays fold-free going from the real map toward λ = 0: down_to is the last
    such λ̃ (only_real_map when the first solved frame already folds); islands are fold-free
    frames found again after the first fold."""
    end, broke, islands = frames[0], False, []
    for f in frames[1:]:
        if not broke and _free(f, key):
            end = f
        elif _free(f, key):
            islands.append(f["lam_tilde"])
        else:
            broke = True
    return {"down_to": end["lam_tilde"], "only_real_map": end is frames[0], "islands": islands}


def _area_run(frames: list[dict]):
    end = frames[0]
    for f in frames[1:]:
        if f["metrics"]["folds"]["area_share"] > FOLD_AREA_LINE:
            break
        end = f
    return end["lam_tilde"]


def headlines(frames: list[dict], lines_at: dict, restart: dict, pairs, labels_absent: list[str],
              expired: bool) -> dict:
    last = frames[-1]
    passes = [f for f in frames if f["metrics"]["wembley"].get("status") == "pass"]
    met = lines_at.get(len(frames) - 1, [])
    return {
        "fold_free": {"peeled": fold_run(frames, "folds"), "full": fold_run(frames, "folds_full")},
        "folded_area_1pct_down_to": _area_run(frames),
        "wembley_at_time_map": last["metrics"]["wembley"],
        "wembley_real_map": frames[0]["metrics"]["wembley"],
        "wembley_passes_at": [f["lam_tilde"] for f in passes],
        "fold_free_and_wembley_pass": [f["lam_tilde"] for f in passes if _free(f)],
        "tc_time_map": {k: last["metrics"].get(k) for k in ("T", "T_range", "C", "C_range", "covered", "of")},
        "tc_real_map": {k: frames[0]["metrics"][k] for k in ("T", "C")},
        "tc_pass": bool(last["metrics"]["T"] >= TC_PASS and last["metrics"]["C"] >= TC_PASS),
        "sigma1_real_map": frames[0]["metrics"]["sigma1"], "sigma1_time_map": last["metrics"]["sigma1"],
        "metropolitan_breaks": count_breaks(met),
        "metropolitan_absent": sorted({a for r in met for a in r["absent"]}),
        "restart_better_found": restart["better_start_found"],
        "reflection_fits_better": [f["lam_tilde"] for f in frames[1:] if f["reflection_fits_better"]],
        "left_out_groups": len(pairs.left_out),
        "labels_absent": labels_absent,
        "expired": expired,
    }


def notice(manifest: dict) -> str:
    """Local use only is #6 L-01's rule; the licence part comes from the build's own manifest."""
    if manifest.get("licences_cleared"):
        on = manifest.get("licences_cleared_on")
        lic = f"cleared for publication on {on}" if on else "cleared for publication"
    else:
        lic = "not cleared for publication"
    return f"Local use only (#6, L-01): kept out of git and off the site. This build's tables: {lic}."


def layout_doc(b, pairs, frames, tri, checks, code, checks_sha256=None) -> tuple[dict, list[str]]:
    labels, labels_absent = [], []
    for sid in checks.get("labels", {}).get("stations", []):
        i = pairs.index_of(sid)
        if i is None:
            labels_absent.append(sid)
        elif i not in labels:
            labels.append(i)
    lines = []
    for run in checks.get("metropolitan", []):
        nodes = [i for i in (pairs.index_of(s) for s in run["stations"]) if i is not None]
        lines.append({"name": run["name"], "nodes": nodes})
    stress_all = np.concatenate([f["stress"] for f in frames])
    top = max(0.05, math.ceil(float(np.percentile(stress_all, 98)) * 20) / 20)
    out_frames = []
    for f in frames:
        m = f["metrics"]
        out_frames.append({
            "lam_tilde": f["lam_tilde"], "lam": _r(f["lam"]),
            "x": [int(round(v)) for v in f["X"][:, 0]], "y": [int(round(v)) for v in f["X"][:, 1]],
            "stress": [round(float(v), 3) for v in f["stress"]],
            "metrics": _r({"sigma1": m["sigma1"], "T": m["T"], "C": m["C"],
                           "folded": m["folds"]["folded"], "folded_area": m["folds"]["area_share"],
                           "boundary_crossings": m["folds"]["boundary_crossings"],
                           "folded_full": m["folds_full"]["folded"],
                           "wembley": m["wembley"].get("status"),
                           "wembley_ratio": m["wembley"].get("ratio_to_median"),
                           "met_breaks": m["met_breaks"], "iterations": f["iterations"]})})
    doc = {
        "schema": 1, "kind": "london-stations-layout", "notice": notice(b.manifest),
        "build": b.manifest.get("build"), "window": b.window, "departure": b.departure,
        "valid_until": b.manifest.get("valid_until"), "table_sha256": b.table_sha256,
        "points_sha256": b.points_sha256, "checks_sha256": checks_sha256, "code": code,
        "attribution": list(b.manifest.get("attribution", [])),
        "frame": {"kind": "local", "origin": list(ORIGIN), "units": "m", "axes": "x east, y south"},
        "s_m_per_min": _r(pairs.s), "stress_scale": [0.0, top],
        "nodes": [{"id": pairs.ids[i], "name": pairs.names[i], "label": pairs.labels[i],
                   "members": pairs.members[i], "lon": round(float(pairs.lonlat[i, 0]), 6),
                   "lat": round(float(pairs.lonlat[i, 1]), 6)} for i in range(pairs.n)],
        "labels": labels, "lines": lines, "triangles": tri.tolist(), "left_out": pairs.left_out,
        "frames": out_frames,
    }
    return doc, labels_absent


# ── Shepard plot ────────────────────────────────────────────────────
def _blue(v: float) -> str:
    lo, hi = (219, 233, 246), (8, 48, 107)
    c = [round(a + (b - a) * v) for a, b in zip(lo, hi)]
    return "#%02x%02x%02x" % tuple(c)


def shepard_svg(bins: dict, title: str) -> str:
    size, pad_l, pad_t, gap = 300, 56, 44, 70
    width = pad_l + 2 * size + gap + 20
    height = pad_t + size + 50
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}" font-family="system-ui, sans-serif" font-size="11">',
             f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
             f'<text x="{pad_l}" y="18" font-size="13" font-weight="600">{title}</text>']
    top = max(b["top"] for b in bins.values())
    for k, (key, name) in enumerate((("real", "Real map"), ("time", "Pure time map (λ = 0)"))):
        b = bins[key]
        ox = pad_l + k * (size + gap)
        px = size / top
        parts.append(f'<text x="{ox}" y="{pad_t - 8}" font-weight="600">{name} '
                     f'({b["pairs"]:,} pairs)</text>')
        parts.append(f'<rect x="{ox}" y="{pad_t}" width="{size}" height="{size}" fill="none" stroke="#999"/>')
        counts = b["counts"]
        edges = b["edges"]
        cmax = math.log1p(counts.max()) if counts.size and counts.max() else 1.0
        bw = (edges[1] - edges[0]) * px
        for i, j in zip(*np.nonzero(counts)):
            v = math.log1p(counts[i, j]) / cmax
            x = ox + edges[i] * px
            y = pad_t + size - edges[j + 1] * px
            if x < ox + size and y >= pad_t - 0.01:
                parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.2f}" height="{bw:.2f}" '
                             f'fill="{_blue(v)}"/>')
        parts.append(f'<line x1="{ox}" y1="{pad_t + size}" x2="{ox + size}" y2="{pad_t}" '
                     'stroke="#d62728" stroke-width="1" stroke-dasharray="4 3"/>')
        if b["median"]:
            pts = " ".join(f"{ox + x * px:.1f},{pad_t + size - y * px:.1f}" for x, y in b["median"]
                           if y <= top)
            parts.append(f'<polyline points="{pts}" fill="none" stroke="#ff7f0e" stroke-width="2"/>')
        for tick in range(0, int(top) + 1, 30):
            tx, ty = ox + tick * px, pad_t + size - tick * px
            parts.append(f'<text x="{tx:.1f}" y="{pad_t + size + 14}" text-anchor="middle">{tick}</text>')
            parts.append(f'<text x="{ox - 6}" y="{ty + 4:.1f}" text-anchor="end">{tick}</text>')
        parts.append(f'<text x="{ox + size / 2}" y="{pad_t + size + 32}" text-anchor="middle">'
                     'travel time (min)</text>')
        parts.append(f'<text transform="translate({ox - 38},{pad_t + size / 2}) rotate(-90)" '
                     'text-anchor="middle">map distance ÷ s (min)</text>')
    parts.append(f'<text x="{pad_l}" y="{height - 4}" fill="#555">Darker = more pairs (log). '
                 'Dashed: map distance equals travel time. Orange: median per 5-minute band.</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


# ── report.md ───────────────────────────────────────────────────────
def _fmt(v, nd=3):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _lam_text(v) -> str:
    return "the real map" if v is None else f"λ̃ = {v:.3g}"


def _ff_text(r: dict) -> str:
    return "at the real map only" if r["only_real_map"] else f"down to {_lam_text(r['down_to'])}"


def _break_text(b: dict) -> str:
    kind, st, before, m = b["kind"], b["station"], b["before"], b["metres"]
    if kind == "nearer the start":
        a, z = b["minutes_from_start"]
        return f"{st} is {m:.0f} m nearer the start than {before} (table: {a} min from the start, {z} back)"
    if kind == "further from the end":
        a, z = b["minutes_to_end"]
        return f"{st} is {m:.0f} m further from the end than {before} (table: {a} min to the end, {z} back)"
    if kind == "turns back":
        return (f"after {before} the line turns back {m:.0f} m to {st} (a {b['map_turn_deg']:.0f}° turn on the "
                f"map, {b['real_turn_deg']:.0f}° on the real map)")
    return f"the hop to {st} crosses {b['crosses']}"


def report_md(rep: dict) -> str:
    h = rep["headlines"]
    p = rep["prep"]
    w0, wr = h["wembley_at_time_map"], h["wembley_real_map"]
    tc = h["tc_time_map"]
    tri = rep["triangles"]
    out = [f"# Stations-only London layout: build {rep['build']}, {rep['window']}", "",
           f"**{rep['notice']}**", "",
           f"Departure {rep['departure']}; tables valid until {rep['valid_until']}; table sha256 "
           f"`{rep['table_sha256'][:16]}…`; code `{rep['code']}`. Definitions: `transit/README.md`, "
           "\"Stations-only layout\".", ""]
    if h["expired"]:
        out += [f"**These tables expired on {rep['valid_until']}.** Rebuild them before relying on this "
                "layout (`transit/README.md`, \"Monthly rebuild\").", ""]
    out += ["## Headlines", ""]
    merges = "; ".join(" + ".join(m) for m in p["merges"]) or "none"
    left = "; ".join(f"group {g['group']} ({len(g['names'])} stations): {', '.join(g['names'])}"
                     for g in rep["left_out"])
    out += [f"- **Nodes:** {p['stations_usable']} usable stations → {p['nodes']} nodes "
            f"(merged under 50 m: {merges}). "
            + (f"**Left out**, not joined both ways to the rest: {left}." if left else "No group is left out."),
            f"- **Pairs:** {p['pairs_used']} used; {p['pairs_one_way_dropped']} dropped for having a time "
            f"one way only; {p['pairs_floored']} floored at 0.5 min. |t_ij − t_ji|: median "
            f"{p['asymmetry_min']['median']:.1f}, p90 {p['asymmetry_min']['p90']:.1f}, max "
            f"{p['asymmetry_min']['max']:.0f} min.",
            f"- **Scale:** s = {rep['s_m_per_min']:.1f} m of map per minute (median real distance ÷ "
            "median time).",
            f"- **Normalised stress (σ₁):** {_fmt(h['sigma1_real_map'])} on the real map, "
            f"{_fmt(h['sigma1_time_map'])} on the pure time map.",
            f"- **Trustworthiness / continuity (k = 10):** {_fmt(tc['T'])} / {_fmt(tc['C'])} on the pure time "
            f"map (range over seeds {_fmt(tc['T_range'][0])}–{_fmt(tc['T_range'][1])} / "
            f"{_fmt(tc['C_range'][0])}–{_fmt(tc['C_range'][1])}; scored over the {tc['covered']} of {tc['of']} "
            f"nodes with at least 10 neighbours that have a time), against the proposed line of {TC_PASS}: "
            f"**{'pass' if h['tc_pass'] else 'fail'}**. The real map scores {_fmt(h['tc_real_map']['T'])} / "
            f"{_fmt(h['tc_real_map']['C'])}, so this measure barely separates the two maps.",
            ]
    ffp, fff = h["fold_free"]["peeled"], h["fold_free"]["full"]
    islands = ffp["islands"] + [v for v in fff["islands"] if v not in ffp["islands"]]
    out.append(f"- **Folds** (a triangle that flips, or two boundary edges that cross): fold-free from the "
               f"real map {_ff_text(ffp)} on the peeled triangulation ({tri['kept']} of {tri['full']} "
               f"triangles, slivers and outer gaps on the boundary peeled off), and {_ff_text(fff)} on the full "
               f"triangulation. "
               + (f"Fold-free again after a fold: {', '.join(_lam_text(v) for v in islands)}. " if islands else "")
               + f"At most 1% of the area is folded down to {_lam_text(h['folded_area_1pct_down_to'])}.")
    if w0.get("status") in ("pass", "fail"):
        out.append(f"- **Wembley Park to Baker Street:** {w0['minutes_directed'][0]} / "
                   f"{w0['minutes_directed'][1]} min. On the pure time map its ratio of map distance to "
                   f"time is {w0['ratio_to_median']:.3f}× the median (tolerance ±{w0['tolerance']:.3f}, "
                   f"σ₁): **{w0['status']}**; {w0['map_m'] / 1000:.2f} km apart on the map. On the real "
                   f"map it is {wr['ratio_to_median']:.3f}× ({wr['status']}). It passes at "
                   + (", ".join(_lam_text(v) for v in h["wembley_passes_at"]) or "no λ") + ".")
    else:
        out.append(f"- **Wembley Park to Baker Street:** {w0.get('status')}.")
    if h["fold_free_and_wembley_pass"]:
        out.append("- **Fold-free (peeled) and passing the Wembley Park check:** "
                   + ", ".join(_lam_text(v) for v in h["fold_free_and_wembley_pass"]) + ".")
    else:
        out.append("- **No λ is both fold-free and passes the Wembley Park check.**")
    out.append(f"- **Metropolitan line order** on the pure time map: {h['metropolitan_breaks']} breaks over a "
               f"tolerance of one map-minute ({rep['s_m_per_min']:.0f} m), listed below"
               + (f"; ids not in this build: {', '.join(h['metropolitan_absent'])}" if h["metropolitan_absent"] else "")
               + ".")
    if h["labels_absent"]:
        out.append(f"- **Label ids not in this build:** {', '.join(h['labels_absent'])}.")
    r = rep["restart"]
    out.append("- **Restarts at λ = 0:** " + ", ".join(f"{k} σ₁ {v['sigma1']:.4f}" for k, v in r["starts"].items())
               + f" against the continuation's {r['continuation_sigma1']:.4f}: "
               + ("**a better start was found**." if r["better_start_found"] else "none is better by more than 0.1%."))
    out.append("- **Solves:** every solve converged and went down at every step (a build where one doesn't is "
               "refused)"
               + (f"; a reflection would fit better at {', '.join(_lam_text(v) for v in h['reflection_fits_better'])}"
                  if h["reflection_fits_better"] else "") + ".")

    out += ["", "## Every frame", "",
            "| Frame | σ₁ | T | C | Folds | Folded area | Boundary crossings | Folds (full) | "
            "Wembley ratio ÷ median | Met breaks | Iterations |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for f in rep["frames"]:
        wem = f["wembley"]
        ratio = f"{wem['ratio_to_median']:.3f} ({wem['status']})" if "ratio_to_median" in wem else wem.get("status")
        out.append(f"| {_lam_text(f['lam_tilde'])} | {f['sigma1']:.4f} | {f['T']:.3f} | {f['C']:.3f} | "
                   f"{f['folds']['folded']} | {f['folds']['area_share'] * 100:.1f}% | "
                   f"{f['folds']['boundary_crossings']} | {f['folds_full']['folded']} | {ratio} | "
                   f"{f['met_breaks']} | {f['iterations']} |")
    out += ["", "## Metropolitan line", "",
            "Each run is followed in its listed order. A break is a station nearer the run's first station "
            "than the one before it, or further from its last; the line turning back where the real line "
            "turns by at most 90°; or the line crossing itself. Departures under one map-minute "
            f"({rep['s_m_per_min']:.0f} m) are counted as small.", ""]
    for key, name in (("time_map", "Pure time map"), ("real_map", "Real map")):
        out.append(f"**{name}.**")
        out.append("")
        for run in rep["metropolitan"][key]:
            line = f"- {run['name']} ({run['stations']} stations): "
            line += ("in order." if run["in_order"] else "; ".join(_break_text(b) for b in run["breaks"]) + ".")
            if run["small"]:
                line += f" {run['small']} small departures."
            if run["absent"]:
                line += f" Absent: {', '.join(run['absent'])}."
            out.append(line)
        out.append("")
    out += ["## Files", "",
            "`layout.json` (and `layout.js`, the same for the viewer), `index.html` (open it from disk), "
            "`report.json`, `shepard.svg`, `pairs.npz` (directed minutes, the times used, the mask).", "",
            "Credits: " + "; ".join(rep["attribution"]) + ".", ""]
    return "\n".join(out)


# ── write everything ────────────────────────────────────────────────
def write(out: Path, b, pairs, frames, tri, tri_full, peeled, checks, lines_at, restart, bins,
          settings_used, code, checks_sha256: str | None = None, expired: bool = False,
          seconds: float = 0.0) -> dict:
    """Build every output in memory, check the viewer's size, then write. A refusal leaves
    nothing behind."""
    out = Path(out)
    doc, labels_absent = layout_doc(b, pairs, frames, tri, checks, code, checks_sha256)
    head = headlines(frames, lines_at, restart, pairs, labels_absent, expired)
    rep = {
        "schema": 1, "notice": doc["notice"], "build": doc["build"], "window": b.window,
        "departure": b.departure, "valid_until": doc["valid_until"], "table_sha256": b.table_sha256,
        "points_sha256": b.points_sha256, "checks_sha256": checks_sha256, "code": code,
        "attribution": doc["attribution"],
        "s_m_per_min": pairs.s, "settings": settings_used, "prep": pairs.counts, "left_out": pairs.left_out,
        "triangles": {"full": int(len(tri_full)), "peeled_off": int(peeled), "kept": int(len(tri))},
        "headlines": head, "restart": restart,
        "frames": [{"lam_tilde": f["lam_tilde"], "lam": f["lam"], "iterations": f["iterations"],
                    "converged": f["converged"], "rose": f["rose"], "monotone": f["monotone"],
                    "rotation_deg": f["rotation_deg"], "reflection_fits_better": f["reflection_fits_better"],
                    **f["metrics"]} for f in frames],
        "metropolitan": {"real_map": lines_at.get(0, []), "time_map": lines_at.get(len(frames) - 1, [])},
        "shepard": {k: {"pairs": v["pairs"], "median": v["median"]} for k, v in bins.items()},
    }
    rep = _r(rep)
    layout_text = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
    layout_js = "window.LAYOUT = " + layout_text + ";\n"
    gz = len(gzip.compress(layout_js.encode("utf-8"), mtime=0)) + sum(
        len(gzip.compress((VIEWER / n).read_bytes(), mtime=0)) for n in VIEWER_FILES)
    if gz > MAX_GZIP_BYTES:
        raise RefusedOutput(f"the viewer would be {gz:,} bytes gzipped, over the {MAX_GZIP_BYTES:,} limit; "
                            "nothing was written")
    md = report_md(rep) + f"\nRun time {seconds:.0f} s.\n"
    svg = shepard_svg(bins, f"Shepard plot, build {doc['build']} {b.window}")

    out.mkdir(parents=True, exist_ok=True)
    (out / "layout.json").write_text(layout_text + "\n", encoding="utf-8")
    (out / "layout.js").write_text(layout_js, encoding="utf-8")
    (out / "report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "report.md").write_text(md, encoding="utf-8")
    (out / "shepard.svg").write_text(svg, encoding="utf-8")
    np.savez_compressed(out / "pairs.npz", ids=np.array(pairs.ids), directed=pairs.directed, t=pairs.t,
                        used=pairs.used, xy=pairs.xy, lonlat=pairs.lonlat)
    for name in VIEWER_FILES:
        shutil.copyfile(VIEWER / name, out / name)

    w0 = head["wembley_at_time_map"]
    ffp, fff = head["fold_free"]["peeled"], head["fold_free"]["full"]
    summary = [
        f"σ₁ {head['sigma1_real_map']:.4f} (real map) -> {head['sigma1_time_map']:.4f} (time map); "
        f"T/C at λ=0 {head['tc_time_map']['T']:.3f}/{head['tc_time_map']['C']:.3f} "
        f"(real map {head['tc_real_map']['T']:.3f}/{head['tc_real_map']['C']:.3f})",
        f"fold-free from the real map {_ff_text(ffp)} (peeled), {_ff_text(fff)} (full); ≤1% folded down to "
        f"{_lam_text(head['folded_area_1pct_down_to'])}",
        "Wembley Park at λ=0: " + (f"{w0['status']} ({w0['ratio_to_median']:.3f}× median, ±{w0['tolerance']:.3f})"
                                   if "ratio_to_median" in w0 else str(w0.get("status"))),
        f"Metropolitan breaks at λ=0: {head['metropolitan_breaks']}; restart better: "
        f"{head['restart_better_found']}" + ("; THE TABLES HAVE EXPIRED" if expired else ""),
        f"wrote {out} (viewer {gz / 1e6:.2f} MB gzipped) in {seconds:.0f} s",
    ]
    return {"summary": summary, "gzip_bytes": gz, "headlines": head, "out": out}
