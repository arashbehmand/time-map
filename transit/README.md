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
| `venues` | Named pubs, bars, cafés, restaurants and parks (0.5 ha or more) from the OSM clip, for the meetup (#19): `venues.csv` and `venues.json` (osmium; no Java). Runs on its own too: `python -m transit.venues --build <id>`, on any build that still has its clip |
| `manifest`, `package` | `manifest.json` (refuses incomplete tables; records the settings the tables were built with; lists the venue files when there are any); `transit-london-<id>.tar.xz` with `LICENSE-DATA.txt` (and the venue files) |

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

`python -m transit.build package` makes the archive of derived tables.
`[publication] licences_cleared` in `sources.toml` is true: the owner holds the rail and TfL
licences (confirmed 2026-11-13, `docs/transit-sources.md`). So the archive's `LICENSE-DATA.txt`
grants the ODbL with every credit, and `python -m transit.build publish --confirm` makes a
**draft** GitHub release. With the flag false, the file says the data is not licensed for
publication and `publish` refuses.
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

## Stations-only layout (#6, L-01)

`transit/layout/` places every station so that the distance between two stations on the map
follows the travel time between them, with a weak spring pulling each station back to its real
position. Solving it for a sweep of spring strengths gives a slider from the real map to the
pure time map. It is numpy only (no Java, no scipy) and runs in the app's environment or this
one:

```bash
TRANSIT_OUT=<folder holding the build> python -m transit.layout --build 2026-11-12   # --window wd13
```

It reads `points.csv`, `point_ok.npy`, `tt_<window>.npy`, that table's fingerprint,
`manifest.json` and `transit/layout/checks.toml` (`--checks`). It writes to
`transit/out/layout/<build>/<window>/` in this checkout, beside the builds and never inside one,
whatever `TRANSIT_OUT` is.

It stops with exit code 2, having written nothing, when:

- the table is not the one the manifest lists (sha256, shape), is incomplete, has a fingerprint
  the manifest doesn't record or a value from 181 to 254, or `points.csv` is not the one it was
  built from;
- the checks file can't be read or isn't shaped like `checks.toml`;
- fewer than 32 stations are joined both ways (neighbourhoods of 10 need more), or the scale is
  not a positive number;
- any solve stops at the iteration cap or raises the objective;
- the output folder is under `static/` or `server/` (the app serves or ships them), is inside the
  repo where git would track any file written, or holds a symlink where a file would go;
- the viewer would be over 2 MB gzipped.

| File | What it is |
|---|---|
| `index.html`, `viewer.js`, `viewer.css`, `layout.js` | The viewer: open `index.html` from disk. Stations coloured by leftover stress, a few names, the Metropolitan line on request, and a slider over the solved layouts |
| `layout.json` | Nodes, every frame (the real map first, then each λ) in the renderer's local frame (`static/shared/mesh/frame.js`: metres around Charing Cross, x east, y south), node stress, per-frame metrics, the triangles folds are counted on |
| `report.md`, `report.json` | The verdicts first, then one row per frame |
| `shepard.svg` | Map distance (÷ s, in minutes) against travel time, real map beside the pure time map |
| `pairs.npz` | The directed minutes, the times used and the mask, per node |

**How it is computed.**

- **Nodes:** stations that are usable in both `points.csv` and `point_ok.npy`, at their real
  positions. Stations closer than 50 m merge (single linkage) into one node at their mean
  position, whose time to another node is the fastest member-to-member time.
- **Pairs:** no self-pairs; a pair is used only if it has a time both ways (255 is "no time" and
  never a length). t is the mean of the two directions, floored at 0.5 min; w = t⁻². Only the
  largest group of nodes joined by used pairs is laid out; the report and the viewer name the
  stations of every group left out.
- **Scale:** s = median real distance ÷ median t over the used pairs, fixed once per build.
- **Objective:** Σ w (map distance − s·t)² + λ Σ (distance to the real position)², solved by
  weighted SMACOF (`smacof.py`). Each step solves the majorizing quadratic, so the objective
  can't rise: a step that would raise it is not taken, and one that would raise it beyond float
  noise is a bug that stops the job. λ = λ̃ × the mean of each node's summed weights; λ̃ runs over
  24 values from 30 down to 0.001, then 0, each solve starting from the previous layout. Every
  layout is aligned to the real map by rotation and shift only (with an anchor, the solution is
  already aligned, so this moves it by under a metre; at λ = 0 it fixes the free position).
- **Restart check:** λ = 0 is solved again from classical MDS and two random starts; the report
  flags any that ends more than 0.1% lower than the sweep. A start that stops at the iteration
  cap is marked and the check called incomplete; one that raises the objective stops the job.
- **Normalised stress σ₁:** √(Σ w (map distance − s·t)² ÷ Σ w (s·t)²) over the used pairs.
  A station's leftover stress (its dot's colour) is the same ratio over its own pairs.
- **Trustworthiness and continuity** at k = 10 (Venna & Kaski), time against map distance,
  whole-minute ties broken by tiny seeded jitter, averaged over 5 seeds, scored over the stations
  with at least 10 neighbours that have a time. The proposed pass line is 0.9 at λ = 0; the real
  map's own score is printed beside it.
- **Folds:** a Delaunay triangulation of the real positions. A fold is a triangle whose
  orientation flips, or two boundary edges that cross. The report gives how far the map stays
  fold-free going from the real map toward λ = 0, on the full triangulation and on the one with
  thin or long boundary triangles (smallest angle under 10°, or longest edge over 4× the median)
  peeled off; any fold-free frame found again after a fold; and how far at most 1% of the area
  is folded.
- **Wembley Park:** r = map distance ÷ travel time. The check passes when Wembley Park to Baker
  Street's r is within σ₁ (a relative error) of the median r over all used pairs. Reading "within
  the build's stress" as σ₁ is this job's choice.
- **Metropolitan line:** `checks.toml` lists the line end to end, Aldgate to Uxbridge and Aldgate
  to Rickmansworth. Following each in order, a break is a station nearer the first station than
  the one before it, or further from the last; the line turning back where the real line turns
  by at most 90°; or the line crossing itself. Departures under one map-minute (s metres) are
  counted as small. Ids missing from a build are listed as absent, and a run with fewer than two
  of its stations in a build is reported as not tested.

**Local use only.** Everything it writes is derived from the tables, so it stays in the
git-ignored output folder: never in git, never under `static/`, never on the site. That is #6
L-01's rule whatever the build's licence; the report and the viewer show the licence status from
the build's own manifest.

## Full London time map (#6, L-02)

`transit/layout/timemap.py` lays the whole of London out by travel time on a spring grid that
can't fold, carries the land along, and writes what the app's Whole city page (#28,
`static/whole-city/`) draws with the mesh renderer (`static/shared/mesh/`). It runs in this
folder's environment (shapely 2.1 or later and the `osmium` tool for the land; the solve itself is
numpy only) and needs no Java and no routing:

```bash
TRANSIT_OUT=<folder holding the build> TRANSIT_DATA=<its data folder> \
  transit/.venv/bin/python -m transit.layout.timemap --build 2026-11-12    # --window wd13, --no-land
TRANSIT_DIR=transit/out/2026-11-12 TIMEMAP_DIR=transit/out/timemap/2026-11-12 \
  TRANSIT_ALLOW_UNCLEARED=1 uvicorn server.app:app                         # then open /whole-city/
```

The app serves the folder's files from `/api/whole-city/<window>/<file>` only while the tables it
was made from may be served to the same request (the build id and the window's table sha256 must
match the loaded `TRANSIT_DIR`; see the main README's "Whole city"). The page makes no routing and
no tile requests.

It reads what the stations-only job reads, plus, from `$TRANSIT_DATA/work/<build>/`, the prepared
rail feed `gbt.gtfs.zip` (which stations trains join) and the OSM clip `london.osm.pbf` (the
land), each checked against the manifest's sha256. Nothing is rebuilt. It writes to
`transit/out/timemap/<build>/<window>/` in this checkout. The train links are read for the
window's own service date and hour, so any window runs; weekday 13:00 (wd13) is the one built
and reviewed.

It stops with exit code 2, having written nothing, when:

- any of the stations-only job's input checks fails, or the feed or the clip is missing or not the
  file the manifest describes;
- the checks file isn't shaped like `checks.toml` (its `[[ratio]]` and `[[required_link]]` tables
  included), a `[[required_link]]` names a station that isn't in the layout, or isn't in the feed
  (stops joined to stations wrongly);
- the clip lacks one of London's 33 boroughs, or `osmium` isn't installed (`--no-land` skips the
  land);
- a solve doesn't converge, or raises its energy, or the stations-only layout solved again for the
  comparison fails the same way (its own job would refuse it);
- any triangle of any slider position is flipped, or the guard ring has moved, in the coordinates
  as shipped (whole decimetres) or in their Float32 image; or any mesh triangle folds anywhere along a
  blend between neighbouring keyframes;
- the page would be over 2 MB gzipped at every rung of the land ladder (the data with the app's
  Whole city page and every module it loads: what a visitor downloads);
- the output folder is under `static/` or `server/`, inside the repo where git would track a file
  written, or holds a symlink where a file would go.

| File | What it is |
|---|---|
| `timemap.json`, `land.bin` | What the app's Whole city page draws: the nodes, the mesh, the train links, every position in whole decimetres with its measures, the in-between keyframes (`motion`); the land cut along the mesh. The page (`static/whole-city/`) draws the land through the mesh renderer (cpu back-end, a target per vertex), the stations and names on a canvas above (the Metropolitan line is carried in `lines` for the report; the page no longer draws it apart from the other rail, #23), a slider that runs continuously through every blend, and a "Time map" button that plays the move |
| `report.md`, `report.json` | The verdicts first, then the stations-only layout beside it, what shapes Wembley Park, and one row per position |
| `shepard.svg` | Map distance against travel time: the real map, the stations-only time map and this map's end |
| `panels.svg` | Three panels, north up at one scale: the real map, the first position where Wembley Park passes (or the closest), the end. Triangles shaded by area against the real map, the water and borough outlines, the stations and the Metropolitan line |

**How it is computed.**

- **Nodes** (`mesh.py`): the stations-only job's station nodes; 300 H3 cell centres from
  `points.csv` (any routing flag: they are geometry), picked by farthest-point sampling from the
  stations, none within 50 m of one, ties to the lowest index; and 40 fixed guard nodes on a circle
  8 km outside them, which carry no land and are never drawn.
- **Mesh:** the Delaunay triangulation of the stations' and cells' real positions (rounded to whole
  decimetres), and an annulus of triangles joining its convex hull to the guard ring, never changed.
  A fixed simple outer boundary plus every triangle keeping a positive signed area makes the map
  from the real map to a layout one to one; the guard ring is that boundary. The annulus keeps the
  ring's triangles outside the hull, so the land (clipped to the hull) lies in station-and-cell
  triangles only.
- **Train links** (`links.py`): unordered pairs of stops that a rail, tube, DLR or tram trip running
  on the build's weekday serves one after the other (`stop_sequence` one apart, so a stop the clip
  removed never joins its neighbours), the train leaving the first in the table's window, mapped to
  stations through `parent_station`. One link per pair of nodes. Its rest length is s·t: the
  stations-only layout's scale times the table's time between them (mean of both ways, floored at
  0.5 min). Read with the standard library only.
- **Energy** (`springs.py`): [Σ a·ψ(l/l₀) + ρ·Σ b·ψ₊(l/(s·t)) + Σ A₀·(κ·φ(J) + κs·μ(F))] ÷ (ΣA₀ /
  free nodes), with ψ(λ) = ½(λ² − 1) − ln λ on every grid spring (each mesh edge, rest length its
  real length, weight a third of the real area of its triangles), ψ₊ (ψ when stretched past its
  rest length, 0 when shorter) on every train spring (weight its real length × the median mesh
  edge); on each triangle φ(J) = ½(J + 1/J) − 1 of its area ratio J,
  which resists shrinking, and μ(F) = ‖F‖²/J − 2 of its map F from the real map, which resists
  going thin and is zero for any rotation or uniform scaling; κ = κs = 1. All three are zero and
  flat at rest and infinite at collapse, so no grid spring or triangle can be squashed flat or
  turned over: the grid bunches up instead.
- **Train springs only pull.** A train link says its stations are at most s·t apart. A table time
  between neighbouring stations includes reaching the platform and waiting, and the scale is the
  all-pairs one, so s·t is longer than the real hop for most links (89% on build 2026-11-12,
  median twice the real length). Springs that pushed would make chains of those links buckle
  into zigzags (a line can't grow to twice its length inside the grid), so they act only if their
  stations end up further apart than s·t; the fast links pull places together. The report gives
  the share every build, and how many links end up past their s·t.
- **Slider:** ρ, the train springs' strength against the grid: the real map (ρ = 0, every spring at
  rest), then 24 values from 0.01 to 100, each solved from the one before.
- **Motion:** the page moves smoothly. Between two neighbouring keyframes it draws their straight
  blend; the renderer holds one layout as positions and the next as targets and mixes them on the
  GPU. A blend of two unfolded layouts can still fold on the way, so the sweep checks every step
  exactly: along a blend each triangle's doubled area is a quadratic in the blend fraction, and
  its minimum is computed in exact integers on whole decimetres (`mesh.blend_min_area`). A step
  whose blend would fold is split at its geometric middle: the middle is solved, then the step's
  target is solved again from the middle (so it can differ from a direct solve; the restart check's
  other ladders measure that), up to six times. A step that still folds is a jump in the solution,
  and there the page follows the solver's own steps, each cut short of zero area, kept as
  settling keyframes; a mark under the slider shows where. The job checks every blend again
  before writing and refuses if one folds or the keyframes are out of order. With train springs
  that only pull, build 2026-11-12 needs none of this: no step folds part way, so the keyframes
  are the 25 solved positions. The slider runs continuously from the real map to the end; solved
  positions sit at whole steps, in-between ones at their log ρ. "Time map" plays the
  whole move in about 4.5 s at an even pace of map movement (easing in and out), pressed again
  it plays it back, and pressed mid-move it turns round; its label never changes, only its
  pressed state. Dragging the slider stops the
  play. The arrow and page keys glide to the next or previous solved position from where the map
  is (or on from a glide already under way), taking their share of the 4.5 s and at least 450 ms;
  a key held down steps on as each glide ends. Home and End play to the ends. With reduced
  motion, the button and keys jump. One animation-frame callback drives every move
  (`carry.js` `createMotion`, tested with a fake clock). The readout names the two keyframes
  being blended; station colours blend between the solved positions either side, and the tooltip
  gives both values. The live region stays quiet while the map moves or the slider is dragged,
  and the slider's spoken value changes only when its text does. Faint rings and dashed lines
  show where the named stations really are.
- **Lines on the page:** rail, borough outlines and (with Car on, #23) the main roads get thinner where the
  map squeezes the ground across them: each segment lies in one mesh triangle, whose linear map F
  is exact, and its width is det F ÷ |F·d| of the real width (d its direction), never wider and
  never under 30%. The renderer takes that as an optional width factor per line segment, at both
  ends of the morph (`wf`; without it, as for every other scene, lines keep their width).
  "Smooth lines" (on by default) rounds off the bends lines take where they cross a mesh edge:
  the pieces `land.py` cut are joined back into whole lines, and each line's displacement from
  the real map is averaged along it over ±200 m (real), three times, with the window narrowing to
  zero at line ends, junctions and mesh nodes. The real map is untouched and the blend of two
  smoothed keyframes is the smoothed blend. Water, parks and the mesh keep the exact map, so a
  smoothed line can sit off it: on build 2026-11-12 at the end of the slider, 1% of line points
  move more than 149 m (at most 479 m), and kinks over 10° where a straight line crosses a mesh
  edge drop from 26% to 5%.
- **Solve:** Levenberg–Marquardt Newton on the exact Hessian. Each trial step is cut to 0.9 of the
  first point at which any triangle's area would reach zero along it (the area is a quadratic in
  the step length), so no point on any step is folded; a step that doesn't lower the energy isn't
  taken. Converged at |gradient| ≤ 1e-6; a position that won't converge is reached again through
  up to three levels of sub-steps, then refused. The report says whether the Hessian is positive
  definite at each solution (a strict local minimum). The ring is fixed, so there is no rotation to
  align. A restart check tests whether the first pass and the end depend on how the continuation
  got there. It solves the sweep again from the real map nudged by up to 0.5 m on every free node
  (two seeds, the same steps: a test of the start), and through other ladders of ρ (every other
  step, and straight from the real map in one step: a test of the step size). It reports how far
  Wembley Park, σ₁ and the Metropolitan order move, and the largest shift of any node from the
  shipped layout. On build 2026-11-12 no node moves more than 0.2 m.
- **Land** (`land.py`): water (`natural=water`, `waterway=riverbank`, reservoirs, basins), parks
  (parks, commons, nature reserves, golf courses, recreation grounds, cemeteries, woods, heath;
  water cut out of them), rail (`railway=rail`, `subway`, `light_rail`, `tram` without
  `service=*`; `tunnel=yes` as a tunnel) and the 33 boroughs (`ref:gss` E09000001 to E09000033,
  every part of each, their outlines merged so a shared border is drawn once), from the clip
  through `osmium` and shapely. Water and parks are dissolved before the size floor, then
  simplified, clipped to the hull of stations and cells, and cut along the mesh so every piece lies
  inside one triangle. Every vertex is stored as three mesh nodes and two weights (a point on a
  mesh edge by that edge alone, the same way in every piece that shares it), so its place on any
  layout is the mesh's own piecewise-linear map, and neighbouring pieces meet exactly: a mesh that
  doesn't fold draws land that doesn't fold or crack. The page draws each fill triangle from the
  Float32 positions of the two keyframes it is between, mixed by the shader; along that mix the
  triangle's doubled area is a quadratic, and the build checks its exact minimum over the whole
  blend, on every keyframe and every blend, with a margin of 1 cm a vertex for the shader's own
  rounding (`land.blend_slivers`). Slivers that don't pass (far under a pixel) are left out and
  counted.
  Simplification goes 20, 40, 80 m, then the smallest water and park 2, 5, 10 ha, until the page
  fits in 2 MB gzipped; no layer is dropped.
- **Report:** at every position, on the station nodes, the stations-only job's measures (σ₁,
  trustworthiness and continuity at k = 10, the Wembley Park rule, every `[[ratio]]` row by the
  same rule, the Metropolitan line's order); flipped triangles (always 0); the smallest area ratio
  to the real map and where; how thin triangles get (larger over smaller scale) and where; the
  links past their s·t; the energy's four terms; how far nodes still move from one position to the
  next. Triangle statistics cover the triangles inside the hull, never the guard ring's. Each
  Wembley row also says pass or fail against the stations-only pure time map's σ₁, and the report
  says what shapes Wembley Park (the chain of links to Baker Street, its bend, the triangles between
  them) and why a station no train joins to its reference can end too close to it. The
  stations-only layout is solved again in the same run so both columns come from the same tables.
  Its fold counts (on its own station triangulation) are shown on this map too; here they come from
  bending, not folding.

Everything it writes is derived from the tables, so it stays in the git-ignored output folder
and never goes into git or `static/`. Unlike the stations-only layout, it is published: the app
shows it as Whole city (#28), from `TIMEMAP_DIR`, under the tables' own expiry and licence gate.
Its report and Shepard plot are linked from the page.

## Travel modes (#23, L-03)

Whole city has a toggle for each way of travelling: Walking, Bus, Train and tube, Car. Every mode
is a family of pull-only springs like the train springs, at the same scale s, and the build solves
each combination over the same 25 positions of ρ from the real map. Part 1 (this) is car with
train and tube: three combinations (train alone, the time map above; car alone; car with train
and tube) and the real map, with no mode on. Walking and Bus wait for walking-only and bus-only
times from R5, which come with the next table rebuild; their toggles say so.

```bash
export TRANSIT_OUT=<builds> TRANSIT_DATA=<their data> T4C_ZIP=<path>/LONDON_2022.zip
transit/.venv/bin/python -m transit.layout.carspeed                       # once per zip
transit/.venv/bin/python -m transit.layout.car --build 2026-11-12         # once per build and window
transit/.venv/bin/python -m transit.layout.timemap --build 2026-11-12 --modes car
```

**The car's speeds** (`carspeed.py`). `LONDON_2022.zip` is HERE's Traffic4cast 2022 sample data
(<https://developer.here.com/sample-data>, 2.8 GB; the owner downloads it). It holds, per day from
2019-07-01 to 2020-01-31 (110 days, all before covid), the MeTS-10 pipeline's speeds on
OpenStreetMap edges: one row per directed edge (u, v: OSM node ids) and 15-minute bin with enough
probe data (`speed_classes/london/*.parquet`: u, v, day, t, volume_class, median_speed_kph,
free_flow_kph), and the movies they were made from (not read). There is no road geometry. Kept:
weekdays, no bank holidays, not 23 December to 3 January (74 days), bins 52 to 55 (13:00 to 14:00,
the tables' wd13), bins of at least 1 km/h; per directed edge the median over those rows. The bins
follow London's clock (the morning rush sits at 08:45 in summer time and in GMT). The zip is read
member by member, never unpacked or moved; `T4C_ZIP` (or `--t4c-zip`) says where it is, default
`$TRANSIT_DATA/raw/t4c22/LONDON_2022.zip`. The speeds go to `<data>/work/t4c22/` beside the zip's
`raw/` folder (`london_wd13_speeds.parquet` and `.json`: the days, the zip's sha256, the licence).
**Licence:** academic and non-commercial use (`docs/provider-terms.md`); the owner uses it
non-commercially, with the permissions collected.

**The car links** (`car.py`, osmium and shapely; writes `transit/out/car/<build>/<window>/`):

- **Roads:** `highway=motorway|trunk|primary` from the build's own clip (mainline only, no slip
  roads; not closed to cars), bridges and tunnels included, one way or both as `oneway`,
  `junction=roundabout` or a motorway's implied one way says.
- **Speeds on them, by node id first:** a Traffic4cast edge is simplified between junctions, so
  where both its ends are on our main roads its speed goes to every edge of the shortest path
  between them along those roads, if that is at most 1.5× the straight distance ("before"
  matching). Then, for edges still without one, the straight chord of a Traffic4cast edge whose ends
  the clip still has (`osmium getid`), within 20 m and 30° ("after"). Edges with a synthetic id or a
  node OpenStreetMap no longer has are counted and not used. The rest take their class's median
  measured speed.
- **Coverage on build 2026-11-12:** Traffic4cast's London is a fixed box, longitude −0.378 to
  0.075 (about Hounslow to Woolwich): roads east and west of it have no speed. Inside it 85% of
  the main roads' length has a measured speed; motorways are mostly unmatched (few of their 2019
  node ids are still in OpenStreetMap).
- **Links:** roads are cut into pieces of at most 20 m; each piece end belongs to its nearest
  station or cell (never a guard node). A piece whose ends belong to different nodes A and B makes
  A–B a candidate; within the road stretches owned by A or B the link's time is the drive from A's
  anchor (its own road point nearest it) to B's, each way that exists, averaged, the fastest stretch
  when there are several, floored at 0.5 min. A link with under half its road measured is dropped
  and listed. Cutting finer changes nothing.
- **Thames crossings:** every bridge or tunnel of a motor road that crosses the River Thames inside
  Greater London (`london_region.geojson`, with the tidal river the ONS boundary leaves out filled
  back in), with the link that crosses it, or why there is none (not a main road, closed to cars,
  both banks on one node, the link dropped).

**The car springs: the overhead once a trip.** A car trip pays CAR_OVERHEAD_MIN = 5 minutes for
parking and getting to and from the car, once. Every node a car link touches gets a twin, a free
point in no triangle and never drawn: an access spring (pull-only, rest s·2.5 min) joins the node
to its twin, and road springs (pull-only, rest s·drive minutes) join twins along the links. Two
places are then held at most s·(5 + the drive along the roads) apart, however many nodes the road
passes. A road spring's weight is the train rule (the real distance between its nodes × the median
mesh edge); an access spring's is the sum of its twin's road springs'. A weak tie, ρ·η·a·(d/r)²/2
with η = 10⁻³, holds each twin by its node, so the Hessian stays positive definite where every car
spring is slack; it is zero on the real map. Train and car springs between the same places sit in
parallel and add: where a pair is stretched past both rest lengths the slower one pulls too. Train
hops come from the tables and include a wait at each; a car trip pays its overhead once.

**What it writes** (into `modes/` of the time map's folder, only with `--modes car`; without it
the job's output is exactly what it was, byte for byte):

| File | What it is |
|---|---|
| `modes.json` | The modes (Walking and Bus not available yet, and why), which combination is in which file, train and tube's pulling springs at each position, whether each switch goes straight across, `roads.bin`'s header, every file's size |
| `combo-car.json`, `combo-car-train.json` | Each car combination: every solved position in whole decimetres, its measures and each mode's pulling springs, and its in-between keyframes |
| `roads.bin` | The main roads carried through the mesh like the rail (`land.py`), drawn while Car is on |
| `report.md`, `report.json` | The car data (licence, days, window, coverage before and after), the Thames crossings, the combinations compared at train alone's Wembley pick and at the end (node shift from the real map and from train alone, each mode's pulling share, Wembley Park, σ₁ against the all-mode tables, the thinnest triangle), every switch, the restart check per car combination, the spot check, every position |

`timemap.json` and `land.bin` stay train and tube alone, so the page's first load is unchanged;
the land's Float32 sliver check covers every blend any combination draws, switches included. Each
car file must be under 2 MB gzipped; the page fetches a combination the first time it is switched
on.

**No folds.** Every combination is solved and checked like the time map: 0 flipped triangles at
every shipped position, as shipped and in Float32, and every blend between neighbouring keyframes
checked exactly; the restart check (nudged starts, other ladders of ρ) runs for each car
combination. **Switching** happens at solved positions: the page glides to the nearest one, then
blends straight across to the other combination where the build checked that blend
(`mesh.blend_min_area` on whole decimetres), or else goes back down its own path to the real map
and up the other combination. Both directions of train ↔ car with train and car ↔ car with train
are checked at every position; train ↔ car always passes through one of them (one toggle at a time).

**Spot check.** The station pair at least 3 km apart with the biggest gain by car (its table time
minus the car's door-to-door time along the links, overhead included), and any `[[car_spot]]` row
in `checks.toml`, with their map distance at train alone's Wembley pick and at the end, for each
combination: it passes when car with train ends closer than train alone.

**Serving.** The app serves `modes/` like the map itself, under the same gate (main README,
"Whole city"). The car's speeds are used non-commercially, with the owner's permissions.
