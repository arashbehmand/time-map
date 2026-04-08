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
MAPBOX_ACCESS_TOKEN=pk.your_token_here
```

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
├── config.py                    # Shared config (MAPBOX_TOKEN, warp constants)
├── warp/                        # Isochrone warp module
│   ├── routes.py                # POST /api/warped-map, /api/warp-params
│   ├── pipeline.py              # Orchestrates isochrone → warp → tiles → transform
│   ├── warp.py                  # RadialIsochroneWarp engine
│   ├── geometry.py              # Viewport, coordinate conversions
│   ├── vector_transform.py      # Geometry warping + label extraction
│   └── mapbox_client.py         # Mapbox Isochrone + vector tile fetching, TTL cache
└── meetup/                      # Meetup optimizer module
    ├── routes.py                # POST /api/meetup/solve
    ├── models.py                # Pydantic request/response models
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
| `POST` | `/api/warped-map` | Full warped map frame (GeoJSON + labels) |
| `POST` | `/api/warp-params` | Warp LUT + ring metadata for client-side shader |
| `POST` | `/api/isochrones` | Raw isochrone polygons (via Mapbox Isochrone API) |
| `GET`  | `/tiles/{z}/{x}/{y}` | Mapbox vector tile proxy |
| `GET`  | `/glyphs/{fontstack}/{range}` | Mapbox font glyph proxy |
| `POST` | `/api/meetup/solve` | Find optimal meeting point (via Mapbox Matrix API) |

## Acknowledgements

This project is built on top of several excellent open-source projects and APIs:

- **[Mapbox](https://www.mapbox.com/)** — Isochrone API, Directions Matrix API, Vector Tiles, and Font Glyphs power the core mapping and routing functionality
- **[MapLibre GL JS](https://maplibre.org/)** — open-source map rendering library
- **[H3](https://h3geo.org/)** — Uber's hexagonal hierarchical spatial index
- **[FastAPI](https://fastapi.tiangolo.com/)** — Python web framework

## License

All rights reserved.
