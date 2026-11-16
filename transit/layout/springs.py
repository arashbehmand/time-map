"""
The spring grid of the full London time map (#6, L-02), and its solve. Numpy only.

    E(ρ) = [ Σ_edges a·ψ(l/l₀) + ρ·Σ_links b·ψ₊(l/(s·t))
             + Σ_triangles A₀·(κ·φ(J) + κₛ·μ(F)) ] ÷ (ΣA₀ / free nodes)

- Grid springs on every mesh edge: rest length l₀ = the real length, weight a = a third of the
  real area of the triangles on the edge. They keep the map recognisable.
- Train springs between stations a train serves one after the other: rest length s·t (the
  layout's scale times the table's travel time), weight b = real length × the median mesh edge.
  Both weights are areas, so ρ, the slider, is a plain ratio of the two.
- Train springs only pull: ψ₊(λ) = ψ(λ) for λ ≥ 1 and 0 below. A train link says its two stations
  are at most s·t apart. Most table times between neighbours are longer than the real hop
  (they include reaching the platform and waiting), and a spring that pushed would make a chain
  of them buckle into zigzags: the grid has no room for a line twice its length. Fast hops pull
  their stations together; slow hops leave the grid alone unless their stations end up further
  apart than s·t. ψ₊ is convex and once differentiable; its second derivative jumps at λ = 1.
- ψ(λ) = ½(λ² − 1) − ln λ: zero and flat at rest, close to Hooke's law near it, and infinite as a
  spring shrinks to nothing, so no spring can be compressed fully.
- φ(J) = ½(J + 1/J) − 1 on each triangle's area ratio J = A/A₀: infinite as a triangle's area goes
  to zero. It resists bunching up, and no further.
- μ(F) = ‖F‖²/J − 2 on each triangle's map F from the real map (the larger over the smaller
  singular value, plus its inverse, minus 2): zero for any rotation or uniform scaling, so it
  leaves bunching to φ, and infinite as a triangle goes thin, whatever its area. Edge springs
  can't stop that alone: a triangle can go thin with every edge long.
- Dividing by the real area per free node makes the gradient about one node's worth of force.

ρ = 0 is the real map: every spring is at rest and the energy is exactly zero.

The solve is Levenberg–Marquardt Newton on the exact Hessian. Every trial step is cut to 0.9 of
the first point where any triangle's signed area would reach zero along it (the area is a
quadratic in the step length), so no point on any step is folded, and the energy can only go
down. The guard ring is fixed, so there is no rotation or shift left to align.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from transit.layout.measures import signed_areas
from transit.layout.mesh import Mesh

KAPPA = 1.0                   # the area term's weight against the grid's
KAPPA_SHAPE = 1.0             # the shape term's weight against the grid's
TOL = 1e-6                    # |gradient|∞ at a solution (normalised energy and coordinates)
MAX_ITER = 2000
STEP_CAP = 0.9                # a share of the step to the first zero area
GAIN_MIN = 1e-3               # an accepted step lowers E by at least this share of the model's promise
MU_UP, MU_DOWN = 4.0, 0.3
MU_MIN, MU_MAX = 1e-12, 1e12
SUBDIVIDE = 3                 # levels of ρ halving (geometric) tried when a position won't converge
RHOS = (0.0,) + tuple(float(v) for v in np.geomspace(0.01, 100.0, 24))

# Constant Hessian of a triangle's signed area in (x0, y0, x1, y1, x2, y2).
_HA = np.zeros((6, 6))
for _i, _j, _v in ((0, 3, 0.5), (0, 5, -0.5), (2, 5, 0.5), (2, 1, -0.5), (4, 1, 0.5), (4, 3, -0.5)):
    _HA[_i, _j] = _HA[_j, _i] = _v


def psi(lam):
    lam = np.asarray(lam, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(lam > 0, 0.5 * (lam ** 2 - 1) - np.log(np.where(lam > 0, lam, 1.0)), np.inf)


def psi_pull(lam):
    """ψ₊: ψ where a spring is stretched past its rest length, 0 where it is shorter."""
    lam = np.asarray(lam, dtype=np.float64)
    return np.where(lam >= 1, psi(np.maximum(lam, 1.0)), 0.0)


def phi(J):
    J = np.asarray(J, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(J > 0, 0.5 * (J + 1 / np.where(J > 0, J, 1.0)) - 1, np.inf)


@dataclass
class Model:
    """Everything the energy needs, in metres. Build it with `model()`."""
    P: np.ndarray             # (N, 2) real positions
    fixed: np.ndarray         # (N,) bool
    edges: np.ndarray         # (E, 2)
    l0: np.ndarray            # (E,)
    a: np.ndarray             # (E,)
    links: np.ndarray         # (K, 2)
    rest: np.ndarray          # (K,) s·t, metres
    b: np.ndarray             # (K,)
    tri: np.ndarray           # (T, 3)
    A0: np.ndarray            # (T,)
    kappa: float = KAPPA
    kappa_shape: float = KAPPA_SHAPE
    scale: float = 1.0        # metres per normalised unit (the median mesh edge)
    norm: float = 1.0         # energy divisor: the real area per free node, normalised units
    free: np.ndarray = field(init=False)
    dof: np.ndarray = field(init=False)
    shape_M: np.ndarray = field(init=False)   # (T, 3, 3): ‖F‖² = Σ M_ab x_a·x_b over each triangle's corners

    def __post_init__(self):
        self.shape_M = shape_matrices(self.P, self.tri)
        self.free = np.where(~self.fixed)[0]
        dof = np.full(2 * len(self.P), -1, dtype=np.int64)
        dof[2 * self.free] = 2 * np.arange(len(self.free))
        dof[2 * self.free + 1] = 2 * np.arange(len(self.free)) + 1
        self.dof = dof

    @property
    def n_dof(self) -> int:
        return 2 * len(self.free)


def shape_matrices(P: np.ndarray, tri: np.ndarray) -> np.ndarray:
    """For each triangle, the 3×3 M with ‖F‖² = Σ_ab M_ab (x_a · x_b), F the map from the real
    corners P to the corners x: M = Dᵀ E₀⁻¹ E₀⁻ᵀ D, E₀ = [p1 − p0, p2 − p0], D = [[−1, 1, 0], [−1, 0, 1]]."""
    E0 = np.stack([P[tri[:, 1]] - P[tri[:, 0]], P[tri[:, 2]] - P[tri[:, 0]]], axis=2)
    inv = np.linalg.inv(E0)
    S = inv @ np.swapaxes(inv, 1, 2)
    D = np.array([[-1.0, 1.0, 0.0], [-1.0, 0.0, 1.0]])
    return D.T[None] @ S @ D[None]


def model(mesh: Mesh, links: np.ndarray, rest_m: np.ndarray, kappa: float = KAPPA,
          kappa_shape: float = KAPPA_SHAPE) -> Model:
    links = np.asarray(links, dtype=np.int64).reshape(-1, 2)
    rest_m = np.asarray(rest_m, dtype=np.float64).reshape(-1)
    if len(links) != len(rest_m) or (rest_m <= 0).any() or not np.isfinite(rest_m).all():
        raise ValueError("every train spring needs a positive, finite rest length")
    L = mesh.median_edge
    real = np.hypot(*(mesh.P[links[:, 0]] - mesh.P[links[:, 1]]).T) if len(links) else np.zeros(0)
    b = real * L
    n_free = int((~mesh.fixed).sum())
    return Model(P=mesh.P / L, fixed=mesh.fixed.copy(), edges=mesh.edges, l0=mesh.l0 / L, a=mesh.a_edge / L ** 2,
                 links=links, rest=rest_m / L, b=b / L ** 2, tri=mesh.tri, A0=mesh.A0 / L ** 2, kappa=kappa,
                 kappa_shape=kappa_shape, scale=L, norm=float(mesh.A0.sum()) / L ** 2 / max(n_free, 1))


# ── energy, gradient, Hessian (normalised units) ─────────────────────
def _springs(X, ij, rest, w):
    d = X[ij[:, 0]] - X[ij[:, 1]]
    ln = np.hypot(d[:, 0], d[:, 1])
    return d, ln, ln / rest


def shape_norm2(m: Model, X: np.ndarray) -> np.ndarray:
    """‖F‖² of each triangle at X."""
    C = X[m.tri]                                   # (T, 3, 2)
    return np.einsum("tab,tak,tbk->t", m.shape_M, C, C)


def mips(m: Model, X: np.ndarray) -> np.ndarray:
    """μ = ‖F‖²/J − 2 of each triangle (≥ 0; +inf where J ≤ 0)."""
    J = signed_areas(X, m.tri) / m.A0
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(J > 0, shape_norm2(m, X) / np.where(J > 0, J, 1.0) - 2, np.inf)


def parts(m: Model, X: np.ndarray, rho: float) -> dict:
    """The energy's four terms at X (normalised), +inf where a spring or triangle has collapsed."""
    _, _, lam = _springs(X, m.edges, m.l0, m.a)
    grid = float((m.a * psi(lam)).sum())
    if len(m.links):
        _, _, lk = _springs(X, m.links, m.rest, m.b)
        train = float((m.b * psi_pull(lk)).sum())
    else:
        train = 0.0
    J = signed_areas(X, m.tri) / m.A0
    area = float((m.A0 * phi(J)).sum())
    shape = float((m.A0 * mips(m, X)).sum())
    return {"grid": grid / m.norm, "train": rho * train / m.norm, "area": m.kappa * area / m.norm,
            "shape": m.kappa_shape * shape / m.norm}


def energy(m: Model, X: np.ndarray, rho: float) -> float:
    J = signed_areas(X, m.tri) / m.A0
    if not (J > 0).all():
        return math.inf
    p = parts(m, X, rho)
    return p["grid"] + p["train"] + p["area"] + p["shape"]


def _spring_terms(X, ij, rest, w, coef, pull_only=False):
    """Gradient rows and 2×2 Hessian blocks of Σ coef·w·ψ(l/rest), or of ψ₊ with `pull_only`
    (nothing from a spring shorter than its rest length)."""
    d, ln, lam = _springs(X, ij, rest, w)
    if pull_only:
        w = np.where(lam >= 1, w, 0.0)
    n = d / ln[:, None]
    dpsi = lam - 1 / lam
    d2psi = 1 + 1 / lam ** 2
    g = (coef * w * dpsi / rest)[:, None] * n
    radial = coef * w * d2psi / rest ** 2
    tangent = coef * w * dpsi / (rest * ln)
    nn = n[:, :, None] * n[:, None, :]
    H = radial[:, None, None] * nn + tangent[:, None, None] * (np.eye(2)[None] - nn)
    return g, H


def grad_hess(m: Model, X: np.ndarray, rho: float, hessian: bool = True):
    """Energy, gradient over the free coordinates and (optionally) the dense Hessian there."""
    N = len(X)
    E = energy(m, X, rho)
    if not math.isfinite(E):
        return E, None, None
    G = np.zeros(2 * N)
    rows, cols, vals = [], [], []

    def add_pairs(ij, g, H):
        i2 = np.stack([2 * ij[:, 0], 2 * ij[:, 0] + 1], axis=1)
        j2 = np.stack([2 * ij[:, 1], 2 * ij[:, 1] + 1], axis=1)
        np.add.at(G, i2.ravel(), g.ravel())
        np.add.at(G, j2.ravel(), -g.ravel())
        if hessian:
            for a2, b2, s in ((i2, i2, 1.0), (j2, j2, 1.0), (i2, j2, -1.0), (j2, i2, -1.0)):
                rows.append(np.repeat(a2, 2, axis=1).ravel())
                cols.append(np.tile(b2, (1, 2)).ravel())
                vals.append((s * H).reshape(len(ij), 4).ravel())

    g, H = _spring_terms(X, m.edges, m.l0, m.a, 1.0)
    add_pairs(m.edges, g, H)
    if len(m.links) and rho > 0:
        g, H = _spring_terms(X, m.links, m.rest, m.b, rho, pull_only=True)
        add_pairs(m.links, g, H)

    t = m.tri
    x0, y0 = X[t[:, 0], 0], X[t[:, 0], 1]
    x1, y1 = X[t[:, 1], 0], X[t[:, 1], 1]
    x2, y2 = X[t[:, 2], 0], X[t[:, 2], 1]
    A = 0.5 * ((x1 - x0) * (y2 - y0) - (y1 - y0) * (x2 - x0))
    J = A / m.A0
    dA = 0.5 * np.stack([y1 - y2, x2 - x1, y2 - y0, x0 - x2, y0 - y1, x1 - x0], axis=1)
    dphi = 0.5 * (1 - 1 / J ** 2)
    d2phi = 1 / J ** 3
    # Shape: κₛ·A₀·(N/J − 2), N = ‖F‖² = qᵀ (2M⊗I) q / 2, with ∇J = ∇A/A₀ and ∇²J = H_A/A₀.
    K2 = np.kron(m.shape_M, np.eye(2)[None])                      # (T, 6, 6): ∇²N = 2·K2
    C = X[t].reshape(len(t), 6)                                   # (x0, y0, x1, y1, x2, y2)
    dN = 2 * np.einsum("tij,tj->ti", K2, C)
    N = 0.5 * np.einsum("ti,ti->t", C, dN)
    dJ = dA / m.A0[:, None]
    ks = m.kappa_shape * m.A0
    gt = (m.kappa * dphi)[:, None] * dA + ks[:, None] * (dN / J[:, None] - (N / J ** 2)[:, None] * dJ)
    idx = np.stack([2 * t[:, 0], 2 * t[:, 0] + 1, 2 * t[:, 1], 2 * t[:, 1] + 1, 2 * t[:, 2], 2 * t[:, 2] + 1], axis=1)
    np.add.at(G, idx.ravel(), gt.ravel())
    if hessian:
        outer = lambda u, v: u[:, :, None] * v[:, None, :]  # noqa: E731
        Ht = (m.kappa * d2phi / m.A0)[:, None, None] * outer(dA, dA) \
            + (m.kappa * dphi)[:, None, None] * _HA[None] \
            + ks[:, None, None] * (2 * K2 / J[:, None, None]
                                   - (outer(dN, dJ) + outer(dJ, dN)) / (J ** 2)[:, None, None]
                                   + 2 * (N / J ** 3)[:, None, None] * outer(dJ, dJ)
                                   - (N / J ** 2 / m.A0)[:, None, None] * _HA[None])
        rows.append(np.repeat(idx, 6, axis=1).ravel())
        cols.append(np.tile(idx, (1, 6)).ravel())
        vals.append(Ht.reshape(len(t), 36).ravel())

    G /= m.norm
    g_free = G[m.dof >= 0][np.argsort(m.dof[m.dof >= 0])]
    if not hessian:
        return E, g_free, None
    r = m.dof[np.concatenate(rows)]
    c = m.dof[np.concatenate(cols)]
    v = np.concatenate(vals)
    keep = (r >= 0) & (c >= 0)
    n = m.n_dof
    Hf = np.bincount(r[keep] * n + c[keep], weights=v[keep], minlength=n * n).reshape(n, n) / m.norm
    return E, g_free, Hf


def apply_step(m: Model, X: np.ndarray, d_free: np.ndarray, alpha: float = 1.0) -> np.ndarray:
    Xn = X.copy()
    flat = Xn.reshape(-1)
    sel = m.dof >= 0
    flat[sel] += alpha * d_free[m.dof[sel]]
    return Xn


def max_step(m: Model, X: np.ndarray, d_free: np.ndarray) -> float:
    """The smallest α > 0 at which some triangle's signed area reaches zero along X + α·d
    (inf if none does): A(α) = c0 + c1·α + c2·α² with c0 > 0."""
    D = apply_step(m, np.zeros_like(X), d_free)
    t = m.tri
    u, v = X[t[:, 1]] - X[t[:, 0]], X[t[:, 2]] - X[t[:, 0]]
    du, dv = D[t[:, 1]] - D[t[:, 0]], D[t[:, 2]] - D[t[:, 0]]

    def cross(p, q):
        return p[:, 0] * q[:, 1] - p[:, 1] * q[:, 0]

    c0 = cross(u, v)
    c1 = cross(u, dv) + cross(du, v)
    c2 = cross(du, dv)
    alpha = np.full(len(t), np.inf)
    scale = np.abs(c1) + np.abs(c0) + 1e-300
    lin = np.abs(c2) <= 1e-14 * scale
    with np.errstate(divide="ignore", invalid="ignore"):
        a_lin = np.where(c1 < 0, -c0 / c1, np.inf)
        disc = c1 ** 2 - 4 * c2 * c0
        sq = np.sqrt(np.where(disc >= 0, disc, 0.0))
        q = -0.5 * (c1 + np.where(c1 >= 0, sq, -sq))
        r1 = np.where(c2 != 0, q / c2, np.inf)
        r2 = np.where(q != 0, c0 / q, np.inf)
    roots = np.stack([r1, r2], axis=1)
    roots = np.where((roots > 0) & np.isfinite(roots), roots, np.inf).min(axis=1)
    roots = np.where(disc >= 0, roots, np.inf)
    alpha = np.where(lin, a_lin, roots)
    alpha = np.where(alpha > 0, alpha, np.inf)
    return float(alpha.min()) if len(alpha) else math.inf


# ── the solve ───────────────────────────────────────────────────────
@dataclass
class Solve:
    X: np.ndarray
    rho: float
    converged: bool
    iterations: int
    gmax: float
    rejected: int
    trace: list = field(default_factory=list)
    pd: bool = False          # the Hessian at X is positive definite (a strict local minimum)

    @property
    def monotone(self) -> bool:
        t = np.asarray(self.trace)
        return bool(np.all(np.diff(t) <= 0)) if len(t) > 1 else True


def solve(m: Model, X0: np.ndarray, rho: float, tol: float = TOL, max_iter: int = MAX_ITER,
          callback=None) -> Solve:
    """Minimise E(ρ) from X0 (normalised units; must be unfolded). Every accepted iterate has
    every triangle's area positive and a lower energy than the one before; `callback(X)` sees
    each of them."""
    if not (rho >= 0 and math.isfinite(rho)):
        raise ValueError(f"ρ must be a finite number ≥ 0, not {rho}")
    X = np.array(X0, dtype=np.float64)
    E, g, H = grad_hess(m, X, rho)
    if not math.isfinite(E):
        raise ValueError("the start has a collapsed triangle")
    trace = [E]
    mu = 1e-3 * max(float(np.abs(np.diag(H)).mean()), 1e-12) if H.size else 1.0
    rejected = 0
    it = 0
    gmax = float(np.abs(g).max()) if g.size else 0.0
    eye = np.eye(m.n_dof)
    while gmax > tol and it < max_iter:
        it += 1
        accepted = False
        while mu <= MU_MAX:
            K = H + mu * eye
            try:
                np.linalg.cholesky(K)
            except np.linalg.LinAlgError:
                mu *= MU_UP
                continue
            d = np.linalg.solve(K, -g)
            amax = max_step(m, X, d)
            alpha = min(1.0, STEP_CAP * amax)
            Xt = apply_step(m, X, d, alpha)
            Et = energy(m, Xt, rho)
            pred = -(alpha * float(g @ d) + 0.5 * alpha ** 2 * float(d @ (H @ d)))
            if math.isfinite(Et) and Et < E and pred > 0 and (E - Et) >= GAIN_MIN * pred:
                X, E = Xt, Et
                mu = max(mu * MU_DOWN, MU_MIN)
                accepted = True
                break
            rejected += 1
            mu *= MU_UP
        if not accepted:
            break
        trace.append(E)
        if callback is not None:
            callback(X)
        E, g, H = grad_hess(m, X, rho)
        gmax = float(np.abs(g).max())
    try:
        np.linalg.cholesky(H)
        pd = True
    except np.linalg.LinAlgError:
        pd = False
    return Solve(X, rho, gmax <= tol, it, gmax, rejected, trace, pd)


def _geo_mid(a: float, b: float) -> float:
    return math.sqrt(a * b) if a > 0 else b / 2


def sweep(m: Model, rhos=RHOS, tol: float = TOL, max_iter: int = MAX_ITER, subdivide: int = SUBDIVIDE,
          log=None, X0: np.ndarray | None = None, smooth: bool = True) -> list[dict]:
    """Continuation from the real map (or from X0): each ρ starts from the previous solved layout.
    ρ = 0 is the start itself. A position that won't converge is reached again through geometric
    sub-steps (not shipped), up to `subdivide` levels; if that fails too, the sweep stops there
    and its last entry says so (with the ρ it actually reached).

    With `smooth`, the steps are also kept small enough to move through: a step whose straight
    blend from the previous layout would fold anywhere along the way (mesh.blend_ok, on whole
    decimetres) is split at its geometric middle, the middle is solved from the previous layout and
    the target from the middle, again up to MOTION_DEPTH levels. Each entry's `between` lists the
    positions solved on the way to it, so the page can slide through every step without a fold;
    `smooth_ok` says whether that held."""
    X = (m.P if X0 is None else np.asarray(X0, dtype=np.float64)).copy()
    out = []
    prev = None
    for rho in rhos:
        if rho == 0:
            out.append({"rho": 0.0, "X": X.copy(), "converged": True, "iterations": 0, "gmax": 0.0,
                        "rejected": 0, "monotone": True, "substeps": 0, "pd": True, "reached": 0.0,
                        "between": [], "smooth_ok": True, "trace": [energy(m, X, 0.0)]})
            prev = 0.0
            continue
        between, res, steps, ok = _advance(m, X, prev if prev is not None else 0.0, rho, tol, max_iter, subdivide,
                                           smooth, 0)
        X = res.X
        prev = rho
        out.append({"rho": float(rho), "X": X.copy(), "converged": res.converged, "iterations": res.iterations,
                    "gmax": res.gmax, "rejected": res.rejected, "monotone": res.monotone, "substeps": steps,
                    "pd": res.pd, "reached": float(res.rho), "between": between, "smooth_ok": ok,
                    "trace": res.trace})
        if log:
            log(f"ρ = {rho:.4g}: {res.iterations} iterations, |g| {res.gmax:.2e}"
                + (f", {steps} sub-steps" if steps else "")
                + (f", {len(between)} in-between positions" if between else "")
                + ("" if res.converged else " (NOT CONVERGED)") + ("" if ok else " (A BLEND FOLDS)"))
        if not res.converged or not ok:
            break
    return out


MOTION_DEPTH = 6              # levels of splitting a step whose blend would fold


def blend_unfolded(m: Model, XA: np.ndarray, XB: np.ndarray) -> bool:
    """Whether the straight blend of two layouts (normalised units), as they ship in whole
    decimetres, keeps every triangle's area positive all the way."""
    from transit.layout.mesh import blend_ok
    return blend_ok(m.tri, quantise(XA * m.scale), quantise(XB * m.scale))


def _advance(m, X, rho_a, rho_b, tol, max_iter, levels, smooth, depth):
    """Solve ρ_b from X (at ρ_a); with `smooth`, split the step until each part blends without a
    fold. Returns (in-between positions, the solve at ρ_b, sub-steps, whether every blend holds)."""
    res, steps = _reach(m, X, rho_a, rho_b, tol, max_iter, levels)
    if not res.converged or not smooth or blend_unfolded(m, X, res.X):
        return [], res, steps, True
    if depth >= MOTION_DEPTH:
        # A jump: even across this tiny step of ρ the blend of the two solutions folds, and
        # splitting finds no solved position that blends across it. The motion follows the
        # solve itself as it settles from one to the other.
        settled = settling(m, X, rho_b, tol, max_iter)
        return (settled[0], settled[1], steps, True) if settled is not None else ([], res, steps, False)
    mid = _geo_mid(rho_a, rho_b)
    left, rm, s1, ok1 = _advance(m, X, rho_a, mid, tol, max_iter, levels, smooth, depth + 1)
    if not (rm.converged and ok1):
        return left, rm, steps + s1, ok1
    right, rb, s2, ok2 = _advance(m, rm.X, mid, rho_b, tol, max_iter, levels, smooth, depth + 1)
    entry = {"rho": float(mid), "X": rm.X.copy(), "iterations": rm.iterations, "converged": rm.converged,
             "monotone": rm.monotone, "settling": False}
    return left + [entry] + right, rb, steps + s1 + s2, ok2


def settling(m: Model, X: np.ndarray, rho: float, tol: float = TOL, max_iter: int = MAX_ITER,
             depth: int = 12) -> tuple[list[dict], Solve] | None:
    """The layouts a solve from X at ρ passes through, as keyframes the page can blend between
    without a fold, and the solve itself (where they lead). Every accepted step is cut short of the
    first zero area along it (max_step), so the solve's own path is unfolded all the way; at whole
    decimetres each straight piece is checked again (and halved where rounding costs it), then the
    fewest keyframes are kept whose blends still hold. None if the solve doesn't converge or a piece
    can't be made to hold."""
    seen = [np.array(X, dtype=np.float64)]
    res = solve(m, X, rho, tol=tol, max_iter=max_iter, callback=lambda Y: seen.append(Y.copy()))
    if not res.converged:
        return None
    pts = [seen[0]]

    def straight(a, b, d):
        if blend_unfolded(m, a, b):
            return []
        if d >= depth:
            raise ValueError("a settling step folds at whole decimetres")
        c = (a + b) / 2
        return straight(a, c, d + 1) + [c] + straight(c, b, d + 1)

    try:
        for a, b in zip(seen, seen[1:]):
            pts += straight(a, b, 0) + [b]
    except ValueError:
        return None
    keep, i = [], 0
    while i < len(pts) - 1:
        j = len(pts) - 1
        while j > i + 1 and not blend_unfolded(m, pts[i], pts[j]):
            j -= 1
        if j < len(pts) - 1:
            keep.append(pts[j])
        i = j
    return [{"rho": float(rho), "X": Y, "iterations": 0, "converged": True, "monotone": True, "settling": True}
            for Y in keep], res


def _reach(m, X, rho_a, rho_b, tol, max_iter, levels):
    res = solve(m, X, rho_b, tol=tol, max_iter=max_iter)
    if res.converged or levels <= 0:
        return res, 0
    mid = _geo_mid(rho_a, rho_b)
    r1, s1 = _reach(m, X, rho_a, mid, tol, max_iter, levels - 1)
    if not r1.converged:
        return r1, s1 + 1
    r2, s2 = _reach(m, r1.X, mid, rho_b, tol, max_iter, levels - 1)
    return r2, s1 + s2 + 1


def quantise(X_m: np.ndarray) -> np.ndarray:
    """Metres to whole decimetres (int64), the form every layout ships in."""
    return np.round(np.asarray(X_m, dtype=np.float64) * 10).astype(np.int64)
