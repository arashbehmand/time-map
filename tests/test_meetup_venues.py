"""Meeting at a pub, bar, café, restaurant or park (#19): venue times against a plain
reference, the whole solve against a brute force, the station fallback and its labels, people
on Mapbox, and the venue files' checks, on tiny and random transit builds (tests/tiny_transit.py)."""
import json
import math
import random

import h3
import numpy as np
import pytest
from conftest import FixedRouter, straight_line
from test_meetup_stations import at_station, near, person, reference, request, run
from tiny_transit import (
    CENTRE,
    POND,
    A,
    B,
    C,
    D,
    make_build,
    make_random_build,
    random_venues,
    write_venues,
)

import server.meetup.stations as st
from server.errors import NoFeasiblePoint
from server.meetup.limits import SolveLimits
from server.meetup.routing import Snap
from server.transit.provider import MAX_RING, RING_MINUTES, TransitData
from server.transit.venues import KIND_TAGS, VENUE_WALK_MAX_MIN, clean_name
from transit.tables import haversine_m

SOLVE = "/api/meetup/solve"
WINDOWS = ["wd08", "wd13", "wd19", "sa13"]
M_LAT = 1 / 111_195
M_LON = M_LAT / math.cos(math.radians(CENTRE[1]))


def tiny_venues():
    """The Pond is in a hole of A's walkshed (reached through the next ring of cells); North
    Arms is 300 m north of A; C Park is reached only from table cell C; the café is beyond
    every walkshed. No bars."""
    return [("node/1", "pub", "The Pond", *POND),
            ("node/2", "pub", "North Arms", *near(A, 0, 300)),
            ("node/3", "cafe", "Far Cafe", *D),
            ("node/4", "park", "C Park", *near(C, 100))]


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    d = make_build(tmp_path_factory.mktemp("tiny-venues"), cleared=True)
    write_venues(d, tiny_venues())
    return TransitData.load(d)


@pytest.fixture(scope="module")
def rand(tmp_path_factory):
    return TransitData.load(make_random_build(tmp_path_factory.mktemp("rand-venues"), 40, seed=3,
                                              n_venues=400))


# ── venue times against a plain reference ──────────────────────────
def _ring_hits(data, lon, lat, k):
    """[(table point, walk minutes + ring minutes)] for ring k around the point's cell, every
    walkshed row read in a loop."""
    ws = data.walksheds
    cell = h3.latlng_to_cell(lat, lon, 10)
    ring = [cell] if k == 0 else list(h3.grid_ring(cell, k))
    hits = []
    for c in ring:
        ci = h3.str_to_int(c)
        for s in range(len(ws.points)):
            cells, mins = ws.station(s)
            at = np.flatnonzero(cells == np.uint64(ci))
            if len(at):
                hits.append((int(ws.points[s]), float(mins[at[0]]) + k * RING_MINUTES))
    return hits


def reference_links(data, lon, lat):
    """[(table point, walk minutes)] by hand: the venue's cell, else the nearest ring of cells
    (2 minutes a ring) holding a walk of 10 minutes or less, ring minutes included, from a
    usable point."""
    for k in range(MAX_RING + 1):
        kept = [(p, w) for p, w in _ring_hits(data, lon, lat, k) if data.ok[p] and w <= VENUE_WALK_MAX_MIN]
        if kept:
            return kept
    return []


def reference_walk(data, pin, venue_links, venue):
    """The straight walk from a pin to a venue by hand: when they share an access point (the
    pin's: the first ring any walkshed holds, plus a station within 100 m), the straight line
    times 1.3 at 4 km/h, if that is 10 minutes or less."""
    access = set()
    for k in range(MAX_RING + 1):
        hits = _ring_hits(data, *pin, k)
        if hits:
            access = {p for p, _ in hits}
            break
    nearest = min(data.stations, key=lambda s: haversine_m(pin[0], pin[1], s.lon, s.lat))
    if haversine_m(pin[0], pin[1], nearest.lon, nearest.lat) <= 100:
        access.add(nearest.idx)
    if not access & {p for p, _ in venue_links}:
        return math.inf
    walk = haversine_m(pin[0], pin[1], venue.lon, venue.lat) * 1.3 / (4000 / 60)
    return walk if walk <= VENUE_WALK_MAX_MIN else math.inf


def reference_minutes(links, row):
    return min((row[p] + w for p, w in links), default=math.inf)


def file_venues(data):
    import csv
    with open(data.directory / "venues.csv", newline="") as f:
        return list(csv.DictReader(f))


def test_venue_times_match_a_plain_reference_on_100_random_cases(tmp_path, tiny):
    rng = random.Random(11)
    builds = [tiny] + [TransitData.load(make_random_build(tmp_path, 20, seed=s, n_venues=60))
                       for s in range(3)]
    links = {}
    seen = {"dropped": 0, "kept": 0}
    for data in builds:
        in_pool = {p.pid for v in data.venues.values() for p in v.places}
        for v in file_venues(data):
            got = reference_links(data, float(v["lon"]), float(v["lat"]))
            links[(id(data), v["id"])] = got
            # A venue with no access point within the walk is left out at load, and only then.
            assert (v["id"] in in_pool) == bool(got), v
            seen["dropped" if not got else "kept"] += 1
    for case in range(100):
        data = builds[case % len(builds)]
        kind = rng.choice(sorted(data.venues))
        pool = data.pool(kind)
        s = data.stations[rng.randrange(len(data.stations))]
        pin = near((s.lon, s.lat), rng.uniform(-400, 400), rng.uniform(-400, 400))
        row = data.row(*pin, rng.choice(WINDOWS))
        got = pool.minutes(row)
        assert got[~pool.is_venue].tolist() == row[[p.idx for p in data.places]].tolist()
        want = [reference_minutes(links[(id(data), p.pid)], row)
                for p, v in zip(pool.places, pool.is_venue) if v]
        assert got[pool.is_venue].tolist() == want, case
    assert seen["dropped"] and seen["kept"]
    pond = tiny.pool("pub").places[len(tiny.places)]
    assert pond.name == "The Pond" and not len(tiny.walksheds.near(
        h3.str_to_int(h3.latlng_to_cell(POND[1], POND[0], 10)))[0]), "The Pond is in a walkshed hole"


def test_a_walk_over_ten_minutes_never_counts(rand):
    for links in rand.venues.values():
        assert (links.walk <= VENUE_WALK_MAX_MIN).all() and len(links.walk)
        assert rand.ok[links.point].all()


# ── the whole solve against a brute force ──────────────────────────
def brute_force(data, kind, people, window):
    """(fallback, listed place ids, tie flags, quickest id) by hand, or None when nothing works."""
    ordered = sorted(people, key=lambda p: p["id"])
    rows = [data.row(p["lon"], p["lat"], window) for p in ordered]
    pool = data.pool(kind)
    venues = [p for p, v in zip(pool.places, pool.is_venue) if v] if pool else []
    links = [reference_links(data, v.lon, v.lat) for v in venues]
    places = list(data.places) + venues
    times = [[row[p.idx] for p in data.places] +
             [min(reference_minutes(lk, row), reference_walk(data, (pp["lon"], pp["lat"]), lk, v))
              for v, lk in zip(venues, links)]
             for row, pp in zip(rows, ordered)]
    is_venue = [False] * len(data.places) + [True] * len(venues)
    longest = [max(t[j] for t in times) for j in range(len(places))]
    v_best = min((round(x, 6) for x, v in zip(longest, is_venue) if v), default=math.inf)
    s_best = min((round(x, 6) for x, v in zip(longest, is_venue) if not v), default=math.inf)
    if not math.isfinite(v_best) and not math.isfinite(s_best):
        return None
    if pool is None:
        fallback = "not_loaded"
    elif not venues:
        fallback = "none_in_reach"
    elif not math.isfinite(v_best):
        fallback = "none_work"
    elif math.isfinite(s_best) and v_best > s_best + 10 + 1e-9:
        fallback = "slower"
    else:
        fallback = None
    cols = [j for j, v in enumerate(is_venue) if v == (fallback is None)]
    sub = [[t[j] for j in cols] for t in times]
    fair, quick, picks, ties, q = reference(
        sub, [1.0] * len(ordered), [math.inf] * len(ordered),
        [places[j].lon for j in cols], [places[j].lat for j in cols], 5, data.tie_lines())
    return (fallback, [places[cols[j]].pid for j in picks], ties,
            places[cols[q]].pid if q is not None else None)


def test_the_venue_solver_matches_a_brute_force_on_100_random_groups(tmp_path):
    rng = random.Random(9)
    builds = [TransitData.load(make_random_build(tmp_path, 24, seed=s, n_venues=80)) for s in range(3)]
    seen = {}
    for case in range(100):
        data = builds[case % len(builds)]
        kind = rng.choice(["pub", "bar", "cafe", "restaurant", "park"])
        window = rng.choice(WINDOWS)
        people = [person(f"p{i}", at_station(data, rng.randrange(99), rng.uniform(-300, 300),
                                             rng.uniform(-300, 300)))
                  for i in range(rng.randint(2, 5))]
        want = brute_force(data, kind, people, window)
        try:
            resp = run(data, people, window=window, venue=kind)
        except NoFeasiblePoint:
            resp = None
        if want is None:
            assert resp is None, case
            continue
        assert resp is not None, case
        fallback, picks, ties, q = want
        assert resp.venue_fallback == fallback, case
        assert [r.place_id for r in resp.top] == picks, case
        assert [r.tie for r in resp.top[1:]] == ties, case
        assert (resp.quickest.place_id if resp.quickest else None) == q, case
        assert {r.kind for r in resp.top} == ({kind} if fallback is None else {"station"}), case
        seen[fallback] = seen.get(fallback, 0) + 1
    assert seen.get(None) and seen.get("slower"), seen


def test_participant_order_never_changes_a_venue_answer(rand):
    rng = random.Random(4)
    people = [person(f"p{i}", at_station(rand, rng.randrange(99), rng.uniform(-300, 300)),
                     mode="transit" if i % 2 == 0 else "walking") for i in range(5)]
    first = run(rand, people, venue="pub").model_dump()
    for _ in range(3):
        rng.shuffle(people)
        again = run(rand, people, venue="pub").model_dump()
        again["diagnostics"]["elapsed_ms"] = first["diagnostics"]["elapsed_ms"]
        assert again == first


# ── the rule for choosing, and the fallback's labels ───────────────
def test_a_venue_ten_minutes_slower_still_wins_and_a_tenth_more_does_not():
    is_venue = np.array([False, True, True])
    keep, why, v, s = st.choose(np.array([20.0, 30.0, 31.0]), is_venue)
    assert why is None and keep.tolist() == [False, True, True] and (v, s) == (30.0, 20.0)
    keep, why, _, _ = st.choose(np.array([20.0, 30.1, 31.0]), is_venue)
    assert why == "slower" and keep.tolist() == [True, False, False]
    keep, why, _, _ = st.choose(np.array([20.0, np.inf, np.inf]), is_venue)
    assert why == "none_work"
    # No station works for everyone: the venue has nothing to be compared with.
    assert st.choose(np.array([np.inf, 50.0, 60.0]), is_venue)[1] is None


def test_three_on_public_transport_meet_at_a_named_pub(tiny):
    pins = [near(A, 100), near(B, -100), near(B, 0, 200)]
    resp = run(tiny, [person(f"p{i}", pin) for i, pin in enumerate(pins)], venue="pub")
    assert resp.best.kind == "pub" and resp.best.name in ("The Pond", "North Arms")
    assert resp.venue == "pub" and resp.venue_fallback is None
    assert resp.diagnostics["path"] == "venues" and resp.diagnostics["matrix_requests"] == 0
    pool = tiny.pool("pub")
    j = [p.pid for p in pool.places].index(resp.best.place_id)
    for i, pin in enumerate(pins):
        walk = pool.walk_from(*pin, tiny.access_points(*pin))
        assert resp.best.times[f"p{i}"] == round(pool.minutes(tiny.row(*pin, "wd19"), walk)[j], 2)
    assert all(r.kind == "pub" for r in resp.top)


def test_a_venue_much_slower_than_the_station_gives_the_station_and_says_so(tiny):
    resp = run(tiny, [person("a", near(A, 100)), person("b", near(A, 0, 100))], venue="park")
    assert resp.best.kind == "station" and resp.best.name == "Alpha Road"
    assert resp.venue_fallback == "slower" and resp.diagnostics["path"] == "stations"
    gap = resp.diagnostics["best_venue_max_min"] - resp.diagnostics["best_station_max_min"]
    assert gap > 10
    assert st.note_venue_fallback("park", "slower", gap, "Alpha Road") in resp.notes
    assert "longer than Alpha Road, so this is the station." in resp.notes[-1]


def test_no_venue_within_the_walk_gives_the_station_and_says_so(tiny):
    resp = run(tiny, [person("a", near(A, 100)), person("b", near(B, -100))], venue="cafe")
    assert resp.best.kind == "station" and resp.venue_fallback == "none_in_reach"
    assert resp.notes[-1] == ("No café is within a 10-minute walk of public transport, "
                              "so this is a station.")
    assert tiny.status()["venues"]["kinds"] == {"pub": 2, "cafe": 0, "park": 1}


def test_a_kind_the_build_lacks_gives_the_station_and_says_so(tiny):
    resp = run(tiny, [person("a", near(A, 100)), person("b", near(B, -100))], venue="bar")
    assert resp.best.kind == "station" and resp.venue_fallback == "not_loaded"
    assert resp.notes[-1] == "These travel times come without bars, so this is a station."


def test_asking_for_a_station_is_the_answer_without_venues(tmp_path, rand):
    plain = TransitData.load(make_random_build(tmp_path, 40, seed=3))
    rng = random.Random(1)
    for _ in range(5):
        people = [person(f"p{i}", at_station(rand, rng.randrange(99), rng.uniform(-300, 300)))
                  for i in range(3)]
        for asked in ({}, {"venue": "station"}):
            a, b = run(rand, people, **asked).model_dump(), run(plain, people).model_dump()
            a["diagnostics"]["elapsed_ms"] = b["diagnostics"]["elapsed_ms"]
            assert a == b
            assert a["venue"] == "station" and a["venue_fallback"] is None


# ── people on Mapbox ────────────────────────────────────────────────
def test_the_road_shortlist_keeps_a_station_and_spreads_the_venues():
    lons = np.zeros(10)
    lats = 51.5 + np.array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]) * 0.001     # 111 m apart
    is_venue = np.array([False, False] + [True] * 8)
    order = np.arange(10)
    assert st.pool_shortlist(order, order, is_venue, lons, lats, 1) == [0]
    got = st.pool_shortlist(order, order, is_venue, lons, lats, 6)
    assert got[:1] == [0] and not is_venue[got[0]]
    venues = [j for j in got if is_venue[j]]
    assert venues == [2, 5, 8]          # 333 m apart, the closer ones skipped
    assert got == [0, 1, 2, 5, 8]       # what the venues can't use goes to stations


def test_one_driver_costs_one_request_with_stations_to_compare(rand):
    seen = []

    def fn(mode, o, d):
        seen.append(d)
        return straight_line(mode, o, d)

    router = FixedRouter(fn)
    people = [person("a", at_station(rand, 0)), person("b", at_station(rand, 9)),
              person("c", near(CENTRE, 0, 2500), mode="driving")]
    resp = run(rand, people, router=router, venue="cafe")
    assert router.calls == 1
    stations = {(round(p.lon, 7), round(p.lat, 7)) for p in rand.places}
    asked = [(round(lon, 7), round(lat, 7)) for lon, lat in seen]
    assert 1 <= sum(a in stations for a in asked) and len(asked) <= 24
    venues = [a for a in asked if a not in stations]
    assert venues and all(haversine_m(*venues[i], *venues[j]) >= 250
                          for i in range(len(venues)) for j in range(i + 1, len(venues)))
    for r in resp.top:
        assert (r.lon, r.lat) in asked
        assert r.times["c"] == round(straight_line("driving", near(CENTRE, 0, 2500), (r.lon, r.lat)), 2)
    assert resp.best.kind in ("cafe", "station")
    if resp.best.kind == "cafe":
        assert st.NOTE_NO_TIME_OF_DAY in resp.notes


def test_a_road_trip_ending_far_from_a_venue_drops_it(rand):
    people = [person("a", at_station(rand, 2)), person("b", near(CENTRE, 1000), mode="walking")]
    first = run(rand, people, venue="restaurant")
    assert first.best.kind == "restaurant"
    gone = (first.best.lon, first.best.lat)

    def snap(mode, d):
        return Snap(d[0], d[1] + 450 * M_LAT) if (round(d[0], 7), round(d[1], 7)) == gone else Snap(*d)

    resp = run(rand, people, router=FixedRouter(straight_line, snap_fn=snap), venue="restaurant")
    assert (resp.best.lon, resp.best.lat) != gone and resp.diagnostics["dropped_off_street"] >= 1


def test_the_smallest_shortlist_is_one_station_and_one_venue(rand):
    seen = []

    def fn(mode, o, d):
        seen.append(d)
        return straight_line(mode, o, d)

    people = [person("a", at_station(rand, 2)), person("b", near(CENTRE, 1000), mode="walking")]
    resp = run(rand, people, router=FixedRouter(fn), venue="pub",
               limits=SolveLimits(elements_left_client=2))
    stations = {(round(p.lon, 7), round(p.lat, 7)) for p in rand.places}
    assert len(seen) == 2 and sum((round(d[0], 7), round(d[1], 7)) in stations for d in seen) == 1
    assert resp.diagnostics["shortlist"] == 2


# ── the venue files ─────────────────────────────────────────────────
def test_venues_listed_in_the_manifest_load_too(tmp_path):
    d = make_build(tmp_path, cleared=True)
    write_venues(d, tiny_venues(), listed=True)
    assert TransitData.load(d).status()["venues"]["kinds"] == {"pub": 2, "cafe": 0, "park": 1}


@pytest.mark.parametrize("spoil", ["flip_byte", "other_clip", "no_json", "listed_wrong"])
def test_venue_files_that_don_t_check_out_are_left_out(tmp_path, spoil):
    d = make_build(tmp_path, cleared=True)
    write_venues(d, tiny_venues(), listed=spoil == "listed_wrong")
    if spoil in ("flip_byte", "listed_wrong"):
        raw = bytearray((d / "venues.csv").read_bytes())
        raw[-3] = ord("9") if raw[-3] != ord("9") else ord("8")
        (d / "venues.csv").write_bytes(bytes(raw))
    elif spoil == "other_clip":
        meta = json.loads((d / "venues.json").read_text())
        meta["osm_clip_sha256"] = "0" * 64
        (d / "venues.json").write_text(json.dumps(meta))
    else:
        (d / "venues.json").unlink()
    data = TransitData.load(d)
    assert data.venues == {} and data.status()["venues"]["kinds"] == {}
    resp = run(data, [person("a", near(A, 100)), person("b", near(B, -100))], venue="pub")
    assert resp.best.kind == "station" and resp.venue_fallback == "not_loaded"


def test_bad_rows_are_left_out_and_names_are_cleaned(tmp_path):
    d = make_build(tmp_path, cleared=True)
    write_venues(d, [("node/1", "pub", "  The\x07 Lamb  ", *near(A, 0, 300)),
                     ("node/2", "nightclub", "Fabric", *near(A, 0, 300)),
                     ("node/3", "pub", "", *near(A, 0, 300)),
                     ("node/4", "pub", "No Point", "x", "51.5")])
    data = TransitData.load(d)
    assert [p.name for p in data.venues["pub"].places] == ["The Lamb"]
    assert clean_name("x" * 200) == "x" * 80


def test_the_build_and_the_server_agree_on_the_kinds():
    from transit.venues import KINDS
    assert KINDS == KIND_TAGS


# ── what the reviews found ──────────────────────────────────────────
def test_someone_starting_beside_a_venue_walks_there_straight(tiny):
    pond = next(p for p in tiny.pool("pub").places if p.name == "The Pond")
    pin = near(POND, 0, 100)                       # 100 m north of The Pond, 250 m from A
    pool = tiny.pool("pub")
    j = pool.places.index(pond)
    row = tiny.row(*pin, "wd19")
    via_station = pool.minutes(row)[j]
    walk = pool.walk_from(*pin, tiny.access_points(*pin))
    straight = haversine_m(*pin, pond.lon, pond.lat) * 1.3 / (4000 / 60)
    assert pool.minutes(row, walk)[j] == pytest.approx(straight) and straight < via_station - 2
    # A venue that shares no access point with the pin is never walked to straight.
    far = pool.walk_from(*near(B, 0, 50), tiny.access_points(*near(B, 0, 50)))
    assert np.isinf(far).all()


def test_a_venue_whose_own_cell_is_only_far_reached_uses_the_next_ring(tmp_path):
    """The pub's own cell is in A's walkshed at 12 minutes only (a gate on the far side, say);
    the cells around it are 5 to 7 minutes from A. The next ring, 2 minutes on, gets it in."""
    import hashlib

    from tiny_transit import walkshed

    from transit.tables import Walksheds, cell_int
    d = make_build(tmp_path, cleared=True)
    pub = near(A, 0, 400)
    own = cell_int(*pub, 10)
    cells, mins = walkshed(A, 900)
    mins = np.where(cells == np.uint64(own), 12.0, mins)
    Walksheds.build([(0, cells, mins), (1, *walkshed(B, 900)), (2, *walkshed(C, 400))]).save(d / "walksheds.npz")
    m = json.loads((d / "manifest.json").read_text())
    data = (d / "walksheds.npz").read_bytes()
    m["outputs"]["walksheds.npz"].update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    (d / "manifest.json").write_text(json.dumps(m))
    write_venues(d, [("node/1", "pub", "Gate Arms", *pub)])
    built = TransitData.load(d)
    links = built.venues["pub"]
    assert len(links.places) == 1 and (links.walk <= 10).all()
    assert sorted(set(links.walk.tolist())) == sorted({w for _, w in reference_links(built, *pub)})
    assert min(links.walk) >= RING_MINUTES          # every link came through ring 1


def test_a_time_limit_no_venue_meets_is_not_called_out_of_reach(tiny):
    resp = run(tiny, [person("a", near(A, 100), max_minutes=3), person("b", near(A, 0, 100))], venue="park")
    assert resp.best.kind == "station" and resp.venue_fallback == "none_work"
    assert resp.notes[-1] == "No park works for all of you, so this is a station."


def test_the_gap_in_the_slower_note_is_never_shown_as_ten():
    assert st.gap_text(10.04) == "10.1" and st.gap_text(10.5) == "10.5"
    assert st.gap_text(13.0) == "13" and st.gap_text(12.96) == "13"
    assert "10.1 min longer" in st.note_venue_fallback("pub", "slower", 10.04, "Bank")


def test_a_newer_extract_of_the_same_clip_loads_over_a_stale_manifest_record(tmp_path):
    d = make_build(tmp_path, cleared=True)
    write_venues(d, tiny_venues(), listed=True)
    manifest = json.loads((d / "manifest.json").read_text())
    write_venues(d, tiny_venues()[:2])                         # the standalone command, rerun
    m = json.loads((d / "manifest.json").read_text())
    m["outputs"]["venues.csv"] = manifest["outputs"]["venues.csv"]   # the manifest's old record
    (d / "manifest.json").write_text(json.dumps(m))
    assert TransitData.load(d).status()["venues"]["kinds"] == {"pub": 2}


def test_venues_the_manifest_lists_but_are_gone_are_said(tmp_path):
    from server.transit.venues import load_venues
    d = make_build(tmp_path, cleared=True)
    write_venues(d, tiny_venues(), listed=True)
    (d / "venues.csv").unlink()
    assert load_venues(d, json.loads((d / "manifest.json").read_text())) == (None, "venues_missing")


def test_a_venue_that_breaks_the_link_build_never_takes_the_tables_with_it(tmp_path, monkeypatch):
    d = make_build(tmp_path, cleared=True)
    write_venues(d, tiny_venues())

    def boom(lon, lat):
        raise ValueError("bad cell")
        yield

    monkeypatch.setattr(TransitData, "_rings", staticmethod(boom))
    data = TransitData.load(d)
    assert data.venues == {} and data.places and data.status()["venues"]["kinds"] == {}


def test_out_of_range_points_are_left_out(tmp_path):
    d = make_build(tmp_path, cleared=True)
    write_venues(d, [("node/1", "pub", "North Pole", 0.0, 95.0), ("node/2", "pub", "Off", 200.0, 51.5),
                     ("node/3", "pub", "Lamb", *near(A, 0, 300))])
    from server.transit.venues import load_venues
    rows, why = load_venues(d, json.loads((d / "manifest.json").read_text()))
    assert why == "" and [r.name for r in rows] == ["Lamb"]


def test_no_common_place_counts_stations_only():
    """Ana's only time is to P0, 20 km from the walker; the walker reaches P1 and a pub beside
    it, which Ana has no time to. The refusal counts the one station checked, not the pub."""
    from test_meetup_stations import _FakeData

    from server.transit.provider import Place, Pool, VenueLinks

    class WithPubs(_FakeData):
        def pool(self, kind):
            pub = Place("node/1", "The Ship", 0.0, 51.3305, -1, "pub")
            links = VenueLinks([pub], np.array([1]), np.array([2.0]), np.array([0]),
                               np.array([0.0]), np.array([51.3305]))
            return Pool(kind, self.places + [pub], np.array([False, False, True]), np.arange(2), links)

        def access_points(self, lon, lat):
            return np.zeros(0, np.int64)

    data = WithPubs([[5, np.inf]], np.array([0.0, 0.0]), np.array([51.5, 51.33]))
    with pytest.raises(NoFeasiblePoint) as exc:
        run(data, [person("a", (0, 51.5)), person("b", (0, 51.32), mode="walking")], venue="pub")
    assert exc.value.extra["reason"] == "no_common_place"
    assert exc.value.extra["who"] == [{"index": 0, "reason": "reaches_few", "reach": 0, "of": 1}]


# ── through the API ─────────────────────────────────────────────────
@pytest.fixture
def venues_client(make_client, settings, tmp_path):
    d = make_build(tmp_path, cleared=True)
    write_venues(d, tiny_venues())
    return make_client(settings.with_(transit_dir=str(d), transit_block_startup=True))


def test_the_api_meets_at_a_pub_and_says_which_kinds_there_are(mapbox, venues_client):
    status = venues_client.get("/api/transit/status").json()
    assert status["venues"] == {"kinds": {"pub": 2, "cafe": 0, "park": 1}, "walk_max_min": 10.0}
    body = {"participants": [person("a", near(A, 100)), person("b", near(B, -100)),
                             person("c", near(B, 0, 200))], "venue": "pub"}
    resp = venues_client.post(SOLVE, json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["best"]["kind"] == "pub" and data["best"]["place_id"] in ("node/1", "node/2")
    assert set(data["best"]["times"]) == {"a", "b", "c"}
    assert data["venue"] == "pub" and data["venue_fallback"] is None
    assert data["diagnostics"]["path"] == "venues" and data["diagnostics"]["elapsed_ms"] < 1000
    assert "© OpenStreetMap contributors" in data["credits"]
    assert mapbox.count() == 0
    assert venues_client.post(SOLVE, json={**body, "venue": "nightclub"}).status_code == 422


def test_a_group_with_nobody_on_public_transport_ignores_the_venue(venues_client):
    resp = venues_client.post(SOLVE, json={"participants": [person("a", A, "walking"),
                                                            person("b", B, "walking")],
                                           "venue": "pub"})
    assert resp.status_code == 200 and resp.json()["best"]["kind"] != "pub"


def test_three_people_among_thousands_of_venues_in_under_a_second(tmp_path):
    data = TransitData.load(make_random_build(tmp_path, 60, seed=5, n_venues=5000))
    assert sum(len(v.places) for v in data.venues.values()) > 1000
    people = [person(f"p{i}", at_station(data, i * 7, 120, -80)) for i in range(3)]
    req = request(people, venue="pub")
    assert req.venue == "pub"
    resp = run(data, people, venue="pub")
    assert resp.best.kind == "pub" and resp.best.name and len(resp.best.times) == 3
    assert resp.diagnostics["elapsed_ms"] < 1000


def test_the_random_venues_are_sorted_as_the_build_writes_them():
    rows = random_venues(50, 2)
    kinds = ["pub", "bar", "cafe", "restaurant", "park"]
    assert rows == sorted(rows, key=lambda v: (kinds.index(v[1]), int(v[0].split("/")[1])))
