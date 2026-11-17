"""Meeting at a station (server/meetup/stations.py): the v2 scorer against a plain reference,
the shortlist for people on Mapbox, and the API around it, on tiny and random transit builds
(tests/tiny_transit.py)."""
import asyncio
import math
import random
from datetime import date

import httpx
import numpy as np
import pytest
from conftest import FixedRouter, matrix_reply, straight_line
from tiny_transit import CENTRE, A, B, make_build, make_random_build

import server.meetup.stations as st
from server.errors import NoFeasiblePoint
from server.meetup.limits import GroupTooSpread, SolveLimits, plan_stations
from server.meetup.models import SolveRequest
from server.meetup.routing import Snap, SolveAllowance, solve_allowance
from server.settings import Settings
from server.transit.provider import TABLE_ERROR_MIN, TransitData
from transit.tables import haversine_m

SOLVE = "/api/meetup/solve"
M_LAT = 1 / 111_195
M_LON = M_LAT / math.cos(math.radians(CENTRE[1]))


def near(point, dx_m=0.0, dy_m=0.0):
    return point[0] + dx_m * M_LON, point[1] + dy_m * M_LAT


def at_station(data, i, dx_m=0.0, dy_m=0.0):
    """A pin near the build's i-th station (so it is inside a walkshed)."""
    s = data.stations[i % len(data.stations)]
    return near((s.lon, s.lat), dx_m, dy_m)


def person(pid, point, mode="transit", **kw):
    return {"id": pid, "lon": point[0], "lat": point[1], "mode": mode, **kw}


def request(people, **kw):
    return SolveRequest.model_validate({"participants": people, **kw})


def run(data, people, router=None, window="wd19", settings=None, limits=None, **kw):
    req = request(people, **kw)
    group = st.prepare(req, data)
    plan = plan_stations(req, settings or Settings(), limits, len(group.cand))
    allowance = SolveAllowance.for_plan(plan, settings or Settings()) if group.road else None
    token = solve_allowance.set(allowance)
    try:
        return asyncio.run(st.solve_stations(req, data, router or FixedRouter(straight_line), plan,
                                             window, group))
    finally:
        solve_allowance.reset(token)


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    return TransitData.load(make_build(tmp_path_factory.mktemp("tiny"), cleared=True))


@pytest.fixture(scope="module")
def rand(tmp_path_factory):
    return TransitData.load(make_random_build(tmp_path_factory.mktemp("rand"), 40, seed=3))


# ── the pure scorer against a plain reference ───────────────────────
def reference(times, weights, limits, lons, lats, k, lines):
    """Fairest, quickest and the listed places by hand: loops, no numpy ranking. Returns
    (fairest order, quickest order, listed places, their tie flags, the quickest place)."""
    n_p, n_c = len(times), len(times[0])
    longest, mean = {}, {}
    for j in range(n_c):
        col = [times[i][j] for i in range(n_p)]
        if all(math.isfinite(t) and t <= limits[i] for i, t in enumerate(col)):
            longest[j] = max(col)
            mean[j] = sum(w * t for w, t in zip(weights, col)) / sum(weights)
    fair = sorted(longest, key=lambda j: (round(longest[j], 6), round(mean[j], 6), j))
    quick = sorted(longest, key=lambda j: (round(mean[j], 6), round(longest[j], 6), j))
    if not fair:
        return [], [], [], [], None

    def apart(i, j):
        return haversine_m(lons[i], lats[i], lons[j], lats[j]) >= 500

    q = quick[0]
    if q == fair[0] or not apart(q, fair[0]) or mean[q] > mean[fair[0]] - 1.0:
        q = None
    best = round(longest[fair[0]], 6)
    slack = max(3.0, 0.1 * best)
    picks = [fair[0]]
    others = [q] if q is not None else []
    for j in fair[1:]:
        if len(picks) + len(others) >= k or round(longest[j], 6) - best > slack + 1e-9:
            break
        if j not in others and all(apart(j, i) for i in picks + others):
            picks.append(j)
    band = "le20" if best <= 20 else "20to45" if best <= 45 else "gt45"
    ties = [round(longest[j], 6) - best <= lines[band] + 1e-9 for j in picks[1:]]
    return fair, quick, picks, ties, q


def test_the_scorer_matches_a_plain_reference_on_100_random_cases():
    rng = random.Random(7)
    seen = {True: 0, False: 0}
    for case in range(100):
        # The accepted lines mark almost every alternative a tie; tighter ones test the rule.
        lines = TABLE_ERROR_MIN if case % 2 else {"le20": 1.0, "20to45": 2.0, "gt45": 2.5}
        n_p, n_c = rng.randint(2, 8), rng.randint(1, 30)
        times = np.array([[rng.choice([math.inf] + [rng.uniform(0, 90)] * 12) for _ in range(n_c)]
                          for _ in range(n_p)])
        if case % 3 == 0:   # whole minutes, as the tables give: plenty of exact ties
            times = np.where(np.isfinite(times), np.round(times), times)
        weights = np.array([rng.choice([1.0, 1.0, 2.0, 0.5]) for _ in range(n_p)])
        limits = np.array([rng.choice([math.inf] * 4 + [rng.uniform(20, 80)]) for _ in range(n_p)])
        lons = np.array([CENTRE[0] + rng.uniform(-0.03, 0.03) for _ in range(n_c)])
        lats = np.array([CENTRE[1] + rng.uniform(-0.02, 0.02) for _ in range(n_c)])
        k = rng.randint(1, 5)
        fair, quick, picks, ties, q = reference(times.tolist(), weights, limits, lons, lats, k, lines)
        longest, mean = st.scores(times, weights, limits)
        f_order, q_order = st.rank(longest, mean, "fairest"), st.rank(longest, mean, "quickest")
        assert f_order.tolist() == fair, case
        assert q_order.tolist() == quick, case
        got_q = st.pick_quickest(f_order, q_order, mean, lons, lats)
        assert got_q == q, case
        got = st.pick_places(f_order, longest, lons, lats, k, keep=(got_q,) if got_q is not None else ())
        assert got == picks, case
        if got:
            assert st.tie_flags(longest, got, lines) == ties, case
            # Every place listed, the quickest included, is listed once and 500 m apart.
            shown = got + ([got_q] if got_q is not None else [])
            assert len(set(shown)) == len(shown) and len(shown) <= max(k, 1 + (got_q is not None))
            assert all(haversine_m(lons[i], lats[i], lons[j], lats[j]) >= 500
                       for a, i in enumerate(shown) for j in shown[a + 1:]), case
            seen["quickest"] = seen.get("quickest", 0) + (got_q is not None)
        for t in ties:
            seen[t] += 1
    assert seen[True] and seen[False] and seen["quickest"], seen


def test_the_tie_lines_are_the_accepted_medians():
    from transit.settings import ACCEPTED
    assert TABLE_ERROR_MIN == {b: v[0] for b, v in ACCEPTED["band_lines"].items()}


def test_a_build_s_own_accepted_lines_win(rand):
    rand.manifest["settings"] = {"accepted_lines": {"band_lines": {"le20": [1.5, 4.0], "gt45": ["x"]}}}
    try:
        assert rand.tie_lines() == {**TABLE_ERROR_MIN, "le20": 1.5}
    finally:
        del rand.manifest["settings"]


def test_interleave_takes_turns_without_repeats():
    assert st.interleave(np.array([3, 1, 2]), np.array([1, 4]), 10) == [3, 1, 4, 2]
    assert st.interleave(np.array([3, 1, 2]), np.array([1, 4]), 2) == [3, 1]


# ── places and their times ──────────────────────────────────────────
def test_every_station_is_its_own_place_at_its_own_point(rand):
    # Same-name platforms stay separate: a time is always to the point that is shown.
    names = [p.name for p in rand.places]
    assert len(names) > len(set(names)), "the random build has same-name twins"
    assert [p.idx for p in rand.places] == [s.idx for s in rand.stations]
    row = rand.row(*CENTRE, "wd19")
    for p, t in zip(rand.places, rand.place_minutes(row)):
        assert t == row[p.idx]
        assert (p.lon, p.lat) == (rand.snap_lon[p.idx], rand.snap_lat[p.idx])


def test_two_platforms_of_one_name_never_borrow_each_other_s_times():
    # A and B share a name, 400 m apart: one person is quicker to A, the other to B. Each
    # answer's times are to its own station only, never the best of both (10 and 10).
    lons, lats = np.array([0.0, 0.0057]), np.array([51.5, 51.5])
    data = _FakeData([[10, 16], [16, 10]], lons, lats, names=["Bank", "Bank"])
    resp = run(data, [person("a", (0, 51.5)), person("b", (0, 51.5))])
    assert resp.best.max_time_min == 16 and sorted(resp.best.times.values()) == [10, 16]
    assert [r.name for r in resp.top] == ["Bank"]          # the other platform is under 500 m


def test_all_transit_times_are_the_tables_own(rand):
    pins = [at_station(rand, 0, 150), at_station(rand, 7, -200, 100), at_station(rand, 13, 0, 250)]
    people = [person(f"p{i}", pin) for i, pin in enumerate(pins)]
    resp = run(rand, people, window="wd08")
    for r in [*resp.top, *([resp.quickest] if resp.quickest else [])]:
        place = next(p for p in rand.places if p.pid == r.place_id)
        assert (r.lon, r.lat) == (round(place.lon, 7), round(place.lat, 7))
        for i, pin in enumerate(pins):
            assert r.times[f"p{i}"] == round(rand.row(*pin, "wd08")[place.idx], 2)
    assert resp.window == "wd08" and resp.diagnostics["matrix_requests"] == 0


def test_the_v2_solver_matches_a_brute_force_on_100_random_groups(tmp_path):
    rng = random.Random(5)
    builds = [TransitData.load(make_random_build(tmp_path, 30, seed=s)) for s in range(4)]
    for case in range(100):
        data = builds[case % len(builds)]
        window = rng.choice(["wd08", "wd13", "wd19", "sa13"])
        people = [person(f"p{i}", at_station(data, rng.randrange(99), rng.uniform(-300, 300),
                                             rng.uniform(-300, 300)),
                         weight=rng.choice([1.0, 1.0, 2.0]))
                  for i in range(rng.randint(2, 6))]
        try:
            resp = run(data, people, window=window)
        except NoFeasiblePoint:
            resp = None
        times = [[data.row(p["lon"], p["lat"], window)[place.idx] for place in data.places]
                 for p in sorted(people, key=lambda p: p["id"])]
        weights = [p["weight"] for p in sorted(people, key=lambda p: p["id"])]
        lons = [p.lon for p in data.places]
        lats = [p.lat for p in data.places]
        fair, quick, picks, ties, q = reference(times, weights, [math.inf] * len(people), lons, lats,
                                                5, data.tie_lines())
        if not fair:
            assert resp is None, case
            continue
        assert resp is not None, case
        assert [r.place_id for r in resp.top] == [data.places[j].pid for j in picks], case
        assert [r.tie for r in resp.top[1:]] == ties, case
        assert (resp.quickest.place_id if resp.quickest else None) == \
            (data.places[q].pid if q is not None else None), case


def test_participant_order_never_changes_the_answer(rand):
    rng = random.Random(2)
    pins = [at_station(rand, rng.randrange(99), rng.uniform(-300, 300)) for _ in range(5)]
    people = [person(f"p{i}", pin, mode="transit" if i % 2 == 0 else "walking")
              for i, pin in enumerate(pins)]
    first = run(rand, people).model_dump()
    for _ in range(4):
        rng.shuffle(people)
        again = run(rand, people).model_dump()
        again["diagnostics"]["elapsed_ms"] = first["diagnostics"]["elapsed_ms"]
        assert again == first


def test_quickest_is_shown_only_when_it_is_another_place():
    lons, lats = np.array([0.0, 0.0, 0.0]), np.array([51.5, 51.51, 51.5001])
    data = _FakeData([[10, 4, 10.5], [10, 30, 3]], lons, lats)   # a, b to 3 places
    resp = run(data, [person("a", (0, 51.5)), person("b", (0, 51.5))])
    assert resp.best.name == "P0" and resp.best.max_time_min == 10
    # P2's mean is 6.75 against P0's 10, but it is 11 m from P0: not another place.
    assert resp.quickest is None
    data = _FakeData([[10, 4, 10.5], [10, 13, 30]], lons, lats)
    resp = run(data, [person("a", (0, 51.5)), person("b", (0, 51.5))])
    assert resp.quickest is not None and resp.quickest.name == "P1"   # mean 8.5 against 10
    assert resp.top[0].name == "P0"
    # A saving under a minute is not worth a second answer.
    data = _FakeData([[10, 4, 10.5], [10, 15.5, 30]], lons, lats)
    assert run(data, [person("a", (0, 51.5)), person("b", (0, 51.5))]).quickest is None


def test_the_quickest_place_is_listed_once_and_counts_toward_five():
    # P1 is the quickest and also within the alternatives' 3 minutes of P0: it is the
    # quickest answer only, not Option 2 as well.
    lons, lats = np.array([0.0, 0.0]), np.array([51.5, 51.51])
    resp = run(_FakeData([[10, 4], [10, 13]], lons, lats), [person("a", (0, 51.5)), person("b", (0, 51.5))])
    assert resp.quickest.name == "P1" and [r.name for r in resp.top] == ["P0"]
    # Seven places 1 km apart, all close: the best, the quickest and three more, five in all,
    # every one 500 m from the others.
    lats = 51.5 + np.arange(7) * 0.009
    a = [10, 2, 10.5, 10.6, 10.7, 10.8, 10.9]
    b = [10, 12.9, 10.5, 10.6, 10.7, 10.8, 10.9]
    resp = run(_FakeData([a, b], np.zeros(7), lats), [person("a", (0, 51.5)), person("b", (0, 51.5))])
    shown = [r.name for r in resp.top] + [resp.quickest.name]
    assert shown == ["P0", "P2", "P3", "P4", "P1"] and len(set(shown)) == 5


class _FakeData:
    """Just enough of TransitData for solve_stations: fixed minutes per person (by id order)."""

    def __init__(self, minutes, lons, lats, names=None):
        from server.transit.provider import Place
        self.minutes = [np.asarray(m, dtype=float) for m in minutes]
        self.places = [Place(f"P{i}", names[i] if names else f"P{i}", float(lo), float(la), i)
                       for i, (lo, la) in enumerate(zip(lons, lats))]
        self.windows = {w: {"day": "weekday", "start": "19:00", "minutes": 60}
                        for w in ("wd08", "wd13", "wd19", "sa13")}
        self.manifest, self.build, self._n = {}, "fake", 0
        self.valid_until = date(2026, 12, 12)
        self.service_dates = {"weekday": "2026-12-09"}

    def window_date(self, window):
        return "2026-12-09"

    def covered(self, lon, lat):
        return True

    reachable_from = covered

    def row(self, lon, lat, window):
        m = self.minutes[self._n % len(self.minutes)]
        self._n += 1
        return m

    def place_minutes(self, row):
        return row

    def tie_lines(self):
        return dict(TABLE_ERROR_MIN)


# ── people on Mapbox: the shortlist ─────────────────────────────────
def test_a_driver_is_asked_about_a_shortlist_only(rand):
    seen = []

    def fn(mode, o, d):
        seen.append(d)
        return straight_line(mode, o, d)

    router = FixedRouter(fn)
    people = [person("a", at_station(rand, 0)), person("b", at_station(rand, 9)),
              person("c", near(CENTRE, 0, 3000), mode="driving")]
    resp = run(rand, people, router=router)
    assert router.calls == 1
    asked = {(round(lon, 7), round(lat, 7)) for lon, lat in seen}
    assert len(asked) <= 24
    assert all((r.lon, r.lat) in asked for r in resp.top)
    # The driver's times are the router's, not the estimate.
    for r in resp.top:
        assert r.times["c"] == round(straight_line("driving", near(CENTRE, 0, 3000), (r.lon, r.lat)), 2)
    n_places = resp.diagnostics["places"]
    assert resp.diagnostics["shortlist"] == min(24, n_places)
    if n_places > 24:
        assert st.note_shortlist(24) in resp.notes
    assert st.NOTE_NO_TIME_OF_DAY in resp.notes
    assert isinstance(resp.diagnostics["lower_bound_proven"], bool)


def test_driving_traffic_keeps_to_ten_coordinates(rand):
    req = request([person("a", CENTRE), person("b", CENTRE, mode="driving-traffic")])
    assert plan_stations(req, Settings(), None, 40).coarse_max == 9


def test_the_shortlist_shrinks_to_fit_what_is_left_and_refuses_below_one():
    from server.guard.guard import ClientShareExhausted
    req = request([person("a", CENTRE), person("b", CENTRE, mode="driving")])
    assert plan_stations(req, Settings(), SolveLimits(elements_left_client=10), 40).coarse_max == 10
    with pytest.raises(ClientShareExhausted):
        plan_stations(req, Settings(), SolveLimits(elements_left_client=1), 40)


def test_two_requests_per_road_person_at_most():
    req = request([person("a", CENTRE), person("b", CENTRE, mode="walking"),
                   person("c", CENTRE, mode="walking"), person("d", CENTRE, mode="driving-traffic")])
    plan = plan_stations(req, Settings(), None, 40)
    allowance = SolveAllowance.for_plan(plan, Settings())
    # Each road person is asked about alone: one planned request each, one spare each.
    assert plan.coarse_max == 9                       # driving-traffic: 1 + 9 coordinates
    assert plan.requests == {"matrix": 2, "matrix:driving-traffic": 1}
    assert allowance.requests == {"matrix": 4, "matrix:driving-traffic": 2}
    # And each person's own share is two requests, whatever the others spend.
    mine = allowance.for_person("matrix", 9, 2, 3)
    assert mine.requests == {"matrix": 2} and mine.plan_requests == {"matrix": 1}
    assert mine.gate is allowance.gate
    req = request([person("a", CENTRE), person("b", CENTRE, mode="walking"),
                   person("c", CENTRE, mode="walking")])
    plan = plan_stations(req, Settings(), None, 40)
    assert plan.coarse_max == 24 and plan.requests == {"matrix": 2}
    assert SolveAllowance.for_plan(plan, Settings()).requests == {"matrix": 4}
    # All on public transport: nothing to send.
    plan = plan_stations(request([person("a", CENTRE), person("b", CENTRE)]), Settings(), None, 40)
    assert plan.elements == 0 and plan.requests == {}


def test_a_road_trip_ending_near_the_station_counts_the_walk_and_too_far_drops_it(rand):
    people = [person("a", at_station(rand, 2)), person("b", near(CENTRE, 1000), mode="walking")]
    resp0 = run(rand, people)
    best = (resp0.best.lon, resp0.best.lat)

    def snapped(metres):
        def snap(mode, d):
            if (round(d[0], 7), round(d[1], 7)) == best:
                return Snap(d[0], d[1] + metres * M_LAT)
            return Snap(*d)
        return snap

    # 300 m away: the station stays, with the walk from there in the walker's time.
    resp = run(rand, people, router=FixedRouter(straight_line, snap_fn=snapped(300)))
    kept = [r for r in [*resp.top, *([resp.quickest] if resp.quickest else [])]
            if (r.lon, r.lat) == best]
    if kept:
        want = straight_line("walking", near(CENTRE, 1000), best) + 300 / (4000 / 60)
        assert kept[0].times["b"] == pytest.approx(want, abs=0.02)
        assert st.NOTE_STATION_WALK in resp.notes
    # 450 m away: too far from any road for the walker, so it is left out.
    resp = run(rand, people, router=FixedRouter(straight_line, snap_fn=snapped(450)))
    assert (resp.best.lon, resp.best.lat) != best
    assert resp.diagnostics["dropped_off_street"] >= 1


def test_a_place_left_unchecked_is_said(rand):
    people = [person("a", at_station(rand, 2)), person("b", near(CENTRE, 1000), mode="walking")]
    first = run(rand, people)
    gone = (first.best.lon, first.best.lat)
    resp = run(rand, people, router=FixedRouter(
        straight_line, unresolved_fn=lambda m, d: (round(d[0], 7), round(d[1], 7)) == gone))
    assert (resp.best.lon, resp.best.lat) != gone
    assert st.NOTE_UNCHECKED in resp.notes and resp.diagnostics["dropped_no_route"] == 1


# ── refusals ────────────────────────────────────────────────────────
def test_whoever_is_too_far_from_every_station_is_the_one_named(rand):
    with pytest.raises(GroupTooSpread) as exc:
        run(rand, [person("a", at_station(rand, 2)), person("b", near(CENTRE, 0, 40_000), mode="walking")])
    assert "15 km limit for walking" in exc.value.message
    assert exc.value.extra["who"] == [{"index": 1, "reason": "too_far"}]
    # A walker next to the stations and a driver 300 km away: the driver is the one named.
    with pytest.raises(GroupTooSpread) as exc:
        run(rand, [person("a", at_station(rand, 2)), person("b", at_station(rand, 3), mode="walking"),
                   person("c", (-2.24, 53.48), mode="driving")])
    assert "150 km limit for car" in exc.value.message
    assert exc.value.extra["who"] == [{"index": 2, "reason": "too_far"}]


def test_no_table_time_within_three_hours_is_not_called_no_route(rand):
    router = FixedRouter(straight_line)
    data = _FakeData([[np.inf, np.inf], [5, 6]], np.array([0.0, 0.0]), np.array([51.5, 51.51]))
    with pytest.raises(NoFeasiblePoint) as exc:
        run(data, [person("a", (0, 51.5)), person("b", (0, 51.5)),
                   person("c", (0, 51.5), mode="walking")], router=router)
    assert exc.value.extra == {"reason": "no_transit_time",
                               "who": [{"index": 0, "reason": "no_transit_time"}]}
    assert "within 3 hours" in exc.value.message
    assert router.calls == 0           # named before anyone is asked about by road


def test_a_road_person_s_time_limit_shapes_the_shortlist():
    # One walker (limit 10 min) and one person on public transport. The two far stations
    # look best on the tables, but even at 6 km/h the walker can't reach them in 10 minutes,
    # so the one near station is what gets asked about.
    lats = np.array([51.5 + 0.9 / 111.195, 51.5 + 1.2 / 111.195, 51.5 - 1.2 / 111.195])
    data = _FakeData([[30, 1, 1]], np.zeros(3), lats)
    router = FixedRouter(lambda mode, o, d: haversine_m(*o, *d) / 1000 / 6.0 * 60)
    resp = run(data, [person("a", (0, 51.5)), person("b", (0, 51.5), mode="walking", max_minutes=10)],
               router=router, limits=SolveLimits(elements_left_client=2))
    assert resp.best.name == "P0" and resp.best.times["b"] <= 10


def test_lower_bound_proven_looks_at_the_stations_left_off_the_shortlist():
    # Five stations in a line north of a walker; only the nearest two are asked about.
    lats = 51.5 + np.arange(1, 6) * 0.004
    data = _FakeData([[5] * 5], np.zeros(5), lats)
    people = [person("a", (0, 51.5)), person("b", (0, 51.5), mode="walking")]
    tight = SolveLimits(elements_left_client=2)
    # Mapbox agrees with the straight line: nothing further can beat the nearest station.
    resp = run(data, people, router=FixedRouter(straight_line), limits=tight)
    assert resp.diagnostics["shortlist"] == 2 and resp.diagnostics["lower_bound_proven"] is True
    # Mapbox says the near ones are slow: a station left off the shortlist might be quicker.
    slow = FixedRouter(lambda m, o, d: straight_line(m, o, d) * 5)
    resp = run(data, people, router=slow, limits=tight)
    assert resp.diagnostics["lower_bound_proven"] is False


# ── through the API ─────────────────────────────────────────────────
@pytest.fixture
def stations_client(make_client, settings, tmp_path):
    build = make_random_build(tmp_path, 40, seed=3)
    return make_client(settings.with_(router_provider="mapbox", transit_dir=str(build),
                                      transit_block_startup=True))


@pytest.fixture
def tiny_client(make_client, settings, tmp_path):
    build = make_build(tmp_path, cleared=True)
    return make_client(settings.with_(transit_dir=str(build), transit_block_startup=True))


def body(*people, **kw):
    return {"participants": list(people), **kw}


def test_three_on_public_transport_meet_at_a_named_station(mapbox, tiny_client):
    resp = tiny_client.post(SOLVE, json=body(person("a", near(A, 100)), person("b", near(B, -100)),
                                             person("c", near(B, 0, 200))))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["best"]["name"] in ("Alpha Road", "Bravo") and data["best"]["kind"] == "station"
    assert data["window"] == "wd19" and data["window_label"] == "a weekday, 19:00–20:00"
    assert data["service_date"] == "2026-12-09" and data["valid_until"]
    assert data["service_dates"] == {"weekday": "2026-12-09", "saturday": "2026-12-12"}
    assert data["meeting_area_geojson"] is None
    assert data["credits"] == ["Powered by TfL Open Data", "© OpenStreetMap contributors"]
    assert data["notes"][0] == "Public transport times are for a weekday, 19:00–20:00."
    assert data["diagnostics"]["path"] == "stations" and data["diagnostics"]["elapsed_ms"] < 1000
    assert mapbox.count() == 0


def test_the_window_asked_for_is_the_one_used(tiny_client):
    resp = tiny_client.post(SOLVE, json=body(person("a", near(A, 100)), person("b", near(B, -100)),
                                             window="sa13"))
    assert resp.status_code == 200 and resp.json()["window"] == "sa13"
    assert tiny_client.post(SOLVE, json=body(person("a", A), person("b", B),
                                             window="sunday")).status_code == 422


def test_one_driver_costs_one_matrix_request(mapbox, stations_client):
    data = stations_client.app.state.transit.data
    resp = stations_client.post(SOLVE, json=body(
        person("a", at_station(data, 0)), person("b", at_station(data, 9)),
        person("c", near(CENTRE, 0, 2500), mode="driving")))
    assert resp.status_code == 200, resp.text
    assert mapbox.count("matrix") == 1
    dests = mapbox.calls[-1].url.params["destinations"].split(";")
    assert len(dests) <= 24
    diag = resp.json()["diagnostics"]
    assert diag["matrix_requests"] == 1 and diag["shortlist"] == len(dests)


def test_a_bad_road_reply_never_hides_and_never_goes_over_two_requests(mapbox, stations_client, clock):
    data = stations_client.app.state.transit.data
    people = [person("a", at_station(data, 0)), person("b", at_station(data, 9)),
              person("c", near(CENTRE, 0, 2500), mode="driving")]

    def malformed(request):
        reply = matrix_reply(request)
        reply["durations"][0] = reply["durations"][0][:-1]
        return httpx.Response(200, json=reply)

    def poison_first(request):
        reply = matrix_reply(request)
        if "destinations" in request.url.params and len(request.url.params["destinations"].split(";")) > 1:
            return httpx.Response(200, json={"code": "NoRoute"})
        return httpx.Response(200, json=reply)

    cases = [(lambda r: httpx.Response(200, json={"code": "NoRoute"}), 422, "unroutable_area"),
             (malformed, 502, "upstream_error"), (500, 502, "upstream_error"),
             (poison_first, None, None)]
    for behaviour, status, code in cases:
        mapbox.calls.clear()
        mapbox.behaviour["matrix"] = behaviour
        clock.advance(61)
        resp = stations_client.post(SOLVE, json=body(*people))
        assert mapbox.count("matrix") <= 2, behaviour
        if status is not None:
            assert resp.status_code == status and resp.json()["code"] == code, resp.text
        else:
            assert resp.status_code in (200, 422), resp.text
            if resp.status_code == 200:
                assert st.NOTE_UNCHECKED in resp.json()["notes"]


def test_public_transport_outside_the_tables_is_named(tiny_client):
    resp = tiny_client.post(SOLVE, json=body(person("a", A), person("b", (-1.5, 52.0))))
    assert resp.status_code == 422
    assert resp.json()["code"] == "outside_transit_area"
    assert resp.json()["who"] == [{"index": 1, "reason": "outside_transit_area"}]


def test_public_transport_without_a_way_in_is_named(tiny_client):
    data = tiny_client.app.state.transit.data
    from tiny_transit import C
    pin = next((C[0], C[1] + k * 25 / 111_320) for k in range(1, 40)
               if data.covered(C[0], C[1] + k * 25 / 111_320)
               and not data.reachable_from(C[0], C[1] + k * 25 / 111_320))
    resp = tiny_client.post(SOLVE, json=body(person("b", pin), person("a", A)))
    assert resp.status_code == 422 and resp.json()["code"] == "no_transit_access"
    assert resp.json()["who"] == [{"index": 0, "reason": "no_transit_access"}]


def test_no_tables_means_no_public_transport(make_client, settings, tmp_path, client):
    resp = client.post(SOLVE, json=body(person("a", A), person("b", B)))
    assert resp.status_code == 503 and resp.json()["code"] == "transit_unavailable"
    dev = make_client(settings.with_(transit_dir=str(make_build(tmp_path / "dev")),
                                     transit_allow_uncleared=True, transit_block_startup=True))
    resp = dev.post(SOLVE, json=body(person("a", A), person("b", B)))
    assert resp.status_code == 503 and resp.json()["reason"] == "development_only"


def test_a_group_with_nobody_on_public_transport_keeps_the_grid(tiny_client):
    resp = tiny_client.post(SOLVE, json=body(person("a", A, mode="walking"),
                                             person("b", B, mode="walking")))
    assert resp.status_code == 200
    data = resp.json()
    assert data["diagnostics"]["path"] == "grid" and data["best"]["kind"] in (None, "street")
    assert data["quickest"] is None and data["window"] is None


def _starts(mapbox):
    """How many Matrix requests each start point was in."""
    out = {}
    for call in mapbox.calls:
        if mapbox.api_of(call) != "matrix":
            continue
        coords = call.url.path.rsplit("/", 1)[-1].split(";")
        for i in call.url.params["sources"].split(";"):
            out[coords[int(i)]] = out.get(coords[int(i)], 0) + 1
    return out


def test_no_road_person_is_in_more_than_two_requests(mapbox, stations_client, clock):
    # Two walkers and a cyclist share one rate bucket: each is asked about alone, and nobody's
    # retries or NoRoute splitting spend anyone else's requests.
    data = stations_client.app.state.transit.data
    people = [person("a", at_station(data, 0)), person("b", at_station(data, 5), mode="walking"),
              person("c", at_station(data, 9), mode="walking"),
              person("d", at_station(data, 14), mode="cycling")]
    for behaviour in (lambda r: httpx.Response(200, json={"code": "NoRoute"}), 500):
        mapbox.calls.clear()
        mapbox.behaviour["matrix"] = behaviour
        clock.advance(61)
        stations_client.post(SOLVE, json=body(*people))
        starts = _starts(mapbox)
        assert len(starts) == 3 and all(n <= 2 for n in starts.values()), starts
        assert all(len(c.url.params["sources"].split(";")) == 1 for c in mapbox.calls)


def test_a_no_route_reply_is_always_said_even_when_splitting_recovers(mapbox, stations_client, clock):
    data = stations_client.app.state.transit.data
    sent = []

    def first_no_route(request):
        sent.append(request)
        if len(sent) == 1:
            return httpx.Response(200, json={"code": "NoRoute"})
        return httpx.Response(200, json=matrix_reply(request))

    mapbox.behaviour["matrix"] = first_no_route
    resp = stations_client.post(SOLVE, json=body(person("a", at_station(data, 0)),
                                                 person("b", at_station(data, 9), mode="driving")))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["diagnostics"]["no_route_replies"] == 1 and mapbox.count("matrix") == 2
    assert "The map service couldn't route part of a request" in " ".join(out["notes"])
    # Half the shortlist was asked about again; the other half is said to be unchecked.
    assert out["diagnostics"]["unchecked_for_budget"] > 0
    assert st.note_shortlist(out["diagnostics"]["checked_by_road"]) in out["notes"]


def test_the_grid_says_so_when_splitting_recovers_a_no_route_too(mapbox, matrix_client):
    import server.meetup.solver as solver
    sent = []

    def first_no_route(request):
        sent.append(request)
        if len(sent) == 1:
            return httpx.Response(200, json={"code": "NoRoute"})
        return httpx.Response(200, json=matrix_reply(request))

    mapbox.behaviour["matrix"] = first_no_route
    resp = matrix_client.post(SOLVE, json=body(person("a", (-0.12, 51.51), mode="walking"),
                                               person("b", (-0.10, 51.51), mode="walking")))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    # Every piece answered, so nothing was dropped; the note is there all the same.
    assert out["diagnostics"]["no_route_replies"] == 1
    assert solver.NOTE_NO_ROUTE in out["notes"]


def test_the_shortlist_finds_what_asking_about_every_station_finds(tmp_path):
    # Mixed groups, a router whose detours (up to 35%) the straight-line estimate doesn't
    # know about, and more stations than the shortlist: the 24 asked about by road give the
    # same fairest answer as asking about all of them (measured: 0 misses in 51 groups).
    import dataclasses

    from conftest import bumpy
    builds = [TransitData.load(make_random_build(tmp_path, 60, seed=s, spread_m=4000)) for s in range(3)]
    rng = random.Random(9)
    regrets = []
    for case in range(60):
        data = builds[case % 3]
        people = [person(f"p{i}", at_station(data, rng.randrange(99), rng.uniform(-300, 300)),
                         mode="transit" if i == 0 or rng.random() < 0.4
                         else rng.choice(["walking", "cycling", "driving"]))
                  for i in range(rng.randint(2, 5))]
        if all(p["mode"] == "transit" for p in people):
            continue
        req = request(people)
        g = st.prepare(req, data)
        plan = plan_stations(req, Settings(), None, len(g.cand))
        assert plan.coarse_max == 24 < len(g.cand)
        everything = dataclasses.replace(plan, coarse_max=len(g.cand))
        try:
            short = asyncio.run(st.solve_stations(req, data, FixedRouter(bumpy), plan, "wd19", g))
        except NoFeasiblePoint:
            continue
        full = asyncio.run(st.solve_stations(req, data, FixedRouter(bumpy), everything, "wd19", g))
        regrets.append(short.best.max_time_min - full.best.max_time_min)
    assert len(regrets) >= 40 and max(regrets) <= 1e-6, regrets


def test_times_only_beyond_a_road_person_s_reach_is_no_common_place():
    # The rider has a time to P1 only, 20 km away, beyond the walker's 15 km: that is no
    # common place, not "no trip within 3 hours to any station".
    data = _FakeData([[np.inf, 30]], np.zeros(2), np.array([51.5, 51.5 + 20 / 111.195]))
    with pytest.raises(NoFeasiblePoint) as exc:
        run(data, [person("a", (0, 51.5)), person("b", (0, 51.5), mode="walking")])
    assert exc.value.extra["reason"] == "no_common_place"
    assert exc.value.extra["who"] == [{"index": 0, "reason": "reaches_few", "reach": 0, "of": 1}]
