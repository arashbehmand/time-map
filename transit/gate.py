"""
The validation gate: the tables against TfL Journey Planner.

    python -m transit.gate --build <id> --estimate        # the calls it would make, no calls
    python -m transit.gate --build <id>                   # sample A: the gate
    python -m transit.gate --build <id> --sample b        # only after fixing something A found
    python -m transit.gate --build <id> --provisional     # smaller, for when there is no key

Parts (all by default, or --part cases,bands,ranking), in that order:
- cases: the hand-picked cases in cases.toml, each with its expected result.
- bands: per window, seeded pairs until each TfL band (<=20, 20-45, >45 min) has >= 100;
  every pair is scored through the runtime formula (compose_row); a time on one side only is
  an infinite error. Pass: median / p90 within 2/5, 3/7, 4/10 in all 12 cells. About 9,000
  calls (4 windows x 3 bands x ~110 pairs x 6 departures).
- ranking: 50 groups of three, split over the windows. The table ranks every station; TfL is
  asked (three departures: 10, 30 and 50 min into the window) only about the contenders:
  stations whose table longest trip is within --margin minutes (default 8) of the table's
  best. Each group reports how far down the table's list TfL's best sat; if it is often near
  the margin's edge, rerun with a wider margin.

With TFL_APP_KEY requests go out at most TFL_RATE_PER_MIN (default 100) a minute; without
one it warns and runs at TFL_RATE_PER_MIN_ANON (default 35). Every TfL reply is cached
without the key, so a stopped gate resumes for free and a later keyed run reuses every
keyless reply.

--provisional: a one-hour quick look: 30 pairs per band, three departures (0, 20, 40 min into
the window, a subset of the full six), 12 ranking groups, margin 5 min, and the hand-picked
cases, written to gate/<sample>-provisional/. Its result is labelled a quick look, never the
gate. Pairs are drawn by distance and banded by TfL, so they do not depend on the tables; a
later run of the same sample reuses the cached replies for the pairs it shares.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import tomllib
import zlib
from datetime import datetime

import numpy as np

from transit import settings
from transit.tables import (
    Walksheds,
    accepted_ranking,
    band_of,
    band_passes,
    cell_int,
    compose_pair,
    compose_row,
    decode_minutes,
    error_stats,
    fairest_order,
    group_status,
    haversine_m,
    journeys_pass,
    pair_error,
    ranking_result,
)

log = logging.getLogger("transit.gate")


class Gate:
    def __init__(self, build_id: str, sample: str = "a",
                 band_offsets: tuple[int, ...] = settings.TFL_DEPARTURE_OFFSETS_MIN,
                 pairs_per_band: int = settings.MIN_PAIRS_PER_BAND,
                 group_split: dict[str, int] | None = None,
                 case_offsets: tuple[int, ...] = settings.TFL_DEPARTURE_OFFSETS_MIN):
        from transit.build import Build, load_points

        self.b = Build(build_id)
        self.sample = sample
        self.seed = settings.SEEDS[sample]
        self.band_offsets = tuple(band_offsets)
        self.case_offsets = tuple(case_offsets)
        self.pairs_per_band = pairs_per_band
        self.group_split = group_split or dict(settings.RANKING_GROUP_SPLIT)
        self.pts = load_points(self.b)
        from transit.build import usable_points

        self.ok = usable_points(self.b)        # snapped and not cut off from the network
        self.inside = self.pts["inside_gl"].to_numpy()
        self.kind = self.pts["kind"].to_numpy()
        self.lon = self.pts["snap_lon"].to_numpy(float)
        self.lat = self.pts["snap_lat"].to_numpy(float)
        self.tables = {w: np.load(self.b.out / f"tt_{w}.npy", mmap_mode="r") for w in settings.WINDOWS}
        self.ws = Walksheds.load(self.b.out / "walksheds.npz")
        self.stations = np.where(self.ok & (self.kind == "station"))[0]
        self._tn = None

    @property
    def tn(self):
        if self._tn is None:
            from transit.build import network

            self._tn = network(self.b)
        return self._tn

    def rng(self, tag: str) -> np.random.Generator:
        return np.random.default_rng([self.seed, zlib.crc32(tag.encode())])

    # ── R5 helpers ──────────────────────────────────────────────────
    def direct_walk(self, origin, dests: np.ndarray, window: str = "wd13") -> np.ndarray:
        """R5 walking minutes from origin to table points within 3 km, capped at the walking limit."""
        from transit.r5 import walk_minutes

        out = np.full(len(self.pts), np.inf)
        d = haversine_m(origin[0], origin[1], self.lon, self.lat)
        near = np.where(self.ok & (d <= settings.DIRECT_WALK_MAX_KM * 1000))[0]
        near = near[np.isin(near, dests)] if dests is not None else near
        if len(near):
            out[near] = walk_minutes(self.tn, [origin], list(zip(self.lon[near], self.lat[near])),
                                     self.b.window_start(window),
                                     max_time_min=settings.R5["max_time_walking_min"])[0]
        return out

    def random_origin(self, rng) -> tuple[float, float] | None:
        """A res-10 cell centre inside Greater London that snaps to a street within 150 m."""
        import geopandas as gpd
        import h3
        import shapely

        from transit.points import load_region

        if not hasattr(self, "_region"):
            self._region = load_region()
        minx, miny, maxx, maxy = self._region.bounds
        for _ in range(50):
            lon, lat = rng.uniform(minx, maxx), rng.uniform(miny, maxy)
            if not shapely.contains_xy(self._region, lon, lat):
                continue
            clat, clon = h3.cell_to_latlng(h3.latlng_to_cell(lat, lon, settings.H3_RES_WALK))
            snapped = self.tn.snap_to_network(gpd.GeoSeries([shapely.Point(clon, clat)], crs="EPSG:4326"),
                                              radius=settings.SNAP_RADIUS_STATION_M)
            if not snapped.iloc[0].is_empty:
                return float(snapped.iloc[0].x), float(snapped.iloc[0].y)
        return None

    # ── bands ───────────────────────────────────────────────────────
    def draw_pair(self, rng, window: str, want: str | None):
        table = self.tables[window]
        dests = np.where(self.ok & self.inside)[0]
        for _ in range(20):
            if rng.random() < 0.5:
                o_idx = int(rng.choice(dests))
                origin = (self.lon[o_idx], self.lat[o_idx])
                kind = "table_point"
            else:
                origin = self.random_origin(rng)
                if origin is None:
                    continue
                o_idx, kind = None, "res10"
            if want is not None:
                lo, hi = settings.BAND_DRAW_KM[want]
                d_km = haversine_m(origin[0], origin[1], self.lon[dests], self.lat[dests]) / 1000
                near = dests[(d_km >= lo) & (d_km <= hi)]
                if not len(near):
                    continue
                j = int(rng.choice(near))
            else:
                j = int(rng.choice(dests))
            if o_idx == j:
                continue
            direct = self.direct_walk(origin, np.array([j]), window)
            row = compose_row(table, self.ws, cell_int(origin[0], origin[1], settings.H3_RES_WALK),
                              origin_point=o_idx, direct=direct)
            # Kept whatever the table says (no time included): the band comes from TfL, so the
            # sample does not depend on the tables it scores.
            return {"origin": [float(origin[0]), float(origin[1])], "origin_kind": kind,
                    "origin_point": o_idx, "dest": j, "composed": float(row[j]),
                    "direct_walk": float(direct[j]), "drawn_for": want}
        return None

    async def bands(self, jp) -> dict:
        from transit.tfl import TflError

        out = {}
        need = self.pairs_per_band
        for w in settings.WINDOWS:
            # One random stream per window and band: a smaller run draws exactly the first
            # pairs a larger one does, so the larger run reuses every cached reply.
            rngs = {b: self.rng(f"bands:{w}:{b}") for b in settings.BANDS}
            start = self.b.window_start(w)
            pairs, errored, draws = [], 0, 0
            target = {b: int(need * settings.BAND_DRAW_FACTOR) for b in settings.BANDS}
            have = {b: 0 for b in settings.BANDS}
            while draws < settings.MAX_DRAWS_PER_WINDOW:
                short = [b for b in settings.BANDS if have[b] < need]
                if not short:
                    break
                batch = []
                for b in short:
                    for _ in range(max(5, target[b] - have[b])):
                        p = self.draw_pair(rngs[b], w, b)
                        draws += 1
                        if p is not None:
                            batch.append(p)
                results = await asyncio.gather(*(self._tfl_pair(jp, p, start) for p in batch),
                                               return_exceptions=True)
                for p, r in zip(batch, results):
                    if isinstance(r, BaseException) and not isinstance(r, TflError):
                        raise r          # a bug, not a TfL error
                    if isinstance(r, TflError):
                        errored += 1
                        p["tfl_error"] = str(r)
                        pairs.append(p)
                        continue
                    p["tfl"] = r
                    band, err = pair_error(p["composed"], r)
                    p["band"], p["error"] = band, err
                    p["signed"] = (p["composed"] - r) if math.isfinite(p["composed"]) and math.isfinite(r) else None
                    pairs.append(p)
                    if band is not None:
                        have[band] += 1
                log.info("bands %s: %s after %d draws (%d TfL errors)", w, have, draws, errored)
                target = {b: need for b in settings.BANDS}
            cells = {}
            for band in settings.BANDS:
                rows = [p for p in pairs if p.get("band") == band]
                errs = [p["error"] for p in rows]
                st = error_stats(errs)
                signed = [p["signed"] for p in rows if p.get("signed") is not None]
                st["median_signed"] = float(np.median(signed)) if signed else math.nan
                st["table_point_origins"] = error_stats([p["error"] for p in rows if p["origin_kind"] == "table_point"])
                st["res10_origins"] = error_stats([p["error"] for p in rows if p["origin_kind"] == "res10"])
                st["reachability_mismatch"] = sum(1 for e in errs if math.isinf(e))
                st["lines"] = list(settings.BAND_LINES[band])
                st["passed"] = band_passes(st, settings.BAND_LINES[band], need)
                st["lines_accepted"] = list(settings.ACCEPTED["band_lines"][band])
                st["passed_accepted"] = band_passes(st, settings.ACCEPTED["band_lines"][band], need)
                cells[band] = st
            both_none = sum(1 for p in pairs if "tfl" in p and p.get("band") is None)
            out[w] = {"cells": cells, "pairs": pairs, "draws": draws, "tfl_errors": errored,
                      "both_no_time": both_none}
        return out

    def rescore_bands(self, prev: dict) -> dict:
        """
        A previous run's pairs, scored again with the current tables and walksheds against the
        TfL medians it stored (no TfL calls).
        """
        out = {}
        for w, d in prev.items():
            table = self.tables[w]
            pairs = []
            for p in d["pairs"]:
                if p.get("tfl") is None:
                    continue
                q = dict(p)
                origin = tuple(p["origin"])
                direct = self.direct_walk(origin, np.array([p["dest"]]))
                row = compose_row(table, self.ws, cell_int(origin[0], origin[1], settings.H3_RES_WALK),
                                  origin_point=p.get("origin_point"), direct=direct)
                q["composed_before"], q["composed"] = p["composed"], float(row[p["dest"]])
                band, err = pair_error(q["composed"], p["tfl"])
                q["band"], q["error"] = band, err
                q["signed"] = (q["composed"] - p["tfl"]) if math.isfinite(q["composed"]) and \
                    math.isfinite(p["tfl"]) else None
                pairs.append(q)
            out[w] = {"cells": self._cells(pairs), "pairs": pairs, "rescored_from_cache": True}
        return out

    def _cells(self, pairs: list[dict]) -> dict:
        cells = {}
        for band in settings.BANDS:
            rows = [p for p in pairs if p.get("band") == band]
            errs = [p["error"] for p in rows]
            st = error_stats(errs)
            signed = [p["signed"] for p in rows if p.get("signed") is not None]
            st["median_signed"] = float(np.median(signed)) if signed else math.nan
            st["reachability_mismatch"] = sum(1 for e in errs if math.isinf(e))
            st["lines"] = list(settings.BAND_LINES[band])
            st["passed"] = band_passes(st, settings.BAND_LINES[band], self.pairs_per_band)
            st["lines_accepted"] = list(settings.ACCEPTED["band_lines"][band])
            st["passed_accepted"] = band_passes(st, settings.ACCEPTED["band_lines"][band], self.pairs_per_band)
            cells[band] = st
        return cells

    async def _tfl_pair(self, jp, p, start) -> float:
        dest = (self.lon[p["dest"]], self.lat[p["dest"]])
        extra = {"nationalSearch": "true"} if not self.inside[p["dest"]] else None
        med, js = await jp.window(tuple(p["origin"]), dest, start, extra, offsets=self.band_offsets)
        p["tfl_departures"] = [j.minutes for j in js]
        return med

    # ── ranking ─────────────────────────────────────────────────────
    def groups(self) -> list[dict]:
        cells = np.where(self.ok & self.inside & (self.kind == "cell"))[0]
        lo, hi = settings.RANKING_PAIR_KM
        out = []
        for w, n in self.group_split.items():
            rng = self.rng("ranking:" + w)   # per window: fewer groups are the first ones
            made = 0
            while made < n:
                g = rng.choice(cells, size=3, replace=False)
                d = [haversine_m(self.lon[a], self.lat[a], self.lon[b], self.lat[b]) / 1000
                     for a, b in ((g[0], g[1]), (g[0], g[2]), (g[1], g[2]))]
                # A group needs a station all three reach: a transit island has none, and a
                # meeting point for it is not a ranking question.
                reachable = len(fairest_order(decode_minutes(
                    self.tables[w][np.ix_([int(x) for x in g], self.stations)]))) > 0
                if all(lo <= x <= hi for x in d) and reachable:
                    out.append({"window": w, "people": [int(x) for x in g]})
                    made += 1
        return out

    def contenders(self, window: str, people: list[int], margin: float):
        """Table times (people x stations), the table's order, and the contender columns."""
        t_times = decode_minutes(self.tables[window][np.ix_(people, self.stations)])
        order = fairest_order(t_times)
        if not len(order):
            return t_times, order, np.zeros(0, dtype=int)   # nowhere all three can reach
        t_max = t_times.max(axis=0)
        within = set(np.where(t_max <= t_max[order[0]] + margin)[0].tolist())
        # At least the table's best few, so a sparse area is not a one-station "test".
        cand = np.array(sorted(within | set(order[: settings.RANKING_MIN_CONTENDERS].tolist())), dtype=int)
        return t_times, order, cand

    async def ranking(self, jp, margin: float = settings.RANKING_MARGIN_MIN,
                      groups: list[dict] | None = None) -> dict:
        """
        Rate the ranking groups. `groups` (window and people) replaces the drawn ones: a rescore
        passes the saved groups, so it asks the cache about the same people, never new ones.
        """
        results = []
        groups = self.groups() if groups is None else groups
        for gi, g in enumerate(groups):
            r = await self._rank_group(jp, gi, g, margin)
            results.append(r)
            log.info("ranking group %d/%d (%s): %s, regret %.1f, %d contenders", gi + 1,
                     len(groups), g["window"], "pass" if r["passed"] else "FAIL",
                     r["regret"], r["contenders"])
        passed = sum(1 for r in results if r["passed"])
        near = sum(1 for r in results if r.get("tfl_best_near_edge"))
        status = {k: sum(1 for r in results if r["status"] == k) for k in ("complete", "incomplete", "not tested")}
        # 45 of 50 (a proposal), the same share for a smaller run. Only a complete group can
        # pass: an incomplete or untested one counts against the bar.
        required = math.ceil(settings.RANKING_PASS_GROUPS / settings.RANKING_GROUPS * len(groups))
        acc = settings.ACCEPTED
        accepted = accepted_ranking(results, settings.RANKING_REGRET_MIN, acc["ranking_within_share"],
                                    acc["ranking_max_regret_min"], acc["ranking_min_complete_share"])
        return {"groups": results, "passed_groups": passed, "required": required,
                "group_status": status, "groups_with_tfl_best_near_the_margin_edge": near,
                "passed": passed >= required, "accepted": accepted}

    async def _rank_group(self, jp, gi: int, g: dict, margin: float) -> dict:
        from transit.tfl import TflError

        st = self.stations
        w, people = g["window"], g["people"]
        t_times, order, cand = self.contenders(w, people, margin)
        start = self.b.window_start(w)
        origin = [(self.lon[x], self.lat[x]) for x in people]
        med = np.full((3, len(st)), np.nan)

        async def window(pi: int, k: int) -> None:
            try:
                med[pi, k] = (await jp.window(origin[pi], (self.lon[st[k]], self.lat[st[k]]), start,
                                              offsets=settings.RANKING_DEPARTURE_OFFSETS_MIN))[0]
            except TflError:
                pass

        await asyncio.gather(*(window(pi, int(k)) for pi in range(3) for k in cand))
        r = ranking_result(t_times, med, settings.RANKING_REGRET_MIN)
        r.update(group_status(med, cand, settings.RANKING_MIN_TIMED))
        r["passed"] = bool(r["passed"] and r["status"] == "complete")
        t_max = t_times.max(axis=0)
        best = float(t_max[order[0]])
        # How far down the table's list TfL's best sat: near the margin's edge means a wider
        # margin might have found a better station for TfL.
        gap = float(t_max[r["tfl_best"]] - best) if r["tfl_best"] is not None else math.nan
        ids = self.pts["id"].to_numpy()
        r.update({"group": gi, "window": w, "people": people, "margin": margin,
                  "tfl_missing_cells": int(np.isnan(med[:, cand]).sum()),
                  "contender_stations": [str(ids[st[int(k)]]) for k in cand],
                  "tfl_best_table_gap": gap,
                  "tfl_best_near_edge": bool(math.isfinite(gap) and gap > 0.75 * margin),
                  "pick_station": ids[st[r["pick"]]] if r["pick"] is not None else None,
                  "tfl_best_station": ids[st[r["tfl_best"]]] if r["tfl_best"] is not None else None})
        return r

    # ── hand-picked cases ───────────────────────────────────────────
    async def cases(self, jp) -> list[dict]:
        from transit.r5 import _gdf, one_origin, regional_task, walk_minutes
        from transit.tfl import TflError

        with open(settings.CASES_TOML, "rb") as f:
            spec = tomllib.load(f)["case"]
        out = []
        for c in spec:
            o, d = tuple(c["from"]), tuple(c["to"])
            start = self.b.window_start("wd08")
            if c.get("late_evening"):
                hh, mm = map(int, c["late_evening"].split(":"))
                start = start.replace(hour=hh, minute=mm)
            task = regional_task(self.tn, _gdf(["d"], [d[0]], [d[1]]), start)
            r5_direct = float(one_origin(self.tn, task, o[0], o[1])[0])
            walk = float(walk_minutes(self.tn, [o], [d], start,
                                      max_time_min=settings.R5["max_time_walking_min"])[0, 0])
            # The 22:30 case has no table of its own: it is composed from the 19:00 one.
            composed = compose_pair(self.tables["wd19" if c.get("late_evening") else "wd08"], self.ws,
                                    cell_int(o[0], o[1], settings.H3_RES_WALK),
                                    cell_int(d[0], d[1], settings.H3_RES_WALK), direct=walk)
            row = {"name": c["name"], "expect": c["expect"], "r5_direct": r5_direct,
                   "composed": composed, "walk": walk, "start": start.isoformat()}
            try:
                extra = {"nationalSearch": "true"} if c.get("national_search") else None
                row["tfl"], js = await jp.window(o, d, start, extra, offsets=self.case_offsets)
                row["tfl_0830_legs"] = js[3].legs if len(js) > 3 else []
            except TflError as exc:
                row["tfl_error"] = str(exc)
            row["passed"] = _case_passed(row)
            out.append(row)
        by_name = {r["name"]: r for r in out}
        for c in spec:
            if c.get("pair_with") and c["pair_with"] in by_name:
                a, b = by_name[c["name"]], by_name[c["pair_with"]]
                a["entrance_gap"] = abs(a["composed"] - b["composed"])
                a["passed"] = a["passed"] and a["entrance_gap"] <= 3
        return out


def _case_passed(row: dict) -> bool | None:
    tfl, comp = row.get("tfl"), row["composed"]
    exp = row["expect"]
    if exp == "report":
        return None
    if exp == "no_time":
        return not math.isfinite(comp)
    if exp == "walk_wins":
        # Journey Planner gives "no journey" for a pair this close: then only the walk is checked.
        walked = math.isfinite(row["walk"]) and comp == row["walk"]
        return walked and (tfl is None or not math.isfinite(tfl) or abs(comp - tfl) <= 2)
    if tfl is None:
        return False
    if exp == "within_2_min":
        return abs(comp - tfl) <= 2
    if exp == "band":
        band = band_of(tfl) or band_of(comp)
        return band is not None and abs(comp - tfl) <= settings.BAND_LINES[band][1]
    return None


def estimate(g: Gate, margin: float = settings.RANKING_MARGIN_MIN) -> dict:
    """Calls the gate would send: bands from the draw targets, ranking from the real tables."""
    from transit.build import app_key

    bands = len(settings.WINDOWS) * len(settings.BANDS) * \
        int(g.pairs_per_band * settings.BAND_DRAW_FACTOR) * len(g.band_offsets)
    rate = settings.TFL_RATE_PER_MIN if app_key() else settings.TFL_RATE_PER_MIN_ANON
    counts = [len(g.contenders(gr["window"], gr["people"], margin)[2]) for gr in g.groups()]
    ranking = sum(3 * c * len(settings.RANKING_DEPARTURE_OFFSETS_MIN) for c in counts)
    with open(settings.CASES_TOML, "rb") as f:
        cases = len(tomllib.load(f)["case"]) * len(g.case_offsets)
    total = bands + ranking + cases
    return {"stations": int(len(g.stations)), "bands_calls": bands,
            "ranking_contenders_per_group": {"min": min(counts), "median": float(np.median(counts)),
                                             "max": max(counts)},
            "ranking_margin_min": margin, "ranking_calls": ranking, "cases_calls": cases,
            "band_departures": len(g.band_offsets), "total_calls": total, "rate_per_min": rate,
            "keyless": not app_key(), "hours_at_rate": round(total / rate / 60, 1),
            "note": "bands can need a few more draws than this"}


def report_markdown(rep: dict) -> str:
    lines = [f"# TfL gate, build {rep['build']} (sample {rep['sample']}, seed {rep['seed']})", ""]
    if rep.get("label"):
        lines += [f"**{rep['label'][0].upper() + rep['label'][1:]}.**", ""]
    pf = {True: "PASS", False: "FAIL"}
    v = rep.get("verdict", {})
    acc_v = pf[bool(rep.get("provisional_passed") if rep.get("provisional") else rep.get("passed"))]
    issue_v = pf[bool(v.get("issue_lines"))]
    head = "Quick-look result" if rep.get("provisional") else "Result"
    tail = " (not the gate)" if rep.get("provisional") else ""
    al = settings.ACCEPTED
    lines += [f"**{head}: {acc_v} on the accepted lines; {issue_v} on the issue's lines**{tail}", "",
              "Accepted lines (owner decision, 2026-11-13; the tables are a rough estimate): median / "
              "p90 " + ", ".join(f"{n} {al['band_lines'][b][0]:g} / {al['band_lines'][b][1]:g}"
                                 for b, n in zip(settings.BANDS, ("<=20", "20-45", ">45"))) +
              f"; the three journeys within {al['journey_tolerance_min']:g} min; ranking: at least "
              f"{al['ranking_within_share']:.0%} of the complete groups within "
              f"{settings.RANKING_REGRET_MIN:g} min, none over {al['ranking_max_regret_min']:g}, and "
              f"{al['ranking_min_complete_share']:.0%} of groups complete.", ""]
    lines += [
              f"Error-band departures at {rep.get('band_offsets')} min into each window; ranking "
              f"margin {rep.get('margin')} min; {rep.get('rate_per_min')} requests a minute"
              f"{' without a key' if rep.get('keyless') else ''}.", ""]
    lines += ["Timetables as of: " + json.dumps(rep.get("timetable_as_of", {})),
              "Journey Planner: its live timetable for the service dates " +
              f"{rep['service_dates']['weekday']} and {rep['service_dates']['saturday']}.", ""]
    if "bands" in rep:
        names = {"le20": "<=20", "20to45": "20-45", "gt45": ">45"}
        heads = [f"{names[b]} (issue {settings.BAND_LINES[b][0]:g}/{settings.BAND_LINES[b][1]:g}, accepted "
                 f"{settings.ACCEPTED['band_lines'][b][0]:g}/{settings.ACCEPTED['band_lines'][b][1]:g})"
                 for b in settings.BANDS]
        lines += ["## Error bands (minutes: median abs / p90 abs, n)", "",
                  "| Window | " + " | ".join(heads) + " |", "|---|---|---|---|"]
        for w, r in rep["bands"].items():
            cells = []
            for b in settings.BANDS:
                c = r["cells"][b]
                ok = {True: "ok", False: "FAIL", None: "?"}
                cells.append(f"{c['median']:.1f} / {c['p90']:.1f}, n={c['n']}: issue {ok[c['passed']]}, "
                             f"accepted {ok[c.get('passed_accepted')]}")
            lines.append(f"| {w} | " + " | ".join(cells) + " |")
        lines.append("")
        if rep.get("sample_note"):
            lines += [rep["sample_note"], ""]
        bd = rep.get("breakdowns")
        if bd:
            lines += ["### By kind of pair (median abs / p90 abs, n)", "",
                      "Street to street = neither end is a station. At a station end Journey Planner, "
                      "given a coordinate, walks from the nearest street address.", "",
                      "| Pairs | <=20 | 20-45 | >45 | median signed |", "|---|---|---|---|---|"]
            for name, row in bd["kinds"].items():
                cells = [f"{row[b]['median']:.1f} / {row[b]['p90']:.1f}, n={row[b]['n']}" for b in settings.BANDS]
                lines.append(f"| {name} | " + " | ".join(cells) + f" | {row['median_signed']:+.1f} |")
            lines += ["", f"<=20 min band: {bd['le20_walk']} of {bd['le20_total']} pairs answered by the "
                      "straight walk.", ""]
    if "ranking" in rep:
        rk = rep["ranking"]
        st = rk.get("group_status", {})
        lines += [f"## Ranking: {rk['passed_groups']}/{len(rk['groups'])} groups passed "
                  f"(needs {rk['required']})", "",
                  f"Rated only when TfL has a time for every contender: {st.get('complete', 0)} complete, "
                  f"{st.get('incomplete', 0)} incomplete (a contender without a TfL time), "
                  f"{st.get('not tested', 0)} not tested (fewer than {settings.RANKING_MIN_TIMED} timed "
                  "contenders). Incomplete and untested groups count as not passed. 'TfL's best' is "
                  "among the contenders asked, not all stations.", ""]
        a = rk.get("accepted")
        if a:
            lines += [f"Accepted lines: {a['within']} of {a['complete']} complete groups within "
                      f"{settings.RANKING_REGRET_MIN:g} min of TfL's best, worst {a['worst_regret']:g} min, "
                      f"{a['complete']} of {len(rk['groups'])} groups complete: "
                      f"{'PASS' if a['passed'] else 'FAIL'}.", ""]
        lines.append(f"TfL's best station was near the margin's edge in "
                     f"{rk['groups_with_tfl_best_near_the_margin_edge']} groups (if that is common, "
                     "widen the margin).")
        lines.append("")
        for r in rk["groups"]:
            lines.append(f"- group {r['group']} ({r['window']}): {'pass' if r['passed'] else 'FAIL'} "
                         f"[{r['status']}: {r['timed']}/{r['contenders']} contenders timed], "
                         f"regret {r['regret']:.1f} min, pick {r['pick_station']}, TfL best "
                         f"{r['tfl_best_station']} ({r['tfl_best_table_gap']:.0f} min down the table's "
                         f"list, margin {r['margin']:.0f})")
        lines.append("")
    if rep.get("journeys"):
        lines += ["## Go/no-go journeys (R5 minus TfL station to station, wd08)", "",
                  f"Issue's line {settings.JOURNEY_TOLERANCE_MIN:g} min: {pf[bool(rep.get('journeys_ok'))]}; "
                  f"accepted {settings.ACCEPTED['journey_tolerance_min']:g} min: "
                  f"{pf[bool(rep.get('journeys_ok_accepted'))]}.", ""]
        for j in rep["journeys"]:
            d = j.get("delta_wd08")
            lines.append(f"- {j['name']}: {'no time' if d is None else f'{d:+.1f}'} min, mode "
                         f"{'matches' if j.get('line_family_ok') else 'does not match'}")
        lines.append("")
    if "cases" in rep:
        lines += ["## Hand-picked cases", "", "| Case | expect | composed | R5 direct | TfL | result |",
                  "|---|---|---|---|---|---|"]
        for c in rep["cases"]:
            res = {True: "pass", False: "FAIL", None: "reported"}[c["passed"]]
            tfl = c.get("tfl")
            lines.append(f"| {c['name']} | {c['expect']} | {c['composed']:.1f} | {c['r5_direct']:.1f} | "
                         f"{'' if tfl is None else f'{tfl:.1f}'} | {res} |")
    return "\n".join(lines) + "\n"


def breakdowns(g: Gate, bands: dict) -> dict:
    """Errors by kind of pair (street to street, or a station at one end) and walk-only share."""
    rows = {"street to street": [], "station at one end or both": []}
    le20 = walk = 0
    for d in bands.values():
        for p in d["pairs"]:
            if p.get("band") is None:
                continue
            station = (p.get("origin_point") is not None and g.kind[p["origin_point"]] == "station") or \
                g.kind[p["dest"]] == "station"
            rows["station at one end or both" if station else "street to street"].append(p)
            if p["band"] == "le20":
                le20 += 1
                walk += int(math.isfinite(p.get("direct_walk", math.inf)) and p["composed"] == p["direct_walk"])
    out = {}
    for name, ps in rows.items():
        out[name] = {b: error_stats([p["error"] for p in ps if p["band"] == b]) for b in settings.BANDS}
        signed = [p["signed"] for p in ps if p.get("signed") is not None]
        out[name]["median_signed"] = float(np.median(signed)) if signed else math.nan
    return {"kinds": out, "le20_walk": walk, "le20_total": le20}


async def run(g: Gate, parts: list[str], margin: float = settings.RANKING_MARGIN_MIN,
              label: str | None = None, rescore: dict | None = None,
              provisional: bool = False) -> dict:
    from transit.build import app_key
    from transit.tfl import JourneyPlanner

    key = app_key()
    rate = settings.TFL_RATE_PER_MIN if key else settings.TFL_RATE_PER_MIN_ANON
    if not key:
        log.warning("TFL_APP_KEY is not set: running without a key at %d requests a minute "
                    "(see transit/README.md, 'Accounts and keys')", rate)
    rep: dict = {"build": g.b.id, "sample": g.sample, "seed": g.seed, "label": label,
                 "provisional": provisional,
                 "band_offsets": list(g.band_offsets), "margin": margin, "keyless": not key,
                 "rate_per_min": rate, "started_at": datetime.now().isoformat(timespec="seconds"),
                 "service_dates": json.loads((g.b.out / "dates.json").read_text())}
    man = g.b.out / "manifest.json"
    if man.exists():
        rep["timetable_as_of"] = json.loads(man.read_text())["inputs"].get("timetable_as_of")
    rep["station_access_m"] = settings.R5["station_access_m"]
    if rescore is not None:
        rep["rescored_from"] = rescore["path"]
        rep["label"] = (label + "; " if label else "") + "rescored from cached TfL replies (no new calls)"
    async with JourneyPlanner(key, rate_per_min=rate, cache_only=rescore is not None) as jp:
        if "cases" in parts:
            rep["cases"] = await g.cases(jp)
        if "bands" in parts:
            rep["bands"] = g.rescore_bands(rescore["bands"]) if rescore is not None else await g.bands(jp)
            rep["breakdowns"] = breakdowns(g, rep["bands"])
            if rescore is not None and not rescore.get("sampling", "").startswith("distance"):
                rep["sample_note"] = ("These pairs were drawn before the sampling fix: a pair was kept only "
                                      "when the table's time fell in the band being filled, so a pair the "
                                      "table could not route was never asked about. New runs draw by "
                                      "distance and band by TfL.")
            elif rescore is None:
                rep["sampling"] = "distance-stratified, banded by TfL"
        if "ranking" in parts:
            saved = None
            if rescore is not None and rescore.get("ranking"):
                saved = [{"window": x["window"], "people": [int(p) for p in x["people"]]}
                         for x in rescore["ranking"]["groups"]]
            rep["ranking"] = await g.ranking(jp, margin, groups=saved)
        rep["tfl_requests_sent"], rep["tfl_cached"] = jp.sent, jp.cached
    gonogo = g.b.out / "journeys.json"
    journeys_ok = journeys_ok_accepted = False
    if gonogo.exists():
        js = json.loads(gonogo.read_text())["journeys"]
        journeys_ok = all(j.get("within_tolerance") and j.get("line_family_ok") for j in js)
        journeys_ok_accepted = journeys_pass(js, settings.ACCEPTED["journey_tolerance_min"])
        rep["journeys"] = [{"name": j["name"], "delta_wd08": j.get("delta_wd08"),
                            "line_family_ok": j.get("line_family_ok")} for j in js]
    rep["journeys_ok"], rep["journeys_ok_accepted"] = journeys_ok, journeys_ok_accepted
    cells = [c for r in rep.get("bands", {}).values() for c in r["cells"].values()]
    bands_ok = all(c["passed"] for c in cells)
    bands_ok_accepted = all(c.get("passed_accepted") for c in cells)
    cases_unexplained = [c["name"] for c in rep.get("cases", []) if c["passed"] is False]
    rep["cases_to_explain"] = cases_unexplained
    whole = set(parts) >= {"bands", "ranking"}
    issue = bool(whole and bands_ok and rep["ranking"]["passed"] and journeys_ok)
    accepted = bool(whole and bands_ok_accepted and rep["ranking"]["accepted"]["passed"]
                    and journeys_ok_accepted)
    # The verdict is on the accepted lines (owner decision, 2026-11-13); the issue's is kept.
    rep["verdict"] = {"issue_lines": issue, "accepted_lines": accepted}
    if provisional:
        rep["provisional_passed"], rep["passed"] = accepted, None   # a quick look is never the gate
    else:
        rep["passed"] = accepted
    rep["finished_at"] = datetime.now().isoformat(timespec="seconds")
    return rep


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m transit.gate", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--build", required=True)
    p.add_argument("--sample", choices=tuple(settings.SEEDS), default="a",
                   help="a: the gate; b: a fresh sample, only after fixing something sample a found")
    p.add_argument("--part", default="cases,bands,ranking")
    p.add_argument("--estimate", action="store_true")
    p.add_argument("--margin", type=float, default=None,
                   help=f"ranking contender margin in minutes (default {settings.RANKING_MARGIN_MIN})")
    p.add_argument("--departures", type=int, choices=(3, 6), default=None,
                   help="error-band departures per pair: 6 (every 10 min, the default) or 3 (every 20)")
    p.add_argument("--provisional", action="store_true",
                   help="the one-hour quick look: 30 pairs per band, 3 departures, 12 groups, margin 5")
    p.add_argument("--rescore", metavar="REPORT",
                   help="score a previous run's pairs again (gate/<run>/report.json) with the current "
                        "tables, from cached TfL replies only: no requests are sent")
    args = p.parse_args(argv)
    label = None
    if args.provisional:
        args.departures = args.departures or settings.PROVISIONAL["band_departures"]
        args.margin = args.margin if args.margin is not None else settings.PROVISIONAL["margin"]
        label = settings.PROVISIONAL["label"]
    margin = args.margin if args.margin is not None else settings.RANKING_MARGIN_MIN
    offsets = settings.BAND_DEPARTURE_OFFSETS_3 if args.departures == 3 else settings.TFL_DEPARTURE_OFFSETS_MIN
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # its INFO lines carry the key
    if args.provisional:
        pv = settings.PROVISIONAL
        g = Gate(args.build, args.sample, offsets, pv["pairs_per_band"],
                 {w: pv["groups_per_window"] for w in settings.WINDOWS},
                 settings.BAND_DEPARTURE_OFFSETS_3 if pv["case_departures"] == 3 else offsets)
    else:
        g = Gate(args.build, args.sample, offsets)
    if args.estimate:
        print(json.dumps(estimate(g, margin), indent=2))
        return 0
    rescore = None
    if args.rescore:
        rescore = json.loads((g.b.out / args.rescore).read_text())
        rescore["path"] = args.rescore
    rep = asyncio.run(run(g, args.part.split(","), margin, label, rescore, provisional=args.provisional))
    name = args.sample + ("-provisional" if args.provisional else "") + ("-rescored" if rescore else "")
    out = g.b.out / "gate" / name
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(rep, indent=2, default=str))
    (out / "report.md").write_text(report_markdown(rep))
    log.info("gate sample %s%s: %s", args.sample, " (quick look)" if args.provisional else "",
             "PASS" if (rep.get("provisional_passed") if args.provisional else rep["passed"]) else "FAIL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
