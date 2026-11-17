"""
The land of the full London time map (#6, L-02): the Thames and other water, parks, rail lines
and the 33 borough outlines, from the build's own OpenStreetMap clip (osmium, then shapely, as
transit/venues.py reads it), cut along the mesh so that every piece lies inside one mesh
triangle, and stored so that every vertex moves exactly as the mesh's piecewise-linear map moves
it. A mesh with no flipped triangle then draws land that doesn't fold or crack.

- water: natural=water, waterway=riverbank, landuse=reservoir or basin;
- parks: leisure=park, common, nature_reserve or golf_course; landuse=recreation_ground,
  cemetery or forest; natural=wood or heath; water inside a park is cut out of it;
- rail: railway=rail (major), subway, light_rail or tram (minor), with no service=* tag
  (sidings, yards, spurs); tunnel=yes is drawn as a tunnel;
- boroughs: the areas with ref:gss E09000001 to E09000033 (the City is admin_level 6, the other
  32 are 8), every part of each, drawn as the union of their outlines so a shared border is drawn
  once. A build whose clip lacks one is refused.

Water and parks are dissolved first, then pieces smaller than `min_ha` are left out; everything
is simplified at `tol_m` and clipped to the convex hull of the stations and cells, which the
mesh's inner triangles tile exactly (the guard ring's triangles lie outside it).

How a vertex is stored: as three mesh nodes and two weights in 1/65535ths, the third weight one
minus the others, so its place on any layout X is w0·X[c0] + w1·X[c1] + w2·X[c2]. A vertex the
cut put on a mesh edge is stored by that edge alone (its lower node, its higher node, and how far
along from the lower one), and a vertex on a node by the node alone, the same way whichever piece
it belongs to: two pieces that share a point on an edge compute it with the same numbers, so they
never part. carry.js does the same arithmetic in the page; `place` here is its twin.

shapely (2.1 or later, for constrained_delaunay_triangles) and the osmium tool; only the L-02 job
imports this module, and only when it draws land.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np

from transit.layout.frame import to_local
from transit.layout.mesh import WEIGHT_Q as Q
from transit.layout.prep import RefusedInput, RefusedOutput

WATER = {"natural": {"water"}, "waterway": {"riverbank"}, "landuse": {"reservoir", "basin"}}
PARK = {"leisure": {"park", "common", "nature_reserve", "golf_course"},
        "landuse": {"recreation_ground", "cemetery", "forest"}, "natural": {"wood", "heath"}}
RAIL_MAJOR = {"rail"}
RAIL_MINOR = {"subway", "light_rail", "tram"}
BOROUGHS = tuple(f"E090000{k:02d}" for k in range(1, 34))
# Style names from static/shared/mesh/schema-streets-v8.js (tests check they exist there).
STYLE_NAMES = ("water", "park", "boundary", "major_rail", "minor_rail", "rail_tunnel")
# The car's main roads (#23, L-03), carried like the rail into their own file (roads.bin).
ROAD_STYLES = ("motorway", "trunk", "primary")
FILL_LAYERS = ("water", "park")
LINE_LAYERS = ("boundary", "rail")
MIN_PIECE_M2 = 1.0
MIN_PIECE_M = 1.0
EDGE_EPS = 1e-7               # a barycentric weight this small puts the vertex on the edge or node
# The size ladder: simplification first, then the smallest water and park kept. Never a layer.
LADDER = ((20.0, 2.0), (40.0, 2.0), (80.0, 2.0), (80.0, 5.0), (80.0, 10.0))
COARSE = (60.0, 5.0)          # for the report's panels


def _matches(tags: dict, rule: dict) -> bool:
    return any(tags.get(k) in v for k, v in rule.items())


# ── reading the clip ────────────────────────────────────────────────
def extract(clip: Path, work: Path) -> Path:
    """The clip's candidate features as GeoJSON lines (osmium tags-filter, then export)."""
    work.mkdir(parents=True, exist_ok=True)
    filtered = work / "land.osm.pbf"
    lines = work / "land.geojsonseq"
    exprs = [f"nwr/{k}={','.join(sorted(v))}" for k, v in {**WATER}.items()]
    exprs += [f"nwr/{k}={','.join(sorted(v))}" for k, v in PARK.items() if k != "natural"]
    exprs += ["nwr/natural=" + ",".join(sorted(WATER["natural"] | PARK["natural"]))]
    exprs += ["w/railway=" + ",".join(sorted(RAIL_MAJOR | RAIL_MINOR)), "r/boundary=administrative"]
    try:
        subprocess.run(["osmium", "tags-filter", str(clip), *exprs, "-o", str(filtered), "--overwrite"],
                       check=True, capture_output=True)
        subprocess.run(["osmium", "export", str(filtered), "-f", "geojsonseq",
                        "--geometry-types=linestring,polygon", "--add-unique-id=type_id", "-o", str(lines),
                        "--overwrite"], check=True, capture_output=True)
    except FileNotFoundError:
        raise RefusedInput("the osmium tool is not installed (brew install osmium-tool); --no-land skips the "
                           "land") from None
    except subprocess.CalledProcessError as e:
        raise RefusedInput(f"osmium failed on {clip.name}: {e.stderr.decode(errors='replace')[-300:]}") from None
    return lines


def parse_lines(lines: Iterable[str]) -> Iterable[dict]:
    from transit.venues import parse_lines as venue_lines
    return venue_lines(lines)


@dataclass
class Layers:
    """Shapely geometries in local metres, before clipping and cutting."""
    water: list = field(default_factory=list)          # polygons
    park: list = field(default_factory=list)           # polygons
    rail: list = field(default_factory=list)           # (linestring, style name)
    boroughs: dict = field(default_factory=dict)       # gss code -> (multi)polygon, every part
    counts: dict = field(default_factory=dict)


def _local(geom):
    import shapely
    return shapely.transform(geom, lambda c: to_local(c[:, 0], c[:, 1]))


def classify(features: Iterable[dict]) -> Layers:
    """Features (GeoJSON, properties = OSM tags) to water, parks, rail and boroughs. Every
    feature carrying a borough's code is part of that borough."""
    import shapely
    from shapely.geometry import shape

    out = Layers()
    parts: dict[str, list] = {}
    seen = {"features": 0, "water": 0, "park": 0, "rail": 0, "boroughs": 0, "unreadable": 0}
    for f in features:
        seen["features"] += 1
        tags = f.get("properties") or {}
        geom = f.get("geometry") or {}
        gtype = geom.get("type")
        poly = gtype in ("Polygon", "MultiPolygon")
        line = gtype in ("LineString", "MultiLineString")
        gss = tags.get("ref:gss", "")
        is_borough = poly and tags.get("boundary") == "administrative" and gss in BOROUGHS
        is_water = poly and _matches(tags, WATER)
        is_park = poly and not is_water and _matches(tags, PARK)
        rail = tags.get("railway")
        is_rail = line and rail in (RAIL_MAJOR | RAIL_MINOR) and not tags.get("service")
        if not (is_borough or is_water or is_park or is_rail):
            continue
        try:
            g = _local(shape(geom))
        except (ValueError, TypeError, AttributeError, KeyError):
            seen["unreadable"] += 1
            continue
        if g.is_empty:
            continue
        if not g.is_valid:
            g = g.buffer(0) if poly else g
        if is_borough:
            parts.setdefault(gss, []).append(g)
            seen["boroughs"] += 1
        if is_water:
            out.water.append(g)
            seen["water"] += 1
        elif is_park:
            out.park.append(g)
            seen["park"] += 1
        if is_rail:
            style = "rail_tunnel" if tags.get("tunnel") == "yes" else (
                "major_rail" if rail in RAIL_MAJOR else "minor_rail")
            out.rail.append((g, style))
            seen["rail"] += 1
    out.boroughs = {code: shapely.union_all(gs) for code, gs in sorted(parts.items())}
    out.counts = seen
    return out


def check_boroughs(layers: Layers) -> None:
    missing = [c for c in BOROUGHS if c not in layers.boroughs]
    if missing:
        raise RefusedInput(f"the OSM clip has {33 - len(missing)} of London's 33 boroughs (ref:gss); missing: "
                           f"{', '.join(missing)}")


# ── simplifying, clipping and cutting along the mesh ────────────────
@dataclass
class Carried:
    """Land cut along the mesh. Fills: pieces, each inside one triangle, already triangulated;
    lines: runs, each inside one triangle. Every vertex is stored as nodes and weights (`encode`)."""
    tol_m: float
    min_ha: float
    fill_style: np.ndarray        # (P,) style index (STYLE_NAMES)
    fill_nv: np.ndarray           # (P,) vertices per piece
    fill_nt: np.ndarray           # (P,) triangles per piece
    fill_c: np.ndarray            # (V, 3) mesh nodes
    fill_w: np.ndarray            # (V, 2) weights of nodes 1 and 2, in 1/Q-ths
    fill_idx: np.ndarray          # (3·T,) vertex index within its piece
    line_style: np.ndarray        # (R,)
    line_nv: np.ndarray           # (R,)
    line_c: np.ndarray            # (W, 3)
    line_w: np.ndarray            # (W, 2)
    counts: dict = field(default_factory=dict)


def _polys(g) -> list:
    if g is None or g.is_empty:
        return []
    if g.geom_type == "Polygon":
        return [g]
    if hasattr(g, "geoms"):
        return [p for part in g.geoms for p in _polys(part)]
    return []


def _lines(g) -> list:
    if g is None or g.is_empty:
        return []
    if g.geom_type == "LineString":
        return [g]
    if hasattr(g, "geoms"):
        return [p for part in g.geoms for p in _lines(part)]
    return []


def prepare(layers: Layers, hull, tol_m: float, min_ha: float) -> dict:
    """Dissolve, filter, simplify and clip each layer: {"water": [polygons], "park": [...],
    "boundary": [lines], "rail": [(line, style)]}. Water and parks are dissolved before the
    size floor, so a body drawn as several small features counts as one."""
    import shapely

    min_m2 = min_ha * 10_000
    w = shapely.union_all(layers.water) if layers.water else None
    p = shapely.union_all(layers.park) if layers.park else None
    if p is not None and w is not None:
        p = p.difference(w)
    out = {}
    for name, g in (("water", w), ("park", p)):
        big = [q for q in _polys(g) if q.area >= min_m2]
        if not big:
            out[name] = []
            continue
        g = shapely.union_all(big).simplify(tol_m, preserve_topology=True).intersection(hull)
        out[name] = [q for q in _polys(g) if q.area >= MIN_PIECE_M2]
    if layers.boroughs:
        outline = shapely.union_all([b.boundary for b in layers.boroughs.values()])
        outline = shapely.line_merge(outline).simplify(tol_m).intersection(hull)
        out["boundary"] = _lines(outline)
    else:
        out["boundary"] = []
    rail = []
    for style in ("major_rail", "minor_rail", "rail_tunnel"):
        parts = [g for g, s in layers.rail if s == style]
        if not parts:
            continue
        merged = shapely.line_merge(shapely.union_all(parts)).simplify(tol_m).intersection(hull)
        rail += [(ln, style) for ln in _lines(merged)]
    out["rail"] = rail
    return out


def encode(P: np.ndarray, corners: np.ndarray, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Points (n, 2) inside the triangle `corners` (three node indices) as (nodes (n, 3), weights
    (n, 2) in 1/Q-ths). A point on an edge is stored by the edge (lower node, higher node, higher
    node; weight along from the lower one), on a node by the node: the same numbers whichever
    triangle it was cut in."""
    pts = np.atleast_2d(np.asarray(pts, dtype=np.float64))
    a, b, c = (int(v) for v in corners)
    pa, pb, pc = P[a], P[b], P[c]
    e1, e2, q = pb - pa, pc - pa, pts - pa
    det = e1[0] * e2[1] - e1[1] * e2[0]
    w1 = (q[:, 0] * e2[1] - q[:, 1] * e2[0]) / det
    w2 = (e1[0] * q[:, 1] - e1[1] * q[:, 0]) / det
    W = np.stack([1 - w1 - w2, w1, w2], axis=1)
    nodes = np.array([a, b, c])
    C = np.empty((len(pts), 3), dtype=np.int64)
    Wq = np.zeros((len(pts), 2), dtype=np.int64)
    small = np.abs(W) < EDGE_EPS
    for k in range(len(pts)):
        s = small[k]
        if s.sum() >= 2:                                   # on a node
            n = int(nodes[int(np.argmax(W[k]))])
            C[k] = (n, n, n)
            continue
        if s.sum() == 1:                                   # on the edge opposite the small weight
            u, v = (int(x) for x in nodes[~s])
            lo, hi = min(u, v), max(u, v)
            d = P[hi] - P[lo]
            t = float(np.clip(np.dot(pts[k] - P[lo], d) / np.dot(d, d), 0.0, 1.0))
            tq = int(round(t * Q))
            if tq == 0 or tq == Q:
                n = lo if tq == 0 else hi
                C[k] = (n, n, n)
            else:
                C[k] = (lo, hi, hi)
                Wq[k] = (tq, 0)
            continue
        q1 = int(round(min(max(W[k, 1], 0.0), 1.0) * Q))
        q2 = int(round(min(max(W[k, 2], 0.0), 1.0) * Q))
        if q1 + q2 > Q:
            q2 = Q - q1
        C[k] = (a, b, c)
        Wq[k] = (q1, q2)
    return C, Wq


def place(C: np.ndarray, Wq: np.ndarray, X: np.ndarray) -> np.ndarray:
    """Stored vertices on a layout X (metres): the arithmetic carry.js does, in the same order."""
    w1 = Wq[:, 0] / Q
    w2 = Wq[:, 1] / Q
    w0 = 1 - w1 - w2
    x = w0 * X[C[:, 0], 0] + w1 * X[C[:, 1], 0] + w2 * X[C[:, 2], 0]
    y = w0 * X[C[:, 0], 1] + w1 * X[C[:, 1], 1] + w2 * X[C[:, 2], 1]
    return np.stack([x, y], axis=1)


def _area2(V: np.ndarray, idx: np.ndarray) -> np.ndarray:
    a, b, c = V[idx[:, 0]], V[idx[:, 1]], V[idx[:, 2]]
    return (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])


def carry(prepared: dict, P: np.ndarray, tri: np.ndarray, tol_m: float = 0.0, min_ha: float = 0.0,
          styles: tuple = STYLE_NAMES) -> Carried:
    """Cut each prepared layer along the mesh (real positions P, triangles tri). Lines come from
    "boundary" (lines), "rail" and "roads" ((line, style) pairs); `styles` names the style indices."""
    import shapely

    tri_polys = shapely.polygons(P[tri])
    tree = shapely.STRtree(tri_polys)
    style_id = {n: i for i, n in enumerate(styles)}

    f_style, f_nv, f_nt, f_c, f_w, f_idx = [], [], [], [], [], []
    for name in FILL_LAYERS:
        polys = prepared.get(name, [])
        if not polys:
            continue
        geoms = np.array(polys, dtype=object)
        gi, ti = tree.query(geoms, predicate="intersects")
        pieces = shapely.intersection(geoms[gi], tri_polys[ti])
        for t, piece in zip(ti, pieces):
            for poly in _polys(piece):
                if poly.area < MIN_PIECE_M2:
                    continue
                coords = []
                for tg in shapely.get_parts(shapely.constrained_delaunay_triangles(poly)):
                    if tg.geom_type != "Polygon" or tg.is_empty:
                        continue
                    coords.append(np.asarray(tg.exterior.coords)[:3])
                if not coords:
                    continue
                pts = np.concatenate(coords)
                C, Wq = encode(P, tri[t], pts)
                keys = np.concatenate([C, Wq], axis=1)
                uniq, inv = np.unique(keys, axis=0, return_inverse=True)
                idx = inv.reshape(-1, 3)
                Cu, Wu = uniq[:, :3], uniq[:, 3:]
                real = place(Cu, Wu, P)
                ar = _area2(real, idx)
                idx = np.where((ar < 0)[:, None], idx[:, [0, 2, 1]], idx)[ar != 0]
                if not len(idx):
                    continue
                f_style.append(style_id[name])
                f_nv.append(len(uniq))
                f_nt.append(len(idx))
                f_c.append(Cu)
                f_w.append(Wu)
                f_idx.append(idx.ravel())

    l_style, l_nv, l_c, l_w = [], [], [], []
    for name in (*LINE_LAYERS, "roads"):
        items = prepared.get(name, [])
        if not items:
            continue
        geoms = [g for g, _ in items] if name in ("rail", "roads") else items
        names = [s for _, s in items] if name in ("rail", "roads") else ["boundary"] * len(items)
        geoms = np.array(geoms, dtype=object)
        gi, ti = tree.query(geoms, predicate="intersects")
        pieces = shapely.intersection(geoms[gi], tri_polys[ti])
        for g, t, piece in zip(gi, ti, pieces):
            for ln in _lines(piece):
                if ln.length < MIN_PIECE_M:
                    continue
                C, Wq = encode(P, tri[t], np.asarray(ln.coords))
                keys = np.concatenate([C, Wq], axis=1)
                keep = np.ones(len(keys), dtype=bool)
                keep[1:] = (np.diff(keys, axis=0) != 0).any(axis=1)
                if keep.sum() < 2:
                    continue
                l_style.append(style_id[names[g]])
                l_nv.append(int(keep.sum()))
                l_c.append(C[keep])
                l_w.append(Wq[keep])

    def cat(parts, shape):
        return np.concatenate(parts) if parts else np.zeros(shape, dtype=np.int64)

    c = Carried(tol_m, min_ha,
                np.array(f_style, dtype=np.int64), np.array(f_nv, dtype=np.int64), np.array(f_nt, dtype=np.int64),
                cat(f_c, (0, 3)), cat(f_w, (0, 2)), cat(f_idx, (0,)),
                np.array(l_style, dtype=np.int64), np.array(l_nv, dtype=np.int64), cat(l_c, (0, 3)), cat(l_w, (0, 2)))
    c.counts = counts(c, styles)
    return c


def counts(c: Carried, styles: tuple = STYLE_NAMES) -> dict:
    return {"tol_m": c.tol_m, "min_ha": c.min_ha,
            "fill_pieces": int(len(c.fill_style)), "fill_vertices": int(len(c.fill_c)),
            "fill_triangles": int(len(c.fill_idx) // 3), "line_runs": int(len(c.line_style)),
            "line_vertices": int(len(c.line_c)),
            "by_style": {n: {"fill_pieces": int((c.fill_style == i).sum()),
                             "line_runs": int((c.line_style == i).sum())} for i, n in enumerate(styles)}}


def fill_triangles(c: Carried) -> np.ndarray:
    """Every fill triangle as three indices into fill_c / fill_w."""
    starts = np.concatenate([[0], np.cumsum(c.fill_nv)[:-1]]) if len(c.fill_nv) else np.zeros(0, dtype=np.int64)
    offs = np.repeat(starts, c.fill_nt * 3)
    return (c.fill_idx + offs).reshape(-1, 3)


# How far the shader's own Float32 arithmetic can move a drawn vertex: a few units in the last
# place of a coordinate 30 km from the origin (2 mm each), with room to spare.
BLEND_MARGIN_M = 0.01


def blend_slivers(layouts: list[np.ndarray], tri: np.ndarray, margin_m: float = BLEND_MARGIN_M) -> np.ndarray:
    """Which triangles (index triples into every layout's vertices) lose their orientation, or come
    within margin_m of it, on any layout or anywhere along the straight blend between two
    consecutive ones, the way the page draws them: the shader mixes the two layouts' Float32
    vertices. Along a blend a triangle's doubled area is c0 + c1·s + c2·s²; its minimum on [0, 1]
    is at an end, or at the vertex when c2 > 0 and 0 < −c1 < 2·c2. Moving each vertex by at most
    margin_m changes the doubled area by at most margin_m × the perimeter, and the perimeter along
    a blend is largest at an end, so the minimum must stay above 2 · margin_m · that perimeter.
    The layouts' values are Float32 numbers, so their differences and products are exact in
    float64."""
    tri = np.asarray(tri)
    bad = np.zeros(len(tri), dtype=bool)
    if not len(tri) or not layouts:
        return bad

    def edges(V):
        a, b, c = V[tri[:, 0]], V[tri[:, 1]], V[tri[:, 2]]
        per = np.hypot(*(b - a).T) + np.hypot(*(c - b).T) + np.hypot(*(a - c).T)
        return b - a, c - a, per

    cross = lambda p, q: p[:, 0] * q[:, 1] - p[:, 1] * q[:, 0]  # noqa: E731
    pairs = list(zip(layouts, layouts[1:])) or [(layouts[0], layouts[0])]
    for A, B in pairs:
        u, v, pa = edges(np.asarray(A, dtype=np.float64))
        ub, vb, pb = edges(np.asarray(B, dtype=np.float64))
        du, dv = ub - u, vb - v
        c0, c1, c2 = cross(u, v), cross(u, dv) + cross(du, v), cross(du, dv)
        lo = np.minimum(c0, c0 + c1 + c2)
        inside = (c2 > 0) & (c1 < 0) & (-c1 < 2 * c2)
        lo[inside] = np.minimum(lo[inside], c0[inside] - c1[inside] ** 2 / (4 * c2[inside]))
        bad |= ~(lo > 2 * margin_m * np.maximum(pa, pb))
    return bad


def drop_blend_slivers(c: Carried, layouts: list[np.ndarray],
                       pairs: list[tuple[np.ndarray, np.ndarray]] | None = None) -> tuple[Carried, int]:
    """Fill triangles as the page draws them (placed in float64, stored as Float32, mixed by the
    shader) must keep their orientation, with blend_slivers' margin, on every keyframe and all
    along every blend between consecutive ones (`layouts`, in slider order), and along every blend in
    `pairs` (two layouts each: the travel modes' own slider and the switches between them, #23). A
    sliver that doesn't is dropped (it is far below a pixel); returns the land without them and how
    many there were."""
    if not len(c.fill_idx):
        return c, 0
    tri_all = fill_triangles(c)

    def drawn(X):
        return place(c.fill_c, c.fill_w, np.asarray(X, dtype=np.float64)).astype(np.float32).astype(np.float64)

    V = [drawn(X) for X in layouts]
    bad = blend_slivers(V, tri_all)
    for A, B in pairs or ():
        bad |= blend_slivers([drawn(A), drawn(B)], tri_all)
    n_bad = int(bad.sum())
    if not n_bad:
        return c, 0
    keep_tri = ~bad
    nt_new = np.add.reduceat(keep_tri.astype(np.int64), np.concatenate([[0], np.cumsum(c.fill_nt)[:-1]])) \
        if len(c.fill_nt) else np.zeros(0, dtype=np.int64)
    idx_new = c.fill_idx.reshape(-1, 3)[keep_tri].ravel()
    piece_keep = nt_new > 0
    v_keep = np.repeat(piece_keep, c.fill_nv)
    out = Carried(c.tol_m, c.min_ha, c.fill_style[piece_keep], c.fill_nv[piece_keep], nt_new[piece_keep],
                  c.fill_c[v_keep], c.fill_w[v_keep], idx_new, c.line_style, c.line_nv, c.line_c, c.line_w)
    out.counts = counts(out)
    return out, n_bad


# ── the packed file the page reads ──────────────────────────────────
def pack(c: Carried, n_nodes: int, styles: tuple = STYLE_NAMES, file: str = "land.bin") -> tuple[bytes, dict]:
    """land.bin and the header that describes it (for timemap.json). Little-endian arrays, each
    at a 4-byte boundary: styles uint8, counts uint32, nodes uint16 (three a vertex), weights
    uint16 (two a vertex), fill indices uint16 within each piece."""
    if n_nodes > 65535:
        raise RefusedOutput(f"the mesh has {n_nodes} nodes; land.bin stores node numbers in 16 bits")
    if len(c.fill_nv) and int(c.fill_nv.max()) > 65535:
        raise RefusedOutput(f"a land piece has {int(c.fill_nv.max())} vertices; land.bin indexes a piece's "
                            "vertices in 16 bits")
    arrays = [
        ("fill_style", "uint8", c.fill_style.astype("u1")),
        ("fill_nv", "uint32", c.fill_nv.astype("<u4")),
        ("fill_nt", "uint32", c.fill_nt.astype("<u4")),
        ("fill_c", "uint16", c.fill_c.astype("<u2").ravel()),
        ("fill_w", "uint16", c.fill_w.astype("<u2").ravel()),
        ("fill_idx", "uint16", c.fill_idx.astype("<u2")),
        ("line_style", "uint8", c.line_style.astype("u1")),
        ("line_nv", "uint32", c.line_nv.astype("<u4")),
        ("line_c", "uint16", c.line_c.astype("<u2").ravel()),
        ("line_w", "uint16", c.line_w.astype("<u2").ravel()),
    ]
    blob = bytearray()
    header = []
    for name, dtype, a in arrays:
        blob += b"\0" * (-len(blob) % 4)
        header.append({"name": name, "dtype": dtype, "offset": len(blob), "length": int(a.size)})
        blob += a.tobytes()
    return bytes(blob), {"file": file, "styles": list(styles), "arrays": header, "weight_scale": Q,
                         "tol_m": c.tol_m, "min_ha": c.min_ha, "counts": c.counts}


def unpack(blob: bytes, header: dict) -> Carried:
    """pack()'s inverse (for tests and the report)."""
    out = {}
    for a in header["arrays"]:
        dt = {"uint16": "<u2", "uint8": "u1", "uint32": "<u4", "int32": "<i4"}[a["dtype"]]
        out[a["name"]] = np.frombuffer(blob, dtype=dt, count=a["length"], offset=a["offset"]).astype(np.int64)
    c = Carried(header.get("tol_m", 0.0), header.get("min_ha", 0.0), out["fill_style"], out["fill_nv"],
                out["fill_nt"], out["fill_c"].reshape(-1, 3), out["fill_w"].reshape(-1, 2), out["fill_idx"],
                out["line_style"], out["line_nv"], out["line_c"].reshape(-1, 3), out["line_w"].reshape(-1, 2))
    c.counts = counts(c)
    return c
