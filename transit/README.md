# London transit tables

Mapbox has no public transport, so for London the warp map and the meetup finder need
travel times from somewhere else. This folder builds them offline with
[R5](https://github.com/conveyal/r5) (through [r5py](https://r5py.readthedocs.io/)) and checks
them against TfL Journey Planner (issue #1, T-01).

One command builds everything:

```bash
transit/.venv/bin/python -m transit.build all
```

It writes, per build, into `transit/out/<build>/` (git-ignored):

| File | What it is |
|---|---|
| `points.csv` | The table points: stations at their real positions, then H3 res-8 cell centres over Greater London + 2 km. Columns: idx, kind, id, name, lon, lat, inside_gl, snap_lon, snap_lat, snap_m, ok |
| `tt_wd08.npy`, `tt_wd13.npy`, `tt_wd19.npy`, `tt_sa13.npy` | N×N uint8 tables, whole minutes, row = from, column = to. Median over every departure minute in the window (Wednesday 08-09, 13-14, 19-20; Saturday 13-14). **255 = no time within 180 min** (R5 can't say whether it is over the limit or has no route). Diagonal 0 for usable points |
| `tt_*.fingerprint.json` | What each table was built from (settings, code, points, feeds, OSM clip, departure) and whether it is complete. A table made from anything else is rebuilt, never resumed |
| `point_ok.npy` | False where a point could not be snapped to a street, or is cut off from the network (it reaches, and is reached by, under 10% of the others in every window); its row and column, diagonal included, are 255. `points.json` lists them |
| `walksheds.npz` | Access points and the H3 res-10 cells reached from them on foot: each usable station to the cells within 2 km, each usable table cell to the cells within 1 km (a cell's row carries its bus access). Cell centres with no street within 150 m (water, large parks) are left out (`points`, `indptr`, `cells`, `minutes`; see `transit/tables.py`, `Walksheds`) |
| `manifest.json` | Service dates, every input's URL, version, sha256 and "timetable as of" date, every routing setting, versions, output hashes, `valid_until` |
| `feeds-check.md`, `gonogo.md`, `gate/<sample>/report.md` | The feed audit, the go/no-go and the gate |

Load a table with `numpy.load(path, mmap_mode="r")`. The runtime formula (time from any point
p to a table point j = the faster of walking straight, or walking to a nearby access point k,
a station or a table cell, plus T[k, j]) is `transit.tables.compose_row`; it needs only numpy
and h3.

## Accounts and keys

The build itself needs **no account**: gb-transit (GitHub), Bus Open Data Service (the London
GTFS downloads without a login), Geofabrik, TfL's timetable zip and GOV.UK bank holidays are
all open downloads. **Only the gate needs a key**, the free TfL API key, because it asks TfL
Journey Planner about 18,000 journeys (`--estimate` gives the number for a build). The
go/no-go's 50 or so calls work without a key.

### TfL API key (free, about 10 minutes)

1. Open <https://api-portal.tfl.gov.uk/> and choose **Sign up**. Fill in the form and
   confirm the email TfL sends you.
2. Sign in, open **Products** and choose the free product with the higher limit (shown as
   "500 Requests per min" when this was written). Choose **Subscribe** and give the
   subscription a name, for example `time-map`.
3. Open your **Profile**. Under **Subscriptions**, choose **Show** next to the primary key and
   copy it.
4. Put it in the repo's `.env` file (git-ignored), on its own line (python-dotenv is in
   `transit/requirements.txt`; or `export TFL_APP_KEY=...` instead):

   ```
   TFL_APP_KEY=paste-the-key-here
   ```

5. Check it: `transit/.venv/bin/python -m transit.build check-keys` prints `TfL key OK`. The key
   is never printed, logged or stored in the cache.

The portal's labels may have changed since this was written; the steps are the same idea.

**If no confirmation email arrives** (and you then can't sign in): this is a known fault on
TfL's side. On 27 Apr 2026 TfL staff wrote on the tech forum
(<https://techforum.tfl.gov.uk/t/cant-register-on-the-tfl-api-portal/6248>) that "an
intermittent issue on Microsoft's side" stops some emails from API Management, and that it
had been raised with Microsoft. No fix had been posted by 2026-11-12. Try again later or with
another address.

**Without a key** the API still answers, at a lower limit (third-party guides say 50 a minute;
TfL has not confirmed it). The gate then warns and runs at 35 a minute
(`TFL_RATE_PER_MIN_ANON`). Run the one-hour quick look meanwhile:

```bash
transit/.venv/bin/python -m transit.gate --build <id> --provisional
```

Replies are cached without the key, so a later keyed run reuses every reply for the requests
it shares (the same pairs, departures and dates).
TfL's terms for the data are at <https://tfl.gov.uk/corporate/terms-and-conditions/transport-data-service>.

### Only if needed later

- **Bus Open Data Service** (<https://data.bus-data.dft.gov.uk/>, free): only if the London file
  stops downloading without a login. Create a consumer account, then either save the London
  GTFS from "Download all data" as `transit/data/raw/<build>/bods/itm_london_gtfs.zip`, or put
  the account's API key in `.env` as `BODS_API_KEY`.
- **Transitland** (<https://www.transit.land/>, free key as `TRANSITLAND_API_KEY`): only if the
  rail data's licence does not allow publishing derived tables. Interline's UK rail GTFS on
  Transitland allows derived products with the credit "Powered by National Rail Enquiries".
- **Rail Data Marketplace** (<https://raildata.org.uk/>, free): only to read the Rail Settlement
  Plan agreement that covers the free timetable. Only RSP's licence template has been read
  (`docs/transit-sources.md`).

## Setup (once)

macOS on Apple silicon, as used for the spike (M2, 16 GB):

```bash
brew install osmium-tool
uv venv --python 3.12 transit/.venv
uv pip install --python transit/.venv/bin/python -r transit/requirements.txt
# JDK 21 (r5py needs 21 or later; 27 is untested). Unpacked inside data/, no admin rights:
mkdir -p transit/data/tools/jdk21
curl -L "https://api.adoptium.net/v3/binary/latest/21/ga/mac/aarch64/jdk/hotspot/normal/eclipse" \
  | tar -xz -C transit/data/tools/jdk21 --strip-components 1
```

The tables are plain R5. Two experimental costs exist and are **off by default**
(`TRANSIT_STATION_ACCESS_M`, `TRANSIT_RIDE_COST_MIN`; `python -m transit.calibrate` fits them
on a gate sample's cached replies):

- **`station_access_m`.** Metres added to the street links of rail, tube and tram stops,
  charged on the way in and the way out.
  - It edits R5 7.5.1's in-memory network and refuses any other R5 version.
  - It rebuilds R5's egress distance tables, which takes about 2.5 min.
- **`ride_cost_min`.** Minutes per ride.
  - The result is an upper bound: the cost is applied to window medians, and ride limits are
    1-5 and 8.
  - It makes the table build about 7× slower.

A 300 m / +1 min fit turned out to match Journey Planner's habit of walking from
the nearest street address, not real station access (`docs/transit-spike.md`).

`transit/settings.py` uses `JAVA_HOME` when it is set, and otherwise the JDK under
`transit/data/tools/jdk21`. The JVM heap is 10 GB (`TRANSIT_MAX_MEMORY=12G` to raise it).
Matrices run on `TRANSIT_THREADS` threads (default 1).

Disk: about 2 GB for the England extract (deleted after it is clipped), about 1 GB of feeds,
1-2 GB of R5 cache, under 1 GB of TfL replies. `fetch` stops if less than 8 GB is free.

## Stages

```bash
python -m transit.build <stage> --build <id>      # id defaults to today's date
```

| Stage | Does |
|---|---|
| `fetch` | Downloads the inputs into `data/raw/<id>/` with sha256 and Last-Modified (`--with-tfl-txc` also gets TfL's TransXChange zip) |
| `dates` | Picks the Wednesday and Saturday: at least 10 days ahead, no bank holiday, every feed running, fewest closures. Keeps an existing `dates.json` unless `--redate` (the gate's cached replies are for those dates) |
| `prep` | Keeps only services on those days inside Greater London + 5 km, and one copy of a doubled trip (the newer timetable version); clips the OSM extract with osmium (again if the polygon or extract changed) |
| `check` | The feed audit: missing lines (with minimum trip counts), doubled service, timezones, TfL's own line list, near-duplicate bus trips (reported), the age of gb-transit's TfL copy (fails past 21 days; `--allow-stale-tfl`). `--allow-missing` carries on |
| `network` | Builds the R5 network (cached by r5py between runs); records time and peak memory |
| `points` | Stations and H3 cells, snapped to the street network |
| `journeys` | The go/no-go: three hand-checked journeys, an interchange probe, a walking-speed check |
| `tables` | The four tables (`--window`, `--limit-origins` for a pilot, `--threads`); resumes a table only if its fingerprint matches, restarts it otherwise or with `--force`; then masks points cut off from the network. A pilot leaves the stage incomplete |
| `walksheds` | Station and cell walksheds |
| `manifest`, `package` | `manifest.json` (refuses incomplete tables; records the settings the tables were built with); `transit-london-<id>.tar.xz` with `LICENSE-DATA.txt` |

`all` runs them in order and skips a stage whose inputs, settings and code have not changed.
`--force` reruns every stage (the dates are still kept without `--redate`).

## Gate

```bash
transit/.venv/bin/python -m transit.gate --build <id> --estimate     # how many TfL calls, none sent
transit/.venv/bin/python -m transit.gate --build <id>                # sample A: the gate
transit/.venv/bin/python -m transit.gate --build <id> --sample b     # only after fixing something
```

`--rescore gate/<run>/report.json` scores a previous run's pairs and ranking groups again with
the current tables, from cached replies only (no request is sent): use it after changing the
model. A contender the new tables add, which the earlier run never asked about, makes its group
"incomplete" rather than a pass. A rescore keeps the saved run's sample, departures, margin and
quick-look status (a conflicting option is refused), and refuses a run whose point indexes no
longer name the same points.

`--provisional` is a one-hour quick look for when there is no key: 30 pairs per band
instead of 100, departures every 20 minutes instead of 10, 12 ranking groups instead of 50,
a 5-minute ranking margin. About 2,200 calls at 35 a minute. Its report says "Quick-look
result", never the gate's result.

How pairs are drawn: per window and band, a seeded stream draws an origin (half table points,
half random street points) and a destination at a distance typical of the band. The pair is
kept whatever the table says, including "no time", and is banded by TfL's answer, so the
sample does not depend on the tables it scores.

Sample A is the gate. If it fails and something is fixed, sample B (fresh pairs and groups)
is the one reported, so nothing reported was tuned on the pairs it is scored on. If A passes
untouched, A is reported.

What it asks TfL, at most 100 requests a minute (`TFL_RATE_PER_MIN`):

- **Hand-picked cases** (`cases.toml`): about 100 calls.
- **Error bands:** at least 100 pairs per band per window, six departures each: about 9,000
  calls.
- **Ranking:** 50 groups of three. The table ranks every station; TfL is asked only about
  the contenders, whose table longest trip is within 8 minutes of the table's best
  (`--margin`), and always the table's best 5, at three departures each (10, 30 and 50
  minutes into the window).
  - A group is rated only if TfL has a time for every contender. An incomplete or untested
    group (fewer than 3 timed) counts as not passed.
  - "TfL's best" means best among the contenders asked.
  - The report says how often TfL's best station sat near the edge of the margin. If often,
    rerun with a wider `--margin` (cached replies are reused).

`--estimate` prints the number of calls for a build (about 18,000, 3 hours, on 2026-11-12).

It must run before the build's service dates pass: Journey Planner only plans current and
future dates. Every reply is cached in `data/cache/tfl.sqlite3`, so a stopped gate resumes
without asking again and no request is sent twice.

Every run is scored on two sets of lines (median / p90 absolute error in minutes, in every
window), and the report gives both verdicts:

| | Issue's lines | Accepted lines (verdict) |
|---|---|---|
| ≤20 min | 2 / 5 | 3 / 8 |
| 20-45 min | 3 / 7 | 7 / 14 |
| >45 min | 4 / 10 | 7 / 17 |
| Three go/no-go journeys | within 3 min | within 8 min |
| Ranking | 45 of 50 groups within 3 min of TfL's best (a proposal) | half the complete groups within 3 min, none over 11, two thirds of groups complete |

The accepted lines are the owner's decision of 2026-11-13: the tables are a rough estimate.
Each is the plain tables' worst window on the first quick look, rounded up, plus about one
sampling standard error (`settings.ACCEPTED`). Tightening them is part of revisiting the
tables.

## Monthly rebuild

`python -m transit.build all --build <new id>`, then the gate, about once a month and in any
case before `valid_until` in the manifest. That is the earliest of:
- the feeds' calendar end;
- 35 days after the build;
- the day before the next National Rail timetable change after the service dates.

The changes are listed in `settings.TIMETABLE_CHANGES`; check new ones against Network Rail's
calendar.

## Publishing

**Only derived tables may ever be published, never the source timetables.** The feeds, the
OSM extract and the prepared feeds stay in `transit/data/`; RSP's licence (clause 3.3.3)
forbids passing the rail timetable on. `.gitignore` keeps feed, extract and table files out
of git anywhere in the repo, and `tests/test_no_data_in_git.py` fails if one is tracked.

`python -m transit.build package` makes the archive of derived tables. Until
`[publication] licences_cleared` is set to true in `sources.toml`, its `LICENSE-DATA.txt`
says it is not licensed for publication, and `publish` refuses. Set the flag only after the
licences in `docs/transit-sources.md` allow publishing derived tables and the owner has
agreed. Then `python -m transit.build publish --confirm` makes a **draft** GitHub release.
Published, the tables are derived from OpenStreetMap and go out under the ODbL, with the RSP
notice of clause 3.3.2 (worded as agreed with RSP, if RSP asks for one) and every credit:

- Powered by TfL Open Data
- © OpenStreetMap contributors
- Contains public sector information licensed under the Open Government Licence v3.0
- Timetable data from Rail Delivery Group (Rail Settlement Plan data licence)

## UK2GTFS check

`transit/uk2gtfs/` holds the one-off check of UK2GTFS on TfL's Underground TransXChange (the
issue's question). It runs in Docker and is not part of the build. **It has not been run**
(needs Docker and about 5 GB of disk).
