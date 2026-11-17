# Minutes Away

Three tools for seeing a city by travel time, built with **FastAPI**, **MapLibre GL JS**, and **WebGL2**.

Powered by [Mapbox](https://www.mapbox.com/) — this project relies on several Mapbox APIs for its core functionality:

- **[Mapbox Isochrone API](https://docs.mapbox.com/api/navigation/isochrone/)** — travel-time and walking-distance contours From here turns into a travel-time field
- **[Mapbox Directions Matrix API](https://docs.mapbox.com/api/navigation/matrix/)** — computes real network travel times between participants and candidate meeting points in Meet up
- **[Mapbox Vector Tiles](https://docs.mapbox.com/api/maps/vector-tiles/)** — provides the base map geometry (roads, buildings, labels) rendered via MapLibre GL
- **[Mapbox Fonts / Glyphs](https://docs.mapbox.com/api/maps/fonts/)** — serves font glyphs for map label rendering

| Tool | Question | What it shows |
|------|----------|---------------|
| **From here** (`/from-here/`) | How far can I get from here? | A pin and travel-time bands around it on the real map (walk, cycle, drive, and public transport in London when the tables are installed). "Time map" (experimental) bends it so the bands become circles: distance from the pin means travel time. |
| **Meet up** (`/meet-up/`) | Where should we meet? | Where everyone starts, the place that keeps the longest trip shortest ("Fairest"), and the place with the shortest average trip ("Quickest on average") when that is another place, with each person's minutes. A group can be shared as a link. |
| **Whole city** (`/whole-city/`) | What does London look like when distance means travel time? | London's land and stations moving from the real map to the time map, where a fast line pulls places together (#6, L-02). Only on a server with a time-map build whose tables may be served (see [Whole city](#whole-city)). |

From here and Meet up open on a saved London example, so the first view makes no routing
request; Whole city makes none at all. Every tool's header links the other two. The old paths
`/warp/` and `/meetup/` redirect (308) to `/from-here/` and `/meet-up/`, keeping the rest of the
path and the query; the browser keeps a share link's `#g=…` group.

The two maps have one name each in every tool: the **real map** (the ordinary one) and the
**time map** (bent, so distance means travel time).

### Built with

- **From here:** a travel-time field built on the server (Mapbox isochrones near the pin, the
  London transit tables farther out), drawn as bands on the MapLibre map and bent by a WebGL2
  mesh renderer that moves the map's own geometry.
- **Meet up:** candidate points on an H3 hexagonal grid, travel times from the Mapbox
  Directions Matrix API, and a minimax (fairest) answer with the quickest-on-average place
  beside it, from the places already checked. The API also accepts `sum` and a `hybrid`
  blend as the objective, which the page doesn't offer.
- **Whole city:** the full London time map of `transit/layout` (a spring grid that can't fold),
  drawn by the same mesh renderer from a data folder the server serves under the tables' gate.
- **All three:** FastAPI, MapLibre GL JS, vanilla JavaScript modules with no build step, and
  shared modules in `static/shared/` for the header switcher, the travel-mode chips, the "When?"
  control, asking for a location, and what one tool hands the next.

### From one tool to the next

From here's pin, way of travelling and time of day go with you to Meet up (the pin becomes the
group's first person) and back, through Whole city too. Meet up's group is kept too, so going to
another tool and back through the switcher finds it again, and it wins over a pin brought back
since. Both records live in this tab's `sessionStorage` (`/shared/handoff.js`), the pin rounded to
about 11 m; the records themselves are never sent anywhere (a tool then uses the pin as if you
had placed it, e.g. From here asks the server for its travel times). A shared Meet up link always
wins over both, and the saved examples are never handed on. A new tab starts fresh.

## Prerequisites

- Python 3.11+
- A [Mapbox access token](https://account.mapbox.com/access-tokens/) (requires a Mapbox account)

## Setup

```bash
git clone https://github.com/arashbehmand/time-map.git && cd time-map

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
# Tiles and glyphs (proxied map data)
MAPBOX_TILES_TOKEN=pk.your_tiles_token
# Matrix and Isochrone (server-side routing)
MAPBOX_ROUTING_TOKEN=pk.your_routing_token
```

`MAPBOX_ACCESS_TOKEN` still works as a fallback for both, but separate tokens can be rotated
and tracked on their own.

## Running

```bash
uvicorn server.app:app --reload
```

Open [http://localhost:8000](http://localhost:8000) in your browser for the landing page. The app is installable as a PWA — use your browser's "Add to Home Screen" or "Install App" option to use it on your phone.

| Route | App |
|-------|-----|
| `/` | Home: the three tools |
| `/from-here/` | From here: how far can I get from here? (`static/warp/`) |
| `/meet-up/` | Meet up: where should we meet? (`static/meetup/`) |
| `/whole-city/` | Whole city: London by travel time (`static/whole-city/`) |
| `/warp/…`, `/meetup/…` | Old paths: redirect to the two above |

The folders keep their first names (`static/warp/`, `static/meetup/`, `server/warp/`); only the
public paths follow the tools' names (`TOOL_PATHS` in `server/app.py`).

## Whole city

`python -m transit.layout.timemap --build <id>` writes one folder per timetable window,
`transit/out/timemap/<build>/<window>/`: `timemap.json` and `land.bin` (what the page draws) and
the build's report (`report.md`, `report.json`, `shepard.svg`, `panels.svg`). It is derived from
the transit tables, so it stays out of git (`tests/test_no_data_in_git.py`), and the job refuses
to write into `static/` or `server/`. The page is the app's own code (`static/whole-city/`).

Point the app at one build's folder with `TIMEMAP_DIR`. It reads every window it holds at start-up
(about 2.8 MB each for build 2026-11-12, plus a gzip copy made once) and serves what the page uses (`timemap.json`,
`land.bin`, `report.md`, `shepard.svg`; not `report.json` or `panels.svg`) from
`/api/whole-city/<window>/<file>`, never from a static folder. A window left out against the
loaded tables is logged once, with why. A window is served only while **the loaded
tables may be served to the same request** (unexpired; `licences_cleared`, or
`TRANSIT_ALLOW_UNCLEARED=1` for this machine only, never on Railway) and only if it was made from
exactly those tables: the same build id and the same table file (`table_sha256`). Withdrawing or
expiring the tables withdraws Whole city with them. A new folder needs a restart.

When there is no usable build, the home card and the switcher say so in plain words and link
nowhere, and `/whole-city/` itself (from a bookmark or the installed app's shortcut) says the same
and points to the other two tools. The page links its build's report and Shepard plot under
"How this map was checked" (#6).

**Offline.** Whole city works offline once it has been opened online (the owner's choice,
2026-11-16). The service worker keeps its files after a view, in their own cache
(`whole-city-data`, network first, `static/sw.js`), and the page keeps the status it was shown
with beside them (`static/shared/offline.js`). With no network, the page, the home card and the
switcher use that copy, only while the build's own valid-until date hasn't passed in London, and
only for the times of day that were opened. Online the server's gate decides every time: a refusal
(404) or a status saying Whole city isn't available drops everything kept. A licence withdrawn
while a device stays offline is only seen there once it is back online; expiry is seen offline.
Nothing is precached at install, so a server without a build still installs the app.

On a local machine, with the tables of build 2026-11-12:

```bash
TRANSIT_DIR=transit/out/2026-11-12 TIMEMAP_DIR=transit/out/timemap/2026-11-12 \
  TRANSIT_ALLOW_UNCLEARED=1 uvicorn server.app:app
```

then open <http://127.0.0.1:8000/whole-city/>. The override is refused on Railway and serves only
requests from this machine through no proxy, as for the tables.

## Project Structure

```
server/                          # FastAPI backend
├── app.py                       # Main app, lifespan, shared proxies
├── config.py                    # Shared config (Mapbox tokens, warp constants)
├── errors.py                    # Fixed error responses {code, detail}
├── logging_setup.py             # Logging with token redaction
├── upstream.py                  # The one way to call Mapbox
├── settings.py                  # Limits and budgets (env-configurable)
├── middleware.py                # Request body size limit
├── cache.py                     # Bounded LRU cache with TTL
├── guard/                       # Budget, rate, in-flight and per-IP controls
├── local.py                     # Is a request from this machine (the uncleared-build override)
├── whole_city/routes.py         # GET /api/whole-city/<window>/<file>: Whole city's data, behind the tables' gate
├── warp/                        # From here's travel-time field
│   ├── routes.py                # POST /api/travel-time-field, GET /api/transit/status, POST /api/isochrones
│   ├── travel_field.py          # One field per (pin, mode, window): cache, coalescing, the transit merge
│   ├── field.py                 # Contours → minutes on a grid, frames, the wire format
│   ├── models.py                # Request models
│   ├── mapbox_client.py         # Mapbox Isochrone fetching (minutes or metres), bounded cache
├── transit/                     # The London tables at runtime: validation, composition, coverage, venues;
│                                #   whole_city.py: the full time map, served under the tables' gate
└── meetup/                      # Meetup optimizer module
    ├── routes.py                # POST /api/meetup/solve
    ├── models.py                # Pydantic request/response models
    ├── limits.py                # Pre-flight: radius, candidate and cost caps
    ├── candidates.py            # H3 hex candidate generation
    ├── routing.py               # Mapbox Directions Matrix: batching, strict replies, NoRoute
    ├── stations.py              # Groups on public transport: stations, or pubs, bars, cafés, restaurants, parks
    └── solver.py                # Scoring, refinement, snapped and named answers
static/                          # Frontend assets (vanilla JS modules, no build step)
├── shared/                      # ui.css, api.js (errors, search), latest.js (latest answer only),
│   │                            #   tools.js (names, words), transit.js (windows, expiry, status),
│   │                            #   nav.js (switcher), locate.js (location), handoff.js (tool to tool)
│   ├── mesh/                    # Mesh renderer: warp, tiles → mesh worker, WebGL2 renderer, bands, overlaps
│   └── vendor/                  # pbf, @mapbox/vector-tile, point-geometry, earcut (ES modules)
├── warp/                        # From here (/from-here/) — app.js, views.js (choices), words.js (sentences), example-field.json
│   └── lab/                     # /from-here/lab/: renderer lab (unlinked, synthetic travel times)
├── meetup/                      # Meet up (/meet-up/) — app.js, words.js (its sentences), share.js (share links), example.json
├── whole-city/                  # Whole city (/whole-city/) — app.js, carry.js (the land through the mesh); its data is TIMEMAP_DIR
├── portfolio/                   # Home page
├── manifest.json                # PWA manifest
├── sw.js                        # Service worker (versioned per deploy, network first)
└── icons/                       # PWA icons
transit/                         # London public-transport tables (offline build; the app imports only transit/tables.py)
├── venues.py                    # Meetup venues from the OSM clip (#19): python -m transit.venues
└── layout/                      # London time map (#6): stations only by SMACOF (L-01); the full map as a spring grid that can't fold (L-02)
scripts/make_examples.py         # Regenerates the two saved examples
scripts/try_solves.py            # Runs fixed meetup groups against a server (timings, diagnostics)
tests/js/                        # node --test: warp maths, mesh renderer, latest-answer helper, meetup sentences
docs/warp-renderer-spike.md      # Mesh renderer spike (#5, W-01): what was built, measured and decided
docs/warp-time-polar.md          # Warp v2 (#5, W-02): the field, the two views, Direction 4 on real fields
```

## Tests

```bash
ruff check . && pytest
node --test tests/js/*.test.js   # Node 20+, no packages to install
```

`tests/test_meetup_solver.py` checks the solver against brute force (within a minute of the
best cell at the final resolution, two finer than coarse, on 100 seeded cases, and never
worse than the coarse stage alone or the first two stages alone; these use places with no
street names, so the named-street rule below is tested on its own) and checks that the order
of people doesn't change the answer. `tests/test_matrix_router.py` covers batching, strict replies and
NoRoute; `tests/test_meetup_robustness.py` sends 100 random requests at a misbehaving Matrix
API.

To time real solves, run a server and `python scripts/try_solves.py http://localhost:8000
--group G1 --group G2 --group G3 --repeat 4 --pause 15` (T1 and T2 need `TRANSIT_DIR`). Against live Mapbox, cap the spend on that server first
(`MATRIX_DAILY_BUDGET=8000`, `CLIENT_DAILY_SHARE=1` so the one client running the script may
use all of it, and a scratch `USAGE_DB_PATH`). The p95 it prints counts only solves that
answered 200, and it says so when some didn't.

## London transit tables

Mapbox has no public transport. `transit/` builds London travel-time tables offline from open
timetables with R5 (r5py), plus station walksheds and a manifest, and checks them against TfL
Journey Planner (issue #1). It has its own Python environment and needs Java; the app and CI
don't. How to build, the one key it needs and what it writes: [transit/README.md](transit/README.md).
Sources and licences: [docs/transit-sources.md](docs/transit-sources.md); results so far:
[docs/transit-spike.md](docs/transit-spike.md).

The warp map reads a build at runtime when `TRANSIT_DIR` points at one (a build folder, or a
package from `transit.build`'s package stage, extracted)
(`server/transit/provider.py`, using `transit/tables.py` only). It serves it only if the build is
complete, unexpired, matches the hashes in its manifest and says `licences_cleared: true`.
`TRANSIT_ALLOW_UNCLEARED=1` serves an uncleared build for local development: only to requests
from this machine with no forwarding header, whatever the host, and never on Railway. Without a
usable build, the page offers walk, cycle and drive only.

## Opening examples

`static/warp/example-field.json` and `static/meetup/example.json` are real answers for a London
example (King's Cross, walking; three friends cycling from Angel, Vauxhall and Marble Arch). They are what each page shows before
anyone changes anything, so opening a page costs no routing request. Regenerate them after
changing the field or the response shapes:

```bash
uvicorn server.app:app --port 8000            # with a real MAPBOX_ROUTING_TOKEN
python scripts/make_examples.py http://localhost:8000
```

The saved meetup example was made before answers carried street names, so it opens with
"Meet here" until it is regenerated.

## Service worker

`/sw.js` is served with its cache name set to a hash of the files under `static/`, so every
deploy that changes them gets a new cache and the old one is deleted. It caches only this
site's own app files, fetched network first (the cache is used only offline). It never
touches `/tiles/`, `/glyphs/`, the rest of `/api/` or any other site, MapLibre's CDN included.
The one exception is Whole city's data (`/api/whole-city/`), kept after a view for offline use in
a cache of its own (see [Whole city](#whole-city)).

## How the Warp Works

The server answers, once per (pin, mode, window), with a **travel-time field**: minutes on a
regular grid in metres around the pin (`POST /api/travel-time-field`, `server/warp/`).

- **Walking:** two Mapbox distance isochrones (8 contours, every piece, hole and island kept),
  read at 4 km/h, the London tables' walking speed. Between two contours a place's time follows
  its distance to each boundary, so nothing assumes one reach per direction.
- **Cycling and driving:** two Mapbox time isochrones, 5–60 minutes.
- **Public transport (London, when the tables are installed):** within 2 km of the pin, the
  faster of walking and the tables; beyond, the tables: walk to a nearby station or table cell,
  ride, walk on (#1's formula). No Matrix call. Up to 180 minutes.

The page draws the field two ways (`static/warp/app.js`):

- **The real map** (always): MapLibre with travel-time bands under the labels, contoured in
  the browser (`static/shared/mesh/contours.js`). A tap gives the minutes at that spot.
- **The time map** (experimental): the mesh renderer below. Each place sits at a distance from
  the pin proportional to its minutes, in its own direction (by reach, the default, or true
  compass), so the rings are exact circles. The "Time map" button (pressed or not, its label
  never changes) dissolves the real map into the mesh, then morphs it. A tap lists every place
  drawn under the finger, folds included. For public transport the time map stays hidden
  (`?polar=1` shows it) because it fails Direction 4's checks at all five test origins: see
  [docs/warp-time-polar.md](docs/warp-time-polar.md).

Panning, zooming and resizing never send a request; a new pin, mode or window sends one, and
the old picture stays up until the new one is ready.

### The mesh renderer

`static/shared/mesh/` draws vector geometry (water, parks, rail, main roads) through a mesh
whose vertices move: `screen = mix(ordinary(p), pin + k·target(p), u)`, where `target()` is a
time-polar layout or any layout given as a grid of positions (the London map, #6). Lines are
extruded in screen pixels after the warp, so they stay sharp, and a place 20 km away can be
drawn next to the pin. `warp.js` holds the `lon/lat → screen` function and its GLSL twin. A
worker fetches and tessellates tiles once per tile, and per origin refines edges where the field
bends them (`refine.js`); the morph only changes a uniform. It needs WebGL2 and no
float-texture extension. `/from-here/lab/` (not linked from anywhere) runs the checks: on synthetic
travel times by default, or on the page's own fields with `?field=real`.
`scripts/lab_headless.mjs` drives the lab or `/from-here/` in a headless Chrome. W-01's results are in
[docs/warp-renderer-spike.md](docs/warp-renderer-spike.md).

### The London time map (stations, spike)

`transit/layout/` (#6, L-01) lays London's stations out offline so that map distance follows
travel time, from the transit tables above, by weighted SMACOF with a slider from the real map
to the pure time map. It writes a report and a local viewer into the git-ignored
`transit/out/layout/`, and nothing it makes goes into git or onto the site.
See [transit/README.md](transit/README.md#stations-only-layout-6-l-01).

`python -m transit.layout.timemap` (#6, L-02) draws the whole of London that way: every station
and 300 points between them on a triangle mesh of springs that can't fold, the Thames, parks,
rail lines and borough outlines carried on the same mesh, drawn with the mesh renderer above.
It writes the map's data, a report and three panels into the git-ignored `transit/out/timemap/`,
and the app shows the map as [Whole city](#whole-city).
See [transit/README.md](transit/README.md#full-london-time-map-6-l-02).

## How the Meetup Works

1. Generate candidate meeting points on an [H3 hexagonal grid](https://h3geo.org/) covering the participants' bounding box
2. Query the **Mapbox Directions Matrix API** for actual network travel times from each participant to each candidate. Requests are batched by origins and destinations to fit each profile's coordinate limit, and a stage's requests run at once, three at a time.
3. Score candidates using the chosen objective:
   - **minimax** — minimise the longest individual travel time (fairest)
   - **sum** — minimise weighted average travel time (most efficient)
   - **hybrid** — blend of both (API only)
4. Refine: score the children of the best coarse cells at the next finer resolution. Then
   refine again: score the children of the three best of those finer cells, one resolution
   finer still (at most 21 places, about one more Matrix request per travel mode). Without
   this third stage, two walkers 14 km apart (Wembley Park and Bank) got the snapped centre of
   a cell 1.4 km across, about 4 minutes above the straight-line best; with it they get about
   1.4 minutes above it (issue #26). Keep whichever stage has the best answer (a later stage
   wins a tie), so refining never makes it worse.
5. Return the best meeting point, top-k runners-up and a meeting area polygon. With
   `minimax` (the page's only objective), also return `quickest`: the place with the lowest
   average trip among the places the stages checked, when it is at least 150 m from the best
   and saves at least a minute on the average trip. It costs no extra Matrix call, and
   because the finer stage refines around the fairest places, it is the quickest of the
   places checked, which the page says. Alternatives at the same spot are left out.

Each answer is shown where the Matrix API snapped it onto the street network, with the
street's name when Mapbox gives one. Mapbox often gives no name, for example on a footpath.
When the best place has none, the best place in the same stage that has one is shown instead
if it costs at most three minutes more, and at most 12% of the best place's score (so a short
trip can't nearly double). This is the one case where the answer shown is worse
than the best place found: a note says by how much, the page leaves out its "as short as it
can be" line, and the unnamed places stay among the alternatives
(`diagnostics.named_over_best_min` says what it cost, 0 when nothing was passed over). Each travel mode's times are to that mode's own snapped
point; the point shown is the walking one (then cycling, then driving), and `snap_m` is its
distance from the grid point searched. A candidate is offered only if every mode's snapped
point is within 150 m of the grid point (10% of the cell edge on coarse grids, where hex
centres are rarely that close to a road) and within 150 m of the point shown, so everyone's
trip ends close to the star; if some trip ends more than 50 m away, the answer says so.
Candidates that fail this are probably in water, a rail yard or a park; they can still guide
the finer stage. If nothing in the search passes, candidates up to three times the limit
are offered with a note, and beyond that the search fails (`no_feasible_meeting_point`,
reason `off_street`). Alternatives that snap within 150 m of a better one are left out.
People are scored in id order and ties are broken by a fixed rule, so the same group in
any order gets the same answer.

Matrix replies are read strictly. A `null` duration means that one pair has no route. A
reply with `code: "NoRoute"` (or `NoSegment`, also in a 4xx body) is split to find what
fails: by destinations first, down to the one that fails for everyone, which is dropped;
when both halves fail, by origins, down to a starting point that reaches none of the places
the others reach, which is then a confirmed "no route" for that person. Splitting stops
where the solve's recovery allowance runs out; what is left is unchecked. Anything else
that is missing or malformed (wrong row or column count, a value that isn't a number of
seconds, a missing waypoint) is a provider error: it is asked for once more, then the solve
answers `502 upstream_error`. A request that never reached Mapbox (the connection timed out
or was refused) is asked for once more in the same way; a request that timed out after
reaching it is not, and the solve answers `504 upstream_timeout`. It is never read as "unreachable".

People are named only from replies that answered for them. An unchecked pair says nothing
about anyone's start: more than a quarter of a stage's places unchecked fails the search
(`422 unroutable_area`), or, in a finer stage, keeps the earlier answer.

An answer that is less good than usual says so in `notes` (shown on the page): places were
left unchecked, a finer stage didn't finish or was left out to fit the allowance, few places
were near a street, some trips end a short way from the star, the star was moved to a named
street, or the map service sent a bad reply that was asked for again. `diagnostics` counts what was dropped (`dropped_no_route`, `unchecked_for_budget`,
`dropped_off_street`, over the stages the answer was chosen from), retried (`retries`,
`bad_replies`) and sent (`matrix_requests`, `matrix_elements`). `fine_resolution` and
`finest_resolution` are the finer resolutions searched (`finest_candidates` is the places
the third stage asked about, 0 when it wasn't tried; when it was tried and failed, `stage`
is not `finest`), `answer_resolution` the one the answer came from, and `stage` says
which stage that was (`coarse`, `fine` or `finest`).

### Meeting at a station (public transport)

A group with anyone on public transport (`mode: "transit"`) meets at a station from the
London tables instead (`server/meetup/stations.py`, issue #4). It needs a loaded build
(`TRANSIT_DIR`), served on the same terms as the warp page's fields; without one, the solve
answers `503 transit_unavailable`. A group with nobody on public transport uses the grid
above, unchanged. `diagnostics.path` says which (`"stations"` or `"grid"`).

- **Places.** Every usable station, each on its own column of the tables and at its own
  street point, so every time is to the point shown. Platforms with one name (Bank's
  Underground and DLR) are separate stations; the 500 m spacing below keeps two of them from
  being listed together.
- **When.** `window` is one of the tables' four (`wd08`, `wd13`, `wd19`, `sa13`), default
  `wd19`, weekday evening. The answer names it, and says that walking, cycling and driving
  times have no time of day. The page shows a "When do you meet?" select while anyone is on
  public transport.
- **Who travels how.** People on public transport are timed from the tables: no Mapbox call,
  and an all-transit group doesn't wait for a solve slot. Everyone else is timed by Matrix on
  a shortlist of K stations, ranked first by the table times plus a straight-line estimate at
  each mode's average speed (taking the fairest and the quickest in turn). A station a road
  person can't reach within their own `max_minutes` even at 6 / 30 / 120 km/h (walking,
  cycling, driving) isn't shortlisted. K is at most 24 (9 with `driving-traffic`) and shrinks
  to fit issue #2's budget. Each road person is asked about alone, one origin and the K
  stations in one request, and may send at most two requests, retries and NoRoute splitting
  included; nobody spends anyone else's. The answer says it is the best of the stations
  actually checked by road (`checked_by_road`). `diagnostics.lower_bound_proven` says whether,
  at those 6 / 30 / 120 km/h bounds, no station left unchecked could have beaten the answer;
  the bounds are assumed, not measured, so the page never shows it.
- **Road trips end near the station.** Mapbox puts each station on its road network. The walk
  from there to the station (4 km/h) is added to that person's time, and the answer says so
  when a listed trip ends more than 150 m away. A station more than 400 m from where any road
  mode can reach is left out for the group (`dropped_off_street`). Stations beyond a road
  person's `MODE_RADIUS_KM` aren't candidates; a person with no station in reach is named
  (`422 group_too_spread`, `who`), with their own mode's limit.
- **When Mapbox can't route.** Any NoRoute reply is said in the notes, even when splitting
  recovered every pair. With one spare request a 24-station NoRoute is split once: the better
  half is asked again and the rest is left unchecked and said. Only when no station was
  answered for everyone does the solve fail (`422 unroutable_area`).
- **Two answers.** `best` is the fairest place (the smallest longest trip, then the smallest
  average). `quickest` is the place with the smallest average trip, when it is another place
  at least 500 m away that saves at least a minute on average. The request's `objective` and
  `alpha` don't apply on this path: both answers come back, and `objective` reads `minimax`.
- **Close alternatives.** Up to five places in all, the best and the quickest included, each
  listed once and at least 500 m from the others; `top` holds the best and the places within
  max(3 min, 10% of the best longest trip), and `quickest` is not repeated there. A place
  within the tables' error of the best is a `tie`: the owner's accepted median line (3 / 7 /
  7 min, from the build's manifest, else `TABLE_ERROR_MIN`; kept until a full gate packages
  measured per-window error, #13) for the band of the longest public
  transport trip at the best place. At those lines most listed alternatives are ties, and the
  page then says so once rather than on every row. There is no shaded meeting area and no
  Reach button: the places are discrete, and a Mapbox isochrone wouldn't show the journeys
  these times came from.
- **Refusals.** Someone on public transport outside the tables' area gets `422
  outside_transit_area`, and someone near no street or station in them `422
  no_transit_access`; both list who in `who`. Someone whose table row has no time within 3
  hours to any station gets `no_feasible_meeting_point` with reason `no_transit_time`, not
  `no_route`. The page offers public transport only to pins inside the area, and switches a
  pin added or dragged outside it to walking, saying so.
- **Links.** Each person's time comes with a "Plan journey" link to Google Maps directions in
  their way of travelling. It is opened only when clicked, and the page says that the person's
  start point then goes to Google, which plans for now, not for the window.

### Meeting at a pub, bar, café, restaurant or park (public transport)

A group with someone on public transport can ask to meet at a named venue from
OpenStreetMap instead of a station (issue #19): `venue` is one of `pub` (`amenity=pub`), `bar`
(`amenity=bar`), `cafe` (`amenity=cafe`), `restaurant` (`amenity=restaurant`) or `park`
(`leisure=park`, at least 0.5 ha), or `station`, the default, which is the answer above. The
page shows a "Where?" select next to "When do you meet?", set to a pub and listing only the
kinds the build has (`/api/transit/status` → `venues.kinds`); with no venues in the build it
hides the select and meets at stations. A group with nobody on public transport ignores `venue`.

- **Where the venues come from.** `python -m transit.venues --build <id>` (also the build's
  `venues` stage) reads the build's own OSM clip with osmium and writes `venues.csv` and
  `venues.json` next to the tables. Only named venues count, not ones tagged `access=private`
  or `no`; a venue drawn twice within 50 m is kept once. A pub, bar, café or restaurant
  drawn as a building is pinned inside it. A park is pinned at the closest station within
  800 m when that station stands in the park, else at the park's edge nearest it, else inside
  the park. The server loads `venues.csv` only when `venues.json` records its size and sha256
  and was made from the same OSM clip as the tables (`osm_clip_sha256` equal to the manifest's
  `inputs.osm_clip.sha256`), and drops rows with an unknown kind, no name or a point off the
  globe. Otherwise it logs why and serves stations only, and the tables load either way. A
  build without venue files is fine.
- **Times.** A venue's time is the best, over the stations and table cells whose walksheds
  reach its cell within 10 minutes (about 670 m), of that point's table time plus the walk
  (#1's formula). When its own cell has no such walk, the nearest ring of cells that has one
  is used, 2 minutes a ring, counted inside the 10. A venue no point reaches within 10
  minutes is left out. Someone who starts within 150 m of a venue in a straight line may walk
  there straight, 1.3 times the straight line at 4 km/h, when the pin and the venue share an
  access point: the same short reach the warp field walks straight from a station, too short
  to cross the Thames or a railway yard. Road people are timed by Mapbox as above, and the
  walk from where Mapbox ends the trip is added. A road person's reach (`MODE_RADIUS_KM`) is
  checked against stations and venues together, so a venue everyone can reach is never
  refused for want of a station everyone can reach.
- **Scoring.** Stations and venues of the kind asked for are scored together. The answer is
  made from the venues only, by the rules above (fairest, quickest, 500 m apart, five places, the
  same tie lines). It falls back to the station answer, with a note that says why
  (`venue_fallback`), when:
  - the build has no venues of that kind (`"not_loaded"`);
  - none is within a 10-minute walk of public transport (`"none_in_reach"`);
  - some are, but none works for everyone (`"none_work"`): over someone's time limit, beyond a
    road person's reach, too far from a road, or not on the road shortlist;
  - the fairest venue's longest trip is more than 10 minutes over the fairest station's
    (`"slower"`; exactly 10 still gives the venue, and the note never shows the gap as 10).

  For road people a third of the places on the Matrix shortlist are stations and the venues
  on it are at least 250 m apart, so the comparison costs no second request. The station
  compared with, and shown on a fallback, is then the best of the stations checked by road,
  and the answer says how many. `diagnostics.path` is `"venues"` or `"stations"`, with
  `venue_pool`, `best_venue_max_min` and `best_station_max_min`.
- **What it can't tell.** Opening hours: a venue may be closed, and the page says so. OSM is
  edited by anyone, so a venue may also be gone; names are cleaned and capped at 80 characters.

The table times are a rough estimate (`docs/transit-spike.md`: unbiased street to street,
about 5 min fast at a station end), and the tables walk at 4 km/h where Mapbox walks at
about 5, so in a mixed group a person on public transport can look a few minutes better
off than they are.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/travel-time-field` | A travel-time field for a pin, mode and (transit) timetable window |
| `GET`  | `/api/transit/status` | Whether public transport times are available: windows, coverage, credits, validity |
| `POST` | `/api/isochrones` | Raw isochrone polygons (via Mapbox Isochrone API; the meetup page uses it) |
| `GET`  | `/tiles/{z}/{x}/{y}` | Mapbox vector tile proxy |
| `GET`  | `/glyphs/{fontstack}/{range}` | Mapbox font glyph proxy |
| `POST` | `/api/meetup/solve` | Find optimal meeting point (via Mapbox Matrix API) |
| `POST` | `/api/geocode` | Place search (via OpenStreetMap Nominatim, queued at 1 request/s, cached) |

Errors are returned as `{"code": "<stable code>", "detail": "<fixed sentence>"}`. They never
include upstream URLs, tokens or upstream response bodies. One error adds two fields:
`422 no_feasible_meeting_point` has `reason` (`no_route`, `over_time_limit`,
`no_common_place` or `off_street`) and `who`, a list of `{index, reason}` where `index` is
the person's position in the request; for someone cut off (`reaches_few`, under
`no_common_place`) it also has `reach` and `of`, the places they reach of those checked. The page turns that into a sentence with names; the server never
sees names and never echoes request data.

The server-side warped-map renderer (`/api/warped-map`, which decoded vector tiles in Python)
was removed because no page used it. It is in git history before the commit that removed it
(see `git log -- server/warp/vector_transform.py`).

## Limits

Every request to a billable Mapbox API goes through one guard (`server/guard/`). For each
request, in this order:

1. **Cheap refusals.** The request is refused at once if the API's daily or monthly budget
   is spent (`503 daily_budget_exhausted` / `monthly_budget_exhausted`, with
   `Retry-After`), or if this client has used its share of the day (`429 client_daily_limit`).
2. **In flight.** A small bound on concurrent requests per API (wait up to 10 s, then
   `503 busy`).
3. **Rate.** Per-token windows: Matrix 60/min (driving-traffic 30/min), Isochrone 300/min.
   Wait up to 10 s for a slot, then `503 busy`. The slot is taken right before the send, so
   the window tracks real send times.
4. **Reserve and send.** Units are reserved in the budget (Matrix: elements, meaning
   origin–destination pairs; everything else: requests), retries included. Nothing is
   refunded once sent.

- **Budgets.** Monthly defaults are 90% of Mapbox's free tier. Daily caps default to monthly
  ÷ 28. Counters are stored in SQLite at `USAGE_DB_PATH`. If the store can't be opened,
  billable requests are refused and `/api/meetup/health` answers 503.
- **Client share.** One client (an IPv4 address, or an IPv6 /64) may use at most
  `CLIENT_DAILY_SHARE` (default 25%) of any API's daily budget. These counts live in memory
  and reset on restart.
- **Glyphs.** Glyph requests have not been charged on this Mapbox account, so their cap
  (`GLYPHS_MONTHLY_BUDGET`, default 180,000 a month) is an abuse bound, set like the tiles
  cap. A refused or failed glyph range is served *empty*: maps draw without labels.
  MapLibre drops the whole tile when a glyph range fails, so refusing
  would blank the map.
- **Per IP.** Per-route limits per client (`429 rate_limited` with `Retry-After`). The client
  is the `TRUSTED_PROXY_HOPS`-th entry from the right of `X-Forwarded-For`, read across all
  header lines. uvicorn runs with `--no-proxy-headers`, so this is the only parser. The app
  warns once if the header arrives while `TRUSTED_PROXY_HOPS=0`.

Requests are capped before any work starts:
- 2–8 participants and `margin_km` ≤ 10;
- a search radius of at most 15 km (walking), 40 km (cycling) or 150 km (driving) for the
  group's slowest mode;
- 1–4 travel times of at most 60 minutes (`/api/isochrones`);
- a travel-time field: two isochrone requests per new pin and mode (5-minute cache), 20 a minute
  per client (`PER_IP_TRAVEL_TIME_FIELD_PER_MIN`);
- Whole city's files: 120 a minute per client (`PER_IP_WHOLE_CITY_PER_MIN`), served from memory,
  gzipped once at start-up;
- API bodies of at most 16 KiB.

A meetup search is then *sized to fit*. The candidate count starts at `max_candidates`
(≤ 300) and shrinks, down to 20, until the search fits all of these:
- 4,000 Matrix elements;
- the Matrix rate window;
- what is left of today's budget and of this client's share;
- the rate slots free right now.

The plan counts all three stages: the second is capped at the same count, the third at 21.
The count shrinks before the third stage is given up: a busy rate window can shrink the
second stage's cap while the third stays. If even 20 candidates don't fit, the third stage
is dropped from the plan (the answer then has the "less precise" note), and if that still
doesn't fit, the search is refused before any Mapbox call. The best of the stages is kept. If
a finer stage can't run (budget, rate or upstream trouble), the earlier result is returned
with a note (`diagnostics.stage` says which). One search runs at a time.

A search may also spend a little beyond its plan on recovery: up to 10 more requests per
travel mode (never above the bucket's rate) for asking again after a bad reply or splitting
a NoRoute reply. Admission checks only the plan. A recovery request is sent only if, after
it, every planned request still to be sent has its requests, elements and free rate slots;
it never waits for a slot, and if the guard refuses it for any reason the places it would
have checked are left unchecked and counted. A 429 retry belongs to a planned request and
waits as before.

All of this is per process. **Run one worker on one replica.** The start commands pass
`--workers 1`; the app warns if `WEB_CONCURRENCY` > 1. Adding workers or replicas needs a
shared store first.

Deploy checklist (the app logs a warning at startup for each item it can detect):
- `TRUSTED_PROXY_HOPS`: it defaults to 1 on Railway (detected from Railway's environment
  variables) and 0 elsewhere. Check from two networks (one on a phone's mobile
  data) that each gets its own per-IP limit, and that a forged `X-Forwarded-For` doesn't
  reset it.
- A persistent volume with `USAGE_DB_PATH` on it. Otherwise this month's counters reset on
  every redeploy.
- One replica.
- The caps checked against the Mapbox account's real remaining allowance. Other apps on the
  same account share it.
- `GEOCODER_USER_AGENT` with a contact address, as Nominatim's policy asks.
- `TRANSIT_DIR` pointing at a build whose manifest says `licences_cleared: true` (the owner
  holds the rail and TfL licences, `docs/transit-sources.md`); build 2026-11-12 predates the
  flag, so use the next monthly build. Never set `TRANSIT_ALLOW_UNCLEARED` on a server anyone
  else can reach: a tunnel or a proxy on the same machine that adds no forwarding header
  makes every visitor look local.
- For Whole city, `TIMEMAP_DIR` pointing at the time-map folder made from that same build
  (`transit/out/timemap/<build>/`); a folder made from other tables is never served.
- The RSP clause 3.3.2 notice settled (`[publication] rsp_notice` in `transit/sources.toml`; until
  then `transit.build publish` refuses), and shown with the transit credits if RSP asks for it.
- Mapbox's terms for drawing Streets tiles with our own renderer (the time map) confirmed
  (`docs/provider-terms.md`).
- No CORS origin to configure: the API is same-origin only, so there is no allow-list.

## Privacy rules

Participant locations are often people's homes.

- The application never logs locations, place-search queries or request bodies. Upstream
  errors are logged by API and status only. Unexpected errors are logged by type and code
  location, without their message. Tile and glyph paths are dropped from uvicorn's access
  log, because they show where a visitor is looking.
- Outside our control: uvicorn's access log still records the socket peer's address (the
  proxy on Railway, the visitor when run directly), and Railway's own edge logs keep full
  request paths, including `/tiles/z/x/y`.
- Place searches are POSTed to our server, so they stay out of URLs and access logs, and are
  then sent to Nominatim. The pages say so next to the search box.
- Travel-time fields are POSTed too. The server keeps each one in memory for 5 minutes, keyed
  by the pin rounded to about 11 m, and writes nothing to disk.
- What one tool hands the next (From here's pin, Meet up's first person, the way of travelling
  and the time of day) and Meet up's group, kept for the trip back (`static/shared/handoff.js`),
  stay in the browser: this tab's `sessionStorage`, the pin rounded to about 11 m, never in a URL.
  The records themselves are never sent to our server; a tool that opens with a handed-on pin
  uses it as if it were placed there (From here POSTs it for a travel-time field, as above). They
  go when the tab closes.
- A station answer's "Plan journey" links go to Google Maps directions (the owner's choice,
  2026-11-15). The page builds them; the server never contacts Google. A link sends that
  person's start point and the station to Google only when it is clicked, and the answer
  says so under the times. Links open in a new tab with `rel="noopener noreferrer"`.
- Meetup share links (`static/meetup/share.js`) keep the group in the URL fragment
  (`#g=1&p=lat,lon,mode[,name]&p=…`, with the commas and any name percent-encoded), which the
  browser never sends to a server. Start points are rounded to 3 decimals (about 100 m)
  unless that person's "Exact start point" box is ticked (5 decimals); the boxes start
  unticked and untick whenever the group changes. A name goes in only if someone typed it
  (the saved example's names don't count); search labels, answers and ids never do; names
  lose control and invisible characters. Opening a link finds a place for the group once and
  moves the link from the address bar into that tab's history entry (`history.state`), so a
  reload, or a phone reloading a tab it discarded, brings the group back without finding
  again (press Find). Once the group is edited, the tab forgets the link. A link is still
  readable by anyone who has it, and stays in browser history.

Provider terms and where we stand on each: [docs/provider-terms.md](docs/provider-terms.md).

## Acknowledgements

This project is built on top of several excellent open-source projects and APIs:

- **[Mapbox](https://www.mapbox.com/)** — Isochrone API, Directions Matrix API, Vector Tiles, and Font Glyphs power the core mapping and routing functionality
- **[OpenStreetMap](https://www.openstreetmap.org/copyright)** contributors — map data, and place search via [Nominatim](https://nominatim.org/)
- **[MapLibre GL JS](https://maplibre.org/)** — open-source map rendering library
- **[H3](https://h3geo.org/)** — Uber's hexagonal hierarchical spatial index
- **[FastAPI](https://fastapi.tiangolo.com/)** — Python web framework

## License

All rights reserved.
