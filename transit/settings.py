"""
Every setting of the transit build in one place. The manifest copies all of them, so a table
can always be traced back to the settings that made it.
"""
from __future__ import annotations

import os
from datetime import date, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("TRANSIT_DATA", ROOT / "data"))
OUT = Path(os.environ.get("TRANSIT_OUT", ROOT / "out"))
RAW = DATA / "raw"
WORK = DATA / "work"
CACHE = DATA / "cache"
TOOLS = DATA / "tools"
REGION_GEOJSON = ROOT / "london_region.geojson"
SOURCES_TOML = ROOT / "sources.toml"
CASES_TOML = ROOT / "cases.toml"

TIMEZONE = "Europe/London"

# ── Service dates and windows ───────────────────────────────────────
# One Wednesday and the Saturday of the same week. Windows start at these local times and
# last WINDOW_MINUTES; R5 departs every minute in the window, TfL every 10 minutes.
WINDOWS: dict[str, tuple[str, time]] = {
    "wd08": ("weekday", time(8, 0)),
    "wd13": ("weekday", time(13, 0)),
    "wd19": ("weekday", time(19, 0)),
    "sa13": ("saturday", time(13, 0)),
}
WINDOW_MINUTES = 60
TFL_DEPARTURE_OFFSETS_MIN = (0, 10, 20, 30, 40, 50)
WEEKDAY = 2                      # Wednesday (Monday is 0)
DATE_LEAD_DAYS = 10              # the gate must query these dates before they pass
# National Rail's timetable changes (December: second Sunday; May: third Sunday). A change is
# on a Sunday and the service week runs Wednesday to Saturday, so a week never straddles one;
# the first change after the service dates ends the tables' validity (manifest valid_until).
# Check new dates against Network Rail's published calendar and add them here.
TIMETABLE_CHANGES = (date(2026, 12, 13), date(2027, 5, 16), date(2027, 12, 12), date(2028, 5, 21))
# A third party's kept copy of TfL's timetables older than this, at build time, fails the check
# (gb-transit kept TfL's on 2026-11-03 after TfL refused its download).
TFL_COPY_MAX_AGE_DAYS = 21
CANDIDATE_WEEKS = 4              # weeks compared for the fewest Saturday closures
VALID_DAYS = 35                  # valid_until is at most build date + this

# ── Study area and points ───────────────────────────────────────────
BNG_EPSG = 27700                 # British National Grid, metres
BUFFER_POINTS_M = 2000           # points cover Greater London + 2 km
BUFFER_OSM_M = 5000              # streets and stop_times kept within Greater London + 5 km
H3_RES_POINTS = 8
H3_RES_WALK = 10
STATION_COUNT_RANGE = (400, 1200)
CELL_COUNT_RANGE = (2000, 4500)
SNAP_RADIUS_STATION_M = 150
SNAP_RADIUS_CELL_M = 400
# A snapped point that cannot reach, and cannot be reached from, this share of the other points
# is on a street fragment cut off from the network: it is masked like an unsnapped point.
ISOLATED_SHARE = 0.9
# GTFS route types whose stops are table stations: tram, subway, rail and their extended
# equivalents (rail 100-199, urban rail 400-499, tram 900-999).
STATION_ROUTE_TYPES = {0, 1, 2}
STATION_ROUTE_TYPE_RANGES = ((100, 199), (400, 499), (900, 999))

# ── R5 routing ──────────────────────────────────────────────────────
R5 = {
    "transport_modes": ["TRANSIT"],
    "access_modes": ["WALK"],
    "egress_modes": ["WALK"],
    "speed_walking_kmh": 4.0,           # TfL Journey Planner "Average"
    "departure_time_window_min": WINDOW_MINUTES,
    "percentiles": [50],
    "max_time_min": 180,
    "max_time_walking_min": 30,
    "max_public_transport_rides": 8,
    "snap_to_network": False,           # points are snapped by us first, see points.py
    # Two experimental costs, both off (plain R5). A calibration on the quick look's cached TfL
    # replies (300 m, +1 min a ride) fitted Journey Planner's habit of walking from the nearest
    # street address when given a coordinate at a station: on pairs with no station endpoint
    # the plain tables already sat at a median signed error of 0 and the calibration made them
    # worse. See docs/transit-spike.md, "Review and revision".
    # Metres added to the street links of rail, tube and tram stops (transit.r5.station_access),
    # charged on the way in and the way out.
    "station_access_m": float(os.environ.get("TRANSIT_STATION_ACCESS_M", "0")),
    # Minutes added per ride. Non-zero routes each origin at ride limits 1-5 and 8 and walking
    # alone (about 7x the table build time); the result is an upper bound (see calibrate.py).
    "ride_cost_min": float(os.environ.get("TRANSIT_RIDE_COST_MIN", "0")),
}
JVM_MAX_MEMORY = os.environ.get("TRANSIT_MAX_MEMORY", "10G")
# R5's itinerary search (DetailedItineraries) names the lines a journey takes, but on this
# network it runs out of the 10 GB heap even for rail, tube and tram only. Off by default; the
# go/no-go checks the kind of line by comparing with a mode-only run instead.
R5_ITINERARIES = os.environ.get("TRANSIT_ITINERARIES", "0") == "1"
NETWORK_TIMEOUT_S = 2 * 3600
ORIGIN_CHUNK = 250
MATRIX_THREADS = int(os.environ.get("TRANSIT_THREADS", "1"))

# ── Walksheds ───────────────────────────────────────────────────────
WALKSHED_RADIUS_M = 2000
WALKSHED_MAX_MIN = 45
WALKSHED_STATION_BATCH = 20
# Access points besides stations: the table's own H3 cells, reached on foot within this radius.
# A cell's table row carries its bus access, so an arbitrary point gets buses through it (the
# issue's "add more points near origins").
CELL_ACCESS_RADIUS_M = 1000
# Walkshed cell centres are snapped like table points; a centre with no street within this
# radius (water, large parks) is left out instead of being linked to a far bank.
WALKSHED_SNAP_M = 150

# ── Tables ──────────────────────────────────────────────────────────
MAX_MINUTES = 180                    # same as R5 max_time
UNREACHABLE = 255

# ── TfL Journey Planner ─────────────────────────────────────────────
TFL_API = "https://api.tfl.gov.uk"
JP = {
    "timeIs": "Departing",
    "journeyPreference": "LeastTime",
    "walkingSpeed": "Average",
    "maxWalkingMinutes": "30",
    "useRealTimeLiveArrivals": "false",
    "mode": ",".join([
        "tube", "dlr", "overground", "elizabeth-line", "bus", "tram", "national-rail",
        "cable-car", "river-bus", "replacement-bus", "walking",
    ]),
}
# Well under TfL's limit (its terms on bulk use are unread); the cache sends each request once.
TFL_RATE_PER_MIN = int(os.environ.get("TFL_RATE_PER_MIN", "100"))
# Without a key TfL still answers, at a lower limit (third-party guides say 50 a minute; TfL
# has not said). The gate then goes slower still.
TFL_RATE_PER_MIN_ANON = int(os.environ.get("TFL_RATE_PER_MIN_ANON", "35"))
TFL_IN_FLIGHT = 16
TFL_TIMEOUT_S = 30
TFL_RETRIES = 3
USER_AGENT = "time-map-transit-build/0.1"

# ── Gate ────────────────────────────────────────────────────────────
BANDS = ("le20", "20to45", "gt45")
BAND_LINES = {"le20": (2.0, 5.0), "20to45": (3.0, 7.0), "gt45": (4.0, 10.0)}  # median, p90
MIN_PAIRS_PER_BAND = 100
MAX_DRAWS_PER_WINDOW = 2000
# Destinations are drawn by distance for the band being filled (random pairs across London are
# almost never under 20 min); each pair is still banded by its times.
BAND_DRAW_KM = {"le20": (0.3, 5.0), "20to45": (3.0, 15.0), "gt45": (10.0, 60.0)}
DIRECT_WALK_MAX_KM = 3.0
# Sample A is the gate. Sample B is drawn only if something was fixed after A (so nothing
# reported was tuned on the pairs it is scored on).
SEEDS = {"a": 20261118, "b": 20261125}
BAND_DRAW_FACTOR = 1.1         # first draw per band, as a share of MIN_PAIRS_PER_BAND
RANKING_GROUPS = 50
RANKING_GROUP_SPLIT = {"wd08": 13, "wd13": 13, "wd19": 12, "sa13": 12}
RANKING_PAIR_KM = (3.0, 20.0)
# TfL is asked only about contenders: stations whose table longest trip is within this many
# minutes of the table's best for the group. Measured on build 2026-11-12: 8 min gives a median
# of 17 contenders per group (p90 39), 20 min gave 134 (115k calls), so 8 keeps the gate near
# 18k calls. The report counts groups where TfL's best sat near the margin's edge; if that is
# common, rerun with a wider --margin (cached replies are reused).
RANKING_MARGIN_MIN = 8.0
# Whatever the margin, TfL is asked about at least the table's best few stations, and a group
# with fewer TfL-timed contenders than RANKING_MIN_TIMED is "not tested" (one contender passes
# by construction).
RANKING_MIN_CONTENDERS = 5
RANKING_MIN_TIMED = 3
# Departures per ranking query (minutes into the window). The error bands keep all six.
RANKING_DEPARTURE_OFFSETS_MIN = (10, 30, 50)
# `--departures 3` for the error bands: every 20 min, a subset of the full six, so a later
# full run asks only for the other three per pair.
BAND_DEPARTURE_OFFSETS_3 = (0, 20, 40)
# `--provisional`: a one-hour quick look, for when there is no TfL key (about 2,000 calls at
# 35 a minute). A later run of the same sample reuses the cached replies for the pairs it shares.
PROVISIONAL = {"band_departures": 3, "pairs_per_band": 30, "groups_per_window": 3, "margin": 5.0,
               "case_departures": 3,
               "label": "provisional quick look, not the gate: 30 pairs per band (the issue asks "
                        "for 100), departures every 20 min (it asks for every 10), 12 ranking "
                        "groups (50), ranking margin 5 min"}
RANKING_REGRET_MIN = 3.0
RANKING_PASS_GROUPS = 45       # proposal; the owner may require 50
JOURNEY_TOLERANCE_MIN = 3.0
WALK_CHECK_TOLERANCE_MIN = 2.0

# ── Accepted lines (owner decision, 2026-11-13) ─────────────────────
# The tables are kept as a rough estimate. Every gate run is scored twice: on the issue's lines
# above, and on these, which the verdict uses. Each is the plain tables' worst window on the
# quick look (build 2026-11-12), rounded up to a whole minute, plus headroom for sampling noise:
# 1 min on a median and 2 on a p90 (about one bootstrap standard error at 30-46 pairs a cell),
# 1 min on the journeys and on the worst ranking regret.
ACCEPTED = {
    "band_lines": {"le20": (3.0, 8.0), "20to45": (7.0, 14.0), "gt45": (7.0, 17.0)},
    "journey_tolerance_min": 8.0,
    # Ranking: at least this share of the complete groups within RANKING_REGRET_MIN of TfL's
    # best, no complete group further off than this, and at least this share of groups complete.
    "ranking_within_share": 0.5,
    "ranking_max_regret_min": 11.0,
    "ranking_min_complete_share": 2 / 3,
}

# Free disk needed before fetch starts (GB).
MIN_FREE_GB = 8


def jvm_home() -> str | None:
    """JAVA_HOME for r5py: the env var, else the JDK 21 unpacked under data/tools."""
    if os.environ.get("JAVA_HOME"):
        return os.environ["JAVA_HOME"]
    local = TOOLS / "jdk21" / "Contents" / "Home"
    return str(local) if local.exists() else None


def snapshot() -> dict:
    """The settings the manifest records (paths excluded)."""
    return {
        "timezone": TIMEZONE,
        "windows": {k: [kind, t.strftime("%H:%M")] for k, (kind, t) in WINDOWS.items()},
        "window_minutes": WINDOW_MINUTES,
        "tfl_departure_offsets_min": list(TFL_DEPARTURE_OFFSETS_MIN),
        "buffer_points_m": BUFFER_POINTS_M,
        "buffer_osm_m": BUFFER_OSM_M,
        "h3_res_points": H3_RES_POINTS,
        "h3_res_walk": H3_RES_WALK,
        "snap_radius_station_m": SNAP_RADIUS_STATION_M,
        "snap_radius_cell_m": SNAP_RADIUS_CELL_M,
        "isolated_share": ISOLATED_SHARE,
        "station_route_types": sorted(STATION_ROUTE_TYPES),
        "station_route_type_ranges": [list(r) for r in STATION_ROUTE_TYPE_RANGES],
        "timetable_changes": [d.isoformat() for d in TIMETABLE_CHANGES],
        "tfl_copy_max_age_days": TFL_COPY_MAX_AGE_DAYS,
        "r5": dict(R5),
        "jvm_max_memory": JVM_MAX_MEMORY,
        "walkshed_radius_m": WALKSHED_RADIUS_M,
        "walkshed_max_min": WALKSHED_MAX_MIN,
        "cell_access_radius_m": CELL_ACCESS_RADIUS_M,
        "walkshed_snap_m": WALKSHED_SNAP_M,
        "max_minutes": MAX_MINUTES,
        "unreachable": UNREACHABLE,
        "journey_planner": dict(JP),
        "band_lines": {k: list(v) for k, v in BAND_LINES.items()},
        "accepted_lines": {**ACCEPTED, "band_lines": {k: list(v) for k, v in ACCEPTED["band_lines"].items()}},
    }
