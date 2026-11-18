"""
The travel modes' files (#23), beside the full time map's: written to modes/ of the time map's
folder by `python -m transit.layout.timemap --modes car bus walk` (any of them).

  modes.json            what modes there are (and why one isn't), which combination is in which file,
                        the train springs' pulls at each position, which switches go straight across,
                        roads.bin's header (with the car), the bus and walking links' counts, every
                        file's size
  combo-<id>.json       one combination of modes (ids are the modes' names, sorted, joined by "-":
                        combo-car-train.json, combo-bus-walk.json): every solved position (whole
                        decimetres), its measures and pulls, and its in-between keyframes
  roads.bin             the car's main roads, carried through the mesh like the rail (land.py)
  report.md, .json      the car data and Thames crossings (with the car), the bus and walking times,
                        the combinations compared, the switches, the restart checks and the car's
                        spot check

The app serves modes/ like the map itself, under the same gate, and only the files modes.json lists
(server/transit/whole_city.py).

The train-only combination is the time map itself (timemap.json); a page switches from one
combination to another that differs by one mode by gliding to the nearest solved position and then
either blending straight across (when the build checked that blend never folds: mesh.blend_min_area,
exact) or going back down to the real map and up the other combination, both of which are checked
blends already.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter

import numpy as np

from transit.layout import land as landmod
from transit.layout import mesh as meshmod
from transit.layout.prep import RefusedOutput
from transit.layout.report import _r


def _readable_until(frames, share: float = 0.1) -> float:
    """The last ρ at which at most `share` of the drawn area is stretched past 4:1 (as the time map's
    own report says it)."""
    last = 0.0
    for f in frames:
        if f["metrics"]["stretched_4_share"] > share:
            break
        last = f["rho"]
    return last


MAX_GZIP_BYTES = 2_000_000          # each file a combination loads, as the page's first load
ROAD_TOL_M = 20.0                   # the drawn roads are simplified this much
MODE_NAMES = {"walk": "Walking", "bus": "Bus", "train": "Train and tube", "car": "Car"}
MODE_ORDER = ("walk", "bus", "train", "car")          # the page's buttons
WHY_NOT = {"walk": "This map has no walking-only times.", "bus": "This map has no bus-only times.",
           "car": "This map has no car."}
FAMILY_NAMES = {"train": "Train springs", "bus": "Bus springs", "walk": "Walking springs", "car": "Road springs",
                "car_access": "Access springs"}
CAR_NOTICE = "Car speeds: HERE Traffic4cast 2022 (MeTS-10), used non-commercially with the owner's permission."
PLAIN_NOTICE = ("Bus and walking times: the build's own bus-only and walking-only tables (R5 on its timetables "
                "and OpenStreetMap extract).")
LEFT_OUT_MD = 40                    # left-out pairs named in report.md (all are in report.json)


def notice(chosen) -> str:
    parts = ["Travel modes (#23)."]
    if "car" in chosen:
        parts.append(CAR_NOTICE)
    if set(chosen) & {"bus", "walk"}:
        parts.append(PLAIN_NOTICE)
    return " ".join(parts)


def mode_list(chosen) -> list[dict]:
    return [{"id": i, "label": MODE_NAMES[i], "available": i == "train" or i in chosen,
             "why": "" if i == "train" or i in chosen else WHY_NOT[i]} for i in MODE_ORDER]


def label(cid: str) -> str:
    """A combination's name: "Car + train and tube", "Bus + walking"."""
    words = [MODE_NAMES[m] for m in cid.split("-")]
    return " + ".join([words[0]] + [w.lower() for w in words[1:]])


def combo_files(modes: dict) -> dict:
    from transit.layout.timemap import combo_file
    return {cid: combo_file(cid) for cid in modes["combos"] if cid != "train"}


def _gz(data: bytes) -> int:
    import gzip
    return len(gzip.compress(data, mtime=0))


def _rho(v) -> str:
    return "the real map" if not v else f"ρ = {v:.3g}"


def blend_pairs(modes: dict) -> list[tuple[np.ndarray, np.ndarray]]:
    """Every blend the page can draw for the travel modes, as pairs of layouts (metres): each
    combination's neighbouring keyframes, and each straight switch between combinations. A blend
    between two identical layouts (a mode whose springs all stay slack) and a blend already listed
    are left out: the land check would find nothing new on them."""
    out, seen = [], set()

    def add(a, b):
        if np.array_equal(a, b):
            return
        key = hashlib.sha256(np.ascontiguousarray(a).tobytes() + np.ascontiguousarray(b).tobytes()).digest()
        if key not in seen:
            seen.add(key)
            out.append((a, b))

    for cid, mo in modes["motions"].items():
        if cid == "train":
            continue
        keys = [k["dm"] / 10.0 for k in mo["keys"]]
        for a, b in zip(keys, keys[1:]):
            add(a, b)
    for key, flags in modes["cross"].items():
        a, b = key.split("|")
        for k, fl in enumerate(flags):
            if fl and fl["ok"]:
                add(modes["combos"][a][k]["Xs"], modes["combos"][b][k]["Xs"])
    return out


def roads_layer(roads_doc: dict, mesh) -> tuple[bytes, dict]:
    """The car's main roads carried through the mesh (land.py's lines), packed as roads.bin."""
    import shapely

    hull = shapely.MultiPoint(mesh.P[mesh.kinds != meshmod.RING]).convex_hull
    by = {c: [] for c in landmod.ROAD_STYLES}
    for r in roads_doc["roads"]:
        xy = np.asarray(r["xy"], dtype=np.float64).reshape(-1, 2)
        if r["class"] in by and len(xy) >= 2:
            by[r["class"]].append(shapely.linestrings(xy))
    items = []
    for c, lines in by.items():
        if not lines:
            continue
        merged = shapely.line_merge(shapely.union_all(lines)).simplify(ROAD_TOL_M).intersection(hull)
        items += [(ln, c) for ln in landmod._lines(merged)]
    carried = landmod.carry({"roads": items}, mesh.P, mesh.tri, ROAD_TOL_M, 0.0, styles=landmod.ROAD_STYLES)
    blob, header = landmod.pack(carried, mesh.n, landmod.ROAD_STYLES, "roads.bin")
    return blob, header


def _frames_doc(frames) -> list[dict]:
    from transit.layout.timemap_report import frame_doc
    return [{**frame_doc(f), "pulls": _r(f["pulls"])} for f in frames]


def view_box(modes: dict, mesh) -> list[float]:
    """The box (x0, y0, x1, y1, metres) every keyframe of every combination's stations and cells fits
    in: the page frames the map once, for all of them."""
    inner = mesh.kinds != meshmod.RING
    X = np.concatenate([k["dm"][inner] / 10.0 for mo in modes["motions"].values() for k in mo["keys"]])
    return [round(float(v), 1) for v in (*X.min(axis=0), *X.max(axis=0))]


def plain_block(modes: dict, kind: str) -> dict | None:
    """What modes.json and the report say about the bus or walking links."""
    lk = modes["plain"].get(kind)
    if lk is None:
        return None
    c = lk.counts
    out = {"links": c["links"], **(modes["tables"].get(kind) or {}), "departure": modes.get("departure"),
           "left_out": c["left_out_count"], "rest_over_real": c["rest_over_real"]}
    if kind == "bus":
        out.update({"candidates": c["candidates"], "trips": c["trips_with_a_hop"], "routes": c["routes_with_a_link"],
                    "links_dropped": c["links_dropped"]})
    else:
        out.update({"edges": c["edges"], "dropped": c["dropped"]})
    return out


def pulls_max(modes: dict) -> dict:
    """Per spring family: the most of its springs taut (at or past their rest length, so acting on the
    solve) at any position of any combination, how many of those pull (more than PULL_PAST past), and
    where: the position with the most taut springs, then the most pulling."""
    out = {}
    for cid, fr in modes["combos"].items():
        for f in fr:
            for fam, p in f["pulls"].items():
                key = (p.get("taut", p["n"]), p["n"])
                if fam not in out or key > (out[fam]["taut"], out[fam]["n"]):
                    out[fam] = {"n": p["n"], "taut": key[0], "of": p["of"], "combo": cid, "rho": f["rho"],
                                "top": p.get("top", []), "top_links": p.get("top_links", [])}
    return out


def without(cid: str, mode: str) -> str | None:
    """The combination a toggle of `mode` switches `cid` to (None: the real map)."""
    rest = [m for m in cid.split("-") if m != mode]
    return "-".join(rest) if rest else None


def mode_effect(modes: dict, mesh) -> dict:
    """For bus and walking: the largest node shift (stations and cells) that switching the mode on
    causes, over every combination with it against the same combination without it (the real map
    when it is the only mode), at every solved position; and where."""
    inner = mesh.kinds != meshmod.RING
    names = modes.get("names")
    combos = modes["combos"]
    out = {}
    for mode in ("bus", "walk"):
        if mode not in modes["chosen"]:
            continue
        best = {"max_m": 0.0, "combo": None, "rho": None, "at": None}
        for cid, fr in combos.items():
            if mode not in cid.split("-"):
                continue
            other = without(cid, mode)
            for k, f in enumerate(fr):
                base = combos[other][k]["Xs"] if other else combos["train"][0]["Xs"]
                d = np.hypot(*(f["Xs"] - base).T)
                d[~inner] = 0
                i = int(np.argmax(d))
                if d[i] > best["max_m"]:
                    best = {"max_m": float(d[i]), "combo": cid, "rho": f["rho"],
                            "at": names[i] if names else None}
        out[mode] = best
    return out


def build(*, b, pairs, mesh, links, frames, modes, stress_scale, code, page_gzip, land_dropped,
          bound: dict | None = None) -> tuple[dict, list]:
    """Every modes/ file as bytes ({path: bytes}) and the log's summary lines; refused when a file
    a combination loads is over MAX_GZIP_BYTES gzipped."""
    from transit.layout.timemap import MODES_DIR
    from transit.layout.timemap_report import motion_doc

    chosen = modes["chosen"]
    car = "car" in chosen
    common = {"build": b.manifest.get("build"), "window": b.window, "table_sha256": b.table_sha256, "nodes": int(mesh.n),
              **(bound or {}), "notice": notice(chosen), "code": code}
    files, sizes = {}, {}
    cfiles = combo_files(modes)
    for cid, name in cfiles.items():
        doc = {"schema": 1, "kind": "london-time-map-combo", "id": cid, "modes": cid.split("-"), **common,
               "frames": _frames_doc(modes["combos"][cid]), "motion": motion_doc(modes["motions"][cid])}
        files[name] = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode()
    roads_header = None
    if car:
        roads_blob, roads_header = roads_layer(modes["roads"], mesh)
        files["roads.bin"] = roads_blob
    for name, data in files.items():
        sizes[name] = _gz(data)
    over = {n: s for n, s in sizes.items() if s > MAX_GZIP_BYTES}
    if over:
        raise RefusedOutput("the travel modes' files would be over 2,000,000 bytes gzipped ("
                            + "; ".join(f"{n}: {s:,}" for n, s in over.items()) + "); nothing was written")
    index = {"schema": 1, "kind": "london-time-map-modes", **common, "s_m_per_min": _r(pairs.s),
             "stress_scale": stress_scale, "modes": mode_list(chosen), "default": ["train"],
             "combos": {"train": None, **{cid: {"file": n, "modes": cid.split("-")} for cid, n in cfiles.items()}},
             "rhos": [_r(f["rho"]) for f in frames],
             "train_pulls": [_r(f["pulls"]) for f in frames],
             "cross": {k: [_r(x) for x in v] for k, v in modes["cross"].items()},
             "roads": roads_header, "sizes": sizes, "report": True, "view_box": view_box(modes, mesh),
             **{k: _r(v) for k in ("bus", "walk") if (v := plain_block(modes, k)) is not None}}
    if car:
        cr = modes["car_report"]
        index.update({"licence": cr.get("licence"), "overhead_min": modes["car_links"]["overhead_min"],
                      "data_box_lonlat": cr.get("data_box_lonlat"),
                      "coverage": {k: cr["coverage"][k]["after"]
                                   for k in ("all", "motorway", "inside_the_data_box", "motorway_inside_the_data_box")
                                   if k in cr["coverage"]}})
    rep = report_json(b, pairs, mesh, links, modes, sizes, page_gzip, land_dropped, code)
    files["modes.json"] = json.dumps(index, separators=(",", ":"), ensure_ascii=False).encode()
    files["report.json"] = (json.dumps(rep, indent=1, ensure_ascii=False) + "\n").encode()
    files["report.md"] = report_md(rep).encode()
    summary = summary_lines(rep)
    return {f"{MODES_DIR}/{n}": d for n, d in files.items()}, summary


# ── the report ──────────────────────────────────────────────────────
def report_json(b, pairs, mesh, links, modes, sizes, page_gzip, land_dropped, code) -> dict:
    combos = modes["combos"]
    chosen = modes["chosen"]
    per = {}
    for cid, fr in combos.items():
        flips = max(f["metrics"]["folds"]["flipped"] for f in fr)
        mo = modes["motions"][cid]
        per[cid] = {
            "label": label(cid), "positions": len(fr), "flipped_max": flips,
            "real_map_throughout": all(np.array_equal(f["dm"], fr[0]["dm"]) for f in fr),
            "converged": all(f["converged"] for f in fr),
            "pd_all": all(f["pd"] for f in fr), "iterations": sum(f["iterations"] for f in fr),
            "motion": {"blends": mo["blends"], "inserted": mo["inserted"], "settling": mo["settling"],
                       "min_area_ratio_along_blends": mo["min_area_ratio_along_blends"]},
            "pick": modes["picks"][cid],
            "stretched_4_end": fr[-1]["metrics"]["stretched_4_share"],
            "readable_until": _readable_until(fr),
            "frames": [{"rho": f["rho"], "sigma1": f["metrics"]["sigma1"],
                        "wembley": f["metrics"]["ratios"].get("wembley_park", {}),
                        "area_ratio_min": f["metrics"]["area_ratio_min"], "stretch_max": f["metrics"]["stretch_max"],
                        "shift_median_m": f["metrics"]["shift_median_m"], "flipped": f["metrics"]["folds"]["flipped"],
                        "stretched_4_share": f["metrics"]["stretched_4_share"],
                        "iterations": f["iterations"], "pd": bool(f["pd"]),
                        "pulls": {k: {kk: vv for kk, vv in v.items() if kk != "top_links"} for k, v in f["pulls"].items()}}
                       for f in fr]}
    restart = {cid: {"complete": r["complete"], "ladders": r["ladders"], "floor": r.get("floor", []),
                     "at": {int(k): {"rho": v["rho"], "shift_max_m": v["shift_max_m"], "shift_at": v["shift_at"],
                                     "shift_run": v["shift_run"], "statuses": v["statuses"],
                                     "wembley_km_spread": v["wembley_km_spread"]} for k, v in r["at"].items()}}
               for cid, r in modes["restart"].items()}
    cross = {}
    for key, flags in modes["cross"].items():
        real = [f for f in flags if f]
        cross[key] = {"straight": sum(f["ok"] for f in real), "positions": len(real),
                      "via_real_map": [f["rho"] for f in real if not f["ok"]],
                      "min_area_ratio": min((f["min_area_ratio"] for f in real if f["ok"]), default=None),
                      "flags": flags}
    compare = {int(k): v for k, v in modes["compare"].items()}
    plain = {}
    for kind in ("bus", "walk"):
        lk = modes["plain"].get(kind)
        if lk is not None:
            plain[kind] = {**lk.counts, **(modes["tables"].get(kind) or {}), "departure": modes.get("departure")}
    out = {"schema": 1, "notice": notice(chosen), "build": b.manifest.get("build"), "window": b.window,
           "table_sha256": b.table_sha256, "code": code, "s_m_per_min": pairs.s, "chosen": chosen,
           "combos": per, "compare": compare, "compare_at": modes["compare_at"], "cross": cross, "restart": restart,
           "spots": modes["spots"], "plain": plain, "pulls_max": pulls_max(modes),
           "mode_effect": mode_effect(modes, mesh),
           "sizes": sizes, "page_gzip_bytes": page_gzip, "land_slivers_dropped": land_dropped,
           "train_links": len(links.pairs)}
    if "car" in chosen:
        out["overhead_min"] = modes["car_links"]["overhead_min"]
        out["car"] = modes["car_report"]
    return _r(out)


def _at(d: dict, k):
    """d[k] for a position k whether the keys are numbers (in memory) or strings (from JSON)."""
    return d[k] if k in d else d[str(k)]


def _min(x) -> str:
    return "—" if x is None else f"{x:.0f} min"


def _km(x) -> str:
    return "—" if x is None else f"{x:.2f} km"


def _m(x) -> str:
    """A distance in metres: 0.1 m steps below 100 m, whole metres above."""
    if x is None:
        return "—"
    return f"{x:.1f} m" if x < 100 else f"{x:,.0f} m"


def _pct(x) -> str:
    return "—" if x is None else f"{100 * x:.0f}%"


def _wem(w: dict) -> str:
    if not w or "ratio_to_median" not in w:
        return str((w or {}).get("status", "—"))
    return f"{w['map_m'] / 1000:.2f} km, {w['ratio_to_median']:.3f}× ({w['status']})"


def _kmh(s_m_per_min: float) -> str:
    return f"{s_m_per_min * 60 / 1000:.0f} km/h"


def _car_headlines(rep: dict) -> list[str]:
    cr = rep["car"]
    sp = cr["speeds"]
    days = sp["days_used"]
    L = [f"- **Car times include {rep['overhead_min']:g} minutes once a trip** for parking and getting to and from "
         "the car. The road springs sit on a road layer of twins, so a trip along a road pays it once, however many "
         "nodes the road passes; train, bus and walking times come from the tables and include the walk to the "
         "platform or stop and the wait.",
         "- **How the car differs from the issue's model:** the issue puts two modes' springs in parallel on the same "
         "pair of places, the faster one setting the distance and the slower going slack. Paying the overhead once a "
         "trip needs the road layer instead: car springs join twins, not places, and reach a place only through its "
         "access spring (slack within s·2.5 min of its twin) and a weak tie. Car springs therefore never share a pair "
         "with another mode; both can pull in the same area at once, and the car bounds a pair at s·(5 min + drive). "
         "Train, bus and walking springs do sit in parallel, as the issue says.",
         f"- **The car data:** HERE's Traffic4cast 2022 London speeds (the MeTS-10 pipeline's speeds on "
         f"OpenStreetMap edges), weekdays 13:00–14:00 (15-minute bins {', '.join(str(x) for x in sp['bins'])}), "
         f"{len(days)} days from {days[0]} to {days[-1]}: weekdays only, no bank holidays, no 23 December to "
         f"3 January, all before the covid period. {cr['clock']}",
         f"- **Licence:** {cr['licence']}"]
    cov = cr["coverage"]
    box = cr.get("data_box_lonlat")
    L.append(f"- **Speeds on our roads:** {_pct(cov['all']['before'])} of the main roads' length has a measured "
             f"speed by node id, {_pct(cov['all']['after'])} after matching by geometry too"
             + (f"; inside the area the data covers (longitude {box[0]:.3f} to {box[2]:.3f}: Traffic4cast's London is a "
                f"fixed box, so roads east and west of it have no speed at all), {_pct(cov['inside_the_data_box']['before'])}"
                f" and {_pct(cov['inside_the_data_box']['after'])}" if box else "")
             + f". A link needs at least {_pct(cr['links']['fill_min_share'])} of its road measured; the rest of its road "
             "takes its class's median measured speed."
             + (" Motorways are mostly unmatched: few of their 2019 node ids are still in OpenStreetMap."
                if cov.get("motorway_inside_the_data_box", {}).get("km")
                and cov["motorway_inside_the_data_box"]["after"] < 0.5 else ""))
    br = cr["bridges"]
    linked = [x for x in br if x["status"] == "linked"]
    L.append(f"- **Thames crossings in Greater London:** {len(linked)} of {len(br)} bridges and tunnels of motor "
             "roads have a car link" + ("; the others are listed below with the reason." if len(linked) < len(br)
                                         else "."))
    for s in rep["spots"]:
        if s.get("status") == "absent":
            L.append(f"- **Spot check, {s['name']}:** absent from this build ({', '.join(s['absent'])}).")
            continue
        at = _at(s["at"], max(int(k) for k in s["at"]))
        ctl = _at(s.get("control") or {}, max(int(k) for k in s["at"])) if s.get("control") else None
        what = {"pass": "pass: faster by car, and closer with car on",
                "fail": "fail: faster by car, but not closer with car on",
                "not faster by car": "no claim: not faster by car",
                "no time": "no claim: no time by public transport or no road between them"}.get(s["status"], s["status"])
        L.append(f"- **Spot check, {s['name']}: {s['a_name']} – {s['b_name']}** ({s['real_km']:.1f} km apart): "
                 f"{_min(s['train_table_min'])} by public transport, {_min(s['car_min'])} by car door to door "
                 f"(the car holds them at most {_km(s.get('car_bound_km'))} apart). At the end of the slider "
                 f"{at['train']:.2f} km apart with train only, {at['car-train']:.2f} km with car and train, "
                 f"{at['car']:.2f} km with car only"
                 + (f" (car and train makes it {s['ratio_end']:.2f}× its train-only distance; the median pair 3 km or "
                    f"more apart: {ctl:.2f}×)" if s.get("ratio_end") and ctl else "")
                 + f": **{what}**.")
    return L


def _ratio_words(rr: dict, what: str) -> str:
    if rr.get("min") is None:
        return f"There are no {what} springs to compare. "
    return f"A {what} spring's rest length is at least {rr['min']:.1f}× its real length (median {rr['median']:.1f}×). "


def _effect_words(rep: dict, mode: str, name: str) -> str:
    """How far switching the mode on moves the map, from the shipped layouts (mode_effect)."""
    p = rep["pulls_max"].get(mode, {"n": 0, "taut": 0})
    e = (rep.get("mode_effect") or {}).get(mode) or {"max_m": 0.0}
    if not p.get("taut") and not e["max_m"]:
        return (f"No {name} spring is taut (at or past its rest length) in any combination at any position, and "
                f"switching {name.capitalize()} on moves no station or cell anywhere.")
    out = (f"The most taut at once: {p['taut']} of {p['of']} ({p['n']} more than 1% past), in {label(p['combo'])} "
           f"at {_rho(p['rho'])}" + (f": {', '.join(p['top_links'] or p['top'])}" if p.get("top_links") or p.get("top")
                                    else "") + ". ")
    if e["max_m"]:
        out += (f"Switching {name.capitalize()} on moves the map by at most {_m(e['max_m'])} (in {label(e['combo'])} "
                f"at {_rho(e['rho'])}, near {e['at'] or '—'}).")
    else:
        out += f"Switching {name.capitalize()} on moves no station or cell by a whole decimetre."
    return out


def _scale_words(rep: dict, mode: str, what: str, slower: str) -> str:
    """Whether the mode's springs can act on the real map, from their rest lengths, and whether the
    mode alone moves the map, from its shipped layouts."""
    rr = rep["plain"][mode]["rest_over_real"]
    alone = (rep.get("combos", {}).get(mode) or {}).get("real_map_throughout")
    name = MODE_NAMES[mode]
    fast = rr.get("faster") or 0
    if fast:
        out = (f"{fast} {what} springs are shorter than their real length: that {what.split()[0]} beats this map's "
               f"scale (about {_kmh(rep['s_m_per_min'])} door to door), so they pull from the real map on. ")
    else:
        out = (f"{slower} this map's scale (about {_kmh(rep['s_m_per_min'])} door to door), so on the real map no "
               f"{what} spring is taut. ")
    if alone is not None:
        out += (f"{name} alone is the real map at every position" + (": the shared scale at work, not a failed mode. "
                                                                     if not fast else ". ")
                if alone else f"{name} alone moves the map. ")
    return out


def _plain_headlines(rep: dict) -> list[str]:
    L = []
    w = rep["plain"].get("walk")
    if w:
        L.append(f"- **Walking:** {w['links']} springs on the mesh's edges between stations and cells "
                 f"({w['left_out_count']} left out). " + _ratio_words(w["rest_over_real"], "walking")
                 + _scale_words(rep, "walk", "walking", "Walking is slower than")
                 + "A walking spring otherwise acts only where another mode has stretched two neighbours past their "
                 "walk. " + _effect_words(rep, "walk", "walking"))
    bu = rep["plain"].get("bus")
    if bu:
        dropped = ", ".join(f"{v} {k}" for k, v in bu["links_dropped"].items()) or "none"
        L.append(f"- **Bus:** {bu['links']} springs between nodes a bus serves one after the other, from "
                 f"{bu['candidates']} pairs on {bu['trips_with_a_hop']} bus trips leaving a stop between "
                 f"{bu['window'][0]} and {bu['window'][1]} (left out: {dropped}). " + _ratio_words(bu["rest_over_real"], "bus")
                 + _scale_words(rep, "bus", "bus", "Door to door, with the walk to the stop and the wait, no bus "
                                "between neighbouring places beats")
                 + _effect_words(rep, "bus", "bus")
                 + " The bus feed's route types don't tell a rail replacement bus from any other, so a replacement "
                 "service is counted as a bus: a route named for a line, not a number, is likely one.")
    return L


def _left_out_md(c: dict) -> list[str]:
    rows = c["left_out"]
    if not rows:
        return ["None left out."]
    if c["left_out_count"] > LEFT_OUT_MD:
        by = Counter(r[2] for r in rows)
        return [f"{c['left_out_count']} pairs left out (" + ", ".join(f"{v} {k}" for k, v in by.most_common())
                + "); every one is named in report.json."]
    return ["| Pair | Why |", "|---|---|"] + [f"| {a} – {b} | {w} |" for a, b, w in rows]


def report_md(rep: dict) -> str:
    combos = rep["combos"]
    chosen = rep["chosen"]
    car = "car" in chosen
    L = [f"# Travel modes: build {rep['build']}, {rep['window']}", "", f"**{rep['notice']}**", "",
         f"Code `{rep['code']}`; table sha256 `{rep['table_sha256'][:16]}…`. Definitions: `transit/README.md`, "
         "\"Travel modes\".", "", "## Headlines", ""]
    L.append(f"- **{len(combos)} combinations**, each solved over the same {len(next(iter(combos.values()))['frames'])} "
             f"positions of the slider from the real map (ρ = 0, the same for all): "
             + ", ".join(label(c) for c in combos)
             + f". Train and tube alone ({rep['train_links']} train springs) is the time map itself. One scale for "
             f"all: s = {rep['s_m_per_min']:.1f} m of map per minute (about {_kmh(rep['s_m_per_min'])} door to door). "
             "A spring only pulls: it holds two places at most s × its time apart, and a mode slower than the map "
             "moves nothing.")
    flips = max(c["flipped_max"] for c in combos.values())
    L.append(f"- **Flipped triangles: {flips} at every position of every combination**, on what ships (whole "
             "decimetres) and its Float32 image, and every blend the page draws between neighbouring keyframes of a "
             "combination keeps every triangle's area positive all the way (checked exactly).")
    every = [k for k, c in rep["cross"].items() if c["straight"] == c["positions"]]
    worst = min((c["min_area_ratio"] for c in rep["cross"].values() if c["min_area_ratio"] is not None), default=None)
    L.append(f"- **Switching one mode on or off:** {len(rep['cross'])} switches between combinations; "
             f"{len(every)} blend straight across at every position"
             + (f" (every triangle keeps at least {100 * worst:.1f}% of its area on the way)" if worst is not None else "")
             + (". The others go back through the real map where the straight blend would fold, along both "
                "combinations' own checked blends (below)." if len(every) < len(rep["cross"]) else "."))
    L += _plain_headlines(rep)
    if car:
        L += _car_headlines(rep)
    if rep["restart"]:
        incomplete = [label(c) for c, r in rep["restart"].items() if not r["complete"]]
        allat = [(c, v) for c, r in rep["restart"].items() for v in r["at"].values()]
        floors = [(c, f) for c, r in rep["restart"].items() for f in r.get("floor", [])]
        wc, wv = max(allat, key=lambda cv: cv[1]["shift_max_m"]) if allat else (None, None)
        L.append(f"- **Restart check:** run for {len(rep['restart'])} combinations ("
                 + ("complete for all" if not incomplete else f"INCOMPLETE for {', '.join(incomplete)}")
                 + "; nudged starts and other ladders of ρ)"
                 + (f"; no node more than {wv['shift_max_m']:.1f} m from the shipped layout (worst: {label(wc)} at "
                    f"{_rho(wv['rho'])}, {wv['shift_at'] or '—'}, {wv['shift_run']})" if wv else "") + "."
                 + (f" {len(floors)} nudged runs of {', '.join(sorted({label(c) for c, _ in floors}))} (the real map at "
                    "every shipped position) stopped short of the solver's tolerance within a fraction of a millimetre "
                    "of the real map, where the energy is at its floating-point floor and no step can show a gain. Each "
                    "went on, solving every later position from the last; every one of those solves either converged or "
                    f"stopped the same way, at most {max(f['from_real_m'] for _, f in floors) * 1000:.1f} mm from the "
                    "real map, and the positions checked are scored on those solves."
                    if floors else ""))
    sizes = rep["sizes"]
    L.append(f"- **Size:** the page's first load is {rep['page_gzip_bytes'] / 1e6:.2f} MB gzipped (train alone, as "
             f"without the modes); switching a combination on loads its own file, the largest "
             f"{max(sizes.values()) / 1e3:.0f} kB gzipped" + (", plus roads.bin the first time Car goes on" if car else "")
             + "; each is under the 2 MB limit.")

    L += ["", "## The combinations compared", "",
          "Node shift: every station and cell, against its real place and against train alone at the same position. "
          "σ₁ is against the all-mode transit tables (the only times there are for every pair), so it measures how "
          "far each map is from public-transport time, not from its own modes' times. A spring pulls when it is "
          "stretched more than 1% past its rest length.", ""]
    fams = [f for f in ("train", "bus", "walk", "car", "car_access")
            if any(f in fr["pulls"] for c in combos.values() for fr in c["frames"])]
    for k in rep["compare_at"]:
        rows = _at(rep["compare"], k)
        rho = next(iter(rows.values()))["rho"]
        L += [f"### At {_rho(rho)}" + (" (the end of the slider)" if k == max(rep["compare_at"]) else
                                       " (train alone's first Wembley Park pass, or closest)"), "",
              "| Combination | Median shift from the real map | Largest shift from the real map | Median shift from "
              "train alone | Largest shift from train alone | σ₁ | Wembley Park to Baker Street | Thinnest triangle | "
              "Smallest area kept | Flipped triangles |", "|---|" + "---:|" * 9]
        for c, r in rows.items():
            L.append(f"| {label(c)} | {_m(r['shift_real_median_m'])} | {_m(r['shift_real_max_m'])} "
                     f"({r['shift_real_max_at']}) | {_m(r['shift_train_median_m'])} | "
                     f"{_m(r['shift_train_max_m'])} | {r['sigma1']:.3f} | {_wem(r['wembley'])} | "
                     f"{r['stretch_max']:.1f}:1 near {r['stretch_worst'].get('near')} | "
                     f"{100 * r['area_ratio_min']:.1f}% | {r['flipped']} |")
        L += ["", "| Combination | " + " | ".join(f"{FAMILY_NAMES[f]} pulling" for f in fams) + " |",
              "|---|" + "---|" * len(fams)]
        for c in rows:
            fr = combos[c]["frames"][int(k)]
            cells = []
            for f in fams:
                p = fr["pulls"].get(f)
                cells.append("—" if p is None else f"{p['n']} of {p['of']} ({_pct(p['share'])})"
                             + (f", {p['taut']} taut" if f in ("bus", "walk") and "taut" in p else "")
                             + (f": {', '.join(p.get('top', []))}" if p.get("top") else ""))
            L.append(f"| {label(c)} | " + " | ".join(cells) + " |")
        L.append("")
    L += ["A spring is *taut* at or past its rest length (it acts on the solve) and *pulling* more than 1% past it. "
          "A mode whose springs all stay slack moves nothing: its places are already as close on the map as its "
          "times say.", ""]
    for cid, c in combos.items():
        slack = [f["rho"] for f in c["frames"][1:] if all(p.get("taut", p["n"]) == 0 for p in f["pulls"].values())]
        if c.get("real_map_throughout"):
            L.append(f"- {label(cid)}: the real map at every position (every shipped layout is the real map's)"
                     + ("; no spring is taut anywhere." if len(slack) == len(c["frames"]) - 1 else "."))
        elif slack:
            L.append(f"- {label(cid)}: no spring is taut at {', '.join(_rho(r) for r in slack)}.")

    L += ["", "## How thin triangles get", ""]
    for cid, c in combos.items():
        L.append(f"- {label(cid)}: at the end of the slider {100 * c['stretched_4_end']:.1f}% of the drawn area is "
                 "stretched past 4:1"
                 + ("; at no position is more than a tenth of it." if c["readable_until"] == c["frames"][-1]["rho"]
                    else f"; past {_rho(c['readable_until'])} more than a tenth of it is."))

    L += ["", "## Switching between combinations", "",
          "The page switches at a solved position: it glides to the nearest one, then blends straight to the other "
          "combination at that position when the build checked that blend (every triangle's smallest area along it, "
          "exactly, on whole decimetres), and otherwise goes back down to the real map and up the other combination. "
          "Switching the last mode off, or the first on, goes through the real map.", "",
          "| Switch | Straight at | Through the real map at | Smallest area kept on the way |", "|---|---:|---|---:|"]
    for key, c in rep["cross"].items():
        a, b = key.split("|")
        L.append(f"| {label(a)} ↔ {label(b)} | {c['straight']} of {c['positions']} | "
                 + (", ".join(_rho(r) for r in c["via_real_map"]) or "—") + " | "
                 + (f"{100 * c['min_area_ratio']:.1f}%" if c["min_area_ratio"] is not None else "—") + " |")

    if rep["plain"]:
        L += ["", "## Bus and walking times", "",
              "From the build's own per-mode tables, weekday 13:00–14:00 like the all-mode table, on the same points. "
              "A pair's time is the train rule: each way the fastest time between the two nodes' table points, both "
              "ways needed, their mean floored at half a minute."]
        for kind in ("bus", "walk"):
            c = rep["plain"].get(kind)
            if not c:
                continue
            what = {"bus": "Bus (buses with walking to and from the stops, or a direct walk of up to 30 min if "
                           "faster)", "walk": "Walking (walking only, up to 180 min)"}[kind]
            L += ["", f"### {what}", "",
                  f"Table sha256 `{(c.get('table_sha256') or '')[:16]}…`, departure {c.get('departure')}."]
            if kind == "bus":
                L.append(f"Bus feed `{c['feed']}` on {c['weekday']}: {c['bus_routes']} bus routes, {c['bus_trips_on_day']:,} "
                         f"trips running that day, {c['trips_with_a_hop']:,} with a hop between {c['window'][0]} and "
                         f"{c['window'][1]}; {c['hops_read']:,} hops read, dropped: "
                         + (", ".join(f"{v:,} {k}" for k, v in c["dropped"].items()) or "none")
                         + f". {c['stops']:,} stops, each given to its nearest station or cell (median "
                         f"{(c['stop_to_node_m']['median'] or 0):.0f} m away, 90% within "
                         f"{(c['stop_to_node_m']['p90'] or 0):.0f} m). {c['candidates']} pairs of nodes a bus serves one "
                         f"after the other; {c['links']} kept, on {c['routes_with_a_link']} routes, "
                         f"{c['nodes_without_link']} stations and cells without one. Left out: "
                         + (", ".join(f"{v} {k}" for k, v in c["links_dropped"].items()) or "none")
                         + ". A pair whose bus time is no faster than its walking time has no bus in it (the bus table "
                         "is the faster of a bus and a walk).")
            else:
                L.append(f"{c['edges']} mesh edges between stations and cells; {c['links']} springs; left out: "
                         + (", ".join(f"{v} {k}" for k, v in c["dropped"].items()) or "none") + ".")
            rr = c["rest_over_real"]
            if rr["min"] is not None:
                L.append(f"Rest length over real length: smallest {rr['min']:.2f}, median {rr['median']:.2f}; a spring "
                         "can only pull once the map has stretched its two places past that.")
            L += [""] + _left_out_md(c)

    if car:
        cr = rep["car"]
        sp = cr["speeds"]
        cov = cr["coverage"]
        L += ["", "## The car data", "", "| Roads | km (directed) | Measured by node id | After the geometric match |",
              "|---|---:|---:|---:|"]
        for name, v in cov.items():
            L.append(f"| {name.replace('_', ' ')} | {v['km']:.0f} | {_pct(v['before'])} | {_pct(v['after'])} |")
        m = cr["matching"]
        L += ["", f"Traffic4cast edges with a 13:00–14:00 speed: {m['t4c_edges']:,}; {m['t4c_synthetic']:,} with a "
              f"synthetic node id and {m['t4c_vanished']:,} with a node OpenStreetMap no longer has were not used. "
              f"Traffic4cast's synthetic node ids sit inside roads, motorways most of all: {m.get('t4c_chains', 0):,} "
              f"chains real → synthetic → … → real were contracted to one edge each, at the harmonic mean of their "
              f"speeds, and {m.get('t4c_chains_on_our_roads', 0):,} of them landed on our roads. "
              f"{m['t4c_on_our_roads']:,} edges in all landed on our main roads by node id (the shortest path between "
              f"their ends along them at most {m['path_ratio']}× the straight distance; {m['t4c_path_too_long']:,} did "
              f"not); the rest lent their straight chord to pieces within {m['match_m']:g} m and {m['match_deg']:g}°. "
              "Filled speeds (each class's median measured speed): "
              + ", ".join(f"{k} {v} km/h" for k, v in m["fill_kph"].items() if v) + ".", "",
              f"Speed rows read: {sp['rows_read']:,}; in 13:00–14:00 on the days used: {sp['rows_in_window']:,}; under "
              f"{sp['rules']['min_kph']:g} km/h, left out: {sp['rows_under_min_kph']:,}; by probe volume class: "
              + ", ".join(f"{k}: {v:,}" for k, v in sp["volume_class_rows"].items()) + f". Zip sha256 "
              f"`{sp['zip_sha256'][:16]}…`.", ""]
        lk = cr["links"]
        why = Counter(d["why"].split(":")[0] if "measured" not in d["why"] else "too little of its road measured"
                      for d in lk["dropped"])
        L += [f"Car links: {lk['count']} kept on {lk['nodes']} nodes, from {lk['candidates']} candidates (roads cut "
              f"into {lk['piece_m']:g} m pieces, {lk['pieces']:,} of them); dropped: "
              + (", ".join(f"{v} {k}" for k, v in why.most_common()) or "none") + ". Median measured share of a kept "
              f"link's road: {_pct(lk['measured_share_median'])}.", "",
              "## Thames crossings", "", "| Crossing | Kind | Road | Car link |", "|---|---|---|---|"]
        for x in cr["bridges"]:
            L.append(f"| {x['name']} | {x['kind']} | {'/'.join(x['classes'])}{' ' + x['ref'] if x['ref'] else ''} | "
                     + (", ".join(x["links"]) if x["links"] else f"none: {x['why']}") + " |")
        L += ["", "## Spot check", "",
              "A pair of stations faster by car (door to door, the overhead included) than by public transport should "
              "end closer with car on: that is a pass. A pair not faster by car makes no claim either way. The "
              "biggest gain is picked automatically among pairs at least 3 km apart; `[[car_spot]]` rows in "
              "checks.toml add more. The car bound is s × the car's minutes, the farthest apart the car springs hold "
              "the pair. The control is how much car and train shrinks the median station pair 3 km or more apart "
              "against train only, so a pair that shrinks only as much as everything around it reads as such.", "",
              "| Pair | Real | Public transport | Car | Car bound | "
              + " | ".join(f"{label(c)}, {_rho(_at(rep['compare'], k)['train']['rho'])}"
                           for k in rep["compare_at"] for c in ("train", "car-train", "car"))
              + " | Car + train ÷ train at the end (median pair) | Result |",
              "|---|---:|---:|---:|---:|" + "---:|" * (3 * len(rep["compare_at"])) + "---:|---|"]
        for s in rep["spots"]:
            if s.get("status") == "absent":
                L.append(f"| {s['name']} | absent ({', '.join(s['absent'])}) |")
                continue
            cells = [f"{_at(s['at'], k)[c]:.2f} km" for k in rep["compare_at"] for c in ("train", "car-train", "car")]
            end = max(rep["compare_at"])
            ctl = _at(s["control"], end) if s.get("control") else None
            ratio = (f"{s['ratio_end']:.2f}" + (f" ({ctl:.2f})" if ctl else "")) if s.get("ratio_end") else "—"
            L.append(f"| {s['name']}: {s['a_name']} – {s['b_name']} | {s['real_km']:.1f} km | "
                     f"{_min(s['train_table_min'])} | {_min(s['car_min'])} | {_km(s.get('car_bound_km'))} | "
                     + " | ".join(cells) + f" | {ratio} | {s['status']} |")

    if rep["restart"]:
        L += ["", "## Restart check", "",
              "Each combination solved again from the real map nudged on every free node (the same steps) and "
              "through other ladders of ρ (every other step, and each checked position in one step), compared with "
              "the shipped layout on every node.", "", "| Combination | Position | Largest node shift | Where | Run | "
              "Wembley Park statuses |", "|---|---|---:|---|---|---|"]
        for cid, r in rep["restart"].items():
            for v in r["at"].values():
                L.append(f"| {label(cid)} | {_rho(v['rho'])} | {v['shift_max_m']:.1f} m | {v['shift_at'] or '—'} | "
                         f"{v['shift_run'] or '—'} | {', '.join(v['statuses'])} |")
    L += ["", "## Sizes", "", "| File | Gzipped |", "|---|---:|"]
    L += [f"| {n} | {v / 1e3:.0f} kB |" for n, v in sizes.items()]
    for cid, c in combos.items():
        L += ["", f"## Every position: {label(cid)}", "",
              "| Position | σ₁ | Wembley Park | Smallest area | Thinnest | Area past 4:1 | Median shift | Pulling | "
              "Iterations | Positive definite |", "|---|---:|---|---:|---:|---:|---:|---|---:|---|"]
        for f in c["frames"]:
            pull = "; ".join(f"{k} {p['n']}/{p['of']}" for k, p in f["pulls"].items())
            L.append(f"| {_rho(f['rho'])} | {f['sigma1']:.4f} | {_wem(f['wembley'])} | {100 * f['area_ratio_min']:.1f}% "
                     f"| {f['stretch_max']:.1f}:1 | {100 * f['stretched_4_share']:.1f}% | {f['shift_median_m']:.0f} m | "
                     f"{pull or '—'} | {f['iterations']} | "
                     f"{'yes' if f['pd'] else 'no'} |")
        mo = c["motion"]
        L += ["", f"Motion: {mo['blends']} blends, {mo['inserted']} in-between positions, {mo['settling']} settling "
              f"steps; every triangle keeps at least {100 * mo['min_area_ratio_along_blends']:.1f}% of its area along "
              "every blend."]
    return "\n".join(L) + "\n"


def summary_lines(rep: dict) -> list[str]:
    out = [f"modes: {len(rep['combos'])} combinations; flipped triangles "
           f"{max(c['flipped_max'] for c in rep['combos'].values())} in every combination"]
    every = sum(c["straight"] == c["positions"] for c in rep["cross"].values())
    out.append(f"switches: {every} of {len(rep['cross'])} straight at every position")
    for kind in ("bus", "walk"):
        c = rep["plain"].get(kind)
        if c:
            p = rep["pulls_max"].get(kind, {"n": 0, "taut": 0})
            e = (rep.get("mode_effect") or {}).get(kind) or {"max_m": 0.0}
            out.append(f"{kind}: {c['links']} springs, {c['left_out_count']} pairs left out; at most {p.get('taut', 0)} "
                       f"taut, {p['n']} pulling; moves the map by at most {_m(e['max_m'])}")
    for s in rep["spots"]:
        if s.get("status") != "absent":
            out.append(f"spot check {s['a_name']} – {s['b_name']}: {s['status']}")
    out.append("modes files: " + ", ".join(f"{n} {v / 1e3:.0f} kB gz" for n, v in rep["sizes"].items()))
    return out
