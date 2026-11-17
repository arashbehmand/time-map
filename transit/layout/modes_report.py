"""
The travel modes' files (#23, L-03), beside the full time map's: written to modes/ of the time
map's folder by `python -m transit.layout.timemap --modes car`.

  modes.json            what modes there are, which combination is in which file, the train
                        springs' pulls at each position, which switches go straight across, roads.bin's
                        header, every file's size
  combo-car.json        car alone: every solved position (whole decimetres), its measures and pulls,
  combo-car-train.json  car with train and tube: the same; and both the in-between keyframes
  roads.bin             the main roads, carried through the mesh like the rail (land.py)
  report.md, .json      the car data, the Thames crossings, the combinations compared, the switches,
                        the restart checks and the spot check

The app serves modes/ like the map itself, under the same gate (server/transit/whole_city.py).

The train-only combination is the time map itself (timemap.json); a page switches from it to a
car combination by gliding to the nearest solved position and then either blending straight across
(when the build checked that blend never folds: mesh.blend_min_area, exact) or going back down to
the real map and up the other combination, both of which are checked blends already.
"""
from __future__ import annotations

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

MAX_GZIP_BYTES = 2_000_000          # each file a car combination loads, as the page's first load
ROAD_TOL_M = 20.0                   # the drawn roads are simplified this much
MODE_LIST = (
    {"id": "walk", "label": "Walking", "available": False,
     "why": "Comes with the next table rebuild: it needs walking-only times between the map's points."},
    {"id": "bus", "label": "Bus", "available": False,
     "why": "Comes with the next table rebuild: it needs bus-only times between the map's points."},
    {"id": "train", "label": "Train and tube", "available": True, "why": ""},
    {"id": "car", "label": "Car", "available": True, "why": ""},
)
COMBO_FILES = {"car": "combo-car.json", "car-train": "combo-car-train.json"}
NOTICE = ("Travel modes (#23, L-03). Car speeds: HERE Traffic4cast 2022 (MeTS-10), used non-commercially "
          "with the owner's permission.")


def _gz(data: bytes) -> int:
    import gzip
    return len(gzip.compress(data, mtime=0))


def _rho(v) -> str:
    return "the real map" if not v else f"ρ = {v:.3g}"


def blend_pairs(modes: dict) -> list[tuple[np.ndarray, np.ndarray]]:
    """Every blend the page can draw for the car combinations, as pairs of layouts (metres): each
    combination's neighbouring keyframes, and each straight switch between combinations."""
    out = []
    for cid, mo in modes["motions"].items():
        if cid == "train":
            continue
        keys = [k["dm"] / 10.0 for k in mo["keys"]]
        out += list(zip(keys, keys[1:]))
    for key, flags in modes["cross"].items():
        a, b = key.split("|")
        for k, fl in enumerate(flags):
            if fl and fl["ok"]:
                out.append((modes["combos"][a][k]["Xs"], modes["combos"][b][k]["Xs"]))
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


def build(*, b, pairs, mesh, links, frames, modes, stress_scale, code, page_gzip, land_dropped,
          bound: dict | None = None) -> tuple[dict, list]:
    """Every modes/ file as bytes ({path: bytes}) and the log's summary lines; refused when a file
    a car combination loads is over MAX_GZIP_BYTES gzipped."""
    from transit.layout.timemap import MODES_DIR
    from transit.layout.timemap_report import motion_doc

    common = {"build": b.manifest.get("build"), "window": b.window, "table_sha256": b.table_sha256, "nodes": int(mesh.n),
              **(bound or {}), "notice": NOTICE, "code": code}
    files, sizes = {}, {}
    for cid, name in COMBO_FILES.items():
        doc = {"schema": 1, "kind": "london-time-map-combo", "id": cid, "modes": cid.split("-"), **common,
               "frames": _frames_doc(modes["combos"][cid]), "motion": motion_doc(modes["motions"][cid])}
        files[name] = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode()
    roads_blob, roads_header = roads_layer(modes["roads"], mesh)
    files["roads.bin"] = roads_blob
    for name, data in files.items():
        sizes[name] = _gz(data)
    over = {n: s for n, s in sizes.items() if s > MAX_GZIP_BYTES}
    if over:
        raise RefusedOutput("the travel modes' files would be over 2,000,000 bytes gzipped ("
                            + "; ".join(f"{n}: {s:,}" for n, s in over.items()) + "); nothing was written")
    cl = modes["car_links"]
    index = {"schema": 1, "kind": "london-time-map-modes", **common, "licence": modes["car_report"].get("licence"),
             "overhead_min": cl["overhead_min"], "s_m_per_min": _r(pairs.s), "stress_scale": stress_scale,
             "modes": list(MODE_LIST), "default": ["train"],
             "combos": {"train": None, **{cid: {"file": n, "modes": cid.split("-")} for cid, n in COMBO_FILES.items()}},
             "rhos": [_r(f["rho"]) for f in frames],
             "train_pulls": [_r(f["pulls"]) for f in frames],
             "cross": {k: [_r(x) for x in v] for k, v in modes["cross"].items()},
             "roads": roads_header, "sizes": sizes, "report": True, "view_box": view_box(modes, mesh),
             "data_box_lonlat": modes["car_report"].get("data_box_lonlat"),
             "coverage": {k: modes["car_report"]["coverage"][k]["after"]
                          for k in ("all", "motorway", "inside_the_data_box", "motorway_inside_the_data_box")
                          if k in modes["car_report"]["coverage"]}}
    rep = report_json(b, pairs, mesh, links, modes, sizes, page_gzip, land_dropped, code)
    files["modes.json"] = json.dumps(index, separators=(",", ":"), ensure_ascii=False).encode()
    files["report.json"] = (json.dumps(rep, indent=1, ensure_ascii=False) + "\n").encode()
    files["report.md"] = report_md(rep).encode()
    summary = summary_lines(rep)
    return {f"{MODES_DIR}/{n}": d for n, d in files.items()}, summary


# ── the report ──────────────────────────────────────────────────────
def report_json(b, pairs, mesh, links, modes, sizes, page_gzip, land_dropped, code) -> dict:
    combos = modes["combos"]
    per = {}
    for cid, fr in combos.items():
        flips = max(f["metrics"]["folds"]["flipped"] for f in fr)
        mo = modes["motions"][cid]
        per[cid] = {
            "positions": len(fr), "flipped_max": flips, "converged": all(f["converged"] for f in fr),
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
    cr = modes["car_report"]
    restart = {cid: {"complete": r["complete"], "ladders": r["ladders"],
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
    out = {"schema": 1, "notice": NOTICE, "build": b.manifest.get("build"), "window": b.window,
           "table_sha256": b.table_sha256, "code": code, "s_m_per_min": pairs.s,
           "overhead_min": modes["car_links"]["overhead_min"], "car": cr, "combos": per, "compare": compare,
           "compare_at": modes["compare_at"], "cross": cross, "restart": restart, "spots": modes["spots"],
           "sizes": sizes, "page_gzip_bytes": page_gzip, "land_slivers_dropped": land_dropped,
           "train_links": len(links.pairs)}
    return _r(out)


def _at(d: dict, k):
    """d[k] for a position k whether the keys are numbers (in memory) or strings (from JSON)."""
    return d[k] if k in d else d[str(k)]


def _min(x) -> str:
    return "—" if x is None else f"{x:.0f} min"


def _km(x) -> str:
    return "—" if x is None else f"{x:.2f} km"


def _pct(x) -> str:
    return "—" if x is None else f"{100 * x:.0f}%"


def _wem(w: dict) -> str:
    if not w or "ratio_to_median" not in w:
        return str((w or {}).get("status", "—"))
    return f"{w['map_m'] / 1000:.2f} km, {w['ratio_to_median']:.3f}× ({w['status']})"


LABEL = {"train": "Train and tube", "car": "Car", "car-train": "Car + train and tube"}


def report_md(rep: dict) -> str:
    cr = rep["car"]
    sp = cr["speeds"]
    days = sp["days_used"]
    L = [f"# Travel modes: build {rep['build']}, {rep['window']}", "", f"**{rep['notice']}**", "",
         f"Code `{rep['code']}`; table sha256 `{rep['table_sha256'][:16]}…`. Definitions: `transit/README.md`, "
         "\"Travel modes\".", "", "## Headlines", ""]
    combos = rep["combos"]
    L.append(f"- **Three combinations**, each solved over the same 25 positions of the slider from the real map (ρ = 0, "
             f"the same for all): train and tube alone ({rep['train_links']} train springs, the time map itself), car "
             f"alone ({cr['links']['count']} road springs between {cr['links']['nodes']} nodes) and car with train and "
             f"tube. One scale for all: s = {rep['s_m_per_min']:.1f} m of map per minute.")
    L.append(f"- **Car times include {rep['overhead_min']:g} minutes once a trip** for parking and getting to and from "
             "the car. The road springs sit on a road layer of twins, so a trip along a road pays it once, however many "
             "nodes the road passes; train times come from the tables and include the walk to the platform and the wait.")
    L.append("- **How this differs from the issue's model:** the issue puts two modes' springs in parallel on the same "
             "pair of places, the faster one setting the distance and the slower going slack. Paying the overhead once a "
             "trip needs the road layer instead: car springs join twins, not places, and reach a place only through its "
             "access spring (slack within s·2.5 min of its twin) and a weak tie. Train and car springs therefore never "
             "share a pair; both can pull in the same area at once, and the car bounds a pair at s·(5 min + drive).")
    flips = max(c["flipped_max"] for c in combos.values())
    L.append(f"- **Flipped triangles: {flips} at every position of every combination**, on what ships (whole "
             "decimetres) and its Float32 image, and every blend the page draws between neighbouring keyframes of a "
             "combination keeps every triangle's area positive all the way (checked exactly).")
    for key, c in rep["cross"].items():
        a, b = key.split("|")
        L.append(f"- **Switching {LABEL[a]} ↔ {LABEL[b]}:** the straight blend between the two at the same position "
                 f"holds at {c['straight']} of {c['positions']} positions"
                 + (f" (every triangle keeps at least {100 * c['min_area_ratio']:.1f}% of its area on the way)"
                    if c["min_area_ratio"] is not None else "")
                 + (f"; at {', '.join(_rho(r) for r in c['via_real_map'])} the page goes back through the real map "
                    "instead, along both combinations' own checked blends." if c["via_real_map"] else "."))
    L.append(f"- **The car data:** HERE's Traffic4cast 2022 London speeds (the MeTS-10 pipeline's speeds on "
             f"OpenStreetMap edges), weekdays 13:00–14:00 (15-minute bins {', '.join(str(x) for x in sp['bins'])}), "
             f"{len(days)} days from {days[0]} to {days[-1]}: weekdays only, no bank holidays, no 23 December to "
             f"3 January, all before the covid period. {cr['clock']}")
    L.append(f"- **Licence:** {cr['licence']}")
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
    for cid, r in rep["restart"].items():
        worst = max(r["at"].values(), key=lambda v: v["shift_max_m"]) if r["at"] else None
        L.append(f"- **Restart check, {LABEL[cid]}:** " + ("complete" if r["complete"] else "INCOMPLETE")
                 + f" ({', '.join(r['ladders'])}; nudged starts)"
                 + (f"; no node more than {worst['shift_max_m']:.1f} m from the shipped layout"
                    f" ({worst['shift_at'] or '—'}, {worst['shift_run']})" if worst else "") + ".")
    for cid, c in combos.items():
        L.append(f"- **How thin triangles get, {LABEL[cid]}:** at the end of the slider "
                 f"{100 * c['stretched_4_end']:.1f}% of the drawn area is stretched past 4:1"
                 + ("; at no position is more than a tenth of it." if c["readable_until"] == c["frames"][-1]["rho"]
                    else f"; past {_rho(c['readable_until'])} more than a tenth of it is.") )
    sizes = rep["sizes"]
    L.append(f"- **Size:** the page's first load is {rep['page_gzip_bytes'] / 1e6:.2f} MB gzipped (train alone, as "
             "without the modes); switching Car on loads " + ", ".join(f"{n} {v / 1e3:.0f} kB" for n, v in sizes.items())
             + " gzipped, each under the 2 MB limit.")
    L += ["", "## The combinations compared", "",
          "Node shift: every station and cell, against its real place and against train alone at the same position. "
          "σ₁ is against the all-mode transit tables (the only times there are for every pair), so it measures how "
          "far each map is from public-transport time, not from its own modes' times.", ""]
    for k in rep["compare_at"]:
        rows = rep["compare"][str(k)] if str(k) in rep["compare"] else rep["compare"][k]
        rho = next(iter(rows.values()))["rho"]
        cids = list(rows)
        L += [f"### At {_rho(rho)}" + (" (the end of the slider)" if k == max(rep["compare_at"]) else
                                       " (train alone's first Wembley Park pass, or closest)"), "",
              "| | " + " | ".join(LABEL[c] for c in cids) + " |", "|---|" + "---:|" * len(cids)]

        def row(name, f, rows=rows, cids=cids):
            return f"| {name} | " + " | ".join(f(rows[c]) for c in cids) + " |"

        L.append(row("Median shift from the real map", lambda r: f"{r['shift_real_median_m'] / 1000:.2f} km"))
        L.append(row("Largest shift from the real map", lambda r: f"{r['shift_real_max_m'] / 1000:.2f} km "
                                                                  f"({r['shift_real_max_at']})"))
        L.append(row("Median shift from train alone", lambda r: f"{r['shift_train_median_m'] / 1000:.2f} km"))
        L.append(row("Largest shift from train alone", lambda r: f"{r['shift_train_max_m'] / 1000:.2f} km"))
        L.append(row("σ₁ (all-mode tables)", lambda r: f"{r['sigma1']:.3f}"))
        L.append(row("Wembley Park to Baker Street", lambda r: _wem(r["wembley"])))
        L.append(row("Thinnest triangle", lambda r: f"{r['stretch_max']:.1f}:1 near {r['stretch_worst'].get('near')}"))
        L.append(row("Smallest area kept", lambda r: f"{100 * r['area_ratio_min']:.1f}%"))
        L.append(row("Flipped triangles", lambda r: str(r["flipped"])))
        fr = {c: rep["combos"][c]["frames"][k] for c in cids}
        for fam, name in (("train", "Train springs pulling"), ("car", "Road springs pulling"),
                          ("car_access", "Access springs pulling")):
            cells = []
            for c in cids:
                p = fr[c]["pulls"].get(fam)
                cells.append("—" if p is None else f"{p['n']} of {p['of']} ({_pct(p['share'])})"
                             + (f": {', '.join(p.get('top', []))}" if p.get("top") else ""))
            L.append(f"| {name} | " + " | ".join(cells) + " |")
        L.append("")
    L += ["A spring pulls when it is stretched more than 1% past its rest length. A mode whose springs all stay "
          "slack moves nothing: its places are already as close on the map as its times say.", ""]
    for cid, c in combos.items():
        quiet = [f["rho"] for f in c["frames"][1:] if all(p["n"] == 0 for p in f["pulls"].values())]
        if quiet:
            L.append(f"- {LABEL[cid]}: no spring pulls at {', '.join(_rho(r) for r in quiet)}.")
    L += ["", "## Switching between combinations", "",
          "The page switches at a solved position: it glides to the nearest one, then blends straight to the other "
          "combination at that position when the build checked that blend (every triangle's smallest area along it, "
          "exactly, on whole decimetres), and otherwise goes back down to the real map and up the other combination.",
          "", "| Position | " + " | ".join(f"{LABEL[k.split('|')[0]]} ↔ {LABEL[k.split('|')[1]]}" for k in rep["cross"])
          + " |", "|---|" + "---|" * len(rep["cross"])]
    n = max(len(c["flags"]) for c in rep["cross"].values())
    for i in range(1, n):
        cells = []
        rho = None
        for c in rep["cross"].values():
            f = c["flags"][i]
            rho = f["rho"]
            cells.append(f"straight, {100 * f['min_area_ratio']:.1f}%" if f["ok"] else "through the real map")
        L.append(f"| {_rho(rho)} | " + " | ".join(cells) + " |")
    L += ["", "## The car data", "", "| Roads | km (directed) | Measured by node id | After the geometric match |",
          "|---|---:|---:|---:|"]
    for name, v in cov.items():
        L.append(f"| {name.replace('_', ' ')} | {v['km']:.0f} | {_pct(v['before'])} | {_pct(v['after'])} |")
    m = cr["matching"]
    L += ["", f"Traffic4cast edges with a 13:00–14:00 speed: {m['t4c_edges']:,}; {m['t4c_synthetic']:,} with a synthetic "
          f"node id and {m['t4c_vanished']:,} with a node OpenStreetMap no longer has were not used. "
          f"Traffic4cast's synthetic node ids sit inside roads, motorways most of all: {m.get('t4c_chains', 0):,} "
          f"chains real → synthetic → … → real were contracted to one edge each, at the harmonic mean of their "
          f"speeds, and {m.get('t4c_chains_on_our_roads', 0):,} of them landed on our roads. "
          f"{m['t4c_on_our_roads']:,} edges in all landed on our main roads by node id (the shortest path between their ends along "
          f"them at most {m['path_ratio']}× the straight distance; {m['t4c_path_too_long']:,} did not); the rest lent "
          f"their straight chord to pieces within {m['match_m']:g} m and {m['match_deg']:g}°. Filled speeds (each "
          "class's median measured speed): " + ", ".join(f"{k} {v} km/h" for k, v in m["fill_kph"].items() if v)
          + ".", "",
          f"Speed rows read: {sp['rows_read']:,}; in 13:00–14:00 on the days used: {sp['rows_in_window']:,}; under "
          f"{sp['rules']['min_kph']:g} km/h, left out: {sp['rows_under_min_kph']:,}; by probe volume class: "
          + ", ".join(f"{k}: {v:,}" for k, v in sp["volume_class_rows"].items()) + f". Zip sha256 "
          f"`{sp['zip_sha256'][:16]}…`.", ""]
    lk = cr["links"]
    why = Counter(d["why"].split(":")[0] if "measured" not in d["why"] else "too little of its road measured"
                  for d in lk["dropped"])
    L += [f"Car links: {lk['count']} kept on {lk['nodes']} nodes, from {lk['candidates']} candidates (roads cut into "
          f"{lk['piece_m']:g} m pieces, {lk['pieces']:,} of them); dropped: "
          + (", ".join(f"{v} {k}" for k, v in why.most_common()) or "none") + ". Median measured share of a kept "
          f"link's road: {_pct(lk['measured_share_median'])}.", "",
          "## Thames crossings", "", "| Crossing | Kind | Road | Car link |", "|---|---|---|---|"]
    for x in br:
        L.append(f"| {x['name']} | {x['kind']} | {'/'.join(x['classes'])}{' ' + x['ref'] if x['ref'] else ''} | "
                 + (", ".join(x["links"]) if x["links"] else f"none: {x['why']}") + " |")
    L += ["", "## Spot check", "",
          "A pair of stations faster by car (door to door, the overhead included) than by public transport should "
          "end closer with car on: that is a pass. A pair not faster by car makes no claim either way. The biggest "
          "gain is picked automatically among pairs at least 3 km apart; `[[car_spot]]` rows in checks.toml add more. "
          "The car bound is s × the car's minutes, the farthest apart the car springs hold the pair. The control is "
          "how much car and train shrinks the median station pair 3 km or more apart against train only, so a pair "
          "that shrinks only as much as everything around it reads as such.", "",
          "| Pair | Real | Public transport | Car | Car bound | " + " | ".join(f"{LABEL[c]}, {_rho(rep['compare'][str(k)]['train']['rho'] if str(k) in rep['compare'] else rep['compare'][k]['train']['rho'])}"
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
    L += ["", "## Restart check", "",
          "Each car combination solved again from the real map nudged on every free node (the same steps) and "
          "through other ladders of ρ (every other step, and each checked position in one step), compared with "
          "the shipped layout on every node.", "", "| Combination | Position | Largest node shift | Where | Run | "
          "Wembley Park statuses |", "|---|---|---:|---|---|---|"]
    for cid, r in rep["restart"].items():
        for v in r["at"].values():
            L.append(f"| {LABEL[cid]} | {_rho(v['rho'])} | {v['shift_max_m']:.1f} m | {v['shift_at'] or '—'} | "
                     f"{v['shift_run'] or '—'} | {', '.join(v['statuses'])} |")
    for cid, c in combos.items():
        L += ["", f"## Every position: {LABEL[cid]}", "",
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
    out = [f"modes: flipped triangles {max(c['flipped_max'] for c in rep['combos'].values())} in every combination"]
    for key, c in rep["cross"].items():
        out.append(f"switch {key}: straight at {c['straight']} of {c['positions']} positions")
    for s in rep["spots"]:
        if s.get("status") != "absent":
            out.append(f"spot check {s['a_name']} – {s['b_name']}: {s['status']}")
    out.append("modes files: " + ", ".join(f"{n} {v / 1e3:.0f} kB gz" for n, v in rep["sizes"].items()))
    return out

