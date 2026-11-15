"""
The stations-only London layout (issue #6, L-01) on a synthetic network: no Java, no tables,
no network. tests/layout_network.py writes the build into a temporary folder.
"""
from __future__ import annotations

import gzip
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from layout_network import BUILD, ONE_WAY, WINDOW, write_build, write_checks

from transit import settings
from transit.layout import job, measures, smacof
from transit.layout.frame import EARTH_CIRCUMFERENCE_M, ORIGIN, from_local, to_local
from transit.layout.prep import (
    LengthError,
    RefusedInput,
    check_lengths,
    clean_name,
    load_build,
    station_pairs,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def build_dir(tmp_path):
    return write_build(tmp_path / "out")


@pytest.fixture
def pairs(build_dir):
    return station_pairs(load_build(build_dir, WINDOW))


def _node(pairs, sid):
    i = pairs.index_of(sid)
    assert i is not None, sid
    return i


# ── the solver ──────────────────────────────────────────────────────
@pytest.mark.parametrize("lam_tilde", [1.0, 0.0])
def test_every_step_lowers_the_objective(pairs, lam_tilde):
    start = smacof.classical_mds(pairs) if lam_tilde == 0 else pairs.xy
    res = smacof.solve(start, pairs, lam_tilde * smacof.lam_unit(pairs), tol=1e-12, max_iter=3000)
    trace = np.asarray(res.trace)
    assert res.iterations > 5 and not res.rose
    assert np.all(trace[1:] <= trace[:-1] * (1 + 1e-12))
    assert trace[-1] < trace[0]


def test_the_time_map_has_less_stress_and_pulls_the_express_ends_together(pairs):
    frames = smacof.sweep(pairs)
    assert len(frames) >= 20 and frames[-1]["lam_tilde"] == 0.0
    assert all(f["monotone"] and not f["rose"] for f in frames)
    lam = [f["lam_tilde"] for f in frames]
    assert lam == sorted(lam, reverse=True)
    real = smacof.sigma1(pairs.xy, pairs)
    assert smacof.sigma1(frames[-1]["X"], pairs) < real
    w, b = _node(pairs, "syn:G55"), _node(pairs, "syn:G22")
    assert math.dist(frames[-1]["X"][w], frames[-1]["X"][b]) < math.dist(pairs.xy[w], pairs.xy[b])


# ── 255 is never a length ───────────────────────────────────────────
def test_255_is_never_a_length(pairs):
    i, j = _node(pairs, ONE_WAY[0]), _node(pairs, ONE_WAY[1])
    assert pairs.directed[i, j] == 255 and pairs.directed[j, i] != 255
    assert not pairs.used[i, j] and pairs.w[i, j] == 0 and np.isnan(pairs.t[i, j])
    assert pairs.counts["pairs_one_way_dropped"] == 1

    # Forcing that pair in as a length is refused, by the guard and by the solver itself.
    pairs.used[i, j] = pairs.used[j, i] = True
    pairs.t[i, j] = pairs.t[j, i] = (255 + int(pairs.directed[j, i])) / 2
    pairs.w[i, j] = pairs.w[j, i] = pairs.t[i, j] ** -2
    with pytest.raises(LengthError, match="255"):
        check_lengths(pairs)
    with pytest.raises(LengthError):
        smacof.solve(pairs.xy, pairs, 1.0)


@pytest.mark.parametrize("bad, match", [(181.0, "limit"), (np.nan, "finite"), (0.25, "floor")])
def test_lengths_must_be_real_times(pairs, bad, match):
    i, j = _node(pairs, "syn:G00"), _node(pairs, "syn:G01")
    pairs.t[i, j] = pairs.t[j, i] = bad
    with pytest.raises(LengthError, match=match):
        check_lengths(pairs)


def _set(sid_from, sid_to, value):
    def hook(t, at):
        t[at[sid_from], at[sid_to]] = value
    return hook


@pytest.mark.parametrize("case, match", [
    ("value_200", "between 181 and 254"),
    ("int16", "uint8"),
    ("table_edited", "sha256"),
    ("incomplete", "incomplete"),
    ("points_edited", "points.csv"),
    ("no_table", "has no"),
])
def test_job_refuses_a_broken_build(tmp_path, case, match):
    if case == "value_200":
        d = write_build(tmp_path, table_hook=_set("syn:G00", "syn:G01", 200))
    elif case == "int16":
        d = write_build(tmp_path, dtype=np.int16)
    else:
        d = write_build(tmp_path)
    if case == "table_edited":
        t = np.load(d / f"tt_{WINDOW}.npy")
        t[0, 1] = 1
        np.save(d / f"tt_{WINDOW}.npy", t)
    elif case == "incomplete":
        fp = json.loads((d / f"tt_{WINDOW}.fingerprint.json").read_text())
        fp["complete"] = False
        (d / f"tt_{WINDOW}.fingerprint.json").write_text(json.dumps(fp))
    elif case == "points_edited":
        with open(d / "points.csv", "a") as f:
            f.write("\n")
    elif case == "no_table":
        (d / f"tt_{WINDOW}.npy").unlink()
    with pytest.raises(RefusedInput, match=match):
        load_build(d, WINDOW)


# ── prep ────────────────────────────────────────────────────────────
def test_prep_rules(pairs):
    c = pairs.counts
    assert c["merges"] == [["syn:G00", "syn:T00"]]
    assert _node(pairs, "syn:N50") != _node(pairs, "syn:G50")          # 60 m stays apart
    assert c["stations_excluded"] == 2                                  # X1 not ok, X2 masked
    for sid in ("syn:X1", "syn:X2", "88194ad86dfffff", "syn:I1", "syn:I2"):
        assert pairs.index_of(sid) is None
    assert [sorted(g["ids"]) for g in pairs.left_out] == [["syn:I1", "syn:I2"]]
    assert pairs.n == 36 + 3                                            # grid (G00+T00 one node), N50, Z1, Z2
    assert not pairs.used.diagonal().any()

    # merged: the fastest member-to-member time and the mean position
    m, g01 = _node(pairs, "syn:G00"), _node(pairs, "syn:G01")
    assert pairs.directed[m, g01] == 3 and pairs.directed[g01, m] == 3
    assert pairs.ids[m] == "syn:G00" and len(pairs.members[m]) == 2

    # mean of the two directions, directed kept
    w, b = _node(pairs, "syn:G55"), _node(pairs, "syn:G22")
    assert (pairs.directed[w, b], pairs.directed[b, w]) == (7, 8)
    assert pairs.t[w, b] == pairs.t[b, w] == 7.5
    assert pairs.w[w, b] == pytest.approx(7.5 ** -2)

    # whole-minute zero between two distinct places is floored
    z1, z2 = _node(pairs, "syn:Z1"), _node(pairs, "syn:Z2")
    assert pairs.t[z1, z2] == 0.5 and c["pairs_floored"] == 1


def test_scale_is_median_real_distance_over_median_time(pairs):
    iu = np.triu_indices(pairs.n, 1)
    use = pairs.used[iu]
    real = smacof.distances(pairs.xy)[iu][use]
    assert np.median(pairs.s * pairs.t[iu][use]) == pytest.approx(np.median(real))


def test_clean_name():
    assert clean_name("WEMBLEY PARK UND") == "Wembley Park"
    assert clean_name("Canary Wharf Underground Station") == "Canary Wharf"
    assert clean_name("Stratford (London)") == "Stratford"
    assert clean_name("Harrow-on-the-Hill") == "Harrow-on-the-Hill"


# ── frame and alignment ─────────────────────────────────────────────
def test_frame_matches_frame_js():
    assert np.allclose(to_local(*ORIGIN), [0, 0])
    east = to_local(ORIGIN[0] + 0.01, ORIGIN[1])
    m = EARTH_CIRCUMFERENCE_M * math.cos(math.radians(ORIGIN[1]))
    assert east[0] == pytest.approx(0.01 / 360 * m) and abs(east[1]) < 1e-9
    assert to_local(ORIGIN[0], ORIGIN[1] + 0.01)[1] < 0                   # y points south
    pts = np.array([[-0.3, 51.4], [0.1, 51.65]])
    xy = to_local(pts[:, 0], pts[:, 1])
    assert np.allclose(from_local(xy[:, 0], xy[:, 1]), pts, atol=1e-10)


def test_alignment_is_rotation_and_shift_only():
    rng = np.random.default_rng(3)
    A = rng.normal(size=(30, 2)) * 1000
    th = math.radians(37)
    R = np.array([[math.cos(th), math.sin(th)], [-math.sin(th), math.cos(th)]])
    X = A @ R + [5000, -300]
    out, info = smacof.align_rigid(X, A)
    assert np.allclose(out, A, atol=1e-6) and not info["reflection_fits_better"]
    mirrored = A * [-1, 1]
    out, info = smacof.align_rigid(mirrored, A)
    assert info["reflection_fits_better"]
    assert not np.allclose(out, A, atol=1.0)                              # not flipped back
    assert np.allclose(smacof.distances(out), smacof.distances(mirrored))  # no scale


# ── trustworthiness and continuity ──────────────────────────────────
def _tc_brute(Din, Dout, k):
    n = len(Din)
    def ranks(D, i):
        order = sorted((j for j in range(n) if j != i), key=lambda j: (D[i, j], j))
        return {j: r + 1 for r, j in enumerate(order)}
    tsum = csum = 0
    for i in range(n):
        ri, ro = ranks(Din, i), ranks(Dout, i)
        nin = {j for j, r in ri.items() if r <= k}
        nout = {j for j, r in ro.items() if r <= k}
        tsum += sum(ri[j] - k for j in nout - nin)
        csum += sum(ro[j] - k for j in nin - nout)
    norm = 2 / (n * k * (2 * n - 3 * k - 1))
    return 1 - norm * tsum, 1 - norm * csum


def test_trust_continuity():
    rng = np.random.default_rng(5)
    P = rng.uniform(0, 10, (20, 2))
    D = smacof.distances(P)
    used = ~np.eye(20, dtype=bool)
    tc = measures.TrustContinuity(D, used, k=3, seeds=1, jitter=1e-9)
    same = tc(P)
    assert same["T"] == pytest.approx(1) and same["C"] == pytest.approx(1)
    Q = P.copy()
    Q[[0, 1, 2, 3]] = Q[[3, 2, 1, 0]]
    got = tc(Q)
    T, C = _tc_brute(D, smacof.distances(Q), 3)
    assert got["T"] == pytest.approx(T) and got["C"] == pytest.approx(C)
    assert T < 1 and C < 1


# ── triangulation and folds ─────────────────────────────────────────
def test_delaunay_is_valid_on_random_points():
    rng = np.random.default_rng(11)
    P = rng.uniform(0, 1000, (80, 2))
    tri = measures.delaunay(P)
    assert (measures.signed_areas(P, tri) > 0).all()
    assert measures.signed_areas(P, tri).sum() == pytest.approx(measures.convex_hull_area(P))
    centre, r2 = measures._circumcircles(P, tri)
    d2 = ((P[None, :, :] - centre[:, None, :]) ** 2).sum(-1)
    inside = d2 < r2[:, None] * (1 - 1e-9)
    inside[np.arange(len(tri))[:, None], tri] = False
    assert not inside.any()                                              # empty circumcircles
    _, counts = measures._edge_counts(tri)
    assert set(counts.tolist()) <= {1, 2}


def test_peel_removes_hull_slivers():
    g = np.array([[x, y] for x in range(5) for y in range(5)], dtype=float) * 100
    g += np.random.default_rng(2).uniform(-3, 3, g.shape)
    P = np.vstack([g, [[200, -4]]])                                      # a hair below the bottom edge
    tri = measures.delaunay(P)
    kept, removed = measures.peel(P, tri)
    assert removed >= 1
    bset = {tuple(e) for e in measures.boundary_edges(kept)}
    on_bnd = [any(tuple(sorted((t[k], t[(k + 1) % 3]))) in bset for k in range(3)) for t in kept]
    assert (measures._min_angle_deg(P, kept)[on_bnd] >= measures.PEEL_MIN_DEG).all()


def test_folds_count_flipped_triangles():
    A = np.array([[0, 0], [4, 0], [2, 3], [2, 1]], dtype=float)
    tri = measures.delaunay(A)
    assert len(tri) == 3
    X = A.copy()
    X[3] = [2, -1]                                                        # the centre pushed out
    f = measures.fold_stats(tri, A, X)
    assert f["folded"] == 1 and 0 < f["area_share"] < 1
    assert measures.fold_stats(tri, A, A)["folded"] == 0


def test_boundary_crossings_catch_an_overlap_without_flips():
    m = 13
    A = np.array([[i, v] for v in (0, 1) for i in range(m)], dtype=float)
    tri = []
    for i in range(m - 1):
        b0, b1, t0, t1 = i, i + 1, m + i, m + i + 1
        tri += [[b0, b1, t0], [b1, t1, t0]]
    tri = np.array(tri)
    assert (measures.signed_areas(A, tri) > 0).all()
    turns = 1.25 * 2 * math.pi / (m - 1)
    X = np.array([[(3 - 0.5 * v) * math.cos(i * turns), (3 - 0.5 * v) * math.sin(i * turns)] for i, v in A])
    f = measures.fold_stats(tri, A, X)
    assert f["folded"] == 0 and f["boundary_crossings"] > 0
    assert measures.fold_stats(tri, A, A)["boundary_crossings"] == 0


# ── the issue's checks ──────────────────────────────────────────────
def test_ratio_check(pairs):
    X = pairs.xy
    res = measures.ratio_check(pairs, X, "syn:G55", "syn:G22", sigma=100.0)
    assert res["status"] == "pass" and res["minutes_directed"] == [7, 8]
    assert res["ratio_to_median"] > 1.5                                   # far on the real map for its time
    assert measures.ratio_check(pairs, X, "syn:G55", "syn:G22", sigma=0.1)["status"] == "fail"
    assert measures.ratio_check(pairs, X, "syn:NOPE", "syn:G22", sigma=1)["status"] == "absent"


def test_line_order(pairs):
    run = [{"name": "diagonal", "stations": ["syn:G22", "syn:G33", "syn:G44", "syn:G55", "syn:NOPE"]}]
    res = measures.line_order(pairs, pairs.xy, run)[0]
    assert res["in_order"] and res["absent"] == ["syn:NOPE"] and res["stations"] == 4
    X = pairs.xy.copy()
    X[_node(pairs, "syn:G44")] = X[_node(pairs, "syn:G22")] + [10, 10]
    res = measures.line_order(pairs, X, run)[0]
    assert not res["in_order"]
    bt = res["backtracks"][0]
    assert bt["station"] == "Syn G44" and bt["before"] == "Syn G33" and bt["metres"] > 1000


# ── the whole job ───────────────────────────────────────────────────
def _run(tmp_path, monkeypatch, out_name="layout"):
    monkeypatch.setattr(settings, "OUT", tmp_path / "out")
    if not (tmp_path / "out" / BUILD).exists():
        write_build(tmp_path / "out")
    checks = write_checks(tmp_path / "checks.toml")
    out = tmp_path / out_name
    code = job.main(["--build", BUILD, "--out-dir", str(out), "--checks", str(checks)])
    return code, out


def test_build_end_to_end(tmp_path, monkeypatch):
    code, out = _run(tmp_path, monkeypatch)
    assert code == 0
    for name in ("layout.json", "layout.js", "index.html", "viewer.js", "viewer.css", "report.json",
                 "report.md", "shepard.svg", "pairs.npz"):
        assert (out / name).is_file(), name
    doc = json.loads((out / "layout.json").read_text())
    frames = doc["frames"]
    assert frames[0]["lam_tilde"] is None and len(frames) - 1 >= 20 and frames[-1]["lam_tilde"] == 0
    pairs = station_pairs(load_build(tmp_path / "out" / BUILD, WINDOW))
    assert frames[0]["x"] == [int(round(v)) for v in pairs.xy[:, 0]]
    assert frames[-1]["metrics"]["sigma1"] < frames[0]["metrics"]["sigma1"]
    w, b = _node(pairs, "syn:G55"), _node(pairs, "syn:G22")
    def gap(f):
        return math.hypot(f["x"][w] - f["x"][b], f["y"][w] - f["y"][b])
    assert gap(frames[-1]) < gap(frames[0])
    assert doc["labels"] == [b, w] and doc["notice"] and doc["left_out"]

    rep = json.loads((out / "report.json").read_text())
    h = rep["headlines"]
    for key in ("smallest_fold_free", "fold_free_run_from_real_map_ends_at", "wembley_at_time_map",
                "tc_time_map", "tc_pass", "metropolitan_backtracks", "metropolitan_absent", "restart_better_found"):
        assert key in h
    assert h["metropolitan_absent"] == ["syn:NOPE"]
    assert all(f["monotone"] and not f["rose"] for f in rep["frames"])
    md = (out / "report.md").read_text()
    for words in ("Normalised stress", "Trustworthiness / continuity", "smallest fold-free",
                  "Wembley Park to Baker Street", "Metropolitan line", "not licensed for publication"):
        assert words in md

    gz = sum(len(gzip.compress((out / n).read_bytes())) for n in ("layout.js", "index.html", "viewer.js",
                                                                      "viewer.css"))
    assert gz < 2_000_000
    assert (out / "layout.js").read_text().startswith("window.LAYOUT = {")
    svg = (out / "shepard.svg").read_text()
    assert svg.startswith("<svg") and "<rect" in svg


def test_two_runs_write_the_same_layout(tmp_path, monkeypatch):
    _, a = _run(tmp_path, monkeypatch, "a")
    _, b = _run(tmp_path, monkeypatch, "b")
    assert (a / "layout.json").read_bytes() == (b / "layout.json").read_bytes()


def test_output_inside_the_repo_must_be_git_ignored(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "OUT", tmp_path / "out")
    write_build(tmp_path / "out")
    target = ROOT / "static" / "layout-test-should-not-exist"
    assert job.main(["--build", BUILD, "--out-dir", str(target)]) == 2
    assert "not git-ignored" in capsys.readouterr().err
    assert not target.exists()


def test_viewer_makes_no_requests():
    viewer = ROOT / "transit" / "layout" / "viewer"
    for path in viewer.iterdir():
        text = path.read_text()
        for word in ("http:", "https:", "fetch(", "/api/", "XMLHttpRequest", "import "):
            assert word not in text, f"{path.name} has {word!r}"
    html = (viewer / "index.html").read_text()
    assert '<script src="layout.js"></script>' in html and 'name="robots" content="noindex"' in html


def test_layout_imports_numpy_only():
    code = ("import sys, transit.layout.job; "
            "bad = [m for m in ('scipy', 'pandas', 'sklearn', 'h3', 'shapely', 'pyproj') if m in sys.modules]; "
            "print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
