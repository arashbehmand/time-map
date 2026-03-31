# Urban Data Tools

Interactive tools for visualising travel time and finding fair meeting points, built with **FastAPI**, **MapLibre GL JS**, and **WebGL2**.

| Tool | Description |
|------|-------------|
| **Isochrone Warp Map** | A map that bends to travel time. Isochrone boundaries become concentric circles — equal travel time appears at equal distance from you, regardless of direction. |
| **Meetup Optimizer** | Find the fairest meeting point for a group with different starting locations and transport modes. Minimises worst-case or weighted‑average travel time using H3 hexagonal search and the Mapbox Directions Matrix API. |

## Prerequisites

- Python 3.11+
- A [Mapbox access token](https://account.mapbox.com/access-tokens/)

## Setup

```bash
git clone https://github.com/arashbehmand/urban-data-tools.git && cd urban-data-tools

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

Open [http://localhost:8000](http://localhost:8000) in your browser for the portfolio landing page.

| Route | App |
|-------|-----|
| `/` | Portfolio landing page |
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
│   └── mapbox_client.py         # Isochrone + vector tile fetching, TTL cache
└── meetup/                      # Meetup optimizer module
    ├── routes.py                # POST /api/meetup/solve
    ├── models.py                # Pydantic request/response models
    ├── candidates.py            # H3 hex candidate generation
    ├── routing.py               # Mapbox Directions Matrix integration
    └── solver.py                # Minimax / hybrid / sum optimisation
static/                          # Frontend assets
├── warp/                        # Isochrone Warp — vanilla JS + WebGL2
├── meetup/                      # Meetup Optimizer — vanilla JS + MapLibre GL
└── portfolio/                   # Landing page
requirements.txt
.env                             # MAPBOX_ACCESS_TOKEN (not committed)
```

## How the Warp Works

The `RadialIsochroneWarp` samples each isochrone boundary at 2048 angular bins, records the max radius per bin, fills gaps via circular interpolation, then smooths with a circular moving average.

**Transform regions (center outward):**

1. **Center → first contour** — linear scaling
2. **Between contours** — lerp between adjacent isochrone radii
3. **Outer contour → support boundary** — blend to identity (unwarped)
4. **Beyond support** — identity pass-through

The frontend renders via a GLSL fragment shader that reads a 2048×1 LUT texture and performs the inverse warp per-pixel in real time. MapLibre GL renders tiles off-screen; the shader samples the resulting texture with warp-corrected UVs.

## How the Meetup Optimizer Works

1. Generate candidate meeting points on an [H3 hexagonal grid](https://h3geo.org/) covering the participants' bounding box
2. Query the **Mapbox Directions Matrix API** for actual network travel times from each participant to each candidate (batched by transport mode)
3. Score candidates using the chosen objective:
   - **minimax** — minimise the longest individual travel time (fairest)
   - **sum** — minimise weighted average travel time (most efficient)
   - **hybrid** — blend of both ($\alpha \cdot \text{minimax} + (1 - \alpha) \cdot \text{sum}$)
4. Return the best meeting point, top‑k runners-up, and a meeting area polygon

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/warped-map` | Full warped map frame (GeoJSON + labels) |
| `POST` | `/api/warp-params` | Warp LUT + ring metadata for client-side shader |
| `POST` | `/api/isochrones` | Raw isochrone polygons for a single point |
| `GET`  | `/tiles/{z}/{x}/{y}` | Mapbox vector tile proxy |
| `GET`  | `/glyphs/{fontstack}/{range}` | Mapbox font glyph proxy |
| `POST` | `/api/meetup/solve` | Find optimal meeting point |

## License

All rights reserved.
