# Time-Map

Interactive tools for visualising travel time and finding fair meeting points, built with **FastAPI**, **MapLibre GL JS**, and **WebGL2**.

Powered by [Mapbox](https://www.mapbox.com/) — this project relies on several Mapbox APIs for its core functionality:

- **[Mapbox Isochrone API](https://docs.mapbox.com/api/navigation/isochrone/)** — generates travel-time contour polygons used by the Isochrone Warp map
- **[Mapbox Directions Matrix API](https://docs.mapbox.com/api/navigation/matrix/)** — computes real network travel times between participants and candidate meeting points in the Meetup Optimizer
- **[Mapbox Vector Tiles](https://docs.mapbox.com/api/maps/vector-tiles/)** — provides the base map geometry (roads, buildings, labels) rendered via MapLibre GL
- **[Mapbox Fonts / Glyphs](https://docs.mapbox.com/api/maps/fonts/)** — serves font glyphs for map label rendering

| Tool | Description |
|------|-------------|
| **Isochrone Warp Map** | A map that bends to travel time. Isochrone boundaries become concentric circles — equal travel time appears at equal distance from you, regardless of direction. |
| **Meetup Optimizer** | Find the fairest meeting point for a group with different starting locations and transport modes. Minimises worst-case or weighted-average travel time using H3 hexagonal search and the Mapbox Directions Matrix API. |

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
| `/warp/` | Isochrone Warp Map |
| `/meetup/` | Meetup Optimizer |

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
    ├── routing.py               # Mapbox Directions Matrix integration
    └── solver.py                # Minimax / hybrid / sum optimisation
static/                          # Frontend assets (vanilla JS, no build step)
├── warp/                        # Isochrone Warp — vanilla JS + WebGL2
├── meetup/                      # Meetup Optimizer — vanilla JS + MapLibre GL
├── portfolio/                   # Landing page
├── manifest.json                # PWA manifest
├── sw.js                        # Service worker (app shell caching)
└── icons/                       # PWA icons
```

## How the Warp Works

The `RadialIsochroneWarp` samples each isochrone boundary at 2048 angular bins, records the max radius per bin, fills gaps via circular interpolation, then smooths with a circular moving average.

**Transform regions (center outward):**

1. **Center → first contour** — linear scaling
2. **Between contours** — lerp between adjacent isochrone radii
3. **Outer contour → support boundary** — blend to identity (unwarped)
4. **Beyond support** — identity pass-through

The frontend renders via a GLSL fragment shader that reads a 2048x1 LUT texture and performs the inverse warp per-pixel in real time. MapLibre GL renders tiles off-screen; the shader samples the resulting texture with warp-corrected UVs.

Isochrone polygons are fetched from the **Mapbox Isochrone API** and vector tile geometry from the **Mapbox Vector Tiles API**.

## How the Meetup Optimizer Works

1. Generate candidate meeting points on an [H3 hexagonal grid](https://h3geo.org/) covering the participants' bounding box
2. Query the **Mapbox Directions Matrix API** for actual network travel times from each participant to each candidate (batched by transport mode)
3. Score candidates using the chosen objective:
   - **minimax** — minimise the longest individual travel time (fairest)
   - **sum** — minimise weighted average travel time (most efficient)
   - **hybrid** — blend of both
4. Return the best meeting point, top-k runners-up, and a meeting area polygon

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/warp-params` | Warp LUT + ring metadata for client-side shader |
| `POST` | `/api/isochrones` | Raw isochrone polygons (via Mapbox Isochrone API) |
| `GET`  | `/tiles/{z}/{x}/{y}` | Mapbox vector tile proxy |
| `GET`  | `/glyphs/{fontstack}/{range}` | Mapbox font glyph proxy |
| `POST` | `/api/meetup/solve` | Find optimal meeting point (via Mapbox Matrix API) |

Errors are returned as `{"code": "<stable code>", "detail": "<fixed sentence>"}`. They never
include upstream URLs, tokens or upstream response bodies.

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
second stage is capped at the same count and replaces the coarse result. If it can't run
(budget, rate or upstream trouble), the coarse result is returned (`diagnostics.stage` says
which). One search runs at a time.

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
- No CORS origin to configure: the API is same-origin only, so there is no allow-list.

## Acknowledgements

This project is built on top of several excellent open-source projects and APIs:

- **[Mapbox](https://www.mapbox.com/)** — Isochrone API, Directions Matrix API, Vector Tiles, and Font Glyphs power the core mapping and routing functionality
- **[MapLibre GL JS](https://maplibre.org/)** — open-source map rendering library
- **[H3](https://h3geo.org/)** — Uber's hexagonal hierarchical spatial index
- **[FastAPI](https://fastapi.tiangolo.com/)** — Python web framework

## License

All rights reserved.
