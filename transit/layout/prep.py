"""
From a transit build to the pairs the layout is solved over (#6, Direction 2):

- nodes: usable stations at their real positions, merged where closer than 50 m;
- pairs: no self-pairs; only pairs with a time both ways (255 is "no time", never a length);
  t = the mean of the two directions, floored at 0.5 min; w = t^-2; the directed minutes kept;
- only the largest group of stations joined by such pairs is laid out; the rest are named.

The build is checked against its own manifest and fingerprint before anything is read, so a
pilot, stale or damaged table is refused instead of drawn.
"""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from transit.layout.frame import to_local

UNREACHABLE = 255
MERGE_M = 50.0
FLOOR_MIN = 0.5
WEIGHT_EXPONENT = -2.0
_NAME_SUFFIXES = (" Underground Station", " DLR Station", " Tram Stop", " Rail Station", " UND",
                  " (London)")


class Refused(Exception):
    """The job stops before writing anything: the reason is printed and the exit code is 2."""


class RefusedInput(Refused):
    """The build can't be used as it is (missing, incomplete, or not what its manifest says)."""


class RefusedOutput(Refused):
    """The output folder or the output itself breaks a rule (ignored by git, not shipped, ≤ 2 MB)."""


class RefusedSolve(Refused):
    """A solve did not converge, or raised the objective: its layout is not a solution."""


class LengthError(ValueError):
    """A spring length that must never be one: 255, over the time limit, or not a number."""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class BuildInput:
    dir: Path
    window: str
    points: list[dict]
    point_ok: np.ndarray
    table: np.ndarray            # uint8 (N, N), memory-mapped
    manifest: dict
    fingerprint: dict
    max_time: int
    table_sha256: str
    points_sha256: str

    @property
    def departure(self) -> str | None:
        return self.manifest["outputs"][f"tt_{self.window}"].get("departure")


def load_build(build_dir: Path, window: str) -> BuildInput:
    """Read and check one build's points, mask, table, manifest and table fingerprint."""
    build_dir = Path(build_dir)
    names = {"points": "points.csv", "ok": "point_ok.npy", "table": f"tt_{window}.npy",
             "fingerprint": f"tt_{window}.fingerprint.json", "manifest": "manifest.json"}
    paths = {k: build_dir / v for k, v in names.items()}
    missing = [v for k, v in names.items() if not paths[k].is_file()]
    if missing:
        raise RefusedInput(f"{build_dir} has no {', '.join(missing)}")

    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    outputs = manifest.get("outputs", {})
    entry = outputs.get(f"tt_{window}")
    if entry is None:
        raise RefusedInput(f"manifest.json does not list tt_{window} (manifests list complete tables only)")
    table_sha = sha256(paths["table"])
    if entry.get("sha256") != table_sha:
        raise RefusedInput(f"tt_{window}.npy is not the table manifest.json describes (sha256 differs)")
    ok_entry = outputs.get("point_ok.npy")
    if ok_entry is None or ok_entry.get("sha256") != sha256(paths["ok"]):
        raise RefusedInput("point_ok.npy is not the mask manifest.json describes")

    fingerprint = json.loads(paths["fingerprint"].read_text(encoding="utf-8"))
    if not fingerprint.get("complete"):
        raise RefusedInput(f"tt_{window}.npy is incomplete (a pilot or an interrupted build)")
    if manifest.get("tables_fingerprints", {}).get(window) != fingerprint.get("inputs"):
        raise RefusedInput(f"tt_{window}.fingerprint.json is not the fingerprint manifest.json records")
    points_sha = sha256(paths["points"])
    if fingerprint.get("inputs", {}).get("points_sha256") != points_sha:
        raise RefusedInput(f"points.csv is not the one tt_{window}.npy was built from")

    try:
        max_time = int(manifest["settings"]["r5"]["max_time_min"])
    except (KeyError, TypeError, ValueError):
        raise RefusedInput("manifest.json has no settings.r5.max_time_min") from None
    if not 0 < max_time < UNREACHABLE:
        raise RefusedInput(f"max_time_min {max_time} must be below the 255 sentinel")

    with open(paths["points"], newline="", encoding="utf-8") as f:
        points = list(csv.DictReader(f))
    try:
        in_order = [int(p["idx"]) for p in points] == list(range(len(points)))
    except (KeyError, TypeError, ValueError):
        in_order = False
    if not in_order:
        raise RefusedInput("points.csv rows are not in idx order")
    total = manifest.get("points", {}).get("total")
    if total is not None and total != len(points):
        raise RefusedInput(f"points.csv has {len(points)} rows, manifest.json says {total}")

    table = np.load(paths["table"], mmap_mode="r")
    if table.dtype != np.uint8 or table.ndim != 2 or table.shape != (len(points), len(points)):
        raise RefusedInput(f"tt_{window}.npy must be uint8 of shape ({len(points)}, {len(points)}), "
                           f"not {table.dtype} {table.shape}")
    if list(entry.get("shape", table.shape)) != list(table.shape):
        raise RefusedInput(f"tt_{window}.npy shape differs from manifest.json")
    counts = np.bincount(np.asarray(table).ravel(), minlength=256)
    bad = int(counts[max_time + 1:UNREACHABLE].sum())
    if bad:
        raise RefusedInput(f"tt_{window}.npy has {bad} values between {max_time + 1} and 254: "
                           f"a time must be 0-{max_time} or 255 (no time)")

    point_ok = np.load(paths["ok"])
    if point_ok.dtype != bool or point_ok.shape != (len(points),):
        raise RefusedInput("point_ok.npy must be a boolean mask with one entry per point")

    return BuildInput(build_dir, window, points, point_ok, table, manifest, fingerprint, max_time,
                      table_sha, points_sha)


def clean_name(name: str) -> str:
    """A station's name for a label: 'WEMBLEY PARK UND' -> 'Wembley Park'; 'Nine Elms Underground
    Station (N/A), London' -> 'Nine Elms'."""
    out = name.replace(" (N/A)", "").strip()
    if out.endswith(", London"):
        out = out[: -len(", London")]
    for suffix in _NAME_SUFFIXES:
        if out.endswith(suffix):
            out = out[: -len(suffix)]
    if out.isupper():
        out = " ".join(w[:1].upper() + w[1:].lower() for w in out.split(" "))
    return out


def median_scale(real: np.ndarray, t: np.ndarray) -> float:
    """Metres of map per minute: median real distance ÷ median time, which must be a positive number."""
    s = float(np.median(real) / np.median(t)) if len(real) and len(t) else float("nan")
    if not (np.isfinite(s) and s > 0):
        raise RefusedInput(f"the scale (median real distance ÷ median time) is {s}, not a positive number")
    return s


def _pairwise(xy: np.ndarray) -> np.ndarray:
    diff = xy[:, None, :] - xy[None, :, :]
    return np.sqrt((diff ** 2).sum(-1))


def merge_close(xy: np.ndarray, metres: float = MERGE_M) -> list[list[int]]:
    """Groups of points joined by chains of distances under `metres` (single linkage),
    each sorted, in order of their first member."""
    n = len(xy)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    d = _pairwise(xy)
    for i, j in np.argwhere(np.triu(d < metres, 1)):
        ri, rj = find(int(i)), find(int(j))
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=lambda g: g[0])


def components(adjacent: np.ndarray) -> list[list[int]]:
    """Connected groups of an undirected boolean adjacency matrix, largest first (ties: the
    group with the smallest first member)."""
    n = len(adjacent)
    seen = np.zeros(n, dtype=bool)
    out = []
    for start in range(n):
        if seen[start]:
            continue
        group = np.zeros(n, dtype=bool)
        group[start] = True
        frontier = group.copy()
        while frontier.any():
            nxt = adjacent[frontier].any(axis=0) & ~group
            group |= nxt
            frontier = nxt
        seen |= group
        out.append([int(i) for i in np.where(group)[0]])
    return sorted(out, key=lambda g: (-len(g), g[0]))


@dataclass
class Pairs:
    """The nodes and spring lengths the layout is solved over (all arrays in node order)."""
    ids: list[str]               # each node's smallest member id
    names: list[str]             # member names joined by " / "
    labels: list[str]            # the first member's cleaned name
    members: list[list[int]]     # points.csv idx of each member
    member_ids: list[list[str]]
    lonlat: np.ndarray           # (n, 2) mean of the members
    xy: np.ndarray               # (n, 2) local metres (frame.py)
    directed: np.ndarray         # (n, n) uint8, row = from, the fastest member-to-member time
    t: np.ndarray                # (n, n) minutes: floored mean of both directions, NaN unused, 0 diagonal
    w: np.ndarray                # (n, n) t^-2 where used, else 0
    used: np.ndarray             # (n, n) bool, symmetric, False on the diagonal
    s: float                     # metres of map per minute, fixed once
    max_time: int
    left_out: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.ids)

    @property
    def delta(self) -> np.ndarray:
        """Target map distances in metres (0 where unused)."""
        return np.where(self.used, self.s * np.nan_to_num(self.t, nan=0.0), 0.0)

    def index_of(self, point_id: str) -> int | None:
        for i, ids in enumerate(self.member_ids):
            if point_id in ids:
                return i
        return None


def check_lengths(pairs: Pairs) -> None:
    """Raise LengthError unless every used length is a real time: finite, from two directions
    that both have a time (never 255), at most the build's limit, and at least the floor."""
    used = pairs.used
    if used.shape != pairs.t.shape or not np.array_equal(used, used.T) or used.diagonal().any():
        raise LengthError("the used-pair mask must be symmetric with an empty diagonal")
    t = pairs.t[used]
    if not np.all(np.isfinite(t)):
        raise LengthError("a used length is not a finite number of minutes")
    if (pairs.directed[used] == UNREACHABLE).any() or (pairs.directed.T[used] == UNREACHABLE).any():
        raise LengthError("255 (no time) would be used as a length")
    if (t > pairs.max_time).any():
        raise LengthError(f"a used length is over the build's {pairs.max_time}-minute limit")
    if (t < FLOOR_MIN).any():
        raise LengthError(f"a used length is under the {FLOOR_MIN}-minute floor")
    if not np.allclose(pairs.w[used], t ** WEIGHT_EXPONENT) or (pairs.w[~used] != 0).any():
        raise LengthError("weights must be t^-2 on used pairs and 0 elsewhere")


def station_pairs(b: BuildInput) -> Pairs:
    """Usable stations of a checked build -> merged nodes and their spring lengths."""
    rows = [p for p in b.points
            if p["kind"] == "station" and p["ok"] == "True" and b.point_ok[int(p["idx"])]]
    excluded_stations = sum(1 for p in b.points if p["kind"] == "station") - len(rows)
    if not rows:
        raise RefusedInput("the build has no usable station")
    idx = np.array([int(p["idx"]) for p in rows], dtype=np.int64)
    lonlat = np.array([[float(p["lon"]), float(p["lat"])] for p in rows])
    xy = to_local(lonlat[:, 0], lonlat[:, 1])
    sub = np.asarray(b.table[np.ix_(idx, idx)])

    groups = merge_close(xy)
    by_row = np.stack([sub[g].min(axis=0) for g in groups])
    directed = np.stack([by_row[:, g].min(axis=1) for g in groups], axis=1)
    np.fill_diagonal(directed, 0)
    merges = [[rows[i]["id"] for i in g] for g in groups if len(g) > 1]
    node_lonlat = np.array([lonlat[g].mean(axis=0) for g in groups])

    n = len(groups)
    off = ~np.eye(n, dtype=bool)
    has = directed != UNREACHABLE
    both = has & has.T & off
    one_way = int((has ^ has.T)[np.triu_indices(n, 1)].sum())

    comps = components(both)
    keep = comps[0]
    if len(keep) < 3:
        raise RefusedInput(f"the largest group of stations joined both ways has {len(keep)} nodes")
    left_out = [{"group": gi, "ids": [rows[m]["id"] for i in g for m in groups[i]],
                 "names": [rows[m]["name"] for i in g for m in groups[i]]}
                for gi, g in enumerate(comps[1:], start=1)]

    k = np.array(keep)
    directed = directed[np.ix_(k, k)]
    both = both[np.ix_(k, k)]
    groups = [groups[i] for i in keep]
    node_lonlat = node_lonlat[k]
    n = len(k)

    d = directed.astype(np.float64)
    mean = (d + d.T) / 2
    floored = int((both & (mean < FLOOR_MIN))[np.triu_indices(n, 1)].sum())
    t = np.where(both, np.maximum(mean, FLOOR_MIN), np.nan)
    np.fill_diagonal(t, 0.0)
    w = np.where(both, np.where(both, t, 1.0) ** WEIGHT_EXPONENT, 0.0)

    node_xy = to_local(node_lonlat[:, 0], node_lonlat[:, 1])
    iu = np.triu_indices(n, 1)
    use_iu = both[iu]
    real = _pairwise(node_xy)[iu][use_iu]
    s = median_scale(real, t[iu][use_iu])
    asym = np.abs(d - d.T)[iu][use_iu]

    pairs = Pairs(
        ids=[rows[g[0]]["id"] for g in groups],
        names=[" / ".join(rows[i]["name"] for i in g) for g in groups],
        labels=[clean_name(rows[g[0]]["name"]) for g in groups],
        members=[[int(idx[i]) for i in g] for g in groups],
        member_ids=[[rows[i]["id"] for i in g] for g in groups],
        lonlat=node_lonlat, xy=node_xy, directed=directed, t=t, w=w, used=both, s=s,
        max_time=b.max_time, left_out=left_out,
        counts={"stations": len(rows) + excluded_stations, "stations_usable": len(rows),
                "stations_excluded": excluded_stations, "merges": merges,
                "nodes": n, "pairs_used": int(use_iu.sum()), "pairs_one_way_dropped": one_way,
                "pairs_floored": floored, "groups": len(comps),
                "asymmetry_min": {"median": float(np.median(asym)) if len(asym) else 0.0,
                                  "p90": float(np.percentile(asym, 90)) if len(asym) else 0.0,
                                  "max": float(asym.max()) if len(asym) else 0.0}},
    )
    check_lengths(pairs)
    return pairs
