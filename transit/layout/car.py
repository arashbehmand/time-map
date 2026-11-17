"""
Car links for the London time map's travel modes (#23, L-03): the main roads of the build's own
OpenStreetMap clip, typical weekday 13:00-14:00 speeds on them (transit/layout/carspeed.py), and
the links the car springs sit on.

    python -m transit.layout.car --build <id> [--window wd13] [--t4c-zip PATH]

**Roads.** `highway=motorway|trunk|primary` (mainline, no slip roads), their bridges and tunnels
included, each way one way or both as its `oneway` tag (or `junction=roundabout`, or a motorway's
implied one way) says. Every stretch between two OSM nodes is a directed edge.

**Speeds.** Traffic4cast's edges (u, v: OSM node ids) have no geometry, so they land on our roads by
node id first: where both u and v are on our main roads, the edge's speed goes to every edge of the
shortest path from u to v along them, if that path is at most CAR_PATH_RATIO times the straight
distance (the "before" share). Traffic4cast's synthetic node ids (≥ 2⁴⁰, on no map) sit inside roads,
motorways most of all: each chain real → synthetic → … → real counts as one edge between its real
ends first (contract_chains). Then, for our edges still without one: the straight chord of a
Traffic4cast edge whose ends the clip still has, within CAR_MATCH_M of the edge's middle and within
CAR_MATCH_DEG of its direction, nearest first (the "after" share). An edge claimed by several gets
their median. Edges with a node id the clip no longer has, or a synthetic one on its own, are counted
and not used. The rest of our edges take their class's median measured speed, and are marked
filled.

**Links.** Every road is cut into pieces at most CAR_PIECE_M long; each piece's end belongs to its
nearest station or cell (never a guard node). A piece whose ends belong to different nodes A and B
makes A–B a candidate. Within the road stretches owned by A or B, joined up, the link's time is the
drive from A's anchor (its own road point nearest it) to B's, at the edges' speeds, each way that
exists; the mean of the two (or the one), floored at 0.5 min; the fastest stretch when there are
several. A link with under CAR_FILL_MIN_SHARE of its road length measured is dropped and listed,
as is one over the build's time limit, and one whose two ends are both outside Greater London (the
M25 and its spurs at the mesh's edge, where its fast springs crush cells far apart into slivers). One
link per pair of nodes. Cutting the roads finer changes
which piece ends a boundary falls between, not which nodes are joined.

**Thames crossings.** Every bridge or tunnel of a motor road (any class) that crosses the River
Thames inside Greater London (transit/london_region.geojson), named, with the car link that
crosses it or why there is none.

Writes, into transit/out/car/<build>/<window>/ in this checkout (git-ignored, like every file made
from the transit data): car_links.json (the links, their
times and roads, the mesh they were built on), roads.json (the main roads as polylines, for
drawing) and car_report.json (coverage, drops, bridges, the speeds' provenance).

The tools land.py uses (osmium, shapely); numpy for the rest.
"""
from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from transit import settings
from transit.layout.frame import to_local
from transit.layout.mesh import RING, Mesh
from transit.layout.prep import FLOOR_MIN, Refused, RefusedInput, RefusedOutput

MOTOR = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link",
         "tertiary", "tertiary_link", "unclassified", "residential", "living_street", "service"}
THAMES = "River Thames"
OUT_FILES = ("car_links.json", "roads.json", "car_report.json")


def default_out(build: str, window: str) -> Path:
    from transit.layout.job import REPO
    return REPO / "transit" / "out" / "car" / build / window


# ── reading the clip ────────────────────────────────────────────────
_ESC = re.compile(r"%([0-9a-fA-F]+)%")


def _unescape(s: str) -> str:
    return _ESC.sub(lambda m: chr(int(m.group(1), 16)), s)


def _tags(field_: str) -> dict:
    out = {}
    if not field_:
        return out
    for kv in field_.split(","):
        k, _, v = kv.partition("=")
        out[_unescape(k)] = _unescape(v)
    return out


@dataclass
class Way:
    id: int
    nodes: list[int]
    tags: dict


def parse_opl(lines) -> tuple[dict[int, tuple[float, float]], list[Way]]:
    """OSM's OPL text: node coordinates (lon, lat) and ways (node ids, tags)."""
    nodes, ways = {}, []
    for line in lines:
        line = line.rstrip("\n")
        if not line:
            continue
        kind = line[0]
        parts = line.split(" ")
        if kind == "n":
            x = y = None
            for p in parts[1:]:
                if p.startswith("x") and len(p) > 1:
                    x = float(p[1:])
                elif p.startswith("y") and len(p) > 1:
                    y = float(p[1:])
            if x is not None and y is not None:
                nodes[int(parts[0][1:])] = (x, y)
        elif kind == "w":
            tags, refs = {}, []
            for p in parts[1:]:
                if p.startswith("T"):
                    tags = _tags(p[1:])
                elif p.startswith("N"):
                    refs = [int(r[1:]) for r in p[1:].split(",") if r]
            ways.append(Way(int(parts[0][1:]), refs, tags))
    return nodes, ways


def _osmium(args: list[str], what: str, ok_codes=(0,)) -> None:
    try:
        r = subprocess.run(["osmium", *args], capture_output=True)
    except FileNotFoundError:
        raise RefusedInput("the osmium tool is not installed (brew install osmium-tool)") from None
    if r.returncode not in ok_codes:
        raise RefusedInput(f"osmium failed on {what}: {r.stderr.decode(errors='replace')[-300:]}")


def extract(clip: Path, work: Path) -> Path:
    """The clip's main roads, every bridge and tunnel, and its rivers, as OPL text (with the nodes
    they use)."""
    work.mkdir(parents=True, exist_ok=True)
    filtered, opl = work / "roads.osm.pbf", work / "roads.opl"
    _osmium(["tags-filter", str(clip), "w/highway=" + ",".join(settings.CAR_HIGHWAYS), "w/bridge", "w/tunnel",
             "w/waterway=river", "-o", str(filtered), "--overwrite"], clip.name)
    _osmium(["cat", str(filtered), "-f", "opl", "-o", str(opl), "--overwrite"], filtered.name)
    return opl


def node_coords(clip: Path, ids, work: Path) -> dict[int, tuple[float, float]]:
    """Coordinates of the given node ids that the clip has (osmium getid)."""
    ids = sorted({int(i) for i in ids if 0 < int(i) < settings.T4C_SYNTHETIC_ID})
    if not ids:
        return {}
    work.mkdir(parents=True, exist_ok=True)
    want, got = work / "t4c_nodes.txt", work / "t4c_nodes.opl"
    want.write_text("".join(f"n{i}\n" for i in ids))
    # getid exits 1 when some ids aren't in the file: the vanished ones, counted later.
    _osmium(["getid", "-i", str(want), str(clip), "-f", "opl", "-o", str(got), "--overwrite"], clip.name, (0, 1))
    with open(got, encoding="utf-8") as f:
        nodes, _ = parse_opl(f)
    return nodes


# ── the road graph ──────────────────────────────────────────────────
CLOSED = {"no", "private"}


def closed_to_cars(tags: dict) -> bool:
    """Closed to cars, by OpenStreetMap's access hierarchy: the most specific tag set decides
    (motorcar, then motor_vehicle, then vehicle, then access)."""
    for k in ("motorcar", "motor_vehicle", "vehicle", "access"):
        if k in tags:
            return tags[k] in CLOSED
    return False


def is_car_road(tags: dict) -> bool:
    """A main road a car may drive: motorway, trunk or primary, not an area, not closed to cars."""
    return tags.get("highway") in settings.CAR_HIGHWAYS and tags.get("area") != "yes" and not closed_to_cars(tags)


def directions(tags: dict) -> tuple[bool, bool]:
    """(forward, backward) a car may drive along the way's node order."""
    ow = tags.get("oneway", "")
    if ow == "-1":
        return False, True
    if ow in ("yes", "true", "1"):
        return True, False
    if ow in ("no", "false", "0"):
        return True, True
    if tags.get("junction") in ("roundabout", "circular") or tags.get("highway") == "motorway":
        return True, False
    return True, True


def label_of(tags: dict) -> str:
    """A road's name for the readout: its ref (A40), else its name."""
    return tags.get("ref") or tags.get("name") or tags.get("highway", "road")


@dataclass
class Graph:
    """Our main roads: node ids and positions (local metres), directed edges between consecutive
    OSM nodes, and the way each edge belongs to."""
    ids: np.ndarray                  # (N,) OSM node ids, sorted
    xy: np.ndarray                   # (N, 2)
    ea: np.ndarray                   # (E,) from (index into ids)
    eb: np.ndarray                   # (E,) to
    length: np.ndarray               # (E,) metres
    way: np.ndarray                  # (E,) index into ways
    ways: list[Way]
    missing_nodes: int = 0

    def index(self, osm_id: int) -> int | None:
        k = int(np.searchsorted(self.ids, osm_id))
        return k if k < len(self.ids) and self.ids[k] == osm_id else None

    @property
    def cls(self) -> np.ndarray:
        return np.array([self.ways[w].tags.get("highway", "") for w in self.way.tolist()], dtype=object)


def road_graph(nodes: dict[int, tuple[float, float]], ways: list[Way]) -> Graph:
    roads = [w for w in ways if is_car_road(w.tags)]
    used = sorted({n for w in roads for n in w.nodes if n in nodes})
    ids = np.array(used, dtype=np.int64)
    ll = np.array([nodes[i] for i in used]) if used else np.zeros((0, 2))
    xy = to_local(ll[:, 0], ll[:, 1]) if used else np.zeros((0, 2))
    pos = {n: k for k, n in enumerate(used)}
    ea, eb, wi = [], [], []
    missing = 0
    for k, w in enumerate(roads):
        fwd, bwd = directions(w.tags)
        for a, b in zip(w.nodes, w.nodes[1:]):
            if a not in pos or b not in pos:
                missing += 1
                continue
            if a == b:
                continue
            if fwd:
                ea.append(pos[a]); eb.append(pos[b]); wi.append(k)      # noqa: E702
            if bwd:
                ea.append(pos[b]); eb.append(pos[a]); wi.append(k)      # noqa: E702
    ea, eb = np.array(ea, dtype=np.int64), np.array(eb, dtype=np.int64)
    length = np.hypot(*(xy[eb] - xy[ea]).T) if len(ea) else np.zeros(0)
    return Graph(ids, xy, ea, eb, length, np.array(wi, dtype=np.int64), roads, missing)


def _dijkstra(adj, src: int, dst: int, limit: float, allowed=None):
    """Shortest path src → dst over adj[i] = [(j, cost, edge)], stopping past `limit`;
    (cost, [edges]) or (inf, [])."""
    dist = {src: 0.0}
    prev = {}
    q = [(0.0, src)]
    while q:
        d, i = heapq.heappop(q)
        if d > dist.get(i, math.inf):
            continue
        if i == dst:
            path = []
            while i != src:
                e, i = prev[i]
                path.append(e)
            return d, path[::-1]
        for j, c, e in adj[i]:
            if allowed is not None and j not in allowed:
                continue
            nd = d + c
            if nd <= limit and nd < dist.get(j, math.inf):
                dist[j] = nd
                prev[j] = (e, i)
                heapq.heappush(q, (nd, j))
    return math.inf, []


# ── speeds on our roads ─────────────────────────────────────────────
def contract_chains(u: np.ndarray, v: np.ndarray, kph: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Traffic4cast's synthetic node ids (≥ 2⁴⁰), which no map has, are the nodes it added where a loop
    counter had to be attached to its road graph (its README; the other node ids are OSM's). A counter
    node splits one edge in two, so most have one edge in and one out between two real OSM nodes, and
    the roads with counters (motorways most of all, Highways England's) are largely under them. Each directed chain real → synthetic → … → real becomes one edge between its
    two real ends, at the harmonic mean of its edges' speeds (each edge's length is unknown, so they
    count alike). A chain that branches, loops or ends inside the synthetic nodes is left out.
    Returns (u, v, kph) of the chains."""
    S = settings.T4C_SYNTHETIC_ID
    out = defaultdict(list)
    for a, b, k in zip(u.tolist(), v.tolist(), kph.tolist()):
        out[a].append((b, k))
    cu, cv, ck = [], [], []
    for a in sorted(out):
        if a >= S:
            continue
        for b, k in out[a]:
            if b < S:
                continue
            speeds, cur, seen = [k], b, {b}
            while cur >= S:
                nxt = [(x, sk) for x, sk in out.get(cur, []) if x not in seen and x != a]
                if len(nxt) != 1:
                    break
                cur, sk = nxt[0]
                speeds.append(sk)
                seen.add(cur)
            if cur < S and cur != a and all(x > 0 for x in speeds):
                cu.append(a)
                cv.append(cur)
                ck.append(len(speeds) / sum(1.0 / x for x in speeds))
    return np.array(cu, dtype=np.int64), np.array(cv, dtype=np.int64), np.array(ck, dtype=np.float64)


def place_speeds(g: Graph, speeds: dict, t4c_xy: dict[int, tuple[float, float]]) -> dict:
    """Each of our directed edges' 13:00 speed (km/h) and where it came from (see the module
    docstring). `speeds` = {"u", "v", "median_kph"} arrays; t4c_xy = lon/lat of Traffic4cast node ids
    the clip has (ours included)."""
    import shapely

    E = len(g.ea)
    claims: list[list[float]] = [[] for _ in range(E)]
    adj = defaultdict(list)
    for e, (a, b, ln) in enumerate(zip(g.ea.tolist(), g.eb.tolist(), g.length.tolist())):
        adj[a].append((b, ln, e))
    u, v, kph = (np.asarray(speeds[k]) for k in ("u", "v", "median_kph"))
    u, v = u.astype(np.int64), v.astype(np.int64)
    synthetic = (u >= settings.T4C_SYNTHETIC_ID) | (v >= settings.T4C_SYNTHETIC_ID)
    known = np.array([(int(a) in t4c_xy) and (int(b) in t4c_xy) for a, b in zip(u.tolist(), v.tolist())], dtype=bool)
    counts = {"t4c_edges": int(len(u)), "t4c_synthetic": int(synthetic.sum()),
              "t4c_vanished": int((~synthetic & ~known).sum()), "t4c_on_our_roads": 0, "t4c_path_too_long": 0}
    # Synthetic chains contracted to edges between real nodes, matched like any other edge.
    cu, cv, ck = contract_chains(u, v, kph)
    counts["t4c_chains"] = int(len(cu))
    n0 = len(u)
    u, v, kph = np.concatenate([u, cu]), np.concatenate([v, cv]), np.concatenate([np.asarray(kph, float), ck])
    synthetic = np.concatenate([synthetic, np.zeros(len(cu), dtype=bool)])
    known = np.concatenate([known, np.array([(int(a) in t4c_xy) and (int(b) in t4c_xy) for a, b in zip(cu, cv)],
                                            dtype=bool)])
    on_ids = {}
    for k, (a, b) in enumerate(zip(u.tolist(), v.tolist())):
        if synthetic[k]:
            continue
        ia, ib = g.index(a), g.index(b)
        if ia is None or ib is None or ia == ib:
            continue
        straight = float(np.hypot(*(g.xy[ib] - g.xy[ia])))
        cost, path = _dijkstra(adj, ia, ib, settings.CAR_PATH_RATIO * straight + 1.0)
        if not path:
            counts["t4c_path_too_long"] += 1
            continue
        counts["t4c_on_our_roads"] += 1
        if k >= n0:
            counts["t4c_chains_on_our_roads"] = counts.get("t4c_chains_on_our_roads", 0) + 1
        on_ids[k] = path
        for e in path:
            claims[e].append(float(kph[k]))
    source = np.full(E, "", dtype=object)
    speed = np.full(E, np.nan)
    for e in range(E):
        if claims[e]:
            speed[e] = float(np.median(claims[e]))
            source[e] = "id"
    # Then by geometry, for the edges still without a speed.
    chord_k = [k for k in range(len(u)) if not synthetic[k] and known[k] and k not in on_ids]
    counts["t4c_chords"] = len(chord_k)
    todo = np.where(source == "")[0]
    if chord_k and len(todo):
        A = np.array([t4c_xy[int(u[k])] for k in chord_k])
        B = np.array([t4c_xy[int(v[k])] for k in chord_k])
        A, B = to_local(A[:, 0], A[:, 1]), to_local(B[:, 0], B[:, 1])
        ok = np.hypot(*(B - A).T) > 1.0
        A, B, ck = A[ok], B[ok], np.array(chord_k)[ok]
        lines = shapely.linestrings(np.stack([A, B], axis=1))
        tree = shapely.STRtree(lines)
        mid = (g.xy[g.ea[todo]] + g.xy[g.eb[todo]]) / 2
        pts = shapely.points(mid)
        qi, li = tree.query(pts, predicate="dwithin", distance=settings.CAR_MATCH_M)
        dirs = g.xy[g.eb[todo]] - g.xy[g.ea[todo]]
        cdir = B - A
        best = {}
        cos_min = math.cos(math.radians(settings.CAR_MATCH_DEG))
        for q, li_ in zip(qi.tolist(), li.tolist()):
            d0, d1 = dirs[q], cdir[li_]
            c = float(d0 @ d1) / max(1e-9, float(np.hypot(*d0) * np.hypot(*d1)))
            if c < cos_min:
                continue
            dist = float(shapely.distance(pts[q], lines[li_]))
            if q not in best or dist < best[q][0]:
                best[q] = (dist, li_)
        for q, (_, li_) in best.items():
            e = todo[q]
            speed[e] = float(kph[ck[li_]])
            source[e] = "geometry"
    # Fill: each class's median measured speed.
    cls = g.cls
    fill = {}
    for c in sorted(set(cls.tolist())):
        m = (cls == c) & (source != "")
        fill[c] = float(np.median(speed[m])) if m.any() else math.nan
    for e in np.where(source == "")[0]:
        speed[e] = fill[cls[e]]
        source[e] = "filled" if math.isfinite(fill[cls[e]]) else ""
    counts["fill_kph"] = {c: (round(v, 2) if math.isfinite(v) else None) for c, v in fill.items()}
    return {"speed": speed, "source": source, "counts": counts}


def data_box(t4c_xy: dict, speeds: dict) -> tuple[float, float, float, float] | None:
    """The lon/lat box Traffic4cast's London speeds cover (min lon, min lat, max lon, max lat): its
    city is a fixed rectangle, so roads outside it have no speed at all."""
    ids = set(np.asarray(speeds["u"]).tolist()) | set(np.asarray(speeds["v"]).tolist())
    ll = np.array([t4c_xy[i] for i in ids if i in t4c_xy])
    if not len(ll):
        return None
    return float(ll[:, 0].min()), float(ll[:, 1].min()), float(ll[:, 0].max()), float(ll[:, 1].max())


def coverage(g: Graph, source: np.ndarray, box_xy: tuple | None = None) -> dict:
    """The share of our main-road length (directed: a two-way road counts both ways) with a
    measured speed, by node id only ("before") and after the geometric match too ("after"), by class,
    for bridges and tunnels apart, and inside the data's box (`box_xy`: x0, y0, x1, y1, local metres)."""
    cls = g.cls
    bridge = np.array([g.ways[w].tags.get("bridge", "no") not in ("no", "") for w in g.way.tolist()], dtype=bool)
    tunnel = np.array([g.ways[w].tags.get("tunnel", "no") not in ("no", "") for w in g.way.tolist()], dtype=bool)
    groups = {"all": np.ones(len(cls), dtype=bool), **{c: cls == c for c in settings.CAR_HIGHWAYS},
              "bridges": bridge, "tunnels": tunnel}
    if box_xy is not None:
        x0, y0, x1, y1 = box_xy
        mid = (g.xy[g.ea] + g.xy[g.eb]) / 2
        inside = (mid[:, 0] >= x0) & (mid[:, 0] <= x1) & (mid[:, 1] >= y0) & (mid[:, 1] <= y1)
        groups["inside_the_data_box"] = inside
        groups.update({f"{c}_inside_the_data_box": inside & (cls == c) for c in settings.CAR_HIGHWAYS})
    out = {}
    for name, m in groups.items():
        L = float(g.length[m].sum())
        out[name] = {"km": round(L / 1000, 1),
                     "before": float(g.length[m & (source == "id")].sum() / L) if L else 0.0,
                     "after": float(g.length[m & np.isin(source, ["id", "geometry"])].sum() / L) if L else 0.0}
    return out


# ── pieces, owners and links ────────────────────────────────────────
@dataclass
class Pieces:
    xy: np.ndarray                   # (V, 2) piece-end positions: OSM nodes first, then interior points
    pa: np.ndarray                   # (P,) directed pieces
    pb: np.ndarray
    length: np.ndarray
    edge: np.ndarray                 # (P,) the directed OSM edge each piece is part of
    owner: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))   # (V,) mesh node


def cut(g: Graph, step: float | None = None) -> Pieces:
    """Every directed edge cut into pieces at most `step` long (CAR_PIECE_M). The two directions of a
    two-way road share their interior points."""
    step = settings.CAR_PIECE_M if step is None else step
    xy = [g.xy]
    nxt = len(g.xy)
    interior: dict[tuple[int, int], list[int]] = {}
    pa, pb, pl, pe = [], [], [], []
    for e, (a, b, ln) in enumerate(zip(g.ea.tolist(), g.eb.tolist(), g.length.tolist())):
        k = max(1, math.ceil(ln / step - 1e-9))
        key = (min(a, b), max(a, b))
        if key not in interior:
            lo, hi = key
            t = np.arange(1, k) / k
            pts = g.xy[lo] + t[:, None] * (g.xy[hi] - g.xy[lo])
            interior[key] = list(range(nxt, nxt + len(pts)))
            xy.append(pts)
            nxt += len(pts)
        mids = interior[key] if a < b else interior[key][::-1]
        chain = [a, *mids, b]
        seg = ln / (len(chain) - 1)
        for p, q in zip(chain, chain[1:]):
            pa.append(p); pb.append(q); pl.append(seg); pe.append(e)     # noqa: E702
    return Pieces(np.vstack(xy), np.array(pa, dtype=np.int64), np.array(pb, dtype=np.int64), np.array(pl),
                  np.array(pe, dtype=np.int64))


def owners(points: np.ndarray, mesh: Mesh, chunk: int = 4096) -> np.ndarray:
    """Each point's nearest station or cell (a mesh node index; never a guard node)."""
    cand = np.where(mesh.kinds != RING)[0]
    C = mesh.P[cand]
    out = np.empty(len(points), dtype=np.int64)
    for s in range(0, len(points), chunk):
        d = ((points[s:s + chunk, None, :] - C[None, :, :]) ** 2).sum(-1)
        out[s:s + chunk] = cand[np.argmin(d, axis=1)]
    return out


def _minutes(length_m, kph):
    return length_m / 1000.0 / kph * 60.0


def car_links(g: Graph, sp: dict, mesh: Mesh, max_time: float, labels: list[str] | None = None,
              inside: np.ndarray | None = None) -> dict:
    """The links (see the module docstring). With `inside` (per mesh node: in Greater London), a link
    whose two ends are both outside is dropped: there, at the mesh's edge where cells are far apart,
    the M25's fast springs crush a triangle to a sliver (590:1 on build 2026-11-12)."""
    pc = cut(g)
    pc.owner = owners(pc.xy, mesh)
    speed = sp["speed"][pc.edge]
    measured = np.isin(sp["source"][pc.edge], ["id", "geometry"])
    cost = np.where(np.isfinite(speed) & (speed > 0), _minutes(pc.length, np.where(speed > 0, speed, 1.0)), np.inf)
    adj = defaultdict(list)                  # directed, for timing
    und = defaultdict(set)                   # undirected, for stretches
    for p, (a, b, c) in enumerate(zip(pc.pa.tolist(), pc.pb.tolist(), cost.tolist())):
        if math.isfinite(c):
            adj[a].append((b, c, p))
        und[a].add(b)
        und[b].add(a)
    by_owner = defaultdict(set)
    for vtx, o in enumerate(pc.owner.tolist()):
        by_owner[o].add(vtx)
    oa, ob = pc.owner[pc.pa], pc.owner[pc.pb]
    cand = sorted({(min(x, y), max(x, y)) for x, y in zip(oa.tolist(), ob.tolist()) if x != y})
    n_st = int((mesh.kinds == 0).sum())

    def name(i: int) -> str:
        """A station's name; a cell by its nearest station."""
        if labels and i < len(labels):
            return labels[i]
        if labels and n_st:
            j = int(np.argmin(np.hypot(*(mesh.P[:n_st] - mesh.P[i]).T)))
            return f"near {labels[j]}" if j < len(labels) else f"node {i}"
        return f"node {i}"

    links, dropped = [], []
    for A, B in cand:
        allowed = by_owner[A] | by_owner[B]
        seen, stretches = set(), []
        for start in sorted(allowed):
            if start in seen:
                continue
            comp, stack = {start}, [start]
            while stack:                                  # one joined-up stretch
                i = stack.pop()
                for j in und[i]:
                    if j in allowed and j not in comp:
                        comp.add(j)
                        stack.append(j)
            seen |= comp
            ca = [x for x in comp if pc.owner[x] == A]
            cb = [x for x in comp if pc.owner[x] == B]
            if not ca or not cb:
                continue
            ka = min(ca, key=lambda x: (float(np.hypot(*(pc.xy[x] - mesh.P[A]))), x))
            kb = min(cb, key=lambda x: (float(np.hypot(*(pc.xy[x] - mesh.P[B]))), x))
            ab, path_ab = _dijkstra(adj, ka, kb, math.inf, comp)
            ba, path_ba = _dijkstra(adj, kb, ka, math.inf, comp)
            ways = [t for t in (ab, ba) if math.isfinite(t)]
            if not ways:
                continue
            P = np.array(path_ab + path_ba, dtype=np.int64)
            L = float(pc.length[P].sum())
            share = float(pc.length[P][measured[P]].sum() / L) if L else 0.0
            stretches.append({"t": sum(ways) / len(ways), "ab": ab, "ba": ba, "P": P, "L": L, "share": share})
        rec = {"a": A, "b": B, "a_name": name(A), "b_name": name(B)}
        if not stretches:
            dropped.append({**rec, "why": "no way to drive between them along these roads"})
            continue
        # The fastest stretch with enough of its road measured; when none has, the best measured one,
        # which is then dropped (a filled, faster stretch never hides a measured one).
        ok = [x for x in stretches if x["share"] >= settings.CAR_FILL_MIN_SHARE]
        best = min(ok, key=lambda x: x["t"]) if ok else max(stretches, key=lambda x: (x["share"], -x["t"]))
        P, L, share = best["P"], best["L"], best["share"]
        minutes = max(best["t"], FLOOR_MIN)
        wcount = Counter()
        for p in P.tolist():
            wcount[label_of(g.ways[g.way[pc.edge[p]]].tags)] += float(pc.length[p])
        way_ids = sorted({g.ways[g.way[pc.edge[p]]].id for p in P.tolist()})
        n_dirs = sum(math.isfinite(x) for x in (best["ab"], best["ba"]))
        rec.update({"minutes": round(minutes, 3),
                    "minutes_ab": round(best["ab"], 3) if math.isfinite(best["ab"]) else None,
                    "minutes_ba": round(best["ba"], 3) if math.isfinite(best["ba"]) else None,
                    "road_m": round(L / n_dirs, 1), "real_m": round(float(np.hypot(*(mesh.P[A] - mesh.P[B]))), 1),
                    "measured_share": round(share, 3), "roads": [r for r, _ in wcount.most_common(3)],
                    "ways": way_ids})
        if share < settings.CAR_FILL_MIN_SHARE:
            dropped.append({**rec, "why": f"only {share:.0%} of its road has a measured speed"})
            continue
        if minutes > max_time:
            dropped.append({**rec, "why": f"{minutes:.0f} min, over the build's {max_time}-minute limit"})
            continue
        if inside is not None and not (inside[A] or inside[B]):
            dropped.append({**rec, "why": "both ends outside Greater London"})
            continue
        links.append(rec)
    twins = sorted({x for lk in links for x in (lk["a"], lk["b"])})
    return {"links": links, "dropped": dropped, "twins": twins, "pieces": int(len(pc.pa)),
            "candidates": len(cand), "pieces_obj": pc}


# ── Thames crossings ────────────────────────────────────────────────
REGION_CLOSE_M = 1000.0      # the region file is clipped to the coast: the tidal Thames is a gap in it


def london_polygon():
    """Greater London (transit/london_region.geojson) in local metres, with the tidal Thames, which
    the ONS boundary leaves out (it is clipped to the coastline), filled back in."""
    import shapely
    from shapely.geometry import shape

    doc = json.loads(settings.REGION_GEOJSON.read_text(encoding="utf-8"))
    g = shapely.union_all([shape(f["geometry"]) for f in doc["features"]])
    g = shapely.transform(g, lambda c: to_local(c[:, 0], c[:, 1]))
    return g.buffer(REGION_CLOSE_M).buffer(-REGION_CLOSE_M)


def thames_crossings(nodes, ways, region, g: Graph, result: dict) -> list[dict]:
    """Every motor road's bridge or tunnel that crosses the Thames inside `region` (local metres),
    grouped by name (or ref, or way), each with the car link that crosses it or why none does."""
    import shapely

    def line(w):
        pts = [nodes[n] for n in w.nodes if n in nodes]
        if len(pts) < 2:
            return None
        ll = np.array(pts)
        return shapely.linestrings(to_local(ll[:, 0], ll[:, 1]))

    river = [line(w) for w in ways if w.tags.get("waterway") == "river" and w.tags.get("name") == THAMES]
    river = shapely.union_all([r for r in river if r is not None]) if any(r is not None for r in river) else None
    if river is None:
        return []
    used = defaultdict(list)                      # way id -> links whose roads use it
    for lk in result["links"]:
        for wid in lk["ways"]:
            used[wid].append(lk)
    dropped_by_way = defaultdict(list)
    for d in result["dropped"]:
        for wid in d.get("ways", []):
            dropped_by_way[wid].append(d)
    pc = result["pieces_obj"]
    gway = {w.id: k for k, w in enumerate(g.ways)}
    groups: dict[str, dict] = {}
    for w in ways:
        kind = "bridge" if w.tags.get("bridge", "no") not in ("no", "") else (
            "tunnel" if w.tags.get("tunnel", "no") not in ("no", "") else None)
        road = w.tags.get("highway") if w.tags.get("highway") in MOTOR else w.tags.get("disused:highway")
        if kind is None or road not in MOTOR:
            continue
        ln = line(w)
        if ln is None or not ln.intersects(river):
            continue
        cross = ln.intersection(river)
        if not region.contains(cross.representative_point()):
            continue
        key = w.tags.get("name") or w.tags.get("ref") or f"way {w.id}"
        grp = groups.setdefault(key, {"name": key, "kind": kind, "ref": w.tags.get("ref", ""), "classes": set(),
                                      "ways": [], "links": [], "why": [], "closed": []})
        grp["classes"].add(road)
        grp["closed"].append(w.tags.get("highway") not in MOTOR or closed_to_cars(w.tags))
        grp["ways"].append(w.id)
        if is_car_road(w.tags):
            for lk in used.get(w.id, []):
                grp["links"].append(f"{lk['a_name']} – {lk['b_name']}")
            if not used.get(w.id):
                for d in dropped_by_way.get(w.id, []):
                    grp["why"].append(f"the link {d['a_name']} – {d['b_name']} was dropped: {d['why']}")
                es = np.where(g.way == gway[w.id])[0] if w.id in gway else np.zeros(0, dtype=np.int64)
                ps = np.where(np.isin(pc.edge, es))[0]
                ends = set(pc.owner[pc.pa[ps]].tolist()) | set(pc.owner[pc.pb[ps]].tolist())
                if len(ends) == 1:
                    grp["why"].append("both banks belong to one node, so no link crosses it there")
    out = []
    for key in sorted(groups):
        grp = groups[key]
        classes = sorted(grp["classes"])
        main = any(c in settings.CAR_HIGHWAYS for c in classes)
        links = sorted(set(grp["links"]))
        if links:
            status, why = "linked", ""
        elif all(grp["closed"]):
            status, why = "closed to cars", "closed to motor traffic in OpenStreetMap"
        elif not main:
            status, why = "not a main road", f"{'/'.join(classes)}: car links use motorway, trunk and primary roads only"
        else:
            status = "no link"
            why = "; ".join(sorted(set(grp["why"]))) or "no car link uses it"
        out.append({"name": grp["name"], "kind": grp["kind"], "ref": grp["ref"], "classes": classes,
                    "ways": sorted(grp["ways"]), "status": status, "links": links, "why": why})
    return out


# ── the job ─────────────────────────────────────────────────────────
def mesh_sha256(mesh: Mesh) -> str:
    h = hashlib.sha256()
    h.update(np.ascontiguousarray(mesh.P, dtype="<f8").tobytes())
    h.update(np.ascontiguousarray(mesh.tri, dtype="<i8").tobytes())
    return h.hexdigest()


def build(nodes, ways, t4c_xy, speeds, mesh: Mesh, max_time: float, labels: list[str], region=None,
          log=print) -> dict:
    """Everything car_links.json, roads.json and car_report.json hold, from parsed inputs."""
    g = road_graph(nodes, ways)
    if not len(g.ea):
        raise RefusedInput("the clip has no motorway, trunk or primary road")
    log(f"car: {len(g.ways)} main-road ways, {len(g.ea)} directed edges, {g.length.sum() / 1000:.0f} km")
    sp = place_speeds(g, speeds, t4c_xy)
    box = data_box(t4c_xy, speeds)
    box_xy = None
    if box is not None:
        (x0, y1), (x1, y0) = to_local(np.array([box[0], box[2]]), np.array([box[1], box[3]]))
        box_xy = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    cov = coverage(g, sp["source"], box_xy)
    log(f"car: measured speed on {cov['all']['before']:.0%} of main-road length by node id, "
        f"{cov['all']['after']:.0%} with the geometric match"
        + (f" ({cov['inside_the_data_box']['after']:.0%} inside the data's box)" if box_xy else ""))
    inside = None
    if region is not None:
        import shapely
        inside = shapely.contains(region, shapely.points(mesh.P))
    res = car_links(g, sp, mesh, max_time, labels, inside)
    log(f"car: {len(res['links'])} links on {len(res['twins'])} nodes, {len(res['dropped'])} dropped")
    bridges = thames_crossings(nodes, ways, region, g, res) if region is not None else []
    roads = [{"id": w.id, "class": w.tags.get("highway"), "label": label_of(w.tags),
              "xy": np.round(to_local(*np.array([nodes[n] for n in w.nodes if n in nodes]).T), 1).ravel().tolist()}
             for w in g.ways if sum(n in nodes for n in w.nodes) >= 2]
    res.pop("pieces_obj")
    return {"graph": g, "speeds": sp, "coverage": cov, "links": res, "bridges": bridges, "roads": roads,
            "box": box}


def documents(doc: dict, b, pairs, mesh: Mesh, smeta: dict) -> tuple[dict, dict, dict]:
    """car_links.json, roads.json and car_report.json from build()'s result."""
    from transit.layout import carspeed

    lk = doc["links"]
    common = {"schema": 1, "build": b.manifest.get("build"), "window": b.window, "table_sha256": b.table_sha256,
              "mesh_sha256": mesh_sha256(mesh), "nodes": int(mesh.n), "s_m_per_min": pairs.s,
              "overhead_min": settings.CAR_OVERHEAD_MIN, "notice": "Car speeds: HERE Traffic4cast 2022 (MeTS-10), "
              "used non-commercially with the owner's permission."}
    links_doc = {**common, "kind": "london-car-links", "twins": lk["twins"], "links": lk["links"]}
    report = {**common, "kind": "london-car-report", "speeds": {k: v for k, v in smeta.items() if k != "clock"},
              "clock": carspeed.CLOCK, "licence": carspeed.LICENCE,
              "roads": {"ways": len(doc["graph"].ways), "directed_edges": int(len(doc["graph"].ea)),
                        "directed_km": round(float(doc["graph"].length.sum()) / 1000, 1),
                        "segments_with_a_node_missing": doc["graph"].missing_nodes,
                        "classes": list(settings.CAR_HIGHWAYS)},
              "matching": {**doc["speeds"]["counts"], "path_ratio": settings.CAR_PATH_RATIO,
                           "match_m": settings.CAR_MATCH_M, "match_deg": settings.CAR_MATCH_DEG},
              "coverage": doc["coverage"],
              "data_box_lonlat": doc["box"],
              "links": {"count": len(lk["links"]), "nodes": len(lk["twins"]), "candidates": lk["candidates"],
                        "pieces": lk["pieces"], "piece_m": settings.CAR_PIECE_M,
                        "fill_min_share": settings.CAR_FILL_MIN_SHARE, "dropped": lk["dropped"],
                        "measured_share_median": float(np.median([x["measured_share"] for x in lk["links"]]))
                        if lk["links"] else None},
              "bridges": doc["bridges"]}
    roads_doc = {**common, "kind": "london-car-roads", "roads": doc["roads"]}
    return links_doc, roads_doc, report


def run(b, clip: Path, out: Path, zip_path: Path, *, log=print, work: Path | None = None) -> dict:
    from transit.layout import carspeed
    from transit.layout import timemap as tm
    from transit.layout.job import check_out_dir
    from transit.layout.prep import station_pairs

    out = Path(out)
    check_window(b.window)
    check_out_dir(out, OUT_FILES)
    speeds, smeta = carspeed.load(zip_path)
    pairs = station_pairs(b)
    mesh, _, _ = tm.mesh_of(b, pairs)
    with tempfile.TemporaryDirectory() as tmp:
        w = Path(work or tmp)
        with open(extract(clip, w), encoding="utf-8") as f:
            nodes, ways = parse_opl(f)
        t4c_xy = node_coords(clip, np.concatenate([speeds["u"], speeds["v"]]), w)
    t4c_xy.update(nodes)
    doc = build(nodes, ways, t4c_xy, speeds, mesh, b.max_time, pairs.labels, london_polygon(), log)
    links_doc, roads_doc, report = documents(doc, b, pairs, mesh, smeta)
    out.mkdir(parents=True, exist_ok=True)
    for name, d in (("car_links.json", links_doc), ("roads.json", roads_doc), ("car_report.json", report)):
        (out / name).write_text(json.dumps(d, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
    linked = sum(x["status"] == "linked" for x in doc["bridges"])
    log(f"car: {linked} of {len(doc['bridges'])} Thames crossings have a car link; wrote {out}")
    return {"links": links_doc, "report": report}


def check_window(window: str) -> None:
    """The car's speeds are weekday 13:00-14:00 (T4C_BINS): refuse any other transit window."""
    if window != settings.CAR_WINDOW:
        raise RefusedInput(f"the car's speeds are weekday 13:00-14:00 only: build it for --window "
                           f"{settings.CAR_WINDOW}, not {window}")


def load(build: str, window: str, mesh: Mesh, d: Path | None = None,
         table_sha256: str | None = None) -> tuple[dict, dict, dict]:
    """car_links.json, roads.json and car_report.json of a build, refused unless all three were made
    for this build, window and table (when given) and on this mesh."""
    d = Path(d or default_out(build, window))
    try:
        docs = [json.loads((d / n).read_text(encoding="utf-8")) for n in OUT_FILES]
    except (OSError, ValueError):
        raise RefusedInput(f"no car links in {d}: run python -m transit.layout.car --build {build} "
                           f"--window {window}") from None
    if any(x.get("mesh_sha256") != mesh_sha256(mesh) for x in docs):
        raise RefusedInput(f"the car links in {d} were built on another mesh: run python -m transit.layout.car "
                           f"--build {build} --window {window} again")
    for x in docs:
        other = [k for k, want in (("build", build), ("window", window), ("table_sha256", table_sha256))
                 if want is not None and x.get(k) != want]
        if other:
            raise RefusedInput(f"the car links in {d} were made for another {', '.join(other)}: run "
                               f"python -m transit.layout.car --build {build} --window {window} again")
    return tuple(docs)


def main(argv: list[str] | None = None) -> int:
    from datetime import date

    from transit.layout import timemap as tm
    from transit.layout.prep import load_build

    ap = argparse.ArgumentParser(prog="python -m transit.layout.car",
                                 description="Car links for the London time map's travel modes (#23, L-03).")
    ap.add_argument("--build", required=True)
    ap.add_argument("--window", default="wd13", choices=sorted(settings.WINDOWS))
    ap.add_argument("--t4c-zip", type=Path, default=settings.T4C_ZIP)
    ap.add_argument("--out-dir", type=Path, help="default: transit/out/car/<build>/<window>/ in this repo")
    args = ap.parse_args(argv)
    try:
        date.fromisoformat(args.build)
    except ValueError:
        ap.error(f"--build must be a date (YYYY-MM-DD), not {args.build!r}")
    try:
        check_window(args.window)
        b = load_build(settings.OUT / args.build, args.window)
        _, clip, _ = tm.find_inputs(b, settings.DATA, land=True)
        run(b, clip, args.out_dir or default_out(args.build, args.window), args.t4c_zip)
    except (Refused, RefusedOutput) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
