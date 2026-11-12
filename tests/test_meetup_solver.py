"""The solver's answers, with routers whose times are known exactly."""
import asyncio
import math
import random

import h3
import numpy as np
import pytest
from conftest import FixedRouter, bumpy, straight_line

import server.meetup.solver as solver
from server.errors import NoFeasiblePoint, UnroutableArea
from server.meetup.candidates import edge_km, haversine_km
from server.meetup.limits import plan_solve
from server.meetup.models import SolveRequest
from server.meetup.routing import Snap
from server.settings import Settings

LON, LAT = -0.12, 51.51
KM_LAT = 1 / 111.195                       # degrees of latitude per km
KM_LON = KM_LAT / math.cos(math.radians(LAT))


def at(dx_km, dy_km):
    return LON + dx_km * KM_LON, LAT + dy_km * KM_LAT


def person(pid, dx_km, dy_km, mode="walking", **kw):
    lon, lat = at(dx_km, dy_km)
    return {"id": pid, "lon": lon, "lat": lat, "mode": mode, **kw}


def run(people, router, objective="minimax", **search):
    req = SolveRequest.model_validate({"participants": people, "objective": objective,
                                       "search": search})
    return asyncio.run(solver.solve(req, router, plan_solve(req, Settings())))


def km_between(a, b):
    return haversine_km(a[0], a[1], b[0], b[1])


def best_point(resp):
    return resp.best.lon, resp.best.lat


# ── where groups meet ───────────────────────────────────────────────
def test_two_walkers_meet_near_the_midpoint():
    resp = run([person("a", -1, 0), person("b", 1, 0)], FixedRouter(straight_line))
    assert km_between(best_point(resp), at(0, 0)) < 0.6
    assert resp.diagnostics["stage"] == "fine"
    assert set(resp.best.times) == {"a", "b"}


def test_an_equilateral_triangle_meets_near_its_centre():
    side = 2.0
    h = side * math.sqrt(3) / 2
    people = [person("a", -side / 2, -h / 3), person("b", side / 2, -h / 3), person("c", 0, 2 * h / 3)]
    resp = run(people, FixedRouter(straight_line))
    centre = at(0, 0)
    assert km_between(best_point(resp), centre) < 1.0
    for p in people:
        assert km_between(best_point(resp), centre) < km_between(best_point(resp), (p["lon"], p["lat"]))


def test_one_unreachable_cell_does_not_fail_the_solve():
    people = [person("a", -1, 0), person("b", 1, 0)]
    first = run(people, FixedRouter(straight_line))
    hole = best_point(first)

    def with_hole(mode, o, d):
        return math.inf if km_between(d, hole) < 1e-4 else straight_line(mode, o, d)

    resp = run(people, FixedRouter(with_hole))
    assert km_between(best_point(resp), hole) > 0.05
    assert resp.best.score >= first.best.score


# ── refinement never makes the answer worse ─────────────────────────
class WorseFineStage(FixedRouter):
    """Every place in the second call (the fine stage) is 100 minutes further away."""

    async def duration_matrix(self, mode, origins, destinations):
        result = await super().duration_matrix(mode, origins, destinations)
        if self.calls > 1:
            result.minutes = result.minutes + 100.0
        return result


def test_a_worse_fine_stage_keeps_the_coarse_answer():
    people = [person("a", -1, 0), person("b", 1, 0)]
    coarse_only = _coarse_only(people, FixedRouter(straight_line))
    resp = run(people, WorseFineStage(straight_line))
    assert resp.diagnostics["stage"] == "coarse"
    assert resp.best.score == pytest.approx(coarse_only.best.score)


def _coarse_only(people, router, objective="minimax"):
    real = solver._fine_candidates
    solver._fine_candidates = lambda *a, **kw: []
    try:
        return run(people, router, objective)
    finally:
        solver._fine_candidates = real


# ── against brute force ─────────────────────────────────────────────
def _random_case(rng):
    n = rng.randint(2, 4)
    spread = rng.uniform(1.0, 4.0)
    people = [person(f"p{i}", rng.uniform(-spread, spread) / 2, rng.uniform(-spread, spread) / 2,
                     rng.choice(["walking", "cycling"])) for i in range(n)]
    return people, rng.choice(["minimax", "sum"]), rng.choice([straight_line, bumpy])


def _brute_force(people, objective, fn, res):
    """The best score over every cell at `res` inside the search radius."""
    req = SolveRequest.model_validate({"participants": people, "objective": objective})
    plan = plan_solve(req, Settings())
    c_lon, c_lat = plan.center
    k = math.ceil(plan.radius_km * 1.1 / edge_km(res)) + 2
    cells = []
    for cell in h3.grid_disk(h3.latlng_to_cell(c_lat, c_lon, res), k):
        lat, lon = h3.cell_to_latlng(cell)
        if haversine_km(c_lon, c_lat, lon, lat) <= plan.radius_km * 1.1:
            cells.append((lon, lat))
    parts = req.participants
    times = np.array([[fn(p.mode, (p.lon, p.lat), d) for d in cells] for p in parts])
    score, _, _ = solver._score(times, parts, objective, req.alpha)
    return float(score.min())


def test_within_a_minute_of_brute_force_and_never_worse_than_coarse():
    rng = random.Random(4)
    gaps = []
    for _ in range(100):
        people, objective, fn = _random_case(rng)
        resp = run(people, FixedRouter(fn), objective)
        res = resp.diagnostics["coarse_resolution"] + 1
        brute = _brute_force(people, objective, fn, res)
        gaps.append(resp.best.score - brute)
        assert resp.best.score <= brute + 1.0, (people, objective, fn.__name__)
        coarse = _coarse_only(people, FixedRouter(fn), objective)
        assert resp.best.score <= coarse.best.score + 1e-9
    assert max(gaps) <= 1.0


def test_the_same_group_in_any_order_gets_the_same_answer():
    rng = random.Random(7)
    for _ in range(20):
        people, objective, fn = _random_case(rng)
        first = run(people, FixedRouter(fn), objective)
        shuffled = people[:]
        rng.shuffle(shuffled)
        second = run(shuffled, FixedRouter(fn), objective)
        assert best_point(second) == best_point(first)
        assert second.best.score == first.best.score
        assert second.best.times == first.best.times
        assert [(c.lon, c.lat, c.score) for c in second.top] == [(c.lon, c.lat, c.score) for c in first.top]


# ── the snap ────────────────────────────────────────────────────────
def test_a_place_far_from_any_street_is_not_offered_and_the_answer_is_on_its_street():
    people = [person("a", -1, 0), person("b", 1, 0)]
    first = run(people, FixedRouter(straight_line))
    water = best_point(first)

    def snap(mode, d):
        if km_between(d, water) < 1e-4:
            return Snap(d[0], d[1] + 0.4 * KM_LAT, "Across The River")   # 400 m away
        return Snap(d[0], d[1] + 0.04 * KM_LAT, "High Street")             # 40 m away

    resp = run(people, FixedRouter(straight_line, snap_fn=snap))
    assert resp.best.name == "High Street"
    assert resp.best.snap_m == pytest.approx(40, abs=1)
    assert km_between(best_point(resp), water) > 0.05
    assert resp.diagnostics["dropped_off_street"] >= 1
    # The answer is the snapped point, not the grid point.
    assert all(c.name == "High Street" for c in resp.top)


def _two_mode_snaps(walk_m, drive_m):
    def snap(mode, d):
        if mode == "walking":
            return Snap(d[0], d[1] + walk_m / 1000 * KM_LAT, "Footpath")
        return Snap(d[0], d[1] + drive_m / 1000 * KM_LAT, "Main Road")
    return snap


def test_a_walker_and_a_driver_are_shown_the_walking_point():
    resp = run([person("a", -1, 0, "driving"), person("b", 1, 0, "walking")],
               FixedRouter(straight_line, snap_fn=_two_mode_snaps(20, 60)))
    assert resp.best.name == "Footpath"
    assert resp.best.snap_m == pytest.approx(20, abs=1)   # the shown point's own distance
    assert solver.NOTE_SPREAD not in resp.notes            # the driver ends 40 m away


def test_trips_ending_away_from_the_star_are_said():
    # Walking point 20 m north of the grid point, driving point 120 m south: 140 m apart.
    resp = run([person("a", -1, 0, "driving"), person("b", 1, 0, "walking")],
               FixedRouter(straight_line, snap_fn=_two_mode_snaps(20, -120)))
    assert solver.NOTE_SPREAD in resp.notes


def test_modes_that_snap_far_apart_are_not_offered():
    # Both points are within 150 m of the grid point, on opposite sides: 260 m apart, as on
    # two banks of a river. Neither is offered; nothing else is, so it is an error.
    with pytest.raises(NoFeasiblePoint) as exc:
        run([person("a", -1, 0, "driving"), person("b", 1, 0, "walking")],
            FixedRouter(straight_line, snap_fn=_two_mode_snaps(130, -130)))
    assert exc.value.extra["reason"] == "off_street"


def test_when_little_is_near_a_street_the_rule_is_relaxed_with_a_note():
    def snap(mode, d):
        return Snap(d[0], d[1] + 0.3 * KM_LAT, "Distant Lane")   # 300 m from every grid point

    resp = run([person("a", -1, 0), person("b", 1, 0)], FixedRouter(straight_line, snap_fn=snap))
    assert resp.best.name == "Distant Lane"
    assert solver.NOTE_OFF_STREET in resp.notes
    assert resp.diagnostics["dropped_off_street"] > 0


def test_nothing_near_a_street_is_an_error_not_a_far_answer():
    def snap(mode, d):
        return Snap(d[0], d[1] + 2.0 * KM_LAT, "Distant Lane")   # 2 km from every grid point

    with pytest.raises(NoFeasiblePoint) as exc:
        run([person("a", -1, 0), person("b", 1, 0)], FixedRouter(straight_line, snap_fn=snap))
    assert exc.value.extra == {"reason": "off_street", "who": []}


def test_many_places_far_from_a_street_get_a_note():
    def snap(mode, d):
        far = hash(d) % 3 != 0       # two in three grid points are 400 m from a street
        return Snap(d[0], d[1] + (0.4 if far else 0.02) * KM_LAT, "Lane")

    resp = run([person("a", -1, 0), person("b", 1, 0)], FixedRouter(straight_line, snap_fn=snap))
    assert solver.NOTE_FEW_STREETS in resp.notes
    assert solver.NOTE_OFF_STREET not in resp.notes


def test_the_star_is_inside_the_meeting_area():
    from shapely.geometry import Point, shape

    resp = run([person("a", -1, 0), person("b", 1, 0)],
               FixedRouter(straight_line, snap_fn=lambda m, d: Snap(d[0], d[1] + 0.12 * KM_LAT, "High St")))
    area = shape(resp.meeting_area_geojson["geometry"])
    assert area.contains(Point(resp.best.lon, resp.best.lat))


def test_alternatives_that_snap_to_the_same_spot_are_shown_once():
    def snap(mode, d):
        # Every grid point snaps to one of a few street corners about a kilometre apart.
        return Snap(round(d[0] * 50) / 50, round(d[1] * 100) / 100, "Corner")

    resp = run([person("a", -1, 0), person("b", 1, 0)], FixedRouter(straight_line, snap_fn=snap),
               top_k=5)
    points = [(c.lon, c.lat) for c in resp.top]
    for i, a in enumerate(points):
        for b in points[i + 1:]:
            assert km_between(a, b) * 1000 >= solver.MIN_ALT_GAP_M


# ── unchecked places and no answer ──────────────────────────────────
def test_a_few_unchecked_places_add_a_note():
    people = [person("a", -1, 0), person("b", 1, 0)]
    resp = run(people, FixedRouter(straight_line, unresolved_fn=lambda m, d: hash(d) % 20 == 0))
    assert resp.diagnostics["dropped_no_route"] > 0
    assert solver.NOTE_UNCHECKED in resp.notes


def test_too_many_unchecked_places_fail_with_a_reason():
    with pytest.raises(UnroutableArea):
        run([person("a", -1, 0), person("b", 1, 0)],
            FixedRouter(straight_line, unresolved_fn=lambda m, d: hash(d) % 5 < 2))


class UncheckedFineStage(FixedRouter):
    async def duration_matrix(self, mode, origins, destinations):
        result = await super().duration_matrix(mode, origins, destinations)
        if self.calls > 1:
            mask = np.arange(len(destinations)) % 2 == 0
            result.unresolved = np.broadcast_to(mask, result.minutes.shape).copy()
        return result


def test_a_mostly_unchecked_fine_stage_keeps_the_coarse_answer_with_a_note():
    resp = run([person("a", -1, 0), person("b", 1, 0)], UncheckedFineStage(straight_line))
    assert resp.diagnostics["stage"] == "coarse"
    assert solver.NOTE_FINE_SKIPPED in resp.notes
    # The discarded stage's holes say nothing about the answer shown.
    assert solver.NOTE_UNCHECKED not in resp.notes
    assert resp.diagnostics["dropped_no_route"] == 0


def test_one_mode_left_unchecked_blames_nobody():
    # The walking replies never came back answered; the cyclist and the driver are fine.
    people = [person("w", -1, 0, "walking"), person("c", 1, 0, "cycling"), person("d", 0, 1, "driving")]
    with pytest.raises(UnroutableArea):
        run(people, FixedRouter(straight_line, unresolved_fn=lambda m, d: m == "walking"))


def test_only_the_person_whose_start_has_no_route_is_named():
    people = [person("w", -1, 0, "walking"), person("c", 1, 0, "cycling"), person("d", 0, 1, "driving")]

    def fn(mode, o, d):   # answered, and the walker reaches nothing
        return math.inf if mode == "walking" else straight_line(mode, o, d)

    with pytest.raises(NoFeasiblePoint) as exc:
        run(people, FixedRouter(fn))
    assert exc.value.extra == {"reason": "no_route", "who": [{"index": 0, "reason": "no_route"}]}


def test_someone_who_can_reach_nothing_is_named_by_position():
    # "zed" is first in the request but sorts last by id: the index must be the request's.
    people = [person("zed", -1, 0), person("amy", 1, 0)]
    stuck = (people[0]["lon"], people[0]["lat"])

    def fn(mode, o, d):
        return math.inf if o == stuck else straight_line(mode, o, d)

    with pytest.raises(NoFeasiblePoint) as exc:
        run(people, FixedRouter(fn))
    assert exc.value.extra == {"reason": "no_route", "who": [{"index": 0, "reason": "no_route"}]}


def test_a_time_limit_nobody_can_meet_is_named():
    people = [person("a", -1, 0), person("b", 1, 0, max_minutes=1)]
    slow = (people[1]["lon"], people[1]["lat"])

    def fn(mode, o, d):  # b needs at least 5 minutes to get anywhere, even next door
        return straight_line(mode, o, d) + (5 if o == slow else 0)

    with pytest.raises(NoFeasiblePoint) as exc:
        run(people, FixedRouter(fn))
    assert exc.value.extra == {"reason": "over_time_limit", "who": [{"index": 1, "reason": "over_time_limit"}]}


def test_two_people_with_no_place_in_common():
    people = [person("a", -1, 0), person("b", 1, 0)]

    def fn(mode, o, d):
        west = d[0] < LON
        mine = o[0] < LON
        return straight_line(mode, o, d) if west == mine else math.inf

    with pytest.raises(NoFeasiblePoint) as exc:
        run(people, FixedRouter(fn))
    # Two halves of the same size: nobody reaches fewer places than the other.
    assert exc.value.extra == {"reason": "no_common_place", "who": []}


def test_someone_cut_off_with_a_few_places_is_named():
    people = [person("a", -1, 0), person("isle", 1.5, 0)]
    island = at(1.5, 0)

    def fn(mode, o, d):
        on_island = km_between(d, island) < 1.6   # a few coarse cells
        mine = km_between(o, island) < 0.01
        return straight_line(mode, o, d) if on_island == mine else math.inf

    with pytest.raises(NoFeasiblePoint) as exc:
        run(people, FixedRouter(fn))
    (who,) = exc.value.extra["who"]
    assert exc.value.extra["reason"] == "no_common_place"
    assert who["index"] == 1 and who["reason"] == "reaches_few" and 0 < who["reach"] < who["of"]


def test_the_no_place_error_names_nobody_and_echoes_nothing(client):
    stuck_lon = -0.2795

    def fn(mode, o, d):
        return math.inf if o[0] == stuck_lon else straight_line(mode, o, d)

    client.app.state.meetup_router = FixedRouter(fn)
    resp = client.post("/api/meetup/solve", json={"participants": [
        {"id": "home-of-zed", "lon": stuck_lon, "lat": 51.5635, "mode": "walking"},
        {"id": "amy", "lon": -0.2, "lat": 51.55, "mode": "walking"},
    ]})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "no_feasible_meeting_point"
    assert body["reason"] == "no_route"
    assert body["who"] == [{"index": 0, "reason": "no_route"}]
    assert "home-of-zed" not in resp.text and "51.56" not in resp.text and "0.27" not in resp.text
    assert "time limit" not in body["detail"]
