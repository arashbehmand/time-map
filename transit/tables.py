"""
Pure helpers for the transit tables: numpy and h3 only, no Java, no network.

- encode/decode: whole minutes in uint8, 255 = no time within the cap
- Walksheds: station -> H3 res-10 cell walking minutes, stored CSR-style
- compose_row: the runtime formula, time(p, j) = faster of walking straight, or walking
  from p to a nearby station k plus T[k, j] (or T[p, j] when p is itself a table point)
- service-date picking, gate statistics and the ranking rule

The later server provider (#4) can import this module as it is.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable, Sequence

import h3
import numpy as np

UNREACHABLE = 255
MAX_MINUTES = 180
EARTH_R_M = 6371008.8
BANDS = ("le20", "20to45", "gt45")


# ── minutes <-> uint8 ───────────────────────────────────────────────
def encode_minutes(values: np.ndarray | Sequence[float], max_minutes: int = MAX_MINUTES) -> np.ndarray:
    """
    Minutes (float, NaN or inf for no time) to uint8 whole minutes, rounded half up.
    Anything that rounds above max_minutes, and NaN/inf, becomes 255.
    """
    if max_minutes >= UNREACHABLE:
        raise ValueError("max_minutes must be below the 255 sentinel")
    v = np.asarray(values, dtype=np.float64)
    if np.any(v < 0):
        raise ValueError("negative travel time")
    out = np.full(v.shape, UNREACHABLE, dtype=np.uint8)
    finite = np.isfinite(v)
    rounded = np.floor(v[finite] + 0.5)
    ok = rounded <= max_minutes
    vals = np.full(rounded.shape, UNREACHABLE, dtype=np.uint8)
    vals[ok] = rounded[ok].astype(np.uint8)
    out[finite] = vals
    return out


def decode_minutes(u8: np.ndarray) -> np.ndarray:
    """uint8 minutes to float minutes, inf for 255."""
    a = np.asarray(u8)
    out = a.astype(np.float64)
    out[a == UNREACHABLE] = np.inf
    return out


# ── geometry ────────────────────────────────────────────────────────
def haversine_m(lon1, lat1, lon2, lat2):
    """Great-circle distance in metres (numpy-friendly)."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * np.arcsin(np.sqrt(a))


def disk_cells(lon: float, lat: float, res: int, radius_m: float) -> np.ndarray:
    """H3 cells (uint64, sorted) whose centres are within radius_m of (lon, lat)."""
    centre = h3.latlng_to_cell(lat, lon, res)
    spacing = h3.average_hexagon_edge_length(res, unit="m") * math.sqrt(3)
    k = int(math.ceil(radius_m / spacing)) + 1
    cells = list(h3.grid_disk(centre, k))
    lats, lons = np.array([h3.cell_to_latlng(c) for c in cells]).T
    keep = haversine_m(lon, lat, lons, lats) <= radius_m
    ints = np.array([h3.str_to_int(c) for c, k_ in zip(cells, keep) if k_], dtype=np.uint64)
    return np.sort(ints)


def cell_int(lon: float, lat: float, res: int) -> int:
    return h3.str_to_int(h3.latlng_to_cell(lat, lon, res))


# ── walksheds ───────────────────────────────────────────────────────
@dataclass
class Walksheds:
    """
    For each station row s: points[s] is its index in the table, and
    cells[indptr[s]:indptr[s+1]] (sorted uint64 H3 cells) are reached on foot in
    minutes[indptr[s]:indptr[s+1]]. The same times serve access and egress.
    """
    points: np.ndarray    # int32 (S,)
    indptr: np.ndarray    # int64 (S+1,)
    cells: np.ndarray     # uint64 (E,)
    minutes: np.ndarray   # uint8 (E,)

    def __post_init__(self) -> None:
        if len(self.indptr) != len(self.points) + 1 or self.indptr[-1] != len(self.cells):
            raise ValueError("walkshed arrays do not line up")
        if len(self.cells) != len(self.minutes):
            raise ValueError("walkshed cells and minutes differ in length")
        order = np.argsort(self.cells, kind="stable")
        rows = np.repeat(np.arange(len(self.points)), np.diff(self.indptr))
        self._inv_cells = self.cells[order]
        self._inv_rows = rows[order]
        self._inv_minutes = self.minutes[order]

    @classmethod
    def build(cls, rows: Iterable[tuple[int, np.ndarray, np.ndarray]]) -> Walksheds:
        """rows: (table point index, cells, minutes as float with inf/NaN for no path)."""
        points, indptr, cells, minutes = [], [0], [], []
        for point, c, m in rows:
            c = np.asarray(c, dtype=np.uint64)
            m = encode_minutes(m, max_minutes=UNREACHABLE - 1)
            keep = m != UNREACHABLE
            order = np.argsort(c[keep], kind="stable")
            cells.append(c[keep][order])
            minutes.append(m[keep][order])
            points.append(point)
            indptr.append(indptr[-1] + int(keep.sum()))
        return cls(np.asarray(points, dtype=np.int32), np.asarray(indptr, dtype=np.int64),
                   np.concatenate(cells) if cells else np.zeros(0, np.uint64),
                   np.concatenate(minutes) if minutes else np.zeros(0, np.uint8))

    def station(self, row: int) -> tuple[np.ndarray, np.ndarray]:
        lo, hi = self.indptr[row], self.indptr[row + 1]
        return self.cells[lo:hi], self.minutes[lo:hi]

    def near(self, cell: int) -> tuple[np.ndarray, np.ndarray]:
        """Table point indices of the stations whose walkshed holds this cell, and minutes."""
        key = np.uint64(cell)
        lo = np.searchsorted(self._inv_cells, key, side="left")
        hi = np.searchsorted(self._inv_cells, key, side="right")
        rows = self._inv_rows[lo:hi]
        return self.points[rows], self._inv_minutes[lo:hi]

    def save(self, path: Path) -> None:
        np.savez_compressed(path, points=self.points, indptr=self.indptr, cells=self.cells,
                            minutes=self.minutes)

    @classmethod
    def load(cls, path: Path) -> Walksheds:
        with np.load(path) as z:
            return cls(z["points"], z["indptr"], z["cells"], z["minutes"])


# ── the runtime formula ─────────────────────────────────────────────
def compose_row(table: np.ndarray, walksheds: Walksheds, origin_cell: int | None,
                origin_point: int | None = None, direct: np.ndarray | None = None) -> np.ndarray:
    """
    Minutes from one origin to every table point (inf where there is no time).

    origin_cell: the origin's H3 res-10 cell (stations whose walkshed holds it are used)
    origin_point: the origin's own table index, if it is a table point
    direct: walking minutes straight to each table point (inf where not known)
    """
    n = table.shape[1]
    best = np.full(n, np.inf)
    if origin_point is not None:
        best = np.minimum(best, decode_minutes(table[origin_point]))
    if origin_cell is not None:
        pts, mins = walksheds.near(origin_cell)
        if len(pts):
            via = decode_minutes(table[pts]) + mins.astype(np.float64)[:, None]
            best = np.minimum(best, via.min(axis=0))
    if direct is not None:
        best = np.minimum(best, np.asarray(direct, dtype=np.float64))
    return best


def compose_pair(table: np.ndarray, walksheds: Walksheds, origin_cell: int | None,
                 dest_cell: int | None, origin_point: int | None = None,
                 dest_point: int | None = None, direct: float = math.inf) -> float:
    """
    Minutes between two arbitrary points: walk to a station near the origin (or start at the
    origin's own table point), ride the table, walk from a station near the destination (or
    end at its table point); or walk straight (`direct`). inf when nothing connects them.
    """
    o_pts, o_min = [], []
    if origin_point is not None:
        o_pts.append(np.array([origin_point]))
        o_min.append(np.zeros(1))
    if origin_cell is not None:
        p, m = walksheds.near(origin_cell)
        o_pts.append(p)
        o_min.append(m.astype(np.float64))
    d_pts, d_min = [], []
    if dest_point is not None:
        d_pts.append(np.array([dest_point]))
        d_min.append(np.zeros(1))
    if dest_cell is not None:
        p, m = walksheds.near(dest_cell)
        d_pts.append(p)
        d_min.append(m.astype(np.float64))
    best = direct
    if o_pts and d_pts:
        op, om = np.concatenate(o_pts), np.concatenate(o_min)
        dp, dm = np.concatenate(d_pts), np.concatenate(d_min)
        if len(op) and len(dp):
            via = decode_minutes(table[np.ix_(op, dp)]) + om[:, None] + dm[None, :]
            best = min(best, float(via.min()))
    return float(best)


# ── service dates ───────────────────────────────────────────────────
def candidate_weeks(today: date, first_day: date, last_day: date, bank_holidays: set[date],
                    lead_days: int, before: date | None, weekday: int = 2,
                    count: int = 4) -> list[tuple[date, date]]:
    """
    (weekday, Saturday of the same week) pairs, earliest first: at least lead_days after
    today, inside [first_day, last_day], no bank holiday, and ending before `before`.
    """
    d = today + timedelta(days=lead_days)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    out: list[tuple[date, date]] = []
    while len(out) < count:
        sat = d + timedelta(days=5 - weekday)
        if sat > last_day or (before is not None and sat >= before):
            break
        if d >= first_day and d not in bank_holidays and sat not in bank_holidays:
            out.append((d, sat))
        d += timedelta(days=7)
    return out


def next_change(changes, after: date) -> date | None:
    """The first timetable change on or after `after` (None if the list runs out)."""
    later = sorted(d for d in changes if d >= after)
    return later[0] if later else None


def pick_service_dates(candidates: list[tuple[date, date]],
                       closures: Callable[[date], int] | None = None) -> tuple[date, date]:
    """The candidate week whose Saturday has the fewest planned closures (earliest on ties)."""
    if not candidates:
        raise ValueError("no week fits the feeds' calendars, bank holidays and the timetable change")
    if closures is None:
        return candidates[0]
    return min(candidates, key=lambda c: (closures(c[1]), c[0]))


# ── gate statistics ─────────────────────────────────────────────────
def band_of(minutes: float) -> str | None:
    if not math.isfinite(minutes):
        return None
    if minutes <= 20:
        return "le20"
    if minutes <= 45:
        return "20to45"
    return "gt45"


def pair_error(table_min: float, tfl_min: float) -> tuple[str | None, float | None]:
    """
    (band, absolute error) for one pair. The band is TfL's, or the table's when TfL has no
    journey. A time on one side only is an infinite error; no time on either side is
    agreement and is left out of the statistics (band None).
    """
    t_ok, f_ok = math.isfinite(table_min), math.isfinite(tfl_min)
    if not t_ok and not f_ok:
        return None, None
    band = band_of(tfl_min) if f_ok else band_of(table_min)
    if t_ok and f_ok:
        return band, abs(table_min - tfl_min)
    return band, math.inf


def nearest_rank(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q in 0-100); safe with inf."""
    s = sorted(values)
    if not s:
        return math.nan
    return s[max(0, math.ceil(q / 100 * len(s)) - 1)]


def error_stats(errors: Sequence[float]) -> dict:
    errs = [float(e) for e in errors]
    if not errs:
        return {"n": 0, "median": math.nan, "p90": math.nan, "infinite": 0}
    return {"n": len(errs), "median": float(np.median(errs)), "p90": nearest_rank(errs, 90),
            "infinite": sum(1 for e in errs if math.isinf(e))}


def band_passes(stats: dict, lines: tuple[float, float], min_pairs: int) -> bool:
    return (stats["n"] >= min_pairs and stats["median"] <= lines[0] and stats["p90"] <= lines[1])


def accepted_ranking(groups: list[dict], regret_limit: float, within_share: float,
                     max_regret: float, min_complete_share: float) -> dict:
    """
    The ranking verdict on the accepted lines, from rated groups (each with "status" and
    "regret"): enough groups complete, at least `within_share` of those within `regret_limit`,
    and none worse than `max_regret`.
    """
    complete = [g for g in groups if g["status"] == "complete"]
    within = sum(1 for g in complete if g["regret"] <= regret_limit)
    worst = max((float(g["regret"]) for g in complete), default=math.inf)
    passed = (len(complete) > 0 and len(complete) >= math.ceil(min_complete_share * len(groups) - 1e-9)
              and within >= math.ceil(within_share * len(complete) - 1e-9) and worst <= max_regret)
    return {"complete": len(complete), "within": within, "worst_regret": worst, "passed": bool(passed)}


def journeys_pass(journeys: list[dict], tolerance: float) -> bool:
    """Every go/no-go journey within `tolerance` of TfL (station to station) and on its mode."""
    def ok(j: dict) -> bool:
        d = j.get("delta_wd08")
        return d is not None and math.isfinite(d) and abs(d) <= tolerance and bool(j.get("line_family_ok"))
    return bool(journeys) and all(ok(j) for j in journeys)


def isolated_points(table: np.ndarray, ok: np.ndarray, share: float) -> np.ndarray:
    """
    Indices of usable points that reach at most (1 - share) of the other usable points and are
    reached by at most as many: a point snapped to a street fragment cut off from the network.
    """
    idx = np.where(ok)[0]
    if len(idx) < 2:
        return np.zeros(0, dtype=int)
    sub = np.asarray(table)[np.ix_(idx, idx)]
    off = ~np.eye(len(idx), dtype=bool)
    out_none = ((sub == UNREACHABLE) & off).sum(axis=1) / (len(idx) - 1)
    in_none = ((sub == UNREACHABLE) & off).sum(axis=0) / (len(idx) - 1)
    return idx[(out_none >= share) & (in_none >= share)]


# ── ranking ─────────────────────────────────────────────────────────
def fairest_order(times: np.ndarray) -> np.ndarray:
    """
    Candidates with a time for everyone, best first: longest trip, then mean, then index
    (the meetup solver's rule with equal weights). times: (people, candidates).
    """
    t = np.asarray(times, dtype=np.float64)
    t = np.where(np.isnan(t), np.inf, t)
    mx = t.max(axis=0)
    valid = np.where(np.isfinite(mx))[0]
    mean = t[:, valid].mean(axis=0)
    return valid[np.lexsort((valid, np.round(mean, 6), np.round(mx[valid], 6)))]


def group_status(tfl_times: np.ndarray, contenders: np.ndarray, min_timed: int) -> dict:
    """
    How much TfL evidence a ranking group has. tfl_times is (people, candidates) with NaN for
    "not asked or no answer". A group is "complete" when every person has a TfL time for every
    contender, "incomplete" when some contender lacks one (TfL's best may be the one missing),
    and "not tested" when fewer than `min_timed` contenders are fully timed (a single contender
    passes by construction). Only complete groups are rated.
    """
    t = np.asarray(tfl_times, dtype=np.float64)[:, contenders]
    timed = int((~np.isnan(t)).all(axis=0).sum())
    if timed < min_timed:
        status = "not tested"
    elif timed < len(contenders):
        status = "incomplete"
    else:
        status = "complete"
    return {"status": status, "contenders": int(len(contenders)), "timed": timed}


def ranking_result(table_times: np.ndarray, tfl_times: np.ndarray,
                   regret_limit: float = 3.0) -> dict:
    """
    One group: the table's fairest candidate against TfL's times over the same candidates.
    Passes when the pick is among TfL's three best (ties at third place included) and
    TfL's longest trip at the pick is within regret_limit of TfL's longest at its best.
    NaN in tfl_times means TfL was not asked (pruned) and counts as no time.
    """
    t_order = fairest_order(table_times)
    f = np.where(np.isnan(tfl_times), np.inf, np.asarray(tfl_times, dtype=np.float64))
    f_order = fairest_order(f)
    if len(t_order) == 0 or len(f_order) == 0:
        return {"pick": None, "tfl_best": None, "tfl_top3": [], "in_top3": False,
                "regret": math.inf, "passed": False}
    pick, best = int(t_order[0]), int(f_order[0])
    f_max = f.max(axis=0)
    third = f_max[f_order[min(2, len(f_order) - 1)]]
    in_top3 = bool(np.round(f_max[pick], 6) <= np.round(third, 6))
    regret = float(f_max[pick] - f_max[best])
    return {"pick": pick, "tfl_best": best, "tfl_top3": [int(i) for i in f_order[:3]],
            "in_top3": in_top3, "regret": regret,
            "table_max": float(np.max(table_times[:, pick])),
            "tfl_max_at_pick": float(f_max[pick]), "tfl_max_at_best": float(f_max[best]),
            "passed": in_top3 and regret <= regret_limit}
