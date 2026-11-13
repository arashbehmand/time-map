# Transit spike (T-01): results and decisions

Issue #1, part T-01. Laptop: Apple M2, 16 GB RAM, 8 cores, macOS. Build id `2026-11-12`.
Numbers come from `transit/out/2026-11-12/` (git-ignored); this note is the committed
record. How to rerun everything: [transit/README.md](../transit/README.md). Sources and
licences: [transit-sources.md](transit-sources.md).

The tables of record are **plain R5**: no added station access time, no cost per ride. A
calibrated version was tried and dropped; see "Calibration experiment".

## Done-when, and where each stands

| # | Done when | State |
|---|---|---|
| 1 | A current London GTFS loads into R5, with a table of sources, versions and licences; Underground, DLR, Overground, Elizabeth line, buses, trams (+ National Rail if terms allow) | Met for building. Publishing waits for the rail and TfL licence terms ([transit-sources.md](transit-sources.md)) |
| 2 | Go/no-go: R5 builds on a laptop; three hand-checked journeys within 3 min; memory and build time recorded | Builds on the laptop (3-6 min, 3.1-4.7 GB). The three journeys: 1 of 3 within 3 min station to station, so the strict test is not met; all 3 are within the accepted 8 min. Owner decision: go, as a rough estimate |
| 3 | Tables, walksheds and manifest build from one script | Met. `python -m transit.build all` ran every stage in one go, twice, on 2026-11-13; see "Tables" |
| 4 | The gate passes, or its numbers are published with a decision | Published with a decision. The full gate has not run (no TfL key). The one-hour quick look passes on the owner's accepted lines and fails on the issue's; see "Decision" |

## Feeds

- **Rail and TfL non-bus:** gb-transit `feed-2026-11-12` (`gtfs-rail-and-tfl.zip`): National
  Rail timetable RJTTC999, and TfL's Underground/DLR/tram/river/cable car timetables as kept
  by gb-transit on 2026-11-03 (TfL refused gb-transit's download). That copy was 9 days old
  at build time; the feed check fails a copy over 21 days old.
- **Buses:** BODS London GTFS, feed_version 20261112_041209, route_type 3 only.
- **Streets:** OSM England (Geofabrik, data as of 2026-11-11), clipped to Greater London +
  5 km (158 MB).

**Service dates: Wednesday 2026-12-09 and Saturday 2026-12-12.** The picker took the first
weeks at least 10 days ahead with no bank holiday, and chose the one with the fewest closures:

| Week | Operators with no trips | TfL planned closures (Saturday) | Score |
|---|---|---|---|
| 25 / 28 Nov | Heathrow Express (Saturday) | 10 | 11 |
| 2 / 5 Dec | Gatwick Express (Saturday) | 4 | 5 |
| **9 / 12 Dec** | none | 2 | **2** |

The National Rail timetable changes on Sunday 13 December. A change is always a Sunday and
the service week runs Wednesday to Saturday, so a week never straddles one; the change ends
the tables' validity (`valid_until` 2026-12-12). Later changes are listed in
`settings.TIMETABLE_CHANGES`, so a monthly rebuild keeps working.

**Prep** kept what runs on those two days inside Greater London + 5 km:

| Feed | Trips before → after | stop_times before → after | Doubled trips removed (Wed / Sat) |
|---|---|---|---|
| gb-transit | 384,922 → 39,358 | 4,708,337 → 661,996 | 2 / 0 |
| BODS buses | 453,490 → 214,391 | 17,062,403 → 8,669,475 | 575 / 1,671 |

Prep takes about 85 s.

**Doubled trips in BODS** were the one real data problem. On some routes two timetable
versions are both active on the same day: London United 293 had 229 of its 425 Saturday
trips doubled (the same stops and first departure, slightly different later times). Prep
keeps one trip per route, stop pattern and first departure on each date: the one from the
newer version (the service that starts latest), then the lowest trip id.

Copies whose first departures differ by a minute or two are not removed. The feed check
reports them: 310 Wednesday trips on 32 routes and 225 Saturday trips on 11 routes run within
2 min of another trip on the same route and stops (most on 370, 407, 216, K3 and 406). Some
may be real buses running close together; nothing removes them yet.

**Feed check: pass** (`feeds-check.md`):
- All 11 Tube lines, DLR, trams, cable car and river boats run on both dates.
- So do the six Overground lines (Liberty, Lioness, Mildmay, Suffragette, Weaver, Windrush),
  the Elizabeth line through the central tunnel, and Heathrow Express.
- Thameslink and Southern run London Bridge → East Croydon, and the main National Rail
  operators run.
- Lines where a token service would hide a problem need a minimum number of trips: DLR 500,
  trams 300, Elizabeth line 300, Heathrow Express, Thameslink and Southern 100.
- The Waterloo & City line runs on the weekday only; it has no Saturday service.
- The clip keeps only the London part of long-distance services. Gatwick Express keeps 1
  Saturday trip inside it, because it runs from Victoria to Gatwick without stopping.
- There are 815 bus routes, and 99.3% of the 675 bus routes TfL lists are in the BODS feed.
  Not found: 233, 246, 653, 969, SCS.
- No line is doubled across operators. The Underground appears only once: gb-transit's
  National Rail "London Underground" agency has no routes.
- Two earlier false alarms led to tighter rules.
  - The Victoria line runs trains a minute apart, so only identical first departures count
    as doubled.
  - Bakerloo and Lioness trains share track north of Queen's Park and can leave in the same
    minute, so a cross-operator double must match to the minute at three stations in a row.

## Network

R5 v7.5.1 (r5py 1.1.7), Temurin JDK 21, 10 GB heap, on the prepared feeds and the clipped
OSM extract:

- **Cold build: 157-355 s (3 to 6 min)** over five builds. Peak resident memory 3.1-4.7 GB;
  macOS peak memory footprint 11.1 GB (first build).
- **From r5py's cache: 7-9 s** (the cached network is 0.95 GB on disk).
- **Points:** 640 stations and 3,076 H3 res-8 cells over Greater London + 2 km, 3,716 in all.
  - 7 cells don't snap to a street within 400 m: reservoirs, the Thames, Heathrow's runways.
  - 130 more snap, but to street fragments cut off from the rest. They reach, and are reached
    by, under 10% of the other points in every window. 24 of them are inside Greater London;
    some snap within metres of their centre.
  - All 137 are masked: their rows and columns are 255. That leaves 3,579 usable points.
    `points.json` lists them.

## Go/no-go

Weekday 08:00-09:00 medians, in minutes, from the requested departure (the first wait counts).
R5 departs every minute; TfL departs at 08:00, 08:10, ... 08:50.

| Journey | R5 | TfL station to station | Δ | TfL from coordinates | Δ |
|---|---|---|---|---|---|
| Wembley Park → Baker Street | 22 | 15.5 | +6.5 | 28.0 | −6.0 |
| Paddington → Heathrow T2&3 | 24 | 20.0 | +4.0 | 39.0 | −15.0 |
| London Bridge → East Croydon | 18 | 16.0 | +2.0 | 30.5 | −12.5 |

The other windows (Wed 13:00, Wed 19:00, Sat 13:00) give 20 / 19 / 20, 24 / 24 / 24 and
19 / 19 / 20.

**Result: the strict test (all three within 3 min) is not met, in either comparison: one of
three station to station, none from coordinates.** The timetable, the routing and the laptop
are not the cause; how the two routers treat the ends of a journey is.

- **The rides match.** TfL's legs and the feed agree: Metropolitan line 13-15 min from Wembley
  Park (the fast trains run through Wembley Park and the feed rightly marks them as not
  stopping there), Heathrow Express 15 min, Thameslink 12-16 min.
- **The mode is checked, the line is not.** R5's itinerary search, which names the line, runs
  out of the 10 GB heap on this network, even limited to rail, tube and tram. With only tube
  allowed (or only rail), R5 gives the same time as with everything, so the journey is by the
  expected mode. Tube does not tell the Metropolitan from the Jubilee.
- **Given station ids, TfL starts on the platform.** R5 always starts and ends at a point: it
  walks from the point to the street, from the street to the platform, and adds a 1-min
  boarding allowance. That is 3-4 min even from platform to platform (Metropolitan platforms
  WYP4 → BST8: 20 min median against TfL's 15.5), plus up to 2-4 min at large stations
  (Baker Street's Metropolitan platforms to the station centre).
- **Given coordinates, TfL walks from the nearest street address**, 4-14 min per end ("Walk to
  723 Cromer Road" at Heathrow, 14 min), so it comes out 6-15 min slower than R5.
- **Walking:** on three plain street walks TfL "Average" took 4-5 min longer than R5 at
  4 km/h (22 against 18, 24 against 19, 15 against 11). Three walks can't tell a fixed extra
  from a slower speed (about 3.2 km/h fits as well). The speed stays at 4 km/h; at 3.3 km/h
  short trips got worse (see the quick look).
- **Interchanges, station to station:** R5 is 0.5-8 min slower than TfL (Wembley Park → Bank
  +1.5; Brixton → Old Street +8; Camden Town → Canary Wharf +0.5; Notting Hill Gate → Tower
  Hill +4). R5 walks transfers over OSM streets, and the gb-transit TfL stops share one
  coordinate per station.
- TfL does not accept `910GLNDNBDE` as a start (`910GLNDNBDC` works).

## Tables

`python -m transit.build all --build 2026-11-12 --force --threads 7`, 2026-11-13, 03:05-03:45:
every stage in one go, from the downloaded inputs (reused by checksum; the England extract
had been clipped and deleted earlier, which saves a 1.4 GB download).

| Stage | Time |
|---|---|
| fetch, dates, prep, feed check | 2.1 min |
| network (cold) | 4.9 min |
| points, go/no-go journeys | 0.7 min |
| tables, 4 windows | 30.3 min |
| walksheds, manifest, package | 1.5 min |
| **all** | **39.5 min** |

| Window | Origins | Time | s per origin (7 threads) | Usable pairs with no time within 180 min |
|---|---|---|---|---|
| Wed 08-09 | 3,709 | 6.9 min | 0.11 | 2.0% |
| Wed 13-14 | 3,709 | 7.3 min | 0.12 | 2.3% |
| Wed 19-20 | 3,709 | 8.5 min | 0.14 | 4.5% |
| Sat 13-14 | 3,709 | 7.4 min | 0.12 | 2.6% |

Three more runs of `all`, each started by nothing but a code change (the code each stage runs
is part of its key), rebuilt every stage from prep on in 31-34 min. Their tables, mask, points
and walksheds are byte-identical to the first run's, and the rescored quick look below is the
same each time. The prepared feed zips differ in their bytes (zip timestamps), so a re-prep always
rebuilds the network and the tables.

- **Sizes:** each table is 3,716 × 3,716 uint8 (13.8 MB); peak memory 3.1 GB.
- **Fingerprints:** each table has a `tt_*.fingerprint.json` with the settings, code, points,
  feeds, OSM clip and departure it was built from, and whether every origin is done. A table
  made from anything else is started again, never resumed. The manifest takes its settings
  from these, and refuses an incomplete table.
- **Walksheds:** the access points of the runtime formula. Each of the 640 stations reaches
  the H3 res-10 cells within 2 km (45 min on foot), and each of the 2,939 usable table cells
  reaches the cells within 1 km. A cell's own table row carries its buses, so a street point
  gets bus access through its nearby cells. 1,172,359 walks in 69 s. Cell centres are
  snapped like table points: 10,319 of 158,821 cells have no street within 150 m (water,
  large parks, runways) and are left out.
- **Archive:** `transit-london-2026-11-12.tar.xz` (30.3 MB) holds the four tables, the
  points, the mask, the walksheds, the manifest and `LICENSE-DATA.txt`. Until the licences
  are cleared, that file says the data is not licensed for publication. It is not published.
- **Manifest:** `valid_until` is 2026-12-12, the day before the National Rail timetable
  change.

Checks on the rebuilt tables:

- The diagonal is 0 for usable points. The 137 masked points are 255 in every row and
  column, their diagonal too. No time is above 179 min.
- The Wembley Park → Baker Street cell equals the go/no-go figure in every window (22, 20,
  19, 20).
- **Asymmetry:** the median of |T[i,j] − T[j,i]| is 2-3 min.
- **Triangle check:** T[i,k] > T[i,j] + T[j,k] + 5 min in 0.01% of 200,000 random triples.
  Window medians need not obey the rule (T[j,k] is measured from the window's start, not from
  when you reach j). An earlier record gave 0.6-0.9%; that could not be reproduced.
- **Against the first plain tables (2026-11-12):** 99.94-99.95% of usable cells are
  identical. The rest are mostly faster now (median 3-6 min). The likely cause is that prep now
  keeps the newer version of a doubled bus trip.

## Gate

**Size (owner decision, 2026-11-12):** the first design asked TfL about every station for
every ranking group, about 145,000 calls. The owner meant "all stations" as all stations in
the results, so TfL is asked only about contenders, and the error bands use one sample:

| Part | Calls |
|---|---|
| Error bands | ~7,900 (4 windows × 3 bands × ~110 pairs × 6 departures) |
| Ranking | ~9,900 (contenders within 8 min of the table's best, median 17 per group, 3 departures) |
| Hand-picked cases | 84 |
| **Total** | **~18,000, about 3 hours at 100 a minute** |

A 20-min margin would have meant 134 contenders per group and 115,000 calls.

**How it samples and rates:**

- **Error bands:** pairs are drawn by distance for each band and banded by TfL's time. A pair
  the table cannot route but TfL can is an infinite error, so a missing connection counts.
- **Ranking:** TfL is asked about the stations within the margin of the table's best, and
  always the table's best 5. A group is rated only when TfL has a time for every person to
  every contender ("complete"). A group with a contender TfL has no time for is
  "incomplete", and one with under 3 timed contenders is "not tested"; neither passes. TfL's
  best is the best among the contenders asked, not among all stations.
- **Hand-picked cases** are listed "to explain"; the pass rule is the error bands, the
  ranking and the three go/no-go journeys.
- A quick look is labelled as one, and never sets the gate's pass flag.
- Every run is scored on the issue's lines and on the accepted lines (see "Decision"); the
  verdict uses the accepted ones.

### Provisional quick look, without a key (2026-11-12, 22:38-23:46)

The TfL API portal sent no confirmation email, so there is no key yet. This is a known
Microsoft-side fault, per TfL staff on
<https://techforum.tfl.gov.uk/t/cant-register-on-the-tfl-api-portal/6248>. A one-hour quick
look ran instead, on the first plain tables (2026-11-12).

- **Settings:** `--provisional`, sample A, 35 requests a minute without a key.
- **Sample:** 30 pairs per band, departures at 0, 20 and 40 min into each window, 12 ranking
  groups, a 5-min ranking margin.
- **Calls:** 2,329, with no TfL errors.
- **Drawn the old way.** A pair was kept only when the table's own time fell in the band
  being filled. A pair the table could not route was never asked about, and a short hop the
  table made far too slow could not enter any band. Read its errors as optimistic.

### The revised tables, rescored from the cache (2026-11-13)

The quick look's pairs and ranking groups, scored again on the rebuilt tables from the cached
replies: `python -m transit.gate --build 2026-11-12 --provisional --rescore
gate/a-provisional/report.json`, 1,005 replies from the cache, 0 sent.

**Result: pass on the accepted lines, fail on the issue's lines** (see "Decision"). Median /
p90 absolute error, table minus TfL, with the median signed error in brackets; the issue's
lines median / p90 in the header:

| Window | ≤20 (2/5) | 20-45 (3/7) | >45 (4/10) |
|---|---|---|---|
| Wed 08-09 | 2.0 / 5.0 pass (+2) | 5.0 / 10.0 (−3.5) | 6.0 / 14.0 (−6) |
| Wed 13-14 | 2.0 / 6.0 (+1) | 6.0 / 10.0 (−6) | 6.0 / 15.0 (−6) |
| Wed 19-20 | 1.5 / 3.0 pass (+1) | 4.5 / 12.0 (−4) | 5.0 / 11.0 (−5) |
| Sat 13-14 | 1.0 / 4.0 pass (+1) | 5.0 / 10.0 (−4.5) | 5.5 / 13.0 (−5) |

On the issue's lines 3 of 12 cells pass (the first plain tables gave the same 3);
on the accepted lines all 12 do. 30-46 pairs per cell.

**By kind of pair** (all windows; median / p90):

| Pairs | ≤20 | 20-45 | >45 | Median signed |
|---|---|---|---|---|
| Street to street (neither end a station), n = 215 | 2.0 / 3.0 | 2.0 / 6.0 | 5.0 / 14.0 | 0 |
| A station at one end or both, n = 226 | 2.0 / 6.0 | 6.0 / 11.0 | 7.0 / 13.0 | −5 |
| Origin at a table point, n = 266 | 2.0 / 5.0 | 5.0 / 11.0 | 6.0 / 14.0 | |
| Origin at a street point, n = 175 | 2.0 / 5.0 | 4.0 / 8.0 | 5.5 / 13.0 | |

**Ranking:** on the issue's lines 5 of 12 groups pass, 11 needed; on the accepted lines it
passes. 9 groups are complete and 5 of them pass; regret in the other 4 is 4-10 min. 1 group
is incomplete and 2 are not tested. On the first tables, under the old rules, the quick look
counted 7 passes: two of them came from groups of 2 and 4 contenders, which now have to include
the table's best 5 and lack TfL times for some. TfL's best sat near the margin's edge in 1
group.

**Hand-picked cases:** 11 pass, 2 fail, and the 22:30 case is reported only.

- **Leicester Square → Covent Garden:** 4 against TfL's 7.
- **Cheshunt → Liverpool Street:** 76 composed against TfL's 44 (R5 directly: 34). Cheshunt
  station is outside the table's points (Greater London + 2 km), though inside the streets
  and feeds (+ 5 km). Its nearest table station is Theobalds Grove, a 28-min walk, so the
  composed time is that walk plus the ride. R5 run directly boards at Cheshunt. An earlier
  record blamed missing walksheds or buses; that was wrong.
- **22:30 Wembley Park → Baker Street**, composed from the 19:00 table: 21 against TfL's 27.

**What it shows:**

1. **Street to street, the tables are unbiased** (median signed 0) and pass the ≤20 and
   20-45 lines; over 45 min they miss both (5.0 / 14.0 against 4 / 10).
2. **At a station end the table is 5 min faster than TfL.** Part of that is Journey Planner
   walking from the nearest street address when given a coordinate at a station (see
   "Go/no-go"), not a fault in the tables. Asking TfL by station id at that end would tell
   the two apart.
3. **Longer trips are too fast** (−3.5 to −6 at 20-45 min, −5 to −6 beyond). The likely
   cause is changing trains: R5 counts a change as a street walk between stops that share one
   coordinate per station, 0 min plus a 1-min boarding allowance, while Journey Planner uses
   real interchange times. The bundled R5 can read GTFS transfers (`GTFS_ONLY`, `STOP_PAIR`),
   but r5py hard-codes `OSM_ONLY`, and gb-transit's `transfers.txt` mostly lists
   stay-on-the-same-train links.
4. **Street-point origins do no worse than table-point origins**, so the walksheds and the
   composition are not the cause.
5. **Walking speed alone is not the fix.** At 3.3 km/h the longer bands came close (20-45:
   4.0 / 9.0; >45: 4.0 / 11.0) but short trips got worse (median +3).
6. The ≤20 band is mostly walking: the straight walk answers 66 of its 121 pairs.

### Calibration experiment (2026-11-13; not kept)

With the owner's go-ahead and no new TfL calls, `python -m transit.calibrate` re-routed the
quick look's 441 pairs under two extra costs and scored them against the cached replies:

1. **Station access time** (`transit.r5.station_access`): metres added to the street links of
   the 1,731 rail, tube and tram stops.
2. **A cost per ride,** from R5's fastest time with at most k rides: time = min(walk, min over
   k of T_k + cost × k). The result is an upper bound: k is limited to 1-5 and 8, so 6 and 7
   rides pay for 8, and the minimum of window medians is never below the median of the
   per-minute minimum.

300 m and +1 min a ride scored 10 of 12 cells on R5 run directly, and the tables were rebuilt
with it. A closer look then found three things, and the tables of record went back to plain R5:

- **It fitted Journey Planner's habit, not station access.** Split by kind of pair, the plain
  tables were already unbiased street to street; the calibration made those pairs worse
  (median signed 0 → +2, 20-45 p90 6 → 9). What it corrected was the −5 at station ends,
  which comes largely from how Journey Planner treats a coordinate at a station.
- **It made the three journeys worse:** +11.5, +9 and +7 station to station, against +6.5,
  +4 and +2.
- **It was half applied.** R5's egress uses distance tables built with the network, so the
  longer links were charged on the way in only. Fixed: `station_access` now rebuilds those
  tables, and 300 m alone moves Wembley Park → Baker Street from 22 to 30, about both ends'
  worth at 4 km/h (4.5 min each). The calibrated tables, with 300 m and +1 min a ride, had it
  at 27.

It was also fitted and scored on the same pairs, and its 7 of 12 cells after the rebuild came
from those pairs. The calibrated tables, their rescore and the calibration's report are kept
in `transit/out/2026-11-12/experiments/`. Both costs stay available
(`TRANSIT_STATION_ACCESS_M`, `TRANSIT_RIDE_COST_MIN`), off by default; `station_access`
refuses any R5 but 7.5.1 and checks its own effect.

## Review and revision (2026-11-13)

Checking the first build against its own outputs found, besides the calibration:

- **Rebuilds could keep stale tables.** A changed setting, point list or feed left the old
  rows in place, `--force` did not restart them, and the manifest recorded the live settings.
  Now each table carries a fingerprint (above), stage keys include the code each stage runs,
  and a pilot (`--limit-origins`) is recorded as incomplete and cannot be packaged.
- **The gate's sample and ranking were too kind.** Pairs were picked on the table's own
  band, ranking groups with one contender passed by construction, and a rescore counted
  unasked contenders as "no time". Fixed as described under "Gate"; the saved quick look is
  rescored on its own groups.
- **Composition was station-only,** so a street point lost its buses, and walkshed cells in
  water got times from the bank. Cells are access points now, and walkshed cells are snapped.
- **Cut-off points were usable,** and masked points had a 0 diagonal. Fixed.
- **Provenance:** a reused download could be relabelled as another release (now refused);
  the timetable change was hard-coded and would have stopped the monthly rebuild after
  29 November (now a list); `.env` was not read in the transit environment (python-dotenv is
  pinned); the archive granted ODbL before the rail and TfL terms were read (it now says not
  licensed, and `publish` refuses, until `[publication] licences_cleared` is set).
- **Smaller:** BODS de-duplication now keeps the newer version; sentinels need a minimum
  number of trips; the age of gb-transit's TfL copy is checked; the hand-picked cases that
  could never pass were fixed; strict option parsing; exact counts; seeds by CRC32.
- **On the PR, an automated review found eight more paths to stale outputs,** all fixed. A
  new table could keep an old checkpoint; packaging trusted the manifest after a later pilot;
  the manifest compared only settings across tables; stage keys left out options (`--redate`)
  and the region file; `fetch` could not recover a stale OSM clip; a file placed by hand was
  refused on the second run; a rescore could change its scope or meet a renumbered point list.

## Decision (owner, 2026-11-13)

**Go: the tables are kept as a rough estimate, scored on accepted lines, to be revisited.**

- **Accepted lines.** The owner accepted that the estimates are not exact and set the gate's
  lines to what the plain tables meet: the worst window on the quick look, rounded up, plus
  about one sampling standard error. Every report also gives the verdict on the issue's lines.

  | | Issue's lines | Accepted lines | Quick look, plain tables (worst window) |
  |---|---|---|---|
  | ≤20 min, median / p90 | 2 / 5 | 3 / 8 | 2.0 / 6.0 |
  | 20-45 min | 3 / 7 | 7 / 14 | 6.0 / 12.0 |
  | >45 min | 4 / 10 | 7 / 17 | 6.0 / 15.0 |
  | Three journeys (station to station) | within 3 min | within 8 min | +6.5 |
  | Ranking | 45 of 50 groups within 3 min | half the complete groups within 3 min, none over 11, two thirds of groups complete | 5 of 9 within 3, worst 10, 9 of 12 complete |

  **On the accepted lines the quick look passes; on the issue's lines it fails.**
- **The tables of record are plain R5,** not the calibrated tables first kept, for the
  reasons under "Calibration experiment".
- **The pieces work.** The laptop builds London (3-6 min and 3.1-4.7 GB for the network;
  31-40 min for everything), and one script builds the tables, walksheds and manifest, the
  same to the byte each time it runs.
- **What the numbers mean for the app:** street to street the tables are unbiased; trips over
  20 min come out about 5 min too fast, with a spread of up to 15 min (p90) over 45 min. Good
  enough to bend a map and suggest a meeting area; not for promising a journey time.
- **The full gate has not run.** That needs 100 pairs per band, departures every 10 min, 50
  groups and a TfL key (or a slow keyless run), and a fresh sample: the accepted lines were set
  on the quick look's pairs. Journey Planner plans only current and future dates, so for this
  build it has to run before 2026-12-09; otherwise rebuild for a later week first.
- **Left as they are:** the hand-picked cases stay outside the pass rule (they are listed "to
  explain"); the gate keeps drawing half its origins at table points and half at street points
  (street points do no worse); the bus trips running within 2 min of another on the same route
  are kept until checked against TfL's timetable.

**Next, when this is revisited** (tracked in #13):

1. Run the full gate (sample A), asking TfL by station id at station ends, which separates
   Journey Planner's walk from the nearest street address from real error.
2. Model interchange time: per-station or per-pair transfer times, if a source turns up.
3. Extend the table's stations to the 5 km feed area (Cheshunt), and add the South East BODS
   buses for the buffer's southern edge.
4. Check the near-duplicate bus trips (370, 407, 216, K3, 406) against TfL's timetable.
5. Before publishing anything: read the agreement that covers the rail timetable (only RSP's
   template is read; its clauses are in [transit-sources.md](transit-sources.md)) and TfL's
   derived-data terms, and settle the clause 3.3.2 notice. Only derived tables may ever be
   published, never the source timetables. Run the UK2GTFS check.
6. Tighten the accepted lines as the tables improve.

## UK2GTFS on TfL's Underground files

Not run yet (needs Docker and about 5 GB of disk for the R image): `transit/uk2gtfs/`.
