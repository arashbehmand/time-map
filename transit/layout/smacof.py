"""
Weighted, anchored SMACOF (stress majorization), written here because the common library
versions take neither pair weights nor anchors.

Objective, for one λ:  Σ_{i<j} w_ij (|x_i − x_j| − δ_ij)²  +  λ Σ_i |x_i − a_i|²
with δ = s·t, w = t^-2 and a_i the real position. Each step solves the majorizing quadratic,
X ← (V + λI)⁻¹ (B(X)·X + λA), so the objective never rises and there is no step size. At λ = 0
the pseudo-inverse (V + 11ᵀ/n)⁻¹ − 11ᵀ/n is used and the layout is re-centred. That gives no
guarantee of a global optimum, geographic order or the absence of folds; those are checked.

λ = λ̃ × the mean node weight (mean over i of Σ_j w_ij), so λ̃ = 1 is an anchor about as
strong as an average node's springs taken together.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from transit.layout.prep import Pairs, check_lengths

TOL = 1e-9                 # stop when a step lowers the objective by less than this share
MAX_ITER = 5000
RISE = 1e-10               # a rise above float noise means a bug: stop and flag it
LAM_TILDES = tuple(float(v) for v in np.geomspace(30.0, 1e-3, 24)) + (0.0,)


def distances(X: np.ndarray) -> np.ndarray:
    dx = X[:, 0, None] - X[None, :, 0]
    dy = X[:, 1, None] - X[None, :, 1]
    return np.hypot(dx, dy)


def lam_unit(pairs: Pairs) -> float:
    return float(pairs.w.sum(axis=1).mean())


def _objective(X, w, delta, lam, A, E) -> float:
    pair = 0.5 * float((w * (E - delta) ** 2).sum())
    return pair + (lam * float(((X - A) ** 2).sum()) if lam else 0.0)


def objective(X: np.ndarray, pairs: Pairs, lam: float, A: np.ndarray | None = None) -> float:
    A = pairs.xy if A is None else A
    return _objective(X, pairs.w, pairs.delta, lam, A, distances(X))


def sigma1(X: np.ndarray, pairs: Pairs, E: np.ndarray | None = None) -> float:
    """Normalised stress (Kruskal stress-1) of the springs, the anchor left out."""
    E = distances(X) if E is None else E
    delta = pairs.delta
    return math.sqrt(float((pairs.w * (E - delta) ** 2).sum()) / float((pairs.w * delta ** 2).sum()))


def node_stress(X: np.ndarray, pairs: Pairs, E: np.ndarray | None = None) -> np.ndarray:
    """Each node's leftover stress: the same ratio over its own pairs."""
    E = distances(X) if E is None else E
    delta = pairs.delta
    num = (pairs.w * (E - delta) ** 2).sum(axis=1)
    den = (pairs.w * delta ** 2).sum(axis=1)
    return np.sqrt(num / np.where(den > 0, den, 1.0))


def _guttman_rhs(X: np.ndarray, E: np.ndarray, wd: np.ndarray) -> np.ndarray:
    """B(X)·X, summed as Σ_j w_ij δ_ij (x_i − x_j) / e_ij: each term is a weight times a unit
    vector, so two nodes almost on top of each other can't blow it up the way B's 1/e
    entries do when multiplied out (pairs exactly on top of each other add nothing)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        c = np.where(E > 0, wd / E, 0.0)
    dx = X[:, 0, None] - X[None, :, 0]
    dy = X[:, 1, None] - X[None, :, 1]
    return np.stack([(c * dx).sum(axis=1), (c * dy).sum(axis=1)], axis=1)


@dataclass
class Solve:
    X: np.ndarray
    lam: float
    converged: bool
    rose: bool
    trace: list[float] = field(default_factory=list)

    @property
    def iterations(self) -> int:
        return len(self.trace) - 1


def solve(X0: np.ndarray, pairs: Pairs, lam: float, A: np.ndarray | None = None,
          tol: float = TOL, max_iter: int = MAX_ITER) -> Solve:
    """Minimise the objective for one λ from X0. trace holds the objective at X0 and after
    every accepted step."""
    check_lengths(pairs)
    if not (lam >= 0 and math.isfinite(lam)):
        raise ValueError(f"λ must be a finite number ≥ 0, not {lam}")
    A = pairs.xy if A is None else np.asarray(A, dtype=np.float64)
    n = pairs.n
    w, delta = pairs.w, pairs.delta
    V = -w.copy()
    np.fill_diagonal(V, w.sum(axis=1))
    if lam > 0:
        M = np.linalg.inv(V + lam * np.eye(n))
    else:
        J = np.full((n, n), 1.0 / n)
        M = np.linalg.inv(V + J) - J
    X = np.array(X0, dtype=np.float64)
    if lam == 0:
        X -= X.mean(axis=0)
    E = distances(X)
    f = _objective(X, w, delta, lam, A, E)
    trace = [f]
    converged = rose = False
    wd = w * delta
    for _ in range(max_iter):
        rhs = _guttman_rhs(X, E, wd)
        if lam > 0:
            rhs += lam * A
        Xn = M @ rhs
        if lam == 0:
            Xn -= Xn.mean(axis=0)
        En = distances(Xn)
        fn = _objective(Xn, w, delta, lam, A, En)
        if fn > f:
            # The majorizer can't raise the objective, so the step is not taken. A rise above
            # float noise is a bug (flagged); at the noise floor the solve has converged.
            rose = fn > f * (1 + RISE)
            converged = not rose
            break
        X, E = Xn, En
        trace.append(fn)
        if f - fn <= tol * f:
            converged = True
            break
        f = fn
    return Solve(X, lam, converged, rose, trace)


def align_rigid(X: np.ndarray, A: np.ndarray) -> tuple[np.ndarray, dict]:
    """X moved onto A by a proper rotation and a shift only (no scale, no reflection)."""
    mx, ma = X.mean(axis=0), A.mean(axis=0)
    U, _, Vt = np.linalg.svd((X - mx).T @ (A - ma))
    R = U @ Vt
    reflect = bool(np.linalg.det(R) < 0)
    if reflect:
        U[:, -1] *= -1
        R = U @ Vt
    return (X - mx) @ R + ma, {"rotation_deg": math.degrees(math.atan2(R[0, 1], R[0, 0])),
                               "reflection_fits_better": reflect}


def sweep(pairs: Pairs, lam_tildes=LAM_TILDES, tol: float = TOL, max_iter: int = MAX_ITER) -> list[dict]:
    """Continuation from the real map: each λ starts from the previous aligned layout."""
    A = pairs.xy
    unit = lam_unit(pairs)
    X = A.copy()
    out = []
    for lt in lam_tildes:
        res = solve(X, pairs, lt * unit, A, tol=tol, max_iter=max_iter)
        X, info = align_rigid(res.X, A)
        trace = np.asarray(res.trace)
        out.append({"lam_tilde": float(lt), "lam": float(lt * unit), "X": X,
                    "iterations": res.iterations, "converged": res.converged, "rose": res.rose,
                    "monotone": bool(np.all(np.diff(trace) <= np.abs(trace[:-1]) * 1e-12)),
                    "objective": float(trace[-1]), **info})
    return out


def classical_mds(pairs: Pairs) -> np.ndarray:
    """Torgerson's classical MDS of the target lengths (unused pairs at the longest length)."""
    n = pairs.n
    d = np.where(pairs.used, pairs.delta, pairs.s * pairs.max_time)
    np.fill_diagonal(d, 0.0)
    J = np.eye(n) - 1.0 / n
    vals, vecs = np.linalg.eigh(-0.5 * J @ (d ** 2) @ J)
    return vecs[:, -2:][:, ::-1] * np.sqrt(np.maximum(vals[-2:][::-1], 0.0))


def restart_check(pairs: Pairs, sigma_ref: float, seeds=(1, 2), tol: float = TOL,
                  max_iter: int = MAX_ITER, margin: float = 1e-3) -> dict:
    """Solve λ = 0 again from classical MDS and from random starts. Starts that land on a
    mirror image are fine, so only stress is compared; any start more than `margin` (a share)
    lower than the continuation is flagged."""
    spread = float(pairs.xy.std())
    starts = {"classical_mds": classical_mds(pairs)}
    for seed in seeds:
        starts[f"random_{seed}"] = np.random.default_rng(seed).normal(size=(pairs.n, 2)) * spread
    results = {}
    for name, X0 in starts.items():
        res = solve(X0, pairs, 0.0, tol=tol, max_iter=max_iter)
        results[name] = {"sigma1": sigma1(res.X, pairs), "iterations": res.iterations,
                         "converged": res.converged, "rose": res.rose}
    best = min(r["sigma1"] for r in results.values()) if results else sigma_ref
    return {"continuation_sigma1": sigma_ref, "starts": results, "best_sigma1": best,
            "better_start_found": bool(best < sigma_ref * (1 - margin)),
            "complete": all(r["converged"] and not r["rose"] for r in results.values())}
