"""
The full London time map (issue #6, L-02) on synthetic grids and a synthetic build: no Java, no
tables, no osmium, no network. tests/timemap_network.py writes the build into a temporary folder.
"""
from __future__ import annotations

import gzip
import json
import math
import re
import subprocess
import sys
from datetime import date, time
from pathlib import Path

import numpy as np
import pytest
from layout_network import BUILD, WINDOW
from timemap_network import (
    EXPECTED_LINKS,
    FEED_ID,
    N_CELLS,
    land_features,
    write_checks_l02,
    write_gtfs,
    write_timemap_build,
)

from transit import settings
from transit.layout import land as landmod
from transit.layout import links as linkmod
from transit.layout import measures, springs, timemap, timemap_report
from transit.layout import mesh as meshmod
from transit.layout.measures import signed_areas
from transit.layout.prep import RefusedInput, RefusedSolve, load_build, station_pairs

ROOT = Path(__file__).resolve().parents[1]


# ── small meshes ────────────────────────────────────────────────────
def grid_mesh(n: int = 15, step: float = 1000.0, seed: int = 3) -> meshmod.Mesh:
    rng = np.random.default_rng(seed)
    xy = np.array([(c * step, r * step) for r in range(n) for c in range(n)], dtype=np.float64)
    xy += rng.uniform(-0.2 * step, 0.2 * step, xy.shape)
    return meshmod.build_mesh(xy, np.zeros((0, 2)))


def _pull(mesh: meshmod.Mesh, a: int, b: int, share: float):
    rest = share * float(np.hypot(*(mesh.P[a] - mesh.P[b])))
    return springs.model(mesh, np.array([[a, b]]), np.array([rest]))


def test_psi_and_phi_are_zero_and_flat_at_rest_and_infinite_at_zero():
    for f in (springs.psi, springs.phi):
        assert f(1.0) == 0.0
        h = 1e-6
        assert abs((f(1 + h) - f(1 - h)) / (2 * h)) < 1e-8
        assert f(0.0) == math.inf and f(-0.5) == math.inf
        assert f(1e-12) > f(1e-6) > f(1e-3) > f(0.5) > 0 and f(2.0) > 0     # rises without bound
    assert springs.psi(1e-300) > 600 and springs.phi(1e-6) > 1e5


def test_train_springs_only_pull():
    lam = np.array([0.0, 0.2, 0.99, 1.0, 1.01, 1.5, 3.0])
    got = springs.psi_pull(lam)
    assert (got[:4] == 0).all()                                          # shorter than s·t: nothing
    assert np.array_equal(got[4:], springs.psi(lam[4:]))                 # longer: ψ
    h = 1e-6
    assert abs((springs.psi_pull(1 + h) - springs.psi_pull(1 - h)) / (2 * h)) < 1e-6   # flat at rest


def _springs_on(mesh, pairs_shares):
    ij = np.array([p for p, _ in pairs_shares])
    rest = np.array([s * float(np.hypot(*(mesh.P[a] - mesh.P[b]))) for (a, b), s in pairs_shares])
    return springs.model(mesh, ij, rest)


def test_gradient_and_hessian_match_finite_differences():
    mesh = grid_mesh(5, seed=1)
    m = _springs_on(mesh, [((0, 24), 0.3), ((6, 18), 2.5)])                  # one pulling, one slack
    rng = np.random.default_rng(0)
    X = m.P.copy()
    X[~m.fixed] += rng.uniform(-0.03, 0.03, X[~m.fixed].shape)     # squeezed and sheared, still unfolded
    assert (signed_areas(X, m.tri) > 0).all()
    lam = np.hypot(*(X[m.links[:, 0]] - X[m.links[:, 1]]).T) / m.rest
    assert lam[0] > 1.05 and lam[1] < 0.95                              # both sides of ψ₊, away from its kink
    rho = 2.0
    E, g, H = springs.grad_hess(m, X, rho)
    h = 1e-6
    gd, Hd = np.zeros_like(g), np.zeros_like(H)
    for k in range(m.n_dof):
        d = np.zeros(m.n_dof)
        d[k] = h
        Xp, Xm = springs.apply_step(m, X, d), springs.apply_step(m, X, -d)
        gd[k] = (springs.energy(m, Xp, rho) - springs.energy(m, Xm, rho)) / (2 * h)
        Hd[:, k] = (springs.grad_hess(m, Xp, rho, hessian=False)[1] - springs.grad_hess(m, Xm, rho, hessian=False)[1]) / (2 * h)
    assert np.allclose(g, gd, rtol=1e-5, atol=1e-7)
    assert np.allclose(H, Hd, rtol=1e-4, atol=1e-6)
    assert np.allclose(H, H.T)


def test_a_triangle_sheared_at_constant_area_pays_a_shape_energy_without_bound():
    """The area term alone is blind to shear (the ask: energy rises as a triangle gets 'very short
    or thin'); the shape term isn't, and leaves rotations and uniform scaling free."""
    m = _one_triangle()
    rot = np.array([[0.6, -0.8], [0.8, 0.6]])
    assert springs.mips(m, m.P @ rot.T * 0.3)[0] == pytest.approx(0.0, abs=1e-12)   # turned and shrunk: free
    last = 0.0
    for shear in (0.5, 2.0, 10.0, 100.0):
        X = m.P.copy()
        X[:, 0] += shear * X[:, 1]                       # same area, ever thinner
        assert signed_areas(X, m.tri)[0] == pytest.approx(0.5)
        assert springs.phi(signed_areas(X, m.tri) / m.A0)[0] == 0.0                  # the area term doesn't see it
        mu = springs.mips(m, X)[0]
        assert mu > last
        last = mu
    assert last >= 1e4                                   # μ = shear² here


def _one_triangle():
    P = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    tri = np.array([[0, 1, 2]])
    edges = np.array([[0, 1], [0, 2], [1, 2]])
    return springs.Model(P=P, fixed=np.zeros(3, dtype=bool), edges=edges, l0=np.hypot(*(P[edges[:, 0]] - P[edges[:, 1]]).T),
                         a=np.full(3, 1 / 6), links=np.zeros((0, 2), dtype=np.int64), rest=np.zeros(0), b=np.zeros(0),
                         tri=tri, A0=np.array([0.5]))


def test_max_step_stops_a_step_that_crosses_zero_area_midway():
    m = _one_triangle()
    # Area along the step ∝ 1 − 3α + 2.1α²: positive at α = 1, negative from α ≈ 0.48 to 1.
    d = np.array([0, 0, -3.0, 1.0, -2.1, 0.0])
    end = springs.apply_step(m, m.P, d, 1.0)
    assert signed_areas(end, m.tri)[0] > 0                         # the endpoint alone looks fine
    amax = springs.max_step(m, m.P, d)
    assert amax == pytest.approx((3 - math.sqrt(9 - 8.4)) / 4.2)
    assert signed_areas(springs.apply_step(m, m.P, d, 0.99 * amax), m.tri)[0] > 0
    assert signed_areas(springs.apply_step(m, m.P, d, 1.01 * amax), m.tri)[0] < 0


def test_max_step_on_random_steps():
    mesh = grid_mesh(6, seed=2)
    m = _pull(mesh, 0, 35, 0.2)
    rng = np.random.default_rng(5)
    for _ in range(20):
        d = rng.normal(size=m.n_dof)
        a = springs.max_step(m, m.P, d)
        assert math.isfinite(a)
        assert (signed_areas(springs.apply_step(m, m.P, d, 0.99 * a), m.tri) > 0).all()
        assert (signed_areas(springs.apply_step(m, m.P, d, 1.01 * a), m.tri) <= 0).any()
    assert springs.max_step(m, m.P, np.zeros(m.n_dof)) == math.inf


def test_rho_zero_is_the_real_map():
    mesh = grid_mesh(5)
    m = _pull(mesh, 0, 24, 0.5)
    frames = springs.sweep(m, (0.0, 0.5))
    assert np.array_equal(frames[0]["X"], m.P)
    assert springs.energy(m, m.P, 0.0) == pytest.approx(0.0, abs=1e-10)          # float noise only
    assert frames[1]["converged"] and frames[1]["monotone"]


def test_a_hard_pull_bunches_the_grid_and_never_flips_a_triangle():
    """Two far points joined by a spring at 1% of their distance, made 10,000 times stiffer than
    the grid by continuation: every iterate keeps every triangle the right way round, the ring
    stays put, and the two points end up much closer."""
    mesh = grid_mesh(15)
    a, b = 2 * 15 + 2, 12 * 15 + 12                                    # far apart, inside the grid
    m = _pull(mesh, a, b, 0.01)
    start = float(np.hypot(*(m.P[a] - m.P[b])))
    seen = []

    def check(X):
        seen.append(float(signed_areas(X, m.tri).min()))

    X = m.P.copy()
    for rho in (1.0, 10.0, 100.0, 1000.0, 10000.0):
        res = springs.solve(m, X, rho, callback=check)
        assert res.converged and res.monotone
        X = res.X
        assert (signed_areas(X, m.tri) > 0).all()
        assert np.array_equal(X[m.fixed], m.P[m.fixed])
    assert len(seen) > 20 and min(seen) > 0
    assert float(np.hypot(*(X[a] - X[b]))) < 0.5 * start
    shipped = springs.quantise(X * m.scale) / 10.0                    # what the job would ship
    assert meshmod.fold_check(mesh, shipped)["ok"] and meshmod.area_ratios(mesh, shipped).min() > 0

    # The same pull done the naive way (the two points simply moved to the spring's length, the
    # rest left where they are) turns triangles inside out: the test has teeth.
    naive = m.P.copy()
    mid = (naive[a] + naive[b]) / 2
    for i in (a, b):
        naive[i] = mid + (naive[i] - mid) * 0.01
    assert (signed_areas(naive, m.tri) <= 0).any()


# ── the mesh ────────────────────────────────────────────────────────
def test_thin_cells_is_deterministic_and_spreads_the_points():
    rng = np.random.default_rng(4)
    nodes = rng.uniform(0, 10000, (40, 2))
    cells = np.vstack([rng.uniform(0, 10000, (400, 2)), nodes[:3] + 10])     # three cells sit on nodes
    a, b = meshmod.thin_cells(nodes, cells, n=50), meshmod.thin_cells(nodes, cells, n=50)
    assert np.array_equal(a, b) and len(a) == 50 and len(set(a.tolist())) == 50
    assert not set(a.tolist()) & {400, 401, 402}
    d0 = np.min(np.hypot(*(cells[:, None, :] - nodes[None, :, :]).transpose(2, 0, 1)), axis=1)
    assert a[0] == int(np.argmax(d0))
    assert len(meshmod.thin_cells(nodes, cells, n=1000)) == int((d0 >= 50).sum())   # every cell far enough


def test_the_mesh_is_bounded_by_the_fixed_ring_and_ships_in_decimetres():
    mesh = grid_mesh(6)
    assert mesh.n_ring == meshmod.RING_N and mesh.fixed.sum() == meshmod.RING_N
    assert (mesh.A0 > 0).all() and len(np.unique(mesh.tri)) == mesh.n
    assert np.array_equal(np.round(mesh.P * 10), mesh.P * 10)
    assert np.allclose(mesh.a_edge.sum(), mesh.A0.sum())


def test_fold_check_catches_a_flip_a_moved_ring_and_nan():
    mesh = grid_mesh(5)
    assert meshmod.fold_check(mesh, mesh.P)["ok"]
    X = mesh.P.copy()
    t = mesh.tri[0]
    X[t[0]], X[t[1]] = mesh.P[t[1]], mesh.P[t[0]]
    assert meshmod.fold_check(mesh, X)["flipped"] > 0
    X = mesh.P.copy()
    X[np.where(mesh.fixed)[0][0]] += 0.1
    assert meshmod.fold_check(mesh, X)["ring_moved_m"] > 0 and not meshmod.fold_check(mesh, X)["ok"]
    X = mesh.P.copy()
    X[0, 0] = np.nan
    assert not meshmod.fold_check(mesh, X)["ok"]


def test_locate_gives_the_triangle_and_its_weights():
    mesh = grid_mesh(4)
    pts = mesh.P[mesh.tri[:5]].mean(axis=1)
    which, w = meshmod.locate(mesh.P, mesh.tri, pts)
    assert list(which) == [0, 1, 2, 3, 4] and np.allclose(w, 1 / 3)
    which, _ = meshmod.locate(mesh.P, mesh.tri, [[1e7, 1e7]])
    assert which[0] == -1


# ── the train links ─────────────────────────────────────────────────
@pytest.fixture
def built(tmp_path):
    return write_timemap_build(tmp_path)


@pytest.fixture
def pairs(built):
    return station_pairs(load_build(built[0], WINDOW))


def _links(built, pairs, **kw):
    feed = built[1] / "work" / BUILD / "gbt.gtfs.zip"
    return linkmod.train_links(feed, date(2026, 1, 7), time(13, 0), pairs, feed_id=FEED_ID, **kw)


def test_train_links_come_from_consecutive_stops_in_the_window(built, pairs):
    lk = _links(built, pairs)
    got = {tuple(sorted(pairs.labels[i].split()[-1] for i in p)) for p in lk.pairs.tolist()}
    assert got == EXPECTED_LINKS
    drops = lk.counts["dropped"]
    assert drops["stop_sequence gap"] == 1                     # G15 -> G35: a stop the clip removed
    assert drops["outside the window"] >= 3                     # the 09:00 trip
    assert drops["same node"] == 1                              # G00 -> T00, merged
    assert drops["a stop not in the layout"] == 1               # X9
    assert drops["no time both ways (links)"] == 1              # G02 -> G53
    assert drops["no departure time"] == 1                      # G30 -> G31, untimed
    i, j = lk.pairs[0]
    assert lk.minutes[0] == pairs.t[i, j] and lk.rest_m[0] == pytest.approx(pairs.s * pairs.t[i, j])
    assert lk.trips[[k for k, p in enumerate(lk.pairs.tolist()) if 'G22' in pairs.labels[p[0]] + pairs.labels[p[1]]][0]] >= 1


def test_station_route_types_match_the_table_builds_rule():
    from transit.points import is_station_type
    for rt in (0, 1, 2, 3, 4, 6, 100, 109, 400, 401, 700, 714, 900, 1000):
        assert linkmod.is_station_type(rt) == is_station_type(rt)


def test_a_feed_with_frequencies_is_refused(tmp_path, pairs):
    feed = write_gtfs(tmp_path / "f.zip", extra={"frequencies.txt": [["trip_id", "start_time", "end_time", "headway_secs"]]})
    with pytest.raises(linkmod.LinkError, match="frequencies"):
        linkmod.train_links(feed, date(2026, 1, 7), time(13, 0), pairs, feed_id=FEED_ID)


# ── the land ────────────────────────────────────────────────────────
def _layers(boroughs=33):
    return landmod.classify(land_features(boroughs))


def test_land_is_classified_and_a_missing_borough_is_refused():
    lay = _layers()
    assert lay.counts["water"] == 1 and lay.counts["park"] == 1 and lay.counts["rail"] == 2   # siding left out
    assert sorted(s for _, s in lay.rail) == ["major_rail", "rail_tunnel"]
    assert len(lay.boroughs) == 33
    landmod.check_boroughs(lay)
    with pytest.raises(RefusedInput, match="31 of London's 33 boroughs"):
        landmod.check_boroughs(_layers(31))


def test_style_names_exist_in_the_renderer_schema():
    text = (ROOT / "static" / "shared" / "mesh" / "schema-streets-v8.js").read_text()
    for name in landmod.STYLE_NAMES:
        assert f'name: "{name}"' in text, name


def _synthetic_mesh(built, pairs):
    b = load_build(built[0], WINDOW)
    cells = [p for p in b.points if p["kind"] == "cell"]
    from transit.layout.frame import to_local
    cxy = to_local(np.array([float(p["lon"]) for p in cells]), np.array([float(p["lat"]) for p in cells]))
    return meshmod.build_mesh(pairs.xy, cxy[meshmod.thin_cells(pairs.xy, cxy)])


def test_land_is_cut_along_the_mesh_and_moves_with_it(built, pairs):
    import shapely
    mesh = _synthetic_mesh(built, pairs)
    hull = shapely.MultiPoint(mesh.P[mesh.kinds != meshmod.RING]).convex_hull
    lay = _layers()
    prep = landmod.prepare(lay, hull, 5.0, 0.5)
    # a shared border is drawn once: the outlines are the grid's lines, not the squares' perimeters
    outline = sum(g.length for g in prep["boundary"])
    perimeters = sum(p.intersection(hull).boundary.length for p in lay.boroughs.values())
    assert outline < 0.75 * perimeters
    park = shapely.union_all(prep["park"])
    assert park.area == pytest.approx(1500 ** 2 - 500 ** 2, rel=0.02)      # the pond stays a hole
    c = landmod.carry(prep, mesh.P, mesh.tri, 5.0, 0.5)
    assert c.counts["fill_pieces"] > 4 and c.counts["line_runs"] > 4
    blob, header = landmod.pack(c, mesh.n)
    u = landmod.unpack(blob, header)
    for k in ("fill_c", "fill_w", "fill_idx", "line_c", "line_w", "fill_nv", "line_nv"):
        assert np.array_equal(getattr(u, k), getattr(c, k)), k
    # no land in the guard ring's triangles: every vertex's nodes are stations or cells
    assert not (mesh.kinds[c.fill_c] == meshmod.RING).any() and not (mesh.kinds[c.line_c] == meshmod.RING).any()
    # carried through an unfolded layout, every fill triangle keeps its orientation
    m = springs.model(mesh, np.array([[0, pairs.n - 1]]), np.array([1000.0]))
    X = springs.solve(m, m.P, 5.0).X * m.scale
    assert meshmod.fold_check(mesh, X)["ok"]
    tris = landmod.fill_triangles(c)
    assert (signed_areas(landmod.place(c.fill_c, c.fill_w, mesh.P), tris) > 0).all()
    assert (signed_areas(landmod.place(c.fill_c, c.fill_w, X), tris) > 0).all()


def test_pieces_that_share_a_mesh_edge_store_the_points_on_it_the_same_way(built, pairs):
    """A cut on a sloping shared edge: both pieces hold the point by the edge alone, so it lands
    in the same place on any layout, even when the two triangles' maps differ."""
    import shapely
    mesh = _synthetic_mesh(built, pairs)
    hull = shapely.MultiPoint(mesh.P[mesh.kinds != meshmod.RING]).convex_hull
    c = landmod.carry(landmod.prepare(_layers(), hull, 5.0, 0.5), mesh.P, mesh.tri, 5.0, 0.5)
    on_edge = (c.fill_c[:, 1] == c.fill_c[:, 2]) & (c.fill_c[:, 0] != c.fill_c[:, 1]) & (c.fill_w[:, 1] == 0)
    keys = np.concatenate([c.fill_c, c.fill_w], axis=1)[on_edge]
    piece = np.repeat(np.arange(len(c.fill_nv)), c.fill_nv)[on_edge]
    shared = {}
    for k, p in zip(map(tuple, keys.tolist()), piece.tolist()):
        shared.setdefault(k, set()).add(p)
    seams = [k for k, ps in shared.items() if len(ps) >= 2]
    assert len(seams) > 10                                   # the river and the park cross many edges
    # every seam point sits on its mesh edge on the real map, and on a bent layout too
    m = springs.model(mesh, np.array([[0, pairs.n - 1]]), np.array([1000.0]))
    X = springs.solve(m, m.P, 5.0).X * m.scale             # bent by a pull: every triangle's map differs
    assert meshmod.fold_check(mesh, X)["ok"]
    for k in seams[:50]:
        lo, hi = k[0], k[1]
        q = landmod.place(np.array([k[:3]]), np.array([k[3:]]), X)[0]
        e, d = X[hi] - X[lo], q - X[lo]
        assert abs(e[0] * d[1] - e[1] * d[0]) <= 1e-9 * float(e @ e)
    # One point on a shared edge, cut in each of its two triangles: the same numbers both times.
    t1, t2 = 0, None
    for t in range(1, len(mesh.tri)):
        if len(set(mesh.tri[t]) & set(mesh.tri[t1])) == 2:
            t2 = t
            break
    a, b = sorted(set(mesh.tri[t1]) & set(mesh.tri[t2]))
    pt = mesh.P[a] * 0.37 + mesh.P[b] * 0.63
    enc = [landmod.encode(mesh.P, mesh.tri[t], [pt]) for t in (t1, t2)]
    assert np.array_equal(enc[0][0], enc[1][0]) and np.array_equal(enc[0][1], enc[1][1])
    assert np.array_equal(landmod.place(*enc[0], X), landmod.place(*enc[1], X))
    # Stored the old way (rounded to decimetres, then blended by each piece's own triangle), the
    # same point parts on a bent layout: the seam this encoding closes.

    def own_triangle(t, q):
        i, j, k = mesh.tri[t]
        e1, e2, d = mesh.P[j] - mesh.P[i], mesh.P[k] - mesh.P[i], q - mesh.P[i]
        det = e1[0] * e2[1] - e1[1] * e2[0]
        w1 = (d[0] * e2[1] - d[1] * e2[0]) / det
        w2 = (e1[0] * d[1] - e1[1] * d[0]) / det
        return (1 - w1 - w2) * X[i] + w1 * X[j] + w2 * X[k]

    rounded = np.round(pt * 10) / 10
    assert np.hypot(*(own_triangle(t1, rounded) - own_triangle(t2, rounded))) > 1e-6


def test_land_slivers_that_turn_over_as_drawn_are_dropped(built, pairs):
    import shapely
    mesh = _synthetic_mesh(built, pairs)
    hull = shapely.MultiPoint(mesh.P[mesh.kinds != meshmod.RING]).convex_hull
    c = landmod.carry(landmod.prepare(_layers(), hull, 5.0, 0.5), mesh.P, mesh.tri, 5.0, 0.5)
    same, n = landmod.drop_blend_slivers(c, [mesh.P])
    assert n == 0 and same is c
    mirrored = mesh.P * np.array([-1.0, 1.0])                # every fill triangle turns over
    none, n = landmod.drop_blend_slivers(c, [mesh.P, mirrored])
    assert n == len(landmod.fill_triangles(c)) and len(none.fill_idx) == 0 and len(none.fill_nv) == 0
    assert len(none.line_nv) == len(c.line_nv)               # lines have no orientation to lose


def test_the_metropolitan_line_is_carried_through_the_mesh(built, pairs):
    mesh = _synthetic_mesh(built, pairs)
    nodes = [pairs.index_of(s) for s in ("syn:G22", "syn:G33", "syn:G44", "syn:G55")]
    C, W = meshmod.carry_route(mesh, nodes)
    real = landmod.place(C, W, mesh.P)
    assert np.allclose(real[0], mesh.P[nodes[0]]) and np.allclose(real[-1], mesh.P[nodes[-1]])
    assert len(C) > len(nodes)                               # it crosses mesh edges between stations
    # consecutive vertices share a triangle, so the piece between them moves with one affine map
    tri_sets = [set(t) for t in mesh.tri.tolist()]
    for a, b in zip(C.tolist(), C.tolist()[1:]):
        assert any(set(a) | set(b) <= t for t in tri_sets)


def test_the_guard_ring_stays_outside_the_inner_hull():
    mesh = grid_mesh(7)
    inner = ~(mesh.kinds[mesh.tri] == meshmod.RING).any(axis=1)
    hull = measures.convex_hull_area(mesh.P[mesh.kinds != meshmod.RING])
    assert mesh.A0[inner].sum() == pytest.approx(hull, rel=1e-9)        # the inner triangles tile the hull


# ── the job ─────────────────────────────────────────────────────────
@pytest.fixture
def job_env(tmp_path, monkeypatch, built):
    build_dir, data = built
    monkeypatch.setattr(settings, "OUT", build_dir.parent)
    monkeypatch.setattr(settings, "DATA", data)
    monkeypatch.setattr(timemap, "RESTART_SEEDS", (1,))

    def fake_land(clip, mesh, work, log):                  # the land without osmium
        return timemap.land_from_layers(landmod.classify(land_features()), mesh, log)

    monkeypatch.setattr(timemap, "carry_land", fake_land)
    return tmp_path, build_dir, data


def _main(tmp_path, out_name="timemap", *extra):
    checks = write_checks_l02(tmp_path / "checks.toml")
    out = tmp_path / out_name
    return timemap.main(["--build", BUILD, "--out-dir", str(out), "--checks", str(checks), *extra]), out


def test_build_end_to_end(job_env):
    tmp_path, build_dir, _ = job_env
    code, out = _main(tmp_path)
    assert code == 0
    for name in timemap.OUTPUT_FILES:
        assert (out / name).is_file(), name
    doc = json.loads((out / "timemap.json").read_text())
    frames = doc["frames"]
    assert len(frames) == len(springs.RHOS) and frames[0]["rho"] == 0
    tri = np.array(doc["triangles"]).reshape(-1, 3)
    for f in frames:                                       # 0 flipped triangles, on what ships
        X = np.stack([f["x"], f["y"]], 1) / 10
        assert (signed_areas(X, tri) > 0).all()
        assert f["metrics"]["flipped"] == 0
    pairs = station_pairs(load_build(build_dir, WINDOW))
    w, b = pairs.index_of("syn:G55"), pairs.index_of("syn:G22")

    def gap(f):
        return math.hypot(f["x"][w] - f["x"][b], f["y"][w] - f["y"][b])
    assert gap(frames[-1]) < 0.7 * gap(frames[0])
    assert 0 < doc["nodes"]["cells"] <= N_CELLS + 2 and doc["nodes"]["ring"] == meshmod.RING_N
    assert doc["land"]["file"] == "land.bin" and (out / "land.bin").stat().st_size > 0

    rep = json.loads((out / "report.json").read_text())
    assert rep["headlines"]["flipped_max"] == 0
    assert all(f["converged"] and f["monotone"] for f in rep["frames"])
    assert rep["l01"]["frames"][0]["lam_tilde"] is None
    assert "ratio_1" in rep["frames"][-1]["ratios"]
    assert rep["restart"]["complete"] and rep["restart"]["at"]
    # how thin triangles get: no claim of a crossing that never happens
    md_text = (out / "report.md").read_text()
    if all(f["stretched_4_share"] <= 0.1 for f in rep["frames"]):
        assert "Past about" not in md_text and "At no position is more than a tenth" in md_text
    # the restart check tests both the start (nudged, same steps) and the step size (other ladders)
    assert {"every other step"} <= set(rep["restart"]["ladders"]) and any(n.startswith("one step") for n in rep["restart"]["ladders"])
    for v in rep["restart"]["at"].values():
        assert {r["kind"] for r in v["runs"]} == {"nudged", "ladder"}
        assert v["shift_max_m"] >= 0 and all("shift_max_m" in r for r in v["runs"])
    assert all(f["pd"] for f in rep["frames"])
    md = (out / "report.md").read_text()
    for words in ("Flipped triangles: 0 at every one of the 25 positions", "Wembley Park to Baker Street",
                  "Syn G44 to Syn G22", "Metropolitan line order", "Normalised stress", "Trustworthiness / continuity",
                  "Most squeezed", "How thin triangles get", "Beside the stations-only layout", "What shapes Wembley Park",
                  "Train springs only pull", "The end of the slider is where the ladder stops",
                  "Station triangles bent", "positive definite", "a test of the step size",
                  "Every position", "Train links", "Land", "Local use only (#6, L-02)", "not cleared for publication",
                  "expired"):
        assert words in md, words
    assert "| — |" in md                                      # the real map's smallest area: nothing squeezed

    svg = (out / "panels.svg").read_text()
    assert svg.startswith("<svg") and svg.count('class="panel"') == 3 and 'class="metropolitan"' in svg
    # north up: G55 (north of G22 in the synthetic build) is drawn above it on the real map
    first = svg.split('class="panel"')[0]
    cy = dict((sid, float(y)) for y, sid in re.findall(r'cy="([-\d.]+)" r="3"[^>]*data-station="([^"]+)"', first))
    assert cy["syn:G55"] < cy["syn:G22"]

    page = ("index.html", "app.js", "app.css", "carry.js", "timemap.json", "land.bin")
    gz = sum(len(gzip.compress((out / n).read_bytes())) for n in page) + \
        sum(len(gzip.compress((out / "mesh" / n).read_bytes())) for n in timemap.RENDERER_FILES)
    assert gz < 2_000_000
    shepard = (out / "shepard.svg").read_text()
    assert "Stations-only time map" in shepard and "Spring grid" in shepard


def test_two_runs_write_the_same_files(job_env):
    tmp_path = job_env[0]
    _, a = _main(tmp_path, "a")
    _, b = _main(tmp_path, "b")
    for name in ("timemap.json", "land.bin", "panels.svg", "report.json"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_without_land_the_page_still_draws_the_stations(job_env):
    code, out = _main(job_env[0], "noland", "--no-land")
    assert code == 0
    doc = json.loads((out / "timemap.json").read_text())
    assert doc["land"] is None and (out / "land.bin").stat().st_size == 0


def test_the_copied_renderer_is_the_committed_one_and_the_page_loads_only_its_own_files(job_env):
    _, out = _main(job_env[0])
    for name in timemap.RENDERER_FILES:
        assert (out / "mesh" / name).read_bytes() == (ROOT / "static" / "shared" / "mesh" / name).read_bytes()
    page = ROOT / "transit" / "layout" / "timemap_page"
    html = (page / "index.html").read_text()
    assert sorted(re.findall(r'(?:src|href)="([^"]*)"', html)) == ["app.css", "app.js", "data:,"]
    assert 'name="robots" content="noindex"' in html
    for path in page.iterdir():
        text = path.read_text()
        for word in ("://", "XMLHttpRequest", "WebSocket", "sendBeacon", "EventSource", "/api/", "import("):
            assert word not in text, f"{path.name} has {word!r}"
        for target in re.findall(r'fetch\(([^)]*)\)', text):
            assert target == "name", target                   # only through load(), below
        for target in re.findall(r'\bload\(([^,)]*)', text):
            assert target in ('"timemap.json"', "doc.land.file", "name"), target
        for spec in re.findall(r'from "([^"]+)"', text):
            assert spec.startswith("./"), spec


# ── refusals ────────────────────────────────────────────────────────
def test_a_feed_that_is_not_the_manifests_is_refused(tmp_path, monkeypatch, capsys):
    build_dir, data = write_timemap_build(tmp_path)
    write_gtfs(data / "work" / BUILD / "gbt.gtfs.zip", extra={"feed_info.txt": [["feed_publisher_name"], ["other"]]})
    monkeypatch.setattr(settings, "OUT", build_dir.parent)
    monkeypatch.setattr(settings, "DATA", data)
    code, out = _main(tmp_path)
    assert code == 2 and "gbt.gtfs.zip is not the file manifest.json describes" in capsys.readouterr().err
    assert not out.exists()


def test_a_clip_that_is_not_the_manifests_is_refused(tmp_path, monkeypatch, capsys):
    build_dir, data = write_timemap_build(tmp_path)
    (data / "work" / BUILD / "london.osm.pbf").write_bytes(b"changed")
    monkeypatch.setattr(settings, "OUT", build_dir.parent)
    monkeypatch.setattr(settings, "DATA", data)
    code, out = _main(tmp_path)
    assert code == 2 and "london.osm.pbf is not the file" in capsys.readouterr().err
    code, _ = _main(tmp_path, "noland", "--no-land")         # the clip isn't read without the land
    assert code == 0


def test_a_missing_required_link_is_refused(job_env, capsys):
    tmp_path = job_env[0]
    checks = write_checks_l02(tmp_path / "checks.toml")
    checks.write_text(checks.read_text() + '\n[[required_link]]\na = "syn:G00"\nb = "syn:G55"\n')
    code = timemap.main(["--build", BUILD, "--out-dir", str(tmp_path / "o"), "--checks", str(checks), "--no-land"])
    assert code == 2 and "no train link" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_an_unconverged_solve_writes_nothing(job_env):
    tmp_path, build_dir, data = job_env
    b = load_build(build_dir, WINDOW)
    feed = data / "work" / BUILD / "gbt.gtfs.zip"
    checks, _ = timemap.load_checks(write_checks_l02(tmp_path / "checks.toml"))
    with pytest.raises(RefusedSolve, match="did not converge"):
        timemap.run(b, feed, None, checks, tmp_path / "o", max_iter=1, log=lambda *_: None)
    assert not (tmp_path / "o").exists()


def test_a_flipped_triangle_in_what_would_ship_writes_nothing(job_env, monkeypatch):
    tmp_path, build_dir, data = job_env
    real_sweep = springs.sweep

    def folded(m, *a, **kw):
        out = real_sweep(m, *a, **kw)
        X = out[-1]["X"]
        t = m.tri[np.where(~(m.fixed[m.tri]).any(axis=1))[0][0]]
        X[t[0]], X[t[1]] = X[t[1]].copy(), X[t[0]].copy()           # turn one triangle inside out
        return out

    monkeypatch.setattr(springs, "sweep", folded)
    b = load_build(build_dir, WINDOW)
    checks, _ = timemap.load_checks(write_checks_l02(tmp_path / "checks.toml"))
    with pytest.raises(RefusedSolve, match="flipped"):
        timemap.run(b, data / "work" / BUILD / "gbt.gtfs.zip", None, checks, tmp_path / "o", log=lambda *_: None)
    assert not (tmp_path / "o").exists()


def test_a_page_over_two_megabytes_is_refused(job_env, monkeypatch, capsys):
    monkeypatch.setattr(timemap, "MAX_GZIP_BYTES", 1000)
    code, out = _main(job_env[0])
    assert code == 2 and "every rung of the land ladder" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize("where, words", [
    ("static/timemap-test-should-not-exist", "serves or ships"),
    ("docs/timemap-test-should-not-exist", "not git-ignored"),
])
def test_output_inside_the_repo_must_be_ignored_and_not_shipped(job_env, capsys, where, words):
    target = ROOT / where
    code = timemap.main(["--build", BUILD, "--out-dir", str(target), "--no-land"])
    assert code == 2 and words in capsys.readouterr().err
    assert not target.exists()


def test_default_output_is_beside_the_builds_and_ignored():
    out = timemap.default_out("2026-11-12", "wd13")
    assert out == ROOT / "transit" / "out" / "timemap" / "2026-11-12" / "wd13"
    timemap.check_out_dir(out)


def test_bad_extra_checks_are_refused(tmp_path):
    p = write_checks_l02(tmp_path / "checks.toml")
    p.write_text(p.read_text() + '\n[[ratio]]\nname = "no station"\n')
    with pytest.raises(RefusedInput, match="ratio"):
        timemap.load_checks(p)


def test_the_solver_and_mesh_import_numpy_only():
    code = ("import sys, transit.layout.springs, transit.layout.mesh, transit.layout.links; "
            "bad = [m for m in ('scipy', 'pandas', 'sklearn', 'h3', 'shapely', 'pyproj') if m in sys.modules]; "
            "print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_required_link_naming_a_station_not_in_the_layout_is_refused(job_env, capsys):
    tmp_path = job_env[0]
    checks = write_checks_l02(tmp_path / "checks.toml")
    checks.write_text(checks.read_text() + '\n[[required_link]]\na = "syn:G55"\nb = "syn:TYPO"\n')
    code = timemap.main(["--build", BUILD, "--out-dir", str(tmp_path / "o"), "--checks", str(checks), "--no-land"])
    assert code == 2 and "syn:TYPO is not a station of this build's layout" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_a_stations_only_recompute_that_l01_would_refuse_stops_the_job(job_env, monkeypatch):
    tmp_path, build_dir, data = job_env
    from transit.layout import smacof
    real = smacof.sweep

    def rising(pairs, *a, **kw):
        out = real(pairs, *a, **kw)
        out[3]["rose"] = True
        return out

    monkeypatch.setattr(smacof, "sweep", rising)
    b = load_build(build_dir, WINDOW)
    checks, _ = timemap.load_checks(write_checks_l02(tmp_path / "checks.toml"))
    with pytest.raises(RefusedSolve, match="stations-only layout"):
        timemap.run(b, data / "work" / BUILD / "gbt.gtfs.zip", None, checks, tmp_path / "o", log=lambda *_: None,
                    restarts=False)
    assert not (tmp_path / "o").exists()


def test_the_land_ladder_steps_to_the_next_rung_when_the_page_is_too_big(job_env, monkeypatch):
    tmp_path = job_env[0]
    monkeypatch.setattr(landmod, "LADDER", ((5.0, 0.5), (5.0, 10_000.0)))   # the second rung drops water and parks
    code, first = _main(tmp_path, "rung0")
    assert code == 0
    size = json.loads((first / "report.json").read_text())["page_gzip_bytes"]
    monkeypatch.setattr(timemap, "MAX_GZIP_BYTES", size - 1)
    code, out = _main(tmp_path, "rung1")
    assert code == 0
    doc = json.loads((out / "timemap.json").read_text())
    assert doc["land"]["min_ha"] == 10_000.0 and doc["land"]["counts"]["fill_pieces"] == 0
    assert doc["land"]["counts"]["line_runs"] > 0                           # no layer is dropped: lines stay


def test_when_wembley_park_never_passes_the_report_says_how_close_and_the_panels_hold(job_env):
    tmp_path = job_env[0]
    checks = write_checks_l02(tmp_path / "checks.toml")
    checks.write_text(checks.read_text().replace('station = "syn:G55"\nreference = "syn:G22"',
                                                 'station = "syn:G00"\nreference = "syn:G55"', 1))
    code = timemap.main(["--build", BUILD, "--out-dir", str(tmp_path / "o"), "--checks", str(checks)])
    assert code == 0
    rep = json.loads((tmp_path / "o" / "report.json").read_text())
    assert rep["wembley"]["first_pass"] is None and rep["wembley"]["pick_is_pass"] is False
    md = (tmp_path / "o" / "report.md").read_text()
    assert "no position passes" in md and "outside the tolerance" in md and "What shapes Wembley Park" in md
    svg = (tmp_path / "o" / "panels.svg").read_text()
    titles = re.findall(r'class="panel">([^<]+)<', svg)
    assert any("Closest to a Wembley Park pass" in t for t in titles)
    assert len(titles) == len(set(titles)) and 2 <= len(titles) <= 3


def test_panels_merge_a_position_that_is_two_of_the_three():
    frames = [{"rho": r} for r in (0.0, 1.0, 2.0)]
    picks = timemap_report.panel_picks(frames, {"status": "fail", "pick": 2, "pick_is_pass": False})
    assert [k for k, _ in picks] == [0, 2] and "closest" in picks[1][1].lower() and "end of the slider" in picks[1][1]
    picks = timemap_report.panel_picks(frames, {"status": "absent"})
    assert [k for k, _ in picks] == [0, 1, 2] and picks[1][1].startswith("Middle of the slider")


def test_a_symlinked_mesh_folder_is_refused(tmp_path):
    out = tmp_path / "timemap"
    out.mkdir()
    (out / "mesh").symlink_to(ROOT / "static" / "shared" / "mesh")
    with pytest.raises(timemap.Refused, match="symlink"):
        timemap.check_out_dir(out)


def test_every_module_the_page_imports_is_copied():
    page = ROOT / "transit" / "layout" / "timemap_page"
    mesh_dir = ROOT / "static" / "shared" / "mesh"
    want, todo = set(), [(page / "app.js", "")]
    while todo:
        path, prefix = todo.pop()
        for spec in re.findall(r'from "\./([^"]+)"', path.read_text()):
            name = prefix + spec
            if name in want:
                continue
            want.add(name)
            todo.append(((page / name) if not name.startswith("mesh/") else mesh_dir / name[len("mesh/"):],
                         "mesh/" if name.startswith("mesh/") else ""))
    copied = {*timemap.PAGE_FILES, *(f"mesh/{n}" for n in timemap.RENDERER_FILES)}
    assert want <= copied, want - copied


def test_without_the_step_cap_the_barrier_alone_still_never_accepts_a_fold(monkeypatch):
    mesh = grid_mesh(9)
    m = _pull(mesh, 2 * 9 + 2, 6 * 9 + 6, 0.05)
    monkeypatch.setattr(springs, "max_step", lambda *a, **k: math.inf)
    seen = []
    X = m.P.copy()
    for rho in (1.0, 10.0, 100.0):
        res = springs.solve(m, X, rho, callback=lambda Y: seen.append(float(signed_areas(Y, m.tri).min())))
        X = res.X
    assert seen and min(seen) > 0          # the energy is infinite at J <= 0, so a folded end point is never taken


def test_the_feed_prefix_comes_from_the_stations(pairs):
    assert timemap.feed_prefix(pairs) == FEED_ID

    class Two:
        member_ids = [["gbt:A"], ["bods:B"]]
    with pytest.raises(RefusedInput, match="2 feeds"):
        timemap.feed_prefix(Two())


# ── the smooth motion ───────────────────────────────────────────────
def test_a_blend_of_two_unfolded_layouts_can_fold_on_the_way_and_the_check_sees_it():
    tri = np.array([[0, 1, 2]])
    A = np.array([[0, 0], [100, 0], [0, 100]])
    B = np.array([[0, 0], [-200, 100], [-210, 100]])           # area ∝ 1 − 3s + 2.1s²: positive at both ends
    assert signed_areas(B.astype(float), tri)[0] > 0
    assert not meshmod.blend_ok(tri, A, B)
    sampled = [2 * signed_areas((1 - t) * A + t * B, tri)[0] for t in np.linspace(0, 1, 2001)]
    assert min(sampled) < 0                                   # it does fold half way
    assert meshmod.blend_min_area(tri, A, B)[0] <= min(sampled) + 1e-6
    assert meshmod.blend_ok(tri, A, A) and meshmod.blend_ok(tri, A, A * 2)


def test_the_continuation_keeps_every_step_unfolded_all_the_way(monkeypatch):
    mesh = grid_mesh(12)
    m = _pull(mesh, 2 * 12 + 2, 9 * 12 + 9, 0.02)
    real = springs.blend_unfolded
    # Stricter than the real check (a step must also move little), so the giant step has to be split.
    monkeypatch.setattr(springs, "blend_unfolded",
                        lambda m_, a, b: real(m_, a, b) and float(np.median(np.hypot(*(a - b).T))) < 0.3)
    frames = springs.sweep(m, (0.0, 1e5))
    assert all(f["converged"] and f["smooth_ok"] for f in frames)
    keys = [frames[0]["X"]]
    for f in frames[1:]:
        keys += [e["X"] for e in f["between"]] + [f["X"]]
    assert len(keys) > len(frames)                            # the big steps needed positions in between
    for a, b in zip(keys, keys[1:]):
        assert real(m, a, b)
    between = [e for f in frames for e in f["between"]]
    assert all(e["converged"] for e in between)
    assert any(not e["settling"] for e in between)            # solved in-between positions (equilibria) …
    assert any(e["settling"] for e in between)                # … and, where halving ran out, the solve's own path


def test_settling_steps_follow_the_solve_without_a_fold():
    mesh = grid_mesh(10)
    m = _pull(mesh, 2 * 10 + 2, 7 * 10 + 7, 0.02)
    path, res = springs.settling(m, m.P, 5000.0)               # one huge step: the solve itself is the path
    end = res.X
    assert np.array_equal(end, springs.solve(m, m.P, 5000.0).X)
    pts = [m.P] + [e["X"] for e in path] + [end]
    assert len(pts) >= 2 and all(e["settling"] for e in path)
    for a, b in zip(pts, pts[1:]):
        assert springs.blend_unfolded(m, a, b)


def test_the_page_can_slide_through_every_point_of_the_slider(job_env):
    code, out = _main(job_env[0])
    assert code == 0
    doc = json.loads((out / "timemap.json").read_text())
    tri = np.array(doc["triangles"]).reshape(-1, 3)
    keys = [(i, f) for i, f in enumerate(doc["frames"])] + [(b["at"], b) for b in doc["motion"]["between"]]
    keys.sort(key=lambda kv: kv[0])
    for (_, a), (_, b) in zip(keys, keys[1:]):
        A, B = (np.stack([k["x"], k["y"]], 1) for k in (a, b))
        assert (meshmod.blend_min_area(tri, A, B) > 0).all()
    assert doc["motion"]["blends"] == len(keys) - 1
    md = (out / "report.md").read_text()
    assert "The motion between positions is unfolded too" in md


def test_settling_keyframes_sit_in_path_order_on_the_slider(monkeypatch):
    # The forced-split sweep ends in a jump: a settling run, then the solved position at the same ρ.
    # The keyframes must increase along the slider in exactly the order the motion goes.
    mesh = grid_mesh(12)
    m = _pull(mesh, 2 * 12 + 2, 9 * 12 + 9, 0.02)
    real = springs.blend_unfolded
    monkeypatch.setattr(springs, "blend_unfolded",
                        lambda m_, a, b: real(m_, a, b) and float(np.median(np.hypot(*(a - b).T))) < 0.3)
    frames = springs.sweep(m, (0.0, 1e5))
    monkeypatch.setattr(springs, "blend_unfolded", real)
    for f in frames:
        timemap.ship(f, m, mesh)
    motion = timemap.motion_keyframes(m, mesh, frames, [f"n{i}" for i in range(len(mesh.P))], lambda _: None)
    keys = motion["keys"]
    ats = [k["at"] for k in keys]
    assert ats == sorted(ats) and len(set(ats)) == len(ats)
    s = next(i for i, k in enumerate(keys) if k.get("settling"))
    assert keys[s + 1]["rho"] == keys[s]["rho"] and not keys[s + 1].get("settling")   # it leads to that solve
    assert keys[s - 1]["at"] < keys[s]["at"] < keys[s + 1]["at"]
    assert motion["snaps"] and motion["snaps"][0]["rho"] == keys[s]["rho"] and motion["snaps"][0]["move_m"] > 0


def test_slow_links_leave_the_map_alone_where_pushing_springs_would_move_it(monkeypatch):
    # A row of stations whose train times say "twice as far as they are": springs that pushed would
    # have to lengthen the row inside a grid with no room for it. Pulling only, they do nothing.
    n = 8
    mesh = grid_mesh(n, seed=5)
    row = [(3 * n + c, 3 * n + c + 1) for c in range(1, n - 2)]
    m = _springs_on(mesh, [(p, 2.0) for p in row])
    frames = springs.sweep(m, (0.0, 1.0, 100.0))
    for f in frames:
        assert np.array_equal(f["X"], m.P) and springs.energy(m, f["X"], f["rho"]) < 1e-10

    # Both ways (the springs push too): the row moves.
    terms = springs._spring_terms
    monkeypatch.setattr(springs, "psi_pull", springs.psi)
    monkeypatch.setattr(springs, "_spring_terms", lambda *a, pull_only=False: terms(*a))
    pushed = springs.sweep(m, (0.0, 1.0, 100.0))[-1]["X"]
    moved = np.hypot(*(pushed - m.P).T) * m.scale
    assert moved.max() > 100                                            # metres


def test_a_land_triangle_folding_between_the_samples_of_a_blend_is_caught():
    # Unfolded at both ends and halfway (where a sampled check looks), folded from s = 0.53 to 0.9.
    tri = np.array([[0, 1, 2]])
    A = np.array([[0.0, 0.0], [100.0, 0.0], [0.0, 100.0]])
    B = np.array([[0.0, 0.0], [-200.0, 100.0], [-210.0, 100.0]])
    area2 = lambda X: (X[1, 0] - X[0, 0]) * (X[2, 1] - X[0, 1]) - (X[1, 1] - X[0, 1]) * (X[2, 0] - X[0, 0])  # noqa: E731
    assert min(area2((1 - t) * A + t * B) for t in (0.0, 0.5, 1.0)) > 100
    assert area2(0.3 * A + 0.7 * B) < 0
    assert landmod.blend_slivers([A, B], tri).tolist() == [True]
    assert landmod.blend_slivers([A, A * 2], tri).tolist() == [False]
    thin = np.array([[0.0, 0.0], [100.0, 0.0], [0.0, 0.01]])            # 1 cm tall: inside the margin
    assert landmod.blend_slivers([thin], tri).tolist() == [True]


def test_python_places_land_exactly_as_the_page_does():
    # The fixture of tests/js/mesh-timemap.test.js ("carry.js places stored vertices exactly as
    # land.place does in the build"): the same Float32 values on both sides.
    X = np.array([0, 0, 1500, 100, -200, 800, 1300, 1400], dtype=np.float64).reshape(-1, 2)
    C = np.array([[0, 0, 0], [1, 2, 2], [0, 1, 2]])
    W = np.array([[0, 0], [32767, 0], [13107, 13107]])
    got = landmod.place(C, W, X).astype(np.float32).ravel().tolist()
    assert got == [0, 0, 650.0130004882812, 449.9946594238281, 260, 180]


def _run_job(job_env, **kw):
    tmp_path, build_dir, data = job_env
    b = load_build(build_dir, WINDOW)
    checks, _ = timemap.load_checks(write_checks_l02(tmp_path / "checks.toml"))
    return timemap.run(b, data / "work" / BUILD / "gbt.gtfs.zip", None, checks, tmp_path / "o", log=lambda *_: None, **kw)


def test_a_step_that_still_folds_part_way_writes_nothing(job_env, monkeypatch):
    monkeypatch.setattr(springs, "blend_unfolded", lambda *a: False)      # every blend folds
    monkeypatch.setattr(springs, "settling", lambda *a, **k: None)       # and the solver's path can't be followed
    with pytest.raises(RefusedSolve, match="still folds part way"):
        _run_job(job_env, restarts=False)
    assert not (job_env[0] / "o").exists()


def test_a_solve_that_fails_on_its_way_names_the_rho_it_reached(job_env, monkeypatch):
    real = springs.sweep

    def failing(m, rhos, *a, **kw):
        out = real(m, rhos[:3], *a, **kw)
        out[-1] = {**out[-1], "converged": False, "reached": out[-1]["rho"] / 2}   # stuck at an in-between ρ
        return out

    monkeypatch.setattr(springs, "sweep", failing)
    with pytest.raises(RefusedSolve, match=r"on the way to .* did not converge"):
        _run_job(job_env, restarts=False)


def test_keyframes_out_of_order_on_the_slider_are_refused():
    mesh = grid_mesh(8)
    m = _pull(mesh, 2 * 8 + 2, 5 * 8 + 5, 0.3)
    frames = springs.sweep(m, (0.0, 1.0, 10.0))
    for f in frames:
        timemap.ship(f, m, mesh)
    # an in-between position recorded past the solved position it leads to (ρ 1.5 before ρ = 1)
    frames[1]["between"] = [{"rho": 1.5, "X": frames[1]["X"], "converged": True, "monotone": True, "settling": False}]
    with pytest.raises(RefusedSolve, match="out of order"):
        timemap.motion_keyframes(m, mesh, frames, ["n"] * len(mesh.P), lambda _: None)


def test_split_steps_reach_the_page_in_slider_order(job_env, monkeypatch):
    # Stricter than the real check (each blend must also move little), so steps split and the
    # in-between keyframes go through the whole job into timemap.json.
    real = springs.blend_unfolded
    monkeypatch.setattr(springs, "blend_unfolded",
                        lambda m_, a, b: real(m_, a, b) and float(np.median(np.hypot(*(a - b).T))) < 0.02)
    code, out = _main(job_env[0], "split")
    assert code == 0
    doc = json.loads((out / "timemap.json").read_text())
    mo = doc["motion"]
    assert mo["inserted"] > 0 and len(mo["between"]) == mo["inserted"] + mo["settling"]
    n = len(doc["frames"][0]["x"])
    keys = [(float(i), f) for i, f in enumerate(doc["frames"])] + [(b["at"], b) for b in mo["between"]]
    keys.sort(key=lambda kv: kv[0])
    ats = [a for a, _ in keys]
    assert all(q > p for p, q in zip(ats, ats[1:]))
    for b in mo["between"]:
        assert len(b["x"]) == len(b["y"]) == n and isinstance(b["settling"], bool) and math.floor(b["at"]) < b["at"]
    tri = np.array(doc["triangles"]).reshape(-1, 3)
    for (_, a), (_, b) in zip(keys, keys[1:]):
        assert (meshmod.blend_min_area(tri, np.stack([a["x"], a["y"]], 1), np.stack([b["x"], b["y"]], 1)) > 0).all()
    for j in mo["jumps"]:
        assert j["at_from"] < j["at_to"]
    assert mo["blends"] == len(keys) - 1
