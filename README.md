# Time-Map

Interactive tools for visualising travel time and finding fair meeting points, built with **FastAPI**, **MapLibre GL JS**, and **WebGL2**.

Powered by [Mapbox](https://www.mapbox.com/) — this project relies on several Mapbox APIs for its core functionality:

- **[Mapbox Isochrone API](https://docs.mapbox.com/api/navigation/isochrone/)** — generates travel-time contour polygons used by the warp map
- **[Mapbox Directions Matrix API](https://docs.mapbox.com/api/navigation/matrix/)** — computes real network travel times between participants and candidate meeting points in the meetup tool
- **[Mapbox Vector Tiles](https://docs.mapbox.com/api/maps/vector-tiles/)** — provides the base map geometry (roads, buildings, labels) rendered via MapLibre GL
- **[Mapbox Fonts / Glyphs](https://docs.mapbox.com/api/maps/fonts/)** — serves font glyphs for map label rendering

| Tool | Question | What it shows |
|------|----------|---------------|
| **Warp map** (`/warp/`) | How far can I get from here? | A pin and the 5/10/15/20-minute travel-time lines around it. "Show time map" bends the map so those lines become circles: distance from the pin means travel time. |
| **Meetup** (`/meetup/`) | Where should we meet? | Where everyone starts, and the place that keeps the longest trip shortest ("Fairest") or the average trip shortest ("Quickest on average"), with each person's minutes. |

Both open on a saved London example, so the first view makes no routing request.

### Built with

- **Warp map:** isochrones from the Mapbox Isochrone API, turned into a radial lookup table
  per angle on the server; a WebGL2 GLSL fragment shader bends the MapLibre frame per pixel.
- **Meetup:** candidate points on an H3 hexagonal grid, travel times from the Mapbox
  Directions Matrix API, and a minimax (fairest) or weighted-sum (quickest on average)
  objective. The API also accepts a `hybrid` blend, which the page no longer offers.
- **Both:** FastAPI, MapLibre GL JS, vanilla JavaScript modules with no build step.

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
| `/` | Landing page |
| `/warp/` | How far can I get? (warp map) |
| `/meetup/` | Where should we meet? (meetup) |

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
├── warp/                        # Isochrone warp module
│   ├── routes.py                # POST /api/warp-params
│   ├── pipeline.py              # Orchestrates isochrone → warp LUT
│   ├── warp.py                  # RadialIsochroneWarp engine
│   ├── geometry.py              # Viewport, coordinate conversions
│   ├── models.py                # Request models (travel-time caps)
│   └── mapbox_client.py         # Mapbox Isochrone fetching, bounded cache
└── meetup/                      # Meetup optimizer module
    ├── routes.py                # POST /api/meetup/solve
    ├── models.py                # Pydantic request/response models
    ├── limits.py                # Pre-flight: radius, candidate and cost caps
    ├── candidates.py            # H3 hex candidate generation
    ├── routing.py               # Mapbox Directions Matrix: batching, strict replies, NoRoute
    └── solver.py                # Scoring, refinement, snapped and named answers
static/                          # Frontend assets (vanilla JS modules, no build step)
├── shared/                      # ui.css, api.js (errors, search), latest.js (latest answer only)
│   ├── mesh/                    # Mesh renderer spike: warp, tiles → mesh worker, WebGL2 renderer
│   └── vendor/                  # pbf, @mapbox/vector-tile, point-geometry, earcut (ES modules)
├── warp/                        # Warp map — app.js (WebGL2), warpmath.js, example.json
│   └── lab/                     # /warp/lab/: renderer lab (unlinked, synthetic travel times)
├── meetup/                      # Meetup — app.js, words.js (its sentences), example.json
├── portfolio/                   # Landing page
├── manifest.json                # PWA manifest
├── sw.js                        # Service worker (versioned per deploy, network first)
└── icons/                       # PWA icons
scripts/make_examples.py         # Regenerates the two saved examples
scripts/try_solves.py            # Runs fixed meetup groups against a server (timings, diagnostics)
tests/js/                        # node --test: warp maths, mesh renderer, latest-answer helper, meetup sentences
docs/warp-renderer-spike.md      # Mesh renderer spike (#5, W-01): what was built, measured and decided
```

## Tests

```bash
ruff check . && pytest
node --test tests/js/*.test.js   # Node 20+, no packages to install
```

`tests/test_meetup_solver.py` checks the solver against brute force (within a minute of the
best cell at the final resolution, on 100 seeded cases) and checks that the order of people
doesn't change the answer. `tests/test_matrix_router.py` covers batching, strict replies and
NoRoute; `tests/test_meetup_robustness.py` sends 100 random requests at a misbehaving Matrix
API.

To time real solves, run a server and `python scripts/try_solves.py http://localhost:8000
--repeat 4 --pause 15`. Against live Mapbox, cap the spend on that server first
(`MATRIX_DAILY_BUDGET=8000`, `CLIENT_DAILY_SHARE=1` so the one client running the script may
use all of it, and a scratch `USAGE_DB_PATH`). The p95 it prints counts only solves that
answered 200, and it says so when some didn't.

## Opening examples

`static/warp/example.json` and `static/meetup/example.json` are real answers for a London
example (King's Cross, walking; three friends cycling from Angel, Vauxhall and Marble Arch). They are what each page shows before
anyone changes anything, so opening a page costs no routing request. Regenerate them after
changing the warp maths or the response shapes:

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
touches `/api/`, `/tiles/`, `/glyphs/` or any other site, MapLibre's CDN included.

## How the Warp Works

The `RadialIsochroneWarp` samples each isochrone boundary at 2048 angular bins, records the max radius per bin, fills gaps via circular interpolation, then smooths with a circular moving average.

**Transform regions (center outward):**

1. **Center → first contour** — linear scaling
2. **Between contours** — lerp between adjacent isochrone radii
3. **Outer contour → support boundary** — blend to identity (unwarped)
4. **Beyond support** — identity pass-through

The frontend renders via a GLSL fragment shader that reads a 2048x1 LUT texture and performs the inverse warp per-pixel in real time. MapLibre GL renders tiles off-screen; the shader samples the resulting texture with warp-corrected UVs.

World pixels follow MapLibre's convention (a 512-px world at zoom 0), so a radius computed at
zoom *z* is the same number of screen pixels on the map at zoom *z*. The page asks for one
answer per (pin, travel mode) at a fixed reference zoom (walking 14, cycling 12, driving 11)
and rescales it by 2^(zoom − reference) as you zoom, around the pin's screen position. Panning,
zooming and resizing therefore never send a request, and nothing is fetched while the normal
map is showing: moving the pin there shows only the pin, and "Show time map" fetches the answer
for the newest pin and mode, draws its lines, then bends them. Where a bent area's source is off
screen, it is drawn grey and the legend says to zoom out. The shader reads the lookup table with
`texelFetch` and interpolates between angles itself, because 32-bit float textures are not
filterable on every device. `static/warp/warpmath.js` holds the same maths for the overlay
and the tests.

Isochrone polygons are fetched from the **Mapbox Isochrone API** and vector tile geometry from the **Mapbox Vector Tiles API**.

### The mesh renderer (spike)

`static/shared/mesh/` is the renderer meant to replace the screen re-sampling above (#5,
W-01; whether it does is decided once it has run on phones and real tiles). It draws vector geometry (water, parks, rail, main roads) through a mesh whose vertices
move: `screen = mix(ordinary(p), pin + k·target(p), u)`, where `target()` is a time-polar
layout (bearing kept, distance proportional to travel time) or any layout given as a grid of
positions (the London map, #6). Lines are extruded in screen pixels after the warp, so they
stay sharp, and a place 20 km away can be drawn next to the pin. `warp.js` holds the
`lon/lat → screen` function and its GLSL twin. A worker fetches and tessellates tiles once per
tile; the morph only changes a uniform. It needs WebGL2 and no float-texture extension.
`/warp/lab/` (not linked from anywhere) compares the three back-ends and runs the checks; it uses
synthetic travel times and, unless `?geom=tiles` is given, synthetic geometry, so it needs no
tiles token and spends no tile budget. `scripts/lab_headless.mjs` drives it in a headless Chrome. Results and the
decision are in [docs/warp-renderer-spike.md](docs/warp-renderer-spike.md). The `/warp/` page
does not use it yet.

## How the Meetup Works

1. Generate candidate meeting points on an [H3 hexagonal grid](https://h3geo.org/) covering the participants' bounding box
2. Query the **Mapbox Directions Matrix API** for actual network travel times from each participant to each candidate. Requests are batched by origins and destinations to fit each profile's coordinate limit, and a stage's requests run at once, three at a time.
3. Score candidates using the chosen objective:
   - **minimax** — minimise the longest individual travel time (fairest)
   - **sum** — minimise weighted average travel time (most efficient)
   - **hybrid** — blend of both (API only)
4. Refine: score the children of the best coarse cells at the next finer resolution, and keep
   whichever stage has the better answer, so refining never makes it worse.
5. Return the best meeting point, top-k runners-up and a meeting area polygon.

Each answer is shown where the Matrix API snapped it onto the street network, with the
street's name when Mapbox gives one. Each travel mode's times are to that mode's own snapped
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
answers `502 upstream_error`. It is never read as "unreachable".

People are named only from replies that answered for them. An unchecked pair says nothing
about anyone's start: more than a quarter of a stage's places unchecked fails the search
(`422 unroutable_area`), or, in the finer stage, keeps the coarse answer.

An answer that is less good than usual says so in `notes` (shown on the page): places were
left unchecked, the finer stage didn't finish, few places were near a street, some trips
end a short way from the star, or the map service sent a bad reply that was asked for
again. `diagnostics` counts what was dropped (`dropped_no_route`, `unchecked_for_budget`,
`dropped_off_street`, over the stages the answer was chosen from), retried (`retries`,
`bad_replies`) and sent (`matrix_requests`, `matrix_elements`). `fine_resolution` is the
finer resolution searched and `answer_resolution` the one the answer came from.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/warp-params` | Warp LUT + ring metadata for client-side shader |
| `POST` | `/api/isochrones` | Raw isochrone polygons (via Mapbox Isochrone API) |
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
- 1–4 travel times of at most 60 minutes;
- API bodies of at most 16 KiB.

A meetup search is then *sized to fit*. The candidate count starts at `max_candidates`
(≤ 300) and shrinks, down to 20, until the search fits all of these:
- 4,000 Matrix elements;
- the Matrix rate window;
- what is left of today's budget and of this client's share;
- the rate slots free right now.

If even 20 candidates don't fit, the search is refused before any Mapbox call. The finer
second stage is capped at the same count, and the better of the two stages is kept. If the
finer stage can't run (budget, rate or upstream trouble), the coarse result is returned with
a note (`diagnostics.stage` says which). One search runs at a time.

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
- Shareable links are still planned. When they come, keep the scenario in the URL fragment,
  which is never sent to the server. Round locations unless their owner opts in.

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
