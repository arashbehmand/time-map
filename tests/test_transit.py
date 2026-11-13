"""
The transit build's pure parts: table encoding, walksheds, the runtime formula, service
dates, Journey Planner replies, gate statistics, the ranking rule, GTFS prep and checks, and
the manifest. No Java, no r5py, no network.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

from transit import manifest
from transit.feeds import (
    active_services,
    check_feeds,
    duplicate_trips,
    prep_feed,
    read_gtfs,
    sentinel_hits,
    write_gtfs,
)
from transit.tables import (
    UNREACHABLE,
    Walksheds,
    band_of,
    band_passes,
    candidate_weeks,
    compose_pair,
    compose_row,
    decode_minutes,
    disk_cells,
    encode_minutes,
    error_stats,
    fairest_order,
    nearest_rank,
    pair_error,
    pick_service_dates,
    ranking_result,
)
from transit.tfl import Journey, TflError, parse_journeys, walk_only_minutes, window_median

INF = math.inf


# ── encoding ────────────────────────────────────────────────────────
def test_encode_rounds_half_up_and_caps():
    got = encode_minutes([np.nan, 0, 12.49, 12.5, 180, 180.4, 180.5, 200, INF])
    assert got.dtype == np.uint8
    assert got.tolist() == [255, 0, 12, 13, 180, 180, 255, 255, 255]


def test_encode_rejects_negative_and_bad_cap():
    with pytest.raises(ValueError):
        encode_minutes([-1])
    with pytest.raises(ValueError):
        encode_minutes([1], max_minutes=255)


def test_decode_marks_255_as_inf():
    assert decode_minutes(np.array([0, 7, 255], dtype=np.uint8)).tolist() == [0, 7, INF]


# ── walksheds ───────────────────────────────────────────────────────
def test_walkshed_round_trip_and_lookup(tmp_path):
    ws = Walksheds.build([
        (5, [30, 10, 20], [3.0, 1.0, np.nan]),     # no path to cell 20: dropped
        (2, [20, 40], [4.4, 300.0]),               # 300 min > 254: dropped
    ])
    assert ws.points.tolist() == [5, 2]
    assert ws.indptr.tolist() == [0, 2, 3]
    cells, mins = ws.station(0)
    assert cells.tolist() == [10, 30] and mins.tolist() == [1, 3]
    path = tmp_path / "ws.npz"
    ws.save(path)
    back = Walksheds.load(path)
    pts, mins = back.near(20)
    assert pts.tolist() == [2] and mins.tolist() == [4]
    assert back.near(99)[0].size == 0


def test_disk_cells_are_within_radius():
    import h3

    from transit.tables import haversine_m

    lon, lat = -0.1276, 51.5072
    cells = disk_cells(lon, lat, 10, 500)
    assert len(cells) > 30 and np.all(np.diff(cells.astype(np.float64)) > 0)
    for c in cells[:50]:
        clat, clon = h3.cell_to_latlng(h3.int_to_str(int(c)))
        assert haversine_m(lon, lat, clon, clat) <= 500


# ── the runtime formula ─────────────────────────────────────────────
def _table():
    # 4 points: 0, 1 stations; 2, 3 cells. 6 columns would be the same idea; 4x4 keeps it readable.
    t = np.array([
        [0, 10, 30, 255],
        [12, 0, 15, 40],
        [30, 14, 0, 25],
        [255, 41, 26, 0],
    ], dtype=np.uint8)
    ws = Walksheds.build([(0, [100, 200], [5, 9]), (1, [200], [2])])
    return t, ws


def test_compose_takes_the_best_station():
    t, ws = _table()
    # From cell 200: via station 0 (9 + row 0) or station 1 (2 + row 1); best per column.
    got = compose_row(t, ws, origin_cell=200)
    assert got.tolist() == [min(9 + 0, 2 + 12), min(9 + 10, 2 + 0), min(9 + 30, 2 + 15),
                            min(INF, 2 + 40)]


def test_compose_uses_own_row_and_direct_walk():
    t, ws = _table()
    got = compose_row(t, ws, origin_cell=None, origin_point=3,
                      direct=np.array([INF, INF, 20.0, INF]))
    assert got.tolist() == [INF, 41, 20, 0]


def test_compose_with_nothing_near_is_inf():
    t, ws = _table()
    assert np.isinf(compose_row(t, ws, origin_cell=999)).all()


def test_compose_pair_walks_at_both_ends():
    t, ws = _table()
    # Origin cell 100 (station 0, 5 min); destination cell 200 (station 0 9 min, station 1 2 min).
    assert compose_pair(t, ws, 100, 200) == min(5 + 0 + 9, 5 + 10 + 2)
    assert compose_pair(t, ws, 100, 200, direct=6.0) == 6.0
    assert compose_pair(t, ws, None, None, origin_point=3, dest_point=1) == 41
    assert compose_pair(t, ws, 999, 200) == INF


# ── service dates ───────────────────────────────────────────────────
def test_candidate_weeks_skip_bank_holidays_and_stop_before_the_change():
    weeks = candidate_weeks(date(2026, 11, 12), date(2026, 11, 1), date(2027, 2, 1),
                            {date(2026, 12, 2)}, lead_days=10, before=date(2026, 12, 13))
    assert weeks == [(date(2026, 11, 25), date(2026, 11, 28)),
                     (date(2026, 12, 9), date(2026, 12, 12))]


def test_candidate_weeks_respect_the_calendar():
    assert candidate_weeks(date(2026, 11, 12), date(2026, 11, 1), date(2026, 11, 27), set(), 10,
                           None) == []


def test_pick_prefers_fewest_saturday_closures():
    weeks = [(date(2026, 11, 25), date(2026, 11, 28)), (date(2026, 12, 2), date(2026, 12, 5))]
    assert pick_service_dates(weeks) == weeks[0]
    closures = {date(2026, 11, 28): 4, date(2026, 12, 5): 1}
    assert pick_service_dates(weeks, closures.get) == weeks[1]
    with pytest.raises(ValueError):
        pick_service_dates([])


# ── Journey Planner replies ─────────────────────────────────────────
def _jp(*journeys):
    return {"journeys": [
        {"startDateTime": s, "arrivalDateTime": a, "duration": 0,
         "legs": [{"mode": {"id": "tube"}, "duration": 20, "routeOptions": [{"name": "Metropolitan"}],
                   "instruction": {"summary": "Metropolitan line to Baker Street"}}]}
        for s, a in journeys]}


def test_parse_takes_earliest_arrival_and_counts_the_wait():
    dep = datetime(2026, 11, 25, 8, 0)
    j = parse_journeys(200, _jp(("2026-11-25T08:00:00", "2026-11-25T08:41:00"),
                                ("2026-11-25T08:07:00", "2026-11-25T08:29:00")), dep)
    assert j.status == "ok" and j.minutes == 29.0          # the 7-minute wait counts
    assert j.legs[0]["routes"] == ["Metropolitan"]


def test_parse_no_journey_and_errors():
    dep = datetime(2026, 11, 25, 8, 0)
    assert parse_journeys(200, {"journeys": []}, dep).status == "no_journey"
    assert parse_journeys(404, {"message": "No journey found for your inputs."}, dep).minutes == INF
    with pytest.raises(TflError):
        parse_journeys(300, {"toLocationDisambiguation": {}}, dep)
    with pytest.raises(TflError):
        parse_journeys(500, None, dep)
    with pytest.raises(TflError):
        parse_journeys(200, {"something": "else"}, dep)
    with pytest.raises(TflError):
        parse_journeys(404, {"message": "Resource not available"}, dep)


def test_window_median_over_six_departures():
    js = [Journey("ok", m) for m in (20, 22, 25, 21, 30)] + [Journey("no_journey", INF)]
    assert window_median(js) == 23.5
    assert window_median([Journey("no_journey", INF)] * 4 + [Journey("ok", 5)] * 2) == INF


def test_walk_only_minutes():
    body = {"journeys": [{"duration": 18, "legs": [{"mode": {"id": "walking"}}]},
                         {"duration": 12, "legs": [{"mode": {"id": "bus"}}]}]}
    assert walk_only_minutes(body) == 18.0
    assert walk_only_minutes({"journeys": []}) is None


# ── gate statistics ─────────────────────────────────────────────────
def test_bands_and_pair_errors():
    assert [band_of(x) for x in (0, 20, 20.5, 45, 46, INF)] == ["le20", "le20", "20to45", "20to45",
                                                               "gt45", None]
    assert pair_error(30, 33) == ("20to45", 3)
    assert pair_error(INF, 50) == ("gt45", INF)       # TfL has a time, the table does not
    assert pair_error(12, INF) == ("le20", INF)       # banded by the table's time
    assert pair_error(INF, INF) == (None, None)       # both agree: left out


def test_error_stats_with_infinite_errors():
    errs = [1.0] * 85 + [INF] * 15
    s = error_stats(errs)
    assert s["n"] == 100 and s["median"] == 1.0 and s["p90"] == INF and s["infinite"] == 15
    assert not band_passes(s, (2, 5), 100)
    ok = error_stats([1.0] * 95 + [4.0] * 5)
    assert band_passes(ok, (2, 5), 100) and not band_passes(ok, (2, 5), 101)
    assert nearest_rank([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90) == 9


# ── ranking ─────────────────────────────────────────────────────────
def test_fairest_order_uses_max_then_mean_then_index():
    times = np.array([[10, 20, 20, INF], [30, 25, 25, 1], [10, 25, 15, 1]], dtype=float)
    # maxima 30, 25, 25, inf; means 16.7, 23.3, 20; candidate 3 has no time for one person.
    assert fairest_order(times).tolist() == [2, 1, 0]


def test_ranking_passes_and_fails():
    table = np.array([[10, 12, 30], [10, 14, 30]], dtype=float)        # pick: 0
    tfl = np.array([[12, 11, 31], [13, 12, 29]], dtype=float)          # TfL best: 1 (max 12)
    r = ranking_result(table, tfl)
    assert r["pick"] == 0 and r["tfl_best"] == 1 and r["in_top3"] and r["regret"] == 1
    assert r["passed"]
    far = np.array([[20, 11, 12, 13], [20, 11, 12, 13]], dtype=float)
    r = ranking_result(np.array([[1, 5, 5, 5], [1, 5, 5, 5]], dtype=float), far)
    assert not r["in_top3"] and not r["passed"]


def test_ranking_counts_ties_at_third_place_and_pruned_candidates():
    table = np.array([[1, 9, 9, 9, 9]], dtype=float)                   # pick: 0
    tfl = np.array([[13, 10, 12, 13, np.nan]], dtype=float)            # third place 13, a tie
    r = ranking_result(table, tfl)
    assert r["in_top3"] and r["regret"] == 3 and r["passed"]


# ── GTFS prep and checks ────────────────────────────────────────────
WED, SAT = date(2026, 11, 25), date(2026, 11, 28)


def _feed(tmp_path):
    tables = {
        "agency": pd.DataFrame([{"agency_id": "LU", "agency_name": "London Underground",
                                 "agency_url": "https://tfl.gov.uk", "agency_timezone": "Europe/London"},
                                {"agency_id": "BUS", "agency_name": "Buses",
                                 "agency_url": "https://tfl.gov.uk", "agency_timezone": "Europe/London"}]),
        "stops": pd.DataFrame([
            {"stop_id": "A", "stop_name": "Alpha Station", "stop_lat": "51.50", "stop_lon": "-0.10",
             "location_type": "", "parent_station": "PA"},
            {"stop_id": "PA", "stop_name": "Alpha Station", "stop_lat": "51.50", "stop_lon": "-0.10",
             "location_type": "1", "parent_station": ""},
            {"stop_id": "B", "stop_name": "Beta Station", "stop_lat": "51.52", "stop_lon": "-0.08",
             "location_type": "", "parent_station": ""},
            {"stop_id": "OUT", "stop_name": "Far Away", "stop_lat": "52.50", "stop_lon": "-1.00",
             "location_type": "", "parent_station": ""},
        ]),
        "routes": pd.DataFrame([
            {"route_id": "R1", "agency_id": "LU", "route_short_name": "Metropolitan",
             "route_long_name": "", "route_type": "1"},
            {"route_id": "R2", "agency_id": "BUS", "route_short_name": "73",
             "route_long_name": "", "route_type": "3"},
        ]),
        "trips": pd.DataFrame([
            {"route_id": "R1", "service_id": "WK", "trip_id": "t1", "shape_id": "s"},
            {"route_id": "R1", "service_id": "WK", "trip_id": "t2", "shape_id": "s"},   # doubles t1
            {"route_id": "R1", "service_id": "WK", "trip_id": "t3", "shape_id": "s"},
            {"route_id": "R2", "service_id": "SU", "trip_id": "t4", "shape_id": ""},    # Sundays only
        ]),
        "stop_times": pd.DataFrame([
            {"trip_id": tid, "arrival_time": a, "departure_time": a, "stop_id": s, "stop_sequence": str(q)}
            for tid, rows in {
                "t1": [("08:00:00", "A", 1), ("08:10:00", "B", 2), ("09:30:00", "OUT", 3)],
                "t2": [("08:00:00", "A", 1), ("08:11:00", "B", 2)],     # a second copy of t1
                "t3": [("24:10:00", "A", 1), ("24:20:00", "B", 2)],
                "t4": [("10:00:00", "A", 1), ("10:05:00", "B", 2)],
            }.items() for a, s, q in rows
        ]),
        "calendar": pd.DataFrame([
            {"service_id": "WK", "monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1",
             "friday": "1", "saturday": "1", "sunday": "0", "start_date": "20261101", "end_date": "20261231"},
            {"service_id": "SU", "monday": "0", "tuesday": "0", "wednesday": "0", "thursday": "0",
             "friday": "0", "saturday": "0", "sunday": "1", "start_date": "20261101", "end_date": "20261231"},
        ]),
        "calendar_dates": pd.DataFrame([{"service_id": "WK", "date": "20261128", "exception_type": "2"}]),
    }
    src = tmp_path / "src.zip"
    write_gtfs(src, tables)
    return src


def test_active_services_apply_exceptions(tmp_path):
    feed = read_gtfs(_feed(tmp_path))
    act = active_services(feed, [WED, SAT])
    assert act[WED] == {"WK"} and act[SAT] == set()


def test_prep_keeps_dates_and_area_only(tmp_path):
    out = tmp_path / "prepped.zip"
    info = prep_feed(_feed(tmp_path), out, [WED, SAT], box(-0.5, 51.3, 0.3, 51.7), "lu")
    feed = read_gtfs(out)
    assert set(feed["trips"]["trip_id"]) == {"t1", "t3"}       # t2 doubles t1; t4 runs on Sundays
    assert "OUT" not in set(feed["stop_times"]["stop_id"])                 # outside the area
    assert set(feed["stops"]["stop_id"]) == {"A", "PA", "B"}               # parent kept
    assert "shape_id" not in feed["trips"].columns
    assert feed["calendar_dates"].to_dict("records") == [
        {"service_id": "WK@20261125", "date": "20261125", "exception_type": "1"}]
    assert info["doubled_trips_removed"] == {"2026-11-25": 1, "2026-11-28": 0}
    assert feed["feed_info"]["feed_id"].tolist() == ["lu"]
    assert "24:10:00" in set(feed["stop_times"]["arrival_time"])           # past-midnight kept
    assert info["trips_per_date"] == {"2026-11-25": 2, "2026-11-28": 0}


def test_checks_find_duplicates_and_missing_lines(tmp_path):
    out = tmp_path / "prepped.zip"
    prep_feed(_feed(tmp_path), out, [WED, SAT], box(-0.5, 51.3, 0.3, 51.7), "lu")
    feeds = {"lu": read_gtfs(out)}
    assert duplicate_trips(feeds["lu"], WED).empty              # prep removed the copy
    # Put a copy back: the check finds it.
    copy = {k: v.copy() for k, v in feeds["lu"].items()}
    t1 = copy["trips"][copy["trips"]["trip_id"] == "t1"].assign(trip_id="t1b")
    copy["trips"] = pd.concat([copy["trips"], t1], ignore_index=True)
    st1 = copy["stop_times"][copy["stop_times"]["trip_id"] == "t1"].assign(trip_id="t1b")
    copy["stop_times"] = pd.concat([copy["stop_times"], st1], ignore_index=True)
    dups = duplicate_trips(copy, WED)
    assert dups.iloc[0]["duplicates"] == 1 and dups.iloc[0]["trips"] == 3
    hits = sentinel_hits(feeds, {"name": "Met", "agency": "(?i)underground",
                                 "stops": ["(?i)^alpha", "(?i)^beta"]}, [WED, SAT])
    assert hits == {"2026-11-25": 2, "2026-11-28": 0}
    report = check_feeds(feeds, [{"name": "Met", "agency": "(?i)underground", "route": "(?i)metro"}],
                         [WED, SAT], bus_min_routes=1)
    text = " ".join(report["failures"])
    assert "missing line: Met" in text            # nothing runs on the Saturday
    weekday_only = check_feeds(feeds, [{"name": "Met", "agency": "(?i)underground", "days": "weekday"}],
                               [WED, SAT], bus_min_routes=0)
    assert not any("missing line" in f for f in weekday_only["failures"])
    assert "no trips on 2026-11-28" in text
    assert "bus routes" in text                    # the Sunday-only bus was dropped


# ── manifest ────────────────────────────────────────────────────────
def test_valid_until_is_the_earlier_end():
    assert manifest.valid_until(date(2026, 11, 12), []) == date(2026, 12, 17)
    assert manifest.valid_until(date(2026, 11, 12), [date(2026, 12, 1)]) == date(2026, 12, 1)


def test_manifest_has_required_keys(tmp_path):
    m = manifest.build_manifest("2026-11-12", {"weekday": "2026-11-25", "saturday": "2026-11-28"},
                                {}, {"total": 4}, {}, {"java_version": "21"}, {}, [], ["Powered by TfL Open Data"])
    path = tmp_path / "manifest.json"
    manifest.write(path, m)
    data = json.loads(path.read_text())
    assert all(k in data for k in manifest.REQUIRED_KEYS)
    assert data["encoding"]["unreachable"] == UNREACHABLE and data["versions"]["java_version"] == "21"
    with pytest.raises(ValueError):
        manifest.write(path, {"schema": 1})


def test_pure_modules_do_not_import_r5py():
    code = ("import sys, transit.tables, transit.feeds, transit.tfl, transit.manifest, transit.points; "
            "print('r5py' in sys.modules or 'jpype' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


# ── calibration: per-ride and per-change costs ──────────────────────
def test_costed_takes_walk_or_the_cheapest_ride_count():
    from transit.calibrate import costed

    W = np.array([30.0, 10.0, INF])
    # T_k for ride limits 1, 2, 3, 8 (non-increasing in k; T_k includes walking, as R5 does)
    T = np.array([[25.0, 10.0, 60.0],
                  [20.0, 10.0, 50.0],
                  [20.0, 10.0, 45.0],
                  [20.0, 10.0, 45.0]])
    got = costed(W, T, ride_cost=1, change_cost=2, ride_limits=(1, 2, 3, 8))
    # col 0: min(30, 25+1, 20+2+2, 20+3+4, 20+8+14) = 24
    # col 1: walking wins (10) even though T says 10: costs only ever add
    # col 2: min(inf, 60+1, 50+2+2, 45+3+4, 45+8+14) = 52
    assert got.tolist() == [24.0, 10.0, 52.0]
    assert costed(W, T, 0, 0, (1, 2, 3, 8)).tolist() == [20.0, 10.0, 45.0]


def test_pure_calibrate_module_does_not_start_java():
    code = "import sys, transit.calibrate; print('r5py' in sys.modules or 'jpype' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


# ── review fixes ────────────────────────────────────────────────────
def test_next_timetable_change():
    from transit.tables import next_change

    changes = (date(2026, 12, 13), date(2027, 5, 16))
    assert next_change(changes, date(2026, 12, 12)) == date(2026, 12, 13)
    assert next_change(changes, date(2026, 12, 14)) == date(2027, 5, 16)
    assert next_change(changes, date(2027, 6, 1)) is None


def test_isolated_points_are_cut_off_both_ways():
    from transit.tables import isolated_points

    t = np.array([[0, 10, 255, 20],
                  [10, 0, 255, 30],
                  [255, 255, 0, 255],      # point 2 reaches nobody and nobody reaches it
                  [20, 30, 255, 0]], dtype=np.uint8)
    ok = np.array([True, True, True, True])
    assert isolated_points(t, ok, 0.9).tolist() == [2]
    t2 = t.copy()
    t2[2, 0] = 15                          # one way out is enough to keep it
    assert isolated_points(t2, ok, 0.9).tolist() == []


def test_group_status_rates_only_fully_timed_groups():
    from transit.tables import group_status

    nan = np.nan
    med = np.array([[10, 12, nan, 14], [11, 13, 15, 15], [9, 14, 16, 13]], dtype=float)
    assert group_status(med, np.array([0, 1, 3]), 3)["status"] == "complete"
    assert group_status(med, np.array([0, 1, 2, 3]), 3)["status"] == "incomplete"
    one = group_status(med, np.array([0]), 3)
    assert one["status"] == "not tested" and one["timed"] == 1


def test_prep_keeps_the_newer_version_of_a_doubled_trip(tmp_path):
    src = _feed(tmp_path)
    tables = read_gtfs(src)
    # t2 doubles t1 (same route, stops, first departure); give t2 a newer service.
    tables["calendar"] = pd.concat([tables["calendar"], pd.DataFrame([{
        "service_id": "WK2", "monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1",
        "friday": "1", "saturday": "1", "sunday": "0", "start_date": "20261115", "end_date": "20261231"}])],
        ignore_index=True)
    tables["trips"].loc[tables["trips"]["trip_id"] == "t2", "service_id"] = "WK2"
    newer = tmp_path / "newer.zip"
    write_gtfs(newer, tables)
    out = tmp_path / "prepped.zip"
    info = prep_feed(newer, out, [WED, SAT], box(-0.5, 51.3, 0.3, 51.7), "lu")
    kept = set(read_gtfs(out)["trips"]["trip_id"])
    assert "t2" in kept and "t1" not in kept       # the later timetable version wins
    assert info["trips_per_date"]["2026-11-25"] == 2


def test_walk_wins_case_accepts_no_journey_from_tfl():
    from transit.gate import _case_passed

    row = {"expect": "walk_wins", "composed": 7.0, "walk": 7.0, "tfl": INF}
    assert _case_passed(row)
    assert not _case_passed({**row, "composed": 9.0})
    assert not _case_passed({**row, "tfl": 12.0})


def test_parse_404_only_reads_no_journey_messages():
    dep = datetime(2026, 11, 25, 8, 0)
    assert parse_journeys(404, {"message": "No journey found for your inputs."}, dep).minutes == INF
    with pytest.raises(TflError):
        parse_journeys(404, {"message": "Journey service found nothing to do"}, dep)


def test_download_refuses_to_relabel_a_file(tmp_path):
    from transit.fetch import download, sha256_file

    f = tmp_path / "feed.zip"
    f.write_bytes(b"old release")
    meta = {"url": "https://example.test/old.zip", "release_tag": "feed-1", "sha256": sha256_file(f)}
    (tmp_path / "feed.zip.meta.json").write_text(json.dumps(meta))
    # The same source is reused without a download...
    assert download("https://example.test/old.zip", f, identity={"release_tag": "feed-1"})["release_tag"] == "feed-1"
    # ...but another release is refused, not relabelled.
    with pytest.raises(SystemExit):
        download("https://example.test/new.zip", f, identity={"release_tag": "feed-2"})


def test_build_id_must_be_a_date():
    from transit.build import Build

    with pytest.raises(SystemExit):
        Build("my-build")


def test_archive_is_not_licensed_until_the_flag_is_set():
    from transit.build import LICENCE_PENDING, licences_cleared

    assert licences_cleared() is False
    assert "NOT LICENSED FOR PUBLICATION" in LICENCE_PENDING


def test_stage_code_hash_changes_with_the_code(tmp_path, monkeypatch):
    from transit import build, settings

    for name in ("build.py", "settings.py", "r5.py"):
        (tmp_path / name).write_text("x = 1\n")
    monkeypatch.setattr(settings, "ROOT", tmp_path)
    before = build.code_hash(["r5.py"])
    (tmp_path / "r5.py").write_text("x = 2\n")
    assert build.code_hash(["r5.py"]) != before


def _stub_routing(monkeypatch):
    """table_window with R5 replaced by a stub that counts the origins it routes."""
    from transit import r5

    calls = []
    monkeypatch.setattr(r5, "_gdf", lambda *a: None)
    monkeypatch.setattr(r5, "regional_task", lambda *a, **k: None)
    monkeypatch.setattr(r5, "peak_rss_bytes", lambda: 0)

    def one(tn, task, lon, lat):
        calls.append(lon)
        return np.array([5.0, 7.0, 9.0])

    monkeypatch.setattr(r5, "one_origin", one)
    return r5, calls


def test_table_restarts_when_its_inputs_change_and_resumes_when_not(tmp_path, monkeypatch):
    r5, calls = _stub_routing(monkeypatch)
    pts = pd.DataFrame({"idx": [0, 1, 2], "snap_lon": [0.0, 1.0, 2.0], "snap_lat": [0.0, 0.0, 0.0]})
    ok = np.array([True, True, True])
    out, dep = tmp_path / "tt_wd08.npy", datetime(2026, 12, 9, 8)
    run = lambda fp, **k: r5.table_window(None, pts, ok, dep, out, threads=1, fingerprint=fp, **k)  # noqa: E731
    assert run({"settings": {"a": 1}})["complete"] and len(calls) == 3
    run({"settings": {"a": 1}})
    assert len(calls) == 3                      # same inputs: resumed, nothing left to do
    run({"settings": {"a": 2}})
    assert len(calls) == 6                      # other settings: every row again
    run({"settings": {"a": 2}}, force=True)
    assert len(calls) == 9                      # --force: every row again
    assert json.loads(r5.fingerprint_path(out).read_text())["inputs"] == {"settings": {"a": 2}}


def test_pilot_table_is_incomplete_and_manifest_refuses_it(tmp_path, monkeypatch):
    from transit import build, settings

    r5, _ = _stub_routing(monkeypatch)
    for attr in ("RAW", "WORK", "OUT"):
        monkeypatch.setattr(settings, attr, tmp_path / attr.lower())
    b = build.Build("2026-11-12")
    (b.out / "dates.json").write_text(json.dumps({"weekday": "2026-12-09", "saturday": "2026-12-12"}))
    pts = pd.DataFrame({"idx": [0, 1, 2], "snap_lon": [0.0, 1.0, 2.0], "snap_lat": [0.0, 0.0, 0.0]})
    ok = np.array([True, True, True])
    for w in settings.WINDOWS:
        info = r5.table_window(None, pts, ok, datetime(2026, 12, 9, 8), b.out / f"tt_{w}.npy", threads=1,
                               fingerprint={"settings": {}}, limit_origins=1 if w == "sa13" else None)
        assert info["complete"] is (w != "sa13")
    with pytest.raises(SystemExit, match="tt_sa13.npy is missing or incomplete"):
        build.stage_manifest(b, None)


def test_accepted_ranking_rates_complete_groups_only():
    from transit.tables import accepted_ranking

    g = [{"status": "complete", "regret": r} for r in (0, 0, 0, 0, 0, 4, 7, 8, 10)] + \
        [{"status": "incomplete", "regret": 3}, {"status": "not tested", "regret": 0},
         {"status": "not tested", "regret": 4}]
    a = accepted_ranking(g, 3.0, 0.5, 11.0, 2 / 3)
    assert (a["complete"], a["within"], a["worst_regret"], a["passed"]) == (9, 5, 10.0, True)
    assert not accepted_ranking(g, 3.0, 0.5, 9.0, 2 / 3)["passed"]          # a group 10 min off
    assert not accepted_ranking(g, 3.0, 0.6, 11.0, 2 / 3)["passed"]         # 5 of 9 is under 60%
    few = g[:6] + [{"status": "not tested", "regret": 0}] * 6               # 6 of 12 complete
    assert not accepted_ranking(few, 3.0, 0.5, 11.0, 2 / 3)["passed"]
    assert not accepted_ranking([], 3.0, 0.5, 11.0, 2 / 3)["passed"]


def test_journeys_pass_needs_every_journey_within_and_on_its_mode():
    from transit.tables import journeys_pass

    js = [{"delta_wd08": 6.5, "line_family_ok": True}, {"delta_wd08": 4.0, "line_family_ok": True},
          {"delta_wd08": 2.0, "line_family_ok": True}]
    assert journeys_pass(js, 8.0) and not journeys_pass(js, 3.0)
    assert not journeys_pass(js[:2] + [{"delta_wd08": 2.0, "line_family_ok": False}], 8.0)
    assert not journeys_pass(js[:2] + [{"delta_wd08": math.inf, "line_family_ok": True}], 8.0)
    assert not journeys_pass([], 8.0)
