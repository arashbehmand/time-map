"""
Writes the full London time map's outputs (#6, L-02): timemap.json and land.bin (what the page
draws), the page itself, report.md and report.json, shepard.svg and panels.svg. Everything is
built in memory and checked (the land's rung, its Float32 orientation, the page's size) before a
file is written, so a refusal leaves nothing behind. All of it is derived from the transit
tables, so it goes only to a folder git ignores and the app doesn't ship (timemap.check_out_dir).
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np

from transit.layout import land as landmod
from transit.layout import measures, springs
from transit.layout import mesh as meshmod
from transit.layout.frame import ORIGIN
from transit.layout.prep import RefusedOutput
from transit.layout.report import _r, notice, run_text, shepard_svg

MET = "#9b0056"


def _rho_text(v) -> str:
    return "the real map" if not v else f"ρ = {v:.3g}"


def _lam_text(v) -> str:
    return "the real map" if v is None else f"λ̃ = {v:.3g}"


def _ratio_text(r: dict, strict: bool = False) -> str:
    if "ratio_to_median" not in r:
        return str(r.get("status"))
    s = f"{r['map_m'] / 1000:.2f} km, {r['ratio_to_median']:.3f}× ({r['status']})"
    if strict and "strict_status" in r:
        s += f", {r['strict_status']} against {r['strict_tolerance']:.3f}"
    return s


def _squeeze_text(f: dict) -> str:
    sq = f["squeezed"][0] if f["squeezed"] else None
    if sq is None or sq["ratio"] >= 1 - 1e-9:
        return "—"
    return f"{sq['ratio'] * 100:.1f}% ({sq['near']})"


# ── timemap.json ────────────────────────────────────────────────────
def frame_doc(f: dict) -> dict:
    """One solved position as the page reads it: decimetres, station stress and a few measures."""
    mt = f["metrics"]
    sq = mt["squeezed"][0] if mt["squeezed"] else None
    return {
        "rho": f["rho"], "x": f["dm"][:, 0].tolist(), "y": f["dm"][:, 1].tolist(),
        "stress": [round(float(v), 3) for v in f["stress"]],
        "metrics": _r({"sigma1": mt["sigma1"], "T": mt["T"], "C": mt["C"], "flipped": mt["folds"]["flipped"],
                       "area_ratio_min": mt["area_ratio_min"], "stretch_median": mt["stretch_median"],
                       "stretch_max": mt["stretch_max"],
                       "squeezed_near": sq["near"] if sq and sq["ratio"] < 1 - 1e-9 else None,
                       "met_breaks": mt["met_breaks"], "iterations": mt["iterations"],
                       "ratios": {k: {"status": v.get("status"), "ratio": v.get("ratio_to_median"),
                                      "km": (v["map_m"] / 1000) if "map_m" in v else None,
                                      "tolerance": v.get("tolerance")}
                                  for k, v in mt["ratios"].items()}})}


def page_doc(b, pairs, mesh, links, model, frames, checks, routes, code, checks_sha256, input_sha) -> dict:
    labels = []
    for sid in checks.get("labels", {}).get("stations", []):
        i = pairs.index_of(sid)
        if i is not None and i not in labels:
            labels.append(i)
    from transit.layout.timemap import ratio_list
    rows = []
    for r in ratio_list(checks):
        i, j = pairs.index_of(r["station"]), pairs.index_of(r["reference"])
        rows.append({"key": r["key"], "name": r["name"], "station": i, "reference": j})
    stress_all = np.concatenate([f["stress"] for f in frames])
    top = max(0.05, math.ceil(float(np.percentile(stress_all, 98)) * 20) / 20)
    out_frames = [frame_doc(f) for f in frames]
    st = mesh.kinds == meshmod.STATION
    return {
        "schema": 2, "kind": "london-time-map", "notice": notice(b.manifest, "L-02"),
        "build": b.manifest.get("build"), "window": b.window, "departure": b.departure,
        "valid_until": b.manifest.get("valid_until"), "table_sha256": b.table_sha256,
        "points_sha256": b.points_sha256, "checks_sha256": checks_sha256, "inputs_sha256": input_sha, "code": code,
        "attribution": list(b.manifest.get("attribution", [])),
        "frame": {"kind": "local", "origin": list(ORIGIN), "units": "dm", "axes": "x east, y south"},
        "s_m_per_min": _r(pairs.s),
        "model": model_settings(model),
        "nodes": {"kind": mesh.kinds.tolist(), "stations": int(st.sum()), "cells": mesh.n_cell, "ring": mesh.n_ring,
                  "id": pairs.ids, "label": pairs.labels, "name": pairs.names},
        "triangles": mesh.tri.ravel().tolist(), "links": links.pairs.ravel().tolist(),
        "weight_scale": meshmod.WEIGHT_Q,
        "labels": labels,
        "lines": [{"name": r["name"], "nodes": r["nodes"], "c": r["c"].ravel().tolist(), "w": r["w"].ravel().tolist()}
                  for r in routes],
        "ratios": rows, "stress_scale": [0.0, top],
        "frames": out_frames, "land": None,
    }


def model_settings(m) -> dict:
    return {"energy": "[Σ a·ψ(l/l0) + ρ·Σ b·ψ₊(l/(s·t)) + Σ A0·(κ·φ(J) + κs·μ(F))] ÷ (ΣA0 / free nodes)",
            "psi": "ψ(λ) = ½(λ² − 1) − ln λ, and ψ₊(λ) = ψ(λ) for λ ≥ 1, else 0 (train springs only pull)", "phi": "φ(J) = ½(J + 1/J) − 1, J = A/A0",
            "mu": "μ(F) = ‖F‖²/J − 2, F the triangle's map from the real map",
            "grid_weight": "a third of the real area of the triangles on the edge",
            "link_weight": "real length × median mesh edge", "kappa": m.kappa, "kappa_shape": m.kappa_shape,
            "rhos": list(springs.RHOS),
            "tol_gradient": springs.TOL, "max_iter": springs.MAX_ITER, "step_cap": springs.STEP_CAP,
            "subdivide_levels": springs.SUBDIVIDE, "median_edge_m": _r(m.scale),
            "cells": meshmod.N_CELLS, "cell_min_m": meshmod.CELL_MIN_M, "ring_n": meshmod.RING_N,
            "ring_margin_m": meshmod.RING_MARGIN_M}


# ── panels.svg ──────────────────────────────────────────────────────
def _ratio_colour(r: float) -> str:
    """Squeezed (r < 1) toward orange-red, stretched (r > 1) toward blue, on a log scale."""
    v = math.log2(max(r, 1e-6))
    if v < 0:
        t = min(1.0, -v / 4.0)
        lo, hi = (255, 255, 255), (215, 72, 39)
    else:
        t = min(1.0, v / 2.0)
        lo, hi = (255, 255, 255), (66, 120, 190)
    c = [round(a + (b - a) * t) for a, b in zip(lo, hi)]
    return "#%02x%02x%02x" % tuple(c)


def panel_picks(frames, wembley) -> list[tuple[int, str]]:
    """The real map, the first position where Wembley Park passes (or the closest), and the end of
    the slider; a position that is two of these is one panel with both titles."""
    end = len(frames) - 1
    want = [(0, "The real map")]
    if wembley.get("status") == "absent":
        mid = end // 2
        want.append((mid, f"Middle of the slider, {_rho_text(frames[mid]['rho'])}"))
    else:
        p = wembley["pick"]
        want.append((p, f"{'First Wembley Park pass' if wembley['pick_is_pass'] else 'Closest to a Wembley Park pass'}"
                        f", {_rho_text(frames[p]['rho'])}"))
    want.append((end, f"End of the slider, {_rho_text(frames[end]['rho'])}"))
    out: list[tuple[int, str]] = []
    for k, title in want:
        if out and out[-1][0] == k:
            out[-1] = (k, f"{out[-1][1]}; {title[0].lower() + title[1:]}")
        else:
            out.append((k, title))
    return out


def panels_svg(pairs, mesh, frames, picks: list[tuple[int, str]], coarse, routes, checks, title: str) -> str:
    """Panels at one scale, north up (local y points south, as SVG's does): the mesh's inner
    triangles shaded by area ratio to the real map, the water and borough outlines and the
    Metropolitan line carried through the mesh, the stations, and the Wembley stations named."""
    size, pad, gap, top_pad = 420, 16, 18, 70
    width = pad * 2 + len(picks) * size + (len(picks) - 1) * gap
    height = top_pad + size + 64
    inner_nodes = mesh.kinds != meshmod.RING
    inner_tri = ~(mesh.kinds[mesh.tri] == meshmod.RING).any(axis=1)
    pts = np.concatenate([frames[i]["Xs"][inner_nodes] for i, _ in picks])
    x0, y0 = pts.min(axis=0)
    x1, y1 = pts.max(axis=0)
    span = max(x1 - x0, y1 - y0) * 1.04
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    sc = size / span
    ex0, ey0 = cx - span / 2, cy - span / 2
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}" font-family="system-ui, sans-serif" font-size="11">',
             f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
             f'<text x="{pad}" y="20" font-size="14" font-weight="600">{title}</text>',
             f'<text x="{pad}" y="38" fill="#444">North up, one scale. Triangles shaded by area against the real '
             'map (orange squeezed, blue stretched); a flipped triangle would be red. '
             f'<tspan fill="{MET}">Metropolitan line</tspan> carried through the mesh.</text>']
    labels = {}
    w = checks.get("wembley", {})
    for sid in (w.get("station"), w.get("reference")):
        i = pairs.index_of(sid or "")
        if i is not None:
            labels[i] = pairs.labels[i]
    for r in checks.get("ratio", []):
        for sid in (r["station"], r["reference"]):
            i = pairs.index_of(sid)
            if i is not None:
                labels[i] = pairs.labels[i]
    st_idx = np.where(mesh.kinds == meshmod.STATION)[0]
    for k, (fi, name) in enumerate(picks):
        X = frames[fi]["Xs"]
        ox = pad + k * (size + gap)
        oy = top_pad

        def P(xy, ox=ox, oy=oy):
            return ox + (xy[..., 0] - ex0) * sc, oy + (xy[..., 1] - ey0) * sc

        clip = f"clip{k}"
        parts.append(f'<clipPath id="{clip}"><rect x="{ox}" y="{oy}" width="{size}" height="{size}"/></clipPath>')
        parts.append(f'<g clip-path="url(#{clip})">')
        r = meshmod.area_ratios(mesh, X)
        tx, ty = P(X[mesh.tri])
        for t in np.where(inner_tri)[0]:
            fill = "#e0001b" if r[t] <= 0 else _ratio_colour(float(r[t]))
            parts.append(f'<polygon points="{tx[t, 0]:.1f},{ty[t, 0]:.1f} {tx[t, 1]:.1f},{ty[t, 1]:.1f} '
                         f'{tx[t, 2]:.1f},{ty[t, 2]:.1f}" fill="{fill}" stroke="#d8d8d8" stroke-width="0.3"/>')
        if coarse is not None:
            fx, fy = P(landmod.place(coarse.fill_c, coarse.fill_w, X))
            tris = landmod.fill_triangles(coarse)
            if len(tris):
                d = "".join(f"M{fx[a]:.1f} {fy[a]:.1f}L{fx[b]:.1f} {fy[b]:.1f}L{fx[c]:.1f} {fy[c]:.1f}Z"
                            for a, b, c in tris.tolist())
                parts.append(f'<path d="{d}" fill="#7fb6d6" stroke="none"/>')
            if len(coarse.line_nv):
                lx, ly = P(landmod.place(coarse.line_c, coarse.line_w, X))
                parts.append(f'<path d="{" ".join(_poly_d(lx, ly, coarse.line_nv))}" fill="none" stroke="#c49a5c" '
                             'stroke-width="0.8"/>')
        sx, sy = P(X[st_idx])
        dots = "".join(f"M{x - 1.2:.1f} {y:.1f}a1.2 1.2 0 1 0 2.4 0a1.2 1.2 0 1 0 -2.4 0" for x, y in zip(sx, sy))
        parts.append(f'<path d="{dots}" fill="#333"/>')
        for route in routes:
            mx, my = P(landmod.place(route["c"], route["w"], X))
            parts.append(f'<polyline points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in zip(mx, my))}" '
                         f'fill="none" stroke="{MET}" stroke-width="2" stroke-linejoin="round" class="metropolitan"/>')
        # each labelled station gets its own corner, so stations a few hundred metres apart
        # (Baker Street and Marylebone, the two Wembleys) don't print over each other
        corners = ((6, -5, "start"), (6, 12, "start"), (-6, -5, "end"), (-6, 12, "end"))
        for k_lab, (i, text) in enumerate(labels.items()):
            px, py = P(X[i])
            dx, dy, anchor = corners[k_lab % len(corners)]
            parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" fill="{MET}" stroke="#fff" '
                         f'data-station="{pairs.ids[i]}"/>')
            parts.append(f'<text x="{px + dx:.1f}" y="{py + dy:.1f}" text-anchor="{anchor}" font-size="10" '
                         f'paint-order="stroke" stroke="#fff" stroke-width="3">{text}</text>')
        parts.append("</g>")
        parts.append(f'<rect x="{ox}" y="{oy}" width="{size}" height="{size}" fill="none" stroke="#999"/>')
        parts.append(f'<text x="{ox}" y="{oy - 8}" font-weight="600" class="panel">{name}</text>')
        bar = 5000 * sc
        parts.append(f'<line x1="{ox + 10}" y1="{oy + size - 12}" x2="{ox + 10 + bar:.1f}" y2="{oy + size - 12}" '
                     'stroke="#111" stroke-width="2"/>')
        parts.append(f'<text x="{ox + 10}" y="{oy + size - 18}">5 km</text>')
        parts.append(f'<text x="{ox + size - 14}" y="{oy + 18}" text-anchor="middle" font-weight="600">N</text>')
        parts.append(f'<path d="M{ox + size - 14} {oy + 22}l-4 8h8z" fill="#111"/>')
    legend_y = top_pad + size + 26
    for j, v in enumerate((0.05, 0.25, 0.5, 1.0, 2.0, 4.0)):
        parts.append(f'<rect x="{pad + j * 58}" y="{legend_y}" width="18" height="12" fill="{_ratio_colour(v)}" '
                     'stroke="#999" stroke-width="0.5"/>')
        parts.append(f'<text x="{pad + j * 58 + 22}" y="{legend_y + 10}">{v:g}×</text>')
    parts.append(f'<text x="{pad + 6 * 58 + 8}" y="{legend_y + 10}" fill="#444">a triangle\'s area against the '
                 'real map</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _poly_d(x, y, runs) -> list[str]:
    out, s = [], 0
    for nv in runs.tolist():
        out.append("M" + "L".join(f"{x[j]:.1f} {y[j]:.1f}" for j in range(s, s + nv)))
        s += nv
    return out


# ── report.md ───────────────────────────────────────────────────────
def report_md(rep: dict) -> str:
    h = rep["headlines"]
    frames = rep["frames"]
    out = [f"# Full London time map: build {rep['build']}, {rep['window']}", "", f"**{rep['notice']}**", "",
           f"Departure {rep['departure']}; tables valid until {rep['valid_until']}; table sha256 "
           f"`{rep['table_sha256'][:16]}…`; code `{rep['code']}`. Definitions: `transit/README.md`, "
           "\"Full London time map\".", ""]
    if h["expired"]:
        out += [f"**These tables expired on {rep['valid_until']}.** Rebuild them before relying on this map.", ""]
    mc, lc, st = rep["mesh"], rep["links"], rep["settings"]
    out += ["## Headlines", "",
            f"- **The grid:** {mc['stations']} station nodes and {mc['cells']} H3 cell centres between them, joined by "
            f"their Delaunay triangulation ({mc['inner_triangles']} triangles), and {mc['ring']} fixed guard nodes on a "
            f"ring {mc['ring_margin_m'] / 1000:.0f} km outside, joined to its hull by {mc['triangles'] - mc['inner_triangles']} "
            "more (never drawn, no land). "
            f"{mc['grid_springs']} grid springs. **{lc['links']} train links** from the {lc['feed']} feed "
            f"({lc['station_trips_on_day']} rail, tube, DLR and tram trips on {lc['weekday']}, hops leaving "
            f"{lc['window'][0]}–{lc['window'][1]})"
            + (f"; stations with no link: {', '.join(lc['nodes_without_link'])}" if lc["nodes_without_link"] else "")
            + ".",
            f"- **Model:** {st['energy']}; {st['psi']}; {st['phi']}; {st['mu']}; κ = {st['kappa']:g}, κs = "
            f"{st['kappa_shape']:g}. ψ and φ resist springs and triangles shrinking, μ resists triangles going thin "
            "(it is zero for any rotation or uniform scaling); all three are infinite at collapse. The slider is ρ, "
            f"the train springs' strength against the grid: the real map (ρ = 0), then {len(frames) - 1} values from "
            f"{frames[1]['rho']:g} to {frames[-1]['rho']:g}, each solved from the one before. Scale s = "
            f"{rep['s_m_per_min']:.1f} m of map per minute, as in the stations-only layout.",
            f"- **Flipped triangles: {h['flipped_max']} at every one of the {len(frames)} positions**, checked on the "
            "coordinates as shipped (whole decimetres) and on their Float32 image, with the guard ring unmoved, so the "
            "mesh and the land carried on it never fold. "
            + (f"{h['land_slivers_dropped']} land slivers that would come within "
               f"{landmod.BLEND_MARGIN_M * 100:g} cm of turning over, somewhere along the motion as the page "
               "draws it in Float32, were left out. " if h.get("land_slivers_dropped") else "")
            + f"The stations-only layout flipped {h['l01_folds_first_pass']} of {h['l01_triangles']} triangles at its "
            f"first Wembley pass and {h['l01_folds_time_map']} on its pure time map.",
            f"- **Train springs only pull.** A train link says its stations are at most s·t apart. s·t is longer "
            f"than the real distance for {h['links_longer_share'] * 100:.0f}% of the links (median s·t ÷ real "
            f"{h['links_rest_over_real']:.2f}): a table time between neighbours includes getting to the platform and "
            "waiting, at the all-pairs scale. Those links act only if their stations end up further apart than s·t; "
            "springs that pushed would make chains of them buckle into zigzags, with no room in the grid for a line "
            "twice its length. The fast links (Wembley Park – Finchley Road among them) pull places together. "
            f"At the end of the slider {h['links_past_end'] * 100:.0f}% of the links are more than 1% past their s·t."]
    wa = rep["wembley"]
    wp_end = h["wembley_end"]
    if wa.get("status") == "absent":
        out.append("- **Wembley Park to Baker Street:** absent from this build.")
    else:
        first = wa["first_pass"]
        if first is not None:
            f = frames[first]["ratios"]["wembley_park"]
            out.append(f"- **Wembley Park to Baker Street:** {wp_end['minutes_directed'][0]} / "
                       f"{wp_end['minutes_directed'][1]} min. It first passes at {_rho_text(frames[first]['rho'])}"
                       f" ({_ratio_text(f)}, tolerance ±{f['tolerance']:.3f}, the position's σ₁), and passes at "
                       f"{len(wa['passes'])} of {len(frames)} positions. At the end of the slider it is "
                       f"{_ratio_text(wp_end, True)} (the strict line is the stations-only pure time map's σ₁). "
                       f"The real map has it at {_ratio_text(frames[0]['ratios']['wembley_park'])}.")
        else:
            f = frames[wa["pick"]]["ratios"]["wembley_park"]
            out.append(f"- **Wembley Park to Baker Street: no position passes.** Closest: "
                       f"{_rho_text(frames[wa['pick']]['rho'])}, {_ratio_text(f)}, {wa['gap']:.3f} outside "
                       f"the tolerance of ±{f['tolerance']:.3f}. See \"What shapes Wembley Park\" below.")
    for r in frames[-1]["ratios"].values():
        if r["key"] == "wembley_park":
            continue
        r0 = frames[0]["ratios"].get(r["key"], {})
        passes = sum(f["ratios"][r["key"]].get("status") == "pass" for f in frames)
        out.append(f"- **{r['name']}:** " + (
            f"{r['minutes_directed'][0]} / {r['minutes_directed'][1]} min; {_ratio_text(r0)} on the real map, "
            f"{_ratio_text(r, True)} at the end; passes at {passes} of {len(frames)} positions."
            if "ratio_to_median" in r else f"{r.get('status')}."))
    sw = h["stretch_worst_end"]
    out += [f"- **Metropolitan line order:** {h['met_breaks_real']} breaks on the real map, {h['met_breaks_end']} at "
            f"the end (the stations-only pure time map: {h['l01_met_breaks']}), over a tolerance of one map-minute "
            f"({rep['s_m_per_min']:.0f} m); listed below.",
            f"- **Normalised stress (σ₁):** {h['sigma1_real']:.3f} on the real map, {h['sigma1_end']:.3f} at the end, "
            f"lowest {h['sigma1_min']:.3f} (stations-only: {h['l01_sigma1_real']:.3f} → {h['l01_sigma1_time']:.3f}). "
            "The springs pull only along train links, so σ₁ over every pair of stations is not what this map "
            "minimises, and pairs no train joins are held only by the grid.",
            f"- **Trustworthiness / continuity (k = 10):** {h['T_end']:.3f} / {h['C_end']:.3f} at the end, "
            f"{h['T_real']:.3f} / {h['C_real']:.3f} on the real map (stations-only pure time map: "
            f"{h['l01_T_time']:.3f} / {h['l01_C_time']:.3f}).",
            f"- **Most squeezed:** at the end the smallest triangle keeps {h['squeeze_end']['ratio'] * 100:.1f}% of its "
            f"real area, near {h['squeeze_end']['near']} ({h['squeeze_end']['lat']:.4f}, {h['squeeze_end']['lon']:.4f}); "
            f"{h['under_5pct_end']} triangles keep under 5% and {h['under_25pct_end']} under 25%. Triangles bunch up "
            "under a strong pull and never reach zero.",
            f"- **How thin triangles get:** at the end the median triangle is stretched {h['stretch_median_end']:.2f}:1 "
            f"(larger over smaller scale) and the thinnest {sw['value']:.0f}:1, near {sw['near']}; "
            f"{h['stretched_4_end'] * 100:.0f}% of the area is past 4:1 (at the first pass: "
            f"{h['stretched_4_pick'] * 100:.1f}%). "
            + ("At no position is more than a tenth of the area past 4:1. " if h["stretched_4_never"] else
               f"Past about {_rho_text(h['readable_until'])} more than a tenth of the area is past 4:1 and lines "
               "kink where they cross triangle edges. ")
            + "Every triangle statistic here covers "
            "the triangles inside the stations' and cells' hull (the drawn map), not the guard ring's.",
            f"- **The end of the slider is where the ladder stops, not a limit:** from {_rho_text(frames[-2]['rho'])} to "
            f"{_rho_text(frames[-1]['rho'])} the median node still moves {frames[-1]['move_median_m']:.0f} m. "
            + _restart_text(rep.get("restart"), frames),
            "- **Station triangles bent:** the stations-only layout's fold count, taken on its own station "
            f"triangulation, is {h['l01_style_end'][0]} / {h['l01_style_end'][1]} (peeled / full) at the end of this "
            "slider. On this map those come from bending, not folding: the mesh map is one to one, but a straight "
            "line between two stations isn't straight once the map bends.",
            f"- **Solves:** every position converged (|gradient| ≤ {st['tol_gradient']:g}), went down at every accepted "
            f"step, and {'has a positive definite Hessian (a strict local minimum)' if h['pd_all'] else 'has a Hessian that is not positive definite at ' + ', '.join(_rho_text(v) for v in h['not_pd'])}"
            f"; {h['iterations_total']} Newton steps in all"
            + (f", {h['substeps_total']} sub-steps" if h["substeps_total"] else "")
            + f". The largest rotation against the real map is {h['rotation_max_deg']:.2g}° (the ring holds it).",
            "- **The motion between positions is unfolded too.** The page slides smoothly by blending neighbouring "
            "positions in a straight line, and a blend of two unfolded layouts can fold on the way, so every blend is "
            "checked exactly (each triangle's area is a quadratic along it). "
            + (f"{h['motion']['inserted']} in-between positions were solved and added where one would have folded, "
               "and the position after each split step was solved from them (so it can differ from a direct solve; "
               "the restart check's other ladders measure that)" if h["motion"]["inserted"] else
               "No step needed a position in between")
            + "".join(f"; from {_rho_text(x['from'])} to {x['rho']:.4g} the layout jumps ({x['station']} moves "
                      f"{x['move_m'] / 1000:.1f} km in "
                      + (f"a step of ρ of {(x['rho'] / x['from'] - 1) * 100:.1f}%" if x["from"] > 0 else "the first step")
                      + ") and no solved position in between blends across it, so the page follows the solver's own "
                      "steps there, each unfolded all along"
                      for x in h["motion"]["jumps"])
            + f". All {h['motion']['blends']} blends keep every triangle at "
            f"{math.floor(h['motion']['min_area_ratio_along_blends'] * 1000) / 10:.1f}% of its real area or more, so "
            "every point on the slider is one to one.", ""]

    out += ["## Beside the stations-only layout (L-01)", "",
            "Both solved in this run from the same tables. The first fold row is each map's own triangulation (L-01: "
            "its station triangulation, peeled / full; L-02: its mesh of stations, cells and the ring). The second "
            "is the stations-only triangulation for both, so the two columns compare like for like.", ""]
    cols = rep["compare"]["columns"]
    out.append("| | " + " | ".join(c["title"] for c in cols) + " |")
    out.append("|---|" + "---:|" * len(cols))
    for row in rep["compare"]["rows"]:
        out.append(f"| {row['name']} | " + " | ".join(row["values"]) + " |")
    out.append("")

    if wa.get("status") != "absent":
        out += _wembley_md(wa, frames)

    names = [r["name"].replace("Wembley Stadium", "Stadium") for r in rep["ratio_rows"][1:]]
    out += ["## Every position", "",
            "| Position | σ₁ | T | C | Flipped | Station triangles bent | Smallest area (near) | Thinness median / max | "
            f"Wembley Park | vs {h['l01_sigma1_time']:.3f} | " + " | ".join(names) + " | Met breaks | Links past s·t / p90 past | Median move | "
            "Iterations | Positive definite |",
            "|---|---:|---:|---:|---:|---:|---|---|---|---|" + "---|" * len(names) + "---:|---:|---:|---:|---|"]
    for f in frames:
        wpk = f["ratios"].get("wembley_park", {})
        others = " | ".join(_ratio_text(f["ratios"][r["key"]]) for r in rep["ratio_rows"][1:])
        out.append(f"| {_rho_text(f['rho'])} | {f['sigma1']:.4f} | {f['T']:.3f} | {f['C']:.3f} | "
                   f"{f['folds']['flipped']} | {f['folds_l01']['peeled']['folded']} / {f['folds_l01']['full']['folded']} | "
                   f"{_squeeze_text(f)} | {f['stretch_median']:.2f} / {f['stretch_max']:.1f} | {_ratio_text(wpk)} | "
                   f"{wpk.get('strict_status', '—')} | {others} | {f['met_breaks']} | "
                   f"{f['links_past_share'] * 100:.1f}% / {f['links_past_p90'] * 100:.1f}% | "
                   f"{f['move_median_m']:.0f} m | {f['iterations']} | {'yes' if f['pd'] else 'no'} |")
    out += ["", "## Metropolitan line", "",
            "Each run is followed in its listed order (the same test as the stations-only layout, on the straight "
            "lines between stations). Departures under one map-minute "
            f"({rep['s_m_per_min']:.0f} m) are counted as small.", ""]
    for key, name in rep["metropolitan_order"]:
        out += [f"**{name}.**", ""]
        out += [run_text(r) for r in rep["metropolitan"][key]]
        out.append("")
    out += ["## Train links", "",
            f"{lc['links']} links from {lc['hops_read']} consecutive-stop hops of {lc['station_trips_on_day']} trips. "
            "Dropped hops, each under the first reason that applies: "
            + "; ".join(f"{k} {v}" for k, v in lc["dropped"].items()) + ". "
            + "Links by route: " + ", ".join(f"{k} {v}" for k, v in sorted(lc["links_by_route"].items())
                                             if k in ("tfl_MET", "CH") or k.endswith("MET")) + ".", ""]
    if rep.get("land"):
        ld = rep["land"]
        c = ld["counts"]
        out += ["## Land", "",
                f"From the build's OSM clip: {ld['source_counts']['water']} water, {ld['source_counts']['park']} park "
                f"and {ld['source_counts']['rail']} rail features, the 33 boroughs. Water and parks dissolved, then "
                f"those of {ld['min_ha']:g} ha or more kept, simplified at {ld['tol_m']:g} m, and cut along the mesh "
                f"into {c['fill_pieces']} fill pieces ({c['fill_triangles']} triangles) and {c['line_runs']} line "
                "runs. Every vertex is stored by mesh nodes and weights, a point on a mesh edge by that edge alone, so "
                "neighbouring pieces meet exactly on every layout. Page "
                f"{rep['page_gzip_bytes'] / 1e6:.2f} MB gzipped (the limit is 2 MB).", ""]
    else:
        out += ["## Land", "", f"Not drawn in this run (--no-land). Page {rep['page_gzip_bytes'] / 1e6:.2f} MB gzipped.", ""]
    out += ["## Files", "",
            "`timemap.json` and `land.bin`: what the app's Whole city page draws (`static/whole-city/`). Run the "
            "app with `TIMEMAP_DIR` set to this build's folder (the one above this window's) and open "
            "`/whole-city/`; it is shown only while the tables it was made from may be. `report.json`, "
            "`shepard.svg`, `panels.svg`: this report's data and figures.",
            "", "Credits: " + "; ".join(rep["attribution"]) + ".", ""]
    return "\n".join(out)


def _restart_text(restart, frames) -> str:
    if not restart:
        return "The restart check didn't run."
    parts = []
    for _, v in sorted(restart["at"].items(), key=lambda kv: int(kv[0])):
        if not v["runs"]:
            parts.append(f"at {_rho_text(v['rho'])} no run converged")
            continue
        km = [r["wembley_km"] for r in v["runs"]]
        sig = [r["sigma1"] for r in v["runs"]]
        mb = [r["met_breaks"] for r in v["runs"]]
        far = (f"{v['shift_max_m'] / 1000:.2f} km ({v['shift_at']}, {v['shift_run']})" if v["shift_max_m"] >= 1000
               else f"{v['shift_max_m']:.1f} m" + (f" ({v['shift_at']})" if v["shift_at"] else ""))
        parts.append(f"at {_rho_text(v['rho'])} Wembley Park ends {min(km):.2f}–{max(km):.2f} km "
                     f"({', '.join(sorted(v['statuses']))}), σ₁ {min(sig):.3f}–{max(sig):.3f}, Met breaks "
                     f"{min(mb)}–{max(mb)}, and no node lands more than {far} from its shipped place")
    note = "" if restart["complete"] else " (a run that didn't converge is left out)"
    tested = [v for v in restart["at"].values() if v["runs"]]
    steady = bool(tested) and all(v["shift_max_m"] <= 1.0 for v in tested)
    tail = (" No run converged, so these layouts weren't tested." if not tested else
            " These layouts depend neither on where the continuation starts nor on the size of its steps."
            if steady else " Numbers there belong to this path, not to the model alone: another start or other "
            "steps reach a different layout.")
    return (f"Solved again from the real map nudged by up to {restart['jitter_m']:g} m ({len(restart['seeds'])} "
            f"seeds, the same steps: a test of the start) and through other ladders of ρ "
            f"({'; '.join(restart['ladders'])}: a test of the step size){note}: " + "; ".join(parts) + "." + tail)


def _wembley_md(wa, frames) -> list[str]:
    out = ["## What shapes Wembley Park", ""]
    ch = wa["chain"]
    head = (f"Its table time to its reference is {frames[0]['ratios']['wembley_park']['minutes']:.1f} min, "
            f"{wa['direct_rest_m'] / 1000:.2f} km at the layout's scale, but no train joins them directly. ")
    if ch["stations"] and ch["metres"] is not None:
        out.append(head + "The springs that pull them together are the chain "
                   f"{' – '.join(ch['stations'])}, {ch['metres'] / 1000:.2f} km of rest length. That is the longest the "
                   "chain allows with every hop at rest: the two get closer than that when the chain bends at its "
                   "middle station or a hop is squeezed below its rest, and the grid and the stations' other links "
                   "decide how much.")
    else:
        out.append(head + "No chain of train links joins them either, so only the grid and the links of the "
                   "places around them move them toward each other.")
    out.append("")
    for key, label in (("at_pick", "its first pass" if wa["pick_is_pass"] else "its closest"), ("at_end", "the end")):
        a = wa[key]
        out.append(f"- At {_rho_text(a['rho'])} ({label}): {a['chord_m'] / 1000:.2f} km apart. "
                   + "; ".join(f"{hp['from']} – {hp['to']} {hp['map_m'] / 1000:.2f} km on the map against "
                               f"{hp['rest_m'] / 1000:.2f} km rest ({hp['real_m'] / 1000:.2f} km real)" for hp in a["hops"])
                   + ("; the chain turns " + ", ".join(f"{b['deg']:.0f}° at {b['at']}" for b in a["bends"])
                      if a["bends"] else "")
                   + f". The {wa['corridor_triangles']} triangles the real-map line between them crosses keep "
                   f"{a['corridor_area_median'] * 100:.0f}% of their area at the median ({a['corridor_area_min'] * 100:.0f}% "
                   f"at least) and are stretched up to {a['corridor_stretch_max']:.1f}:1.")
    trend = ", ".join(f"{v:.3f}" for v in wa["end_trend"])
    out.append(f"- Its ratio to the median over the last three positions: {trend}.")
    for o in wa.get("others", []):
        ref_txt = "a train link" if o["linked_to_reference"] else "no train link"
        rk = f", rest {o['rest_km']:.2f} km" if o["rest_km"] is not None else ""
        out.append(f"- {o['name']}: {ref_txt} between them{rk}; its links go to {', '.join(o['links']) or 'nothing'}. "
                   f"Map distance to its reference {o['to_reference_km'][0]:.2f} / {o['to_reference_km'][1]:.2f} / "
                   f"{o['to_reference_km'][2]:.2f} km and to Wembley Park {o['to_wembley_park_km'][0]:.2f} / "
                   f"{o['to_wembley_park_km'][1]:.2f} / {o['to_wembley_park_km'][2]:.2f} km (real map / "
                   f"{'first pass' if wa['pick_is_pass'] else 'closest'} / end)."
                   + ("" if o["linked_to_reference"] else
                      " Nothing in the energy holds it away from that reference: the grid carries it along with its "
                      "neighbours, so it can end too close."))
    out.append("")
    return out


def compare_table(rep_frames, l01, wa) -> dict:
    """The comparison columns: L-01 at the real map, its first Wembley pass and λ = 0; L-02 at
    the real map, its first pass (or closest) and the end."""
    lf = l01["frames"]
    first = wa.get("l01_first_pass") if isinstance(wa, dict) else None
    cols = [("L-01 real map", "l01", 0)]
    if first is not None:
        cols.append((f"L-01 first pass ({_lam_text(lf[first]['lam_tilde'])})", "l01", first))
    cols.append(("L-01 pure time map (λ = 0)", "l01", len(lf) - 1))
    cols.append(("L-02 real map", "l02", 0))
    if wa.get("status") != "absent" and wa["pick"] not in (0, len(rep_frames) - 1):
        p = wa["pick"]
        cols.append((f"L-02 {'first pass' if wa['pick_is_pass'] else 'closest'} ({_rho_text(rep_frames[p]['rho'])})",
                     "l02", p))
    cols.append((f"L-02 end ({_rho_text(rep_frames[-1]['rho'])})", "l02", len(rep_frames) - 1))

    def get(kind, i):
        if kind == "l01":
            m = lf[i]["metrics"]
            st = f"{m['folds']['folded']} / {m['folds_full']['folded']}"
            return {"sigma1": m["sigma1"], "T": m["T"], "C": m["C"], "own": st, "own_area": m["folds"]["area_share"],
                    "stations": st, "st_area": m["folds"]["area_share"], "wem": m["wembley"], "ratios": m["ratios"],
                    "met": m["met_breaks"], "shift": m["shift_median_m"]}
        m = rep_frames[i]
        pl = m["folds_l01"]
        return {"sigma1": m["sigma1"], "T": m["T"], "C": m["C"], "own": str(m["folds"]["flipped"]),
                "own_area": m["folds"]["area_share"],
                "stations": f"{pl['peeled']['folded']} / {pl['full']['folded']}", "st_area": pl["peeled"]["area_share"],
                "wem": m["ratios"].get("wembley_park", {}), "ratios": m["ratios"], "met": m["met_breaks"],
                "shift": m["shift_median_m"]}

    data = [get(k, i) for _, k, i in cols]
    keys = [k for k in (data[0]["ratios"] or {}) if k != "wembley_park"]
    rows = [{"name": "σ₁", "values": [f"{d['sigma1']:.3f}" for d in data]},
            {"name": "T / C (k = 10)", "values": [f"{d['T']:.3f} / {d['C']:.3f}" for d in data]},
            {"name": "Flipped triangles of its own triangulation", "values": [d["own"] for d in data]},
            {"name": "Folded area of its own triangulation", "values": [f"{d['own_area'] * 100:.1f}%" for d in data]},
            {"name": "Station-triangulation triangles turned (peeled / full)", "values": [d["stations"] for d in data]},
            {"name": "Wembley Park to Baker Street", "values": [_ratio_text(d["wem"]) for d in data]}]
    for k in keys:
        rows.append({"name": data[0]["ratios"][k]["name"], "values": [_ratio_text(d["ratios"].get(k, {})) for d in data]})
    rows += [{"name": "Metropolitan breaks", "values": [str(d["met"]) for d in data]},
             {"name": "Median station shift", "values": [f"{d['shift'] / 1000:.2f} km" for d in data]}]
    return {"columns": [{"title": t, "kind": k, "frame": i} for t, k, i in cols], "rows": rows}


def _readable_until(rep_frames, share: float = 0.1) -> float:
    """The last ρ at which at most `share` of the drawn area is stretched past 4:1."""
    last = 0.0
    for f in rep_frames:
        if f["stretched_4_share"] > share:
            break
        last = f["rho"]
    return last


# ── write everything ────────────────────────────────────────────────
def motion_doc(motion) -> dict:
    """The in-between keyframes (positions only) and how the motion was checked."""
    return {"between": [{"rho": _r(k["rho"]), "at": round(float(k["at"]), 6), "settling": k.get("settling", False),
                         "x": k["dm"][:, 0].tolist(), "y": k["dm"][:, 1].tolist()}
                        for k in motion["keys"] if k["main"] is None],
            "blends": motion["blends"], "inserted": motion["inserted"], "settling": motion["settling"],
            "jumps": [{"rho": _r(x["rho"]), "from": _r(x["from"]), "station": x["station"], "move_m": round(x["move_m"]),
                       "at_from": round(float(x["at_from"]), 6), "at_to": round(float(x["at_to"]), 6)}
                      for x in motion["snaps"]],
            "min_area_ratio_along_blends": _r(motion["min_area_ratio_along_blends"])}


def write(out: Path, *, b, pairs, mesh, links, model, frames, cells, pick, checks, lines_at, l01, wembley, land,
          routes, restart, motion, code, checks_sha256, input_sha, expired, started, log, modes=None) -> dict:
    from transit.layout import modes_report
    from transit.layout import timemap as tm

    out = Path(out)
    doc = page_doc(b, pairs, mesh, links, model, frames, checks, routes, code, checks_sha256, input_sha)
    doc["motion"] = motion_doc(motion)
    static = tm.page_static_bytes()
    blob, land_rep, dropped = b"", None, 0
    text = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
    gz = static + tm.gzip_size(text.encode())
    if land is not None:
        sizes = []
        # Every keyframe as drawn, in slider order: the land must keep its orientation on each and
        # all along the blend between each two, in the Float32 values the page mixes.
        keyed = [k["dm"] / 10.0 for k in motion["keys"]]
        # With the travel modes, every blend the page can draw for them too (#23, L-03).
        extra = modes_report.blend_pairs(modes) if modes else None
        for k in range(land["n_rungs"]):
            carried, dropped = landmod.drop_blend_slivers(land["rung"](k), keyed, extra)
            blob, header = landmod.pack(carried, mesh.n)
            header["slivers_dropped"] = dropped
            header["sliver_margin_m"] = landmod.BLEND_MARGIN_M
            doc["land"] = header
            text = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
            gz = static + tm.gzip_size(text.encode()) + tm.gzip_size(blob)
            sizes.append((header["tol_m"], header["min_ha"], gz))
            if gz <= tm.MAX_GZIP_BYTES:
                break
        else:
            raise RefusedOutput("the page would be over 2,000,000 bytes gzipped at every rung of the land ladder ("
                                + "; ".join(f"{t:g} m, {h:g} ha: {s:,}" for t, h, s in sizes)
                                + "); nothing was written")
        land_rep = {**doc["land"], "source_counts": land["source_counts"]}
    elif gz > tm.MAX_GZIP_BYTES:
        raise RefusedOutput(f"the page would be {gz:,} bytes gzipped, over the 2,000,000 limit; nothing was written")

    rep_frames = []
    for f in frames:
        rep_frames.append({"rho": f["rho"], "converged": f["converged"], "monotone": f["monotone"],
                           "rejected": f["rejected"], **f["metrics"]})
    end = rep_frames[-1]
    l01f = l01["frames"]
    absent = wembley.get("status") == "absent"
    l01_first = None if absent else wembley.get("l01_first_pass")
    wpick = None if absent else wembley["pick"]
    squeeze_end = end["squeezed"][0] if end["squeezed"] else {"ratio": 1.0, "near": None, "lat": 0.0, "lon": 0.0}
    real_len = np.hypot(*(mesh.P[links.pairs[:, 0]] - mesh.P[links.pairs[:, 1]]).T) if len(links.pairs) else np.ones(1)
    rest_over_real = links.rest_m / real_len if len(links.pairs) else np.ones(1)
    headlines = {
        "flipped_max": max(f["folds"]["flipped"] for f in rep_frames),
        "land_slivers_dropped": dropped,
        "l01_triangles": l01["triangles"],
        "l01_folds_first_pass": l01f[l01_first]["metrics"]["folds"]["folded"] if l01_first is not None else None,
        "l01_folds_time_map": l01f[-1]["metrics"]["folds"]["folded"],
        "links_longer_share": float((rest_over_real > 1).mean()),
        "links_rest_over_real": float(np.median(rest_over_real)),
        "links_past_end": end["links_past_share"],
        "wembley_end": end["ratios"].get("wembley_park", {}),
        "met_breaks_real": rep_frames[0]["met_breaks"], "met_breaks_end": end["met_breaks"],
        "l01_met_breaks": l01f[-1]["metrics"]["met_breaks"],
        "sigma1_real": rep_frames[0]["sigma1"], "sigma1_end": end["sigma1"],
        "sigma1_min": min(f["sigma1"] for f in rep_frames),
        "l01_sigma1_real": l01f[0]["metrics"]["sigma1"], "l01_sigma1_time": l01f[-1]["metrics"]["sigma1"],
        "T_end": end["T"], "C_end": end["C"], "T_real": rep_frames[0]["T"], "C_real": rep_frames[0]["C"],
        "l01_T_time": l01f[-1]["metrics"]["T"], "l01_C_time": l01f[-1]["metrics"]["C"],
        "squeeze_end": squeeze_end, "stretch_median_end": end["stretch_median"],
        "stretch_worst_end": end["stretch_worst"], "stretched_4_end": end["stretched_4_share"],
        "stretched_4_pick": rep_frames[wpick]["stretched_4_share"] if wpick is not None else end["stretched_4_share"],
        "readable_until": _readable_until(rep_frames),
        "stretched_4_never": all(f["stretched_4_share"] <= 0.1 for f in rep_frames),
        "under_5pct_end": end["under_5pct"], "under_25pct_end": end["under_25pct"],
        "l01_style_end": [end["folds_l01"]["peeled"]["folded"], end["folds_l01"]["full"]["folded"]],
        "pd_all": all(f["pd"] for f in rep_frames), "not_pd": [f["rho"] for f in rep_frames if not f["pd"]],
        "rotation_max_deg": max(abs(f["rotation_deg"]) for f in rep_frames),
        "motion": {k: v for k, v in motion_doc(motion).items() if k != "between"},
        "iterations_total": sum(f["iterations"] for f in rep_frames),
        "substeps_total": sum(f["substeps"] for f in rep_frames),
        "expired": expired,
    }
    from transit.layout.timemap import ratio_list
    met_keys = [(0, "real_map", "Real map")]
    if wpick is not None and wpick not in (0, len(frames) - 1):
        met_keys.append((wpick, "pick", f"{'First Wembley Park pass' if wembley['pick_is_pass'] else 'Closest'} "
                                        f"({_rho_text(frames[wpick]['rho'])})"))
    met_keys.append((len(frames) - 1, "end", f"End of the slider ({_rho_text(frames[-1]['rho'])})"))
    rep = {
        "schema": 2, "notice": doc["notice"], "build": doc["build"], "window": b.window, "departure": b.departure,
        "valid_until": doc["valid_until"], "table_sha256": b.table_sha256, "points_sha256": b.points_sha256,
        "checks_sha256": checks_sha256, "inputs_sha256": input_sha, "code": code, "attribution": doc["attribution"],
        "s_m_per_min": pairs.s, "settings": model_settings(model), "prep": pairs.counts,
        "mesh": {"stations": mesh.n_station, "cells": mesh.n_cell, "ring": mesh.n_ring,
                 "ring_margin_m": meshmod.RING_MARGIN_M, "triangles": int(len(mesh.tri)),
                 "inner_triangles": int((~(mesh.kinds[mesh.tri] == meshmod.RING).any(axis=1)).sum()),
                 "grid_springs": int(len(mesh.edges)), "median_edge_m": model.scale,
                 "cell_ids": [cells[int(i)]["id"] for i in pick]},
        "links": links.counts, "headlines": headlines, "wembley": wembley, "restart": restart,
        "ratio_rows": [{"key": r["key"], "name": r["name"]} for r in ratio_list(checks)],
        "frames": rep_frames,
        "l01": {"triangles": l01["triangles"], "triangles_full": l01["triangles_full"],
                "frames": [{"lam_tilde": f["lam_tilde"], "converged": f["converged"],
                            **{k: v for k, v in f["metrics"].items()}} for f in l01f]},
        "metropolitan": {key: lines_at.get(i, []) for i, key, _ in met_keys},
        "metropolitan_order": [(key, name) for _, key, name in reversed(met_keys)],
        "land": land_rep, "page_gzip_bytes": gz,
    }
    rep["compare"] = compare_table(rep_frames, l01, wembley)
    rep = _r(rep)
    md = report_md(rep)

    bins = {"real": measures.shepard_bins(pairs, frames[0]["Xs"][:pairs.n]),
            "l01": measures.shepard_bins(pairs, l01["time_map_X"]),
            "l02": measures.shepard_bins(pairs, frames[-1]["Xs"][:pairs.n])}
    rep["shepard"] = {k: {"pairs": v["pairs"], "median": _r(v["median"])} for k, v in bins.items()}
    svg = shepard_svg(bins, f"Shepard plot, build {doc['build']} {b.window}",
                      panels=(("real", "Real map"), ("l01", "Stations-only time map (λ = 0)"),
                              ("l02", f"Spring grid, {_rho_text(frames[-1]['rho'])}")))
    panels = panels_svg(pairs, mesh, frames, panel_picks(frames, wembley), land["coarse"] if land else None, routes,
                        checks, f"London by travel time on a spring grid that can't fold, build {doc['build']} {b.window}")

    # The travel modes' files, built and size-checked before anything is written.
    mfiles, msummary = modes_report.build(b=b, pairs=pairs, mesh=mesh, links=links, frames=frames, modes=modes,
                                          stress_scale=doc["stress_scale"], code=code, page_gzip=gz,
                                          land_dropped=dropped) if modes else ({}, [])
    out.mkdir(parents=True, exist_ok=True)
    (out / "timemap.json").write_text(text + "\n", encoding="utf-8")
    (out / "land.bin").write_bytes(blob)
    (out / "report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "report.md").write_text(md + f"\nRun time {time.monotonic() - started:.0f} s.\n", encoding="utf-8")
    (out / "shepard.svg").write_text(svg, encoding="utf-8")
    (out / "panels.svg").write_text(panels, encoding="utf-8")
    for name, data in mfiles.items():
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        (out / name).write_bytes(data)

    w = headlines["wembley_end"]
    summary = [
        f"flipped triangles: {headlines['flipped_max']} at all {len(frames)} positions",
        "Wembley Park: " + (f"first passes at {_rho_text(frames[wembley['first_pass']]['rho'])}; "
                            if wembley.get("first_pass") is not None else "no pass; ")
        + (f"end {w['map_m'] / 1000:.2f} km, {w['ratio_to_median']:.3f}× ({w['status']})" if "map_m" in w else ""),
        f"σ₁ {headlines['sigma1_real']:.3f} -> {headlines['sigma1_end']:.3f}; Met breaks at the end "
        f"{headlines['met_breaks_end']}; thinnest triangle {headlines['stretch_worst_end']['value']:.0f}:1; smallest "
        f"area {squeeze_end['ratio'] * 100:.1f}% near {squeeze_end['near']}",
        f"wrote {out} (page {gz / 1e6:.2f} MB gzipped)",
        *msummary,
    ]
    return {"summary": summary, "gzip_bytes": gz, "headlines": headlines, "out": out, "report": rep}
