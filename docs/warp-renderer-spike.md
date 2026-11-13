# Warp renderer spike: the GPU mesh the London map reuses

Issue #5, part W-01 (Direction 5). Written 2026-11-13 against `master` at 002af68.

**Status.** The renderer, the lab and the checks are built.

- **Desktop results** (Apple M2, headless Chrome 157): back-ends B and C pass every gate the lab
  can measure, except G4 on one field at one origin (the cliff field at Wembley Park, see
  [Chord error per field](#chord-error-per-field-and-origin)).
- **The deck.gl probe (A)** fails the line-width gate.
- **Real tiles** (Mapbox Streets v8, [Results: real tiles](#results-real-tiles)):
  - the renderer passes all five drawing gates;
  - a transit scene is 1.1M–3.3M vertices shaded at 48 m cells in the census, and 3.7M for Canary
    Wharf as the lab draws it: inside the 5M budget. A bent frame of that scene takes 15–24 ms of
    GPU time on the M2.
- **The bent map** gives each direction angle by how much the field reaches there, and lets the
  base map step back as it bends ([On the bent map](#on-the-bent-map-directions-by-reach-the-style-the-network)).
- **Not yet measured:** the two phones.

B is the chosen renderer, with C as its fallback. It still has to be confirmed on the phones. The
steps are in [What is left](#what-is-left).

All travel times here are **synthetic** (`static/warp/lab/stations.json`, made-up speeds over
hand-typed station positions). So is the lab's default geometry. They test the renderer; they
say nothing about transit in London.

## What was built

| Path | What it is |
|------|-----------|
| `static/shared/mesh/frame.js` | Coordinate frames: Web Mercator, local metres around an origin, MapLibre's 512-px camera (with its origin) |
| `static/shared/mesh/field.js` | A travel-time field (minutes on a 100 m grid, −1 = no data) and a grid of target positions (`GRID_NO_DATA` for holes) |
| `static/shared/mesh/warp.js` | The warp: `toScreen(lon, lat, cam, u)`, `placement()`, `jacobian()`, `screenJacobian()`, and `WARP_GLSL`, its shader twin |
| `static/shared/mesh/cover.js` | Which tiles to load: a quadtree, finer near the origin, over what the field reaches |
| `static/shared/mesh/tessellate.js` | Clip to the tile, split lines, cut fills into grid cells and earcut them; the budget |
| `static/shared/mesh/schema-streets-v8.js` | Everything about the tile schema: layers, classes (slip roads included), colours, widths, labels |
| `static/shared/mesh/pipeline.js` | decode → classify → tessellate a tile; pack tiles into one buffer per pass, stamped with their origin |
| `static/shared/mesh/worker.js` | Module worker: fetches `/tiles` (shared across scenes), caches tessellated tiles, packs scenes |
| `static/shared/mesh/caps.js`, `renderer.js` | The WebGL2 renderer, its capability probe and its transform-feedback evaluator |
| `static/shared/mesh/labels.js` | Label placement and the tap index |
| `static/shared/vendor/` | pbf 5.1.2, @mapbox/vector-tile 3.0.0, @mapbox/point-geometry 1.1.0, earcut 3.2.4 |
| `static/warp/lab/` | The lab page (`/warp/lab/`): fields, synthetic geometry, self-test, benchmark, census, deck.gl probe |
| `scripts/lab_headless.mjs` | Drives the lab in a headless Chrome; every desktop number here came from it |
| `docs/warp-renderer-spike-results.json` | The raw self-test and benchmark output behind the tables below |

`/warp/`, `warpmath.js` and `server/` are unchanged. The lab is not linked from anywhere, is
`noindex`, and is not in the service worker's app shell.

### How it draws

```
screen(p) = mix(ordinary(p), pin + k · target(p), u)        p = local metres
```

**The terms:**

- `ordinary(p)` is MapLibre's projection, so at u = 0 the mesh lines up with the normal map.
- `target(p)` is the layout:
  - **polar** keeps the bearing from the origin and sets the distance to the field's minutes
    (exact at every vertex);
  - **grid** interpolates a grid of target positions (any layout, e.g. #6's).

**Places with no target.** Where the field has no data, or a place is past the outer minute, it
fades out as u → 1. It still moves:

- polar: onto the outer ring along its own bearing;
- grid: it stays where it is.

So a triangle or segment with one known and one unknown corner stays short and inside the
ring; it never streaks out to the unbent map. Lines fade per end, along the segment. What to
show past the outer ring is still #5's open question; this is the default.

**Origins.** A scene is packed in metres around one origin and says so (`scene.origin`).
`draw()` refuses a camera or warp from another origin. The lab switches origin, warp, texture
and scene together, when the new scene arrives, and keeps the old view on screen until then.

**Three back-ends** share the worker, the buffers and the draw calls:

- **B (gpu):** the vertex shader evaluates `target()` from an R32F (polar) or RG32F (grid)
  texture, with `texelFetch` and hand-written bilinear interpolation.
- **C (cpu):** the worker evaluates `target()` for every vertex and the shader only mixes by u.
  It needs no textures.
- **A (deck.gl probe):** deck.gl 9.4.1's `LineLayer` and `SolidPolygonLayer` draw the same
  buffers. A `LayerExtension` moves each vertex in `vs:DECKGL_FILTER_GL_POSITION` and fades in
  `vs:DECKGL_FILTER_COLOR`.

**Geometry:**

- **Lines** are one instanced quad per segment. They are extruded in screen pixels after both
  ends are mapped, and drawn as a capsule with analytic anti-aliasing.
- **Fills** are cut into grid cells and drawn with no face culling, so folded (flipped)
  triangles still show.
- **One ground cell at every zoom.** The cell is about 48 m: 128 cells per z12 tile edge, 32 per
  z14, 512 per z10. So tiles of different zooms put the same vertices on their shared edge, and
  the warp can't open a crack there. The tile zoom only sets how generalised the source
  geometry is.
- **Tile meshes** are cached per tile and don't depend on the origin. A new origin repacks
  cached tiles into local metres; float32 is millimetre-exact at 25 km.
- **"Tint folds"** (lab) draws a layer built from the field's own cells, only where it flips,
  under the lines. So every fold shows, not only folded parks and water.

### On the bent map: directions by reach, the style, the network

With exact bearings, a fast line's whole reach lands in a thin wedge. From Canary Wharf, central
London fills about 30° to the west-north-west, its walking areas stacked on each other, every road
drawn at full strength. Three things make the bent map readable.

**Directions by reach** (`angularSpread` in `warp.js`; the lab's "Directions" menu, `?spread=`):

- On each ring of minutes, every direction gets the share of the circle that it holds of the area
  reachable within that time, mixed with an even share. The lab's default mixes half and half
  (`spread` 0.5); 1 is the area share alone, 0 keeps every bearing.
- Within a short walk the reach is a disc, so near the pin bearings stay as they are. Further out,
  the directions a fast line reaches into open up and the rest close.
- What stays exact: the order of directions (north-west stays between north and west), and the
  radius, which is still minutes. On each ring the remap only stretches angles, so it adds no
  folds; a unit test checks the fold sign.
- What it costs: bearings are no longer true. At half by reach they move by up to 20° (Baker
  Street) to 48° (Croydon) at the 45-minute ring, and the widest 10° sector opens 1.9–3.7×. By
  reach alone: up to 38–96°.
- The table is 36 bearings × 10 rings, interpolated bilinearly: a uniform array for B, used in the
  worker for C. It takes about 1 ms per origin to build (node, M2).
- It belongs to the view, not to the field. Whether the product uses it is W-02's call.

**The bent-map style** (the lab's "Scale-aware style"; `?style=plain` turns it off). Everything in
it is 1 at u = 0, so the ordinary map doesn't change.

- **A line thins where the bent map squeezes it sideways:** its width × √(scale across the line),
  between 0.35 and 1. It never gets wider: the bend magnifies most walking areas (about 4× outward
  in the Canary Wharf wedge), and wider lines there only add weight.
- **Each road and rail class recedes as the map bends,** to its own strength (`bent` in the
  schema): motorway 0.8, trunk 0.7, primary 0.55, secondary 0.4, main rail and tunnels 0.45,
  minor rail 0.35. On the bent map the base map is context; the stations and the network carry the
  picture.
- **Folds:** lines on a fold fade to 0.4, folded parks and water to 0.45.
- **Station dots** shrink where the map is squeezed (never grow), and a labelled station's dot is a
  little bigger. Rail is drawn above roads.
- **Cost:** B computes the local scale in the vertex shader (four more field samples per segment);
  C gets it from the worker as one more attribute (16 bytes per segment). Neither costs anything
  measurable: a bent frame of the 3.7M-vertex Canary Wharf scene takes 14–16 ms of GPU time on the
  M2 with the style on or off.

**The network** (lab only; "Network", `?network=0` hides it): the toy lines drawn straight from
stop to stop through the warp, in their own colours, fading in over the morph. On a time map the
ride between two stations has no geography of its own, and the tiles' rail between them crosses
the gaps between walking areas and smears. W-02 can draw #1's real network the same way.

### The contract for the London map (#6)

```js
import { createTargetGrid, GRID_NO_DATA } from "/shared/mesh/field.js";
import { createWarp } from "/shared/mesh/warp.js";
import { makeCamera } from "/shared/mesh/frame.js";
import { createMeshRenderer } from "/shared/mesh/renderer.js";

// 1. Rasterise the layout into a grid of target positions (local metres around `origin`);
//    a node with no target gets x = GRID_NO_DATA.
const grid = createTargetGrid({ origin, cell: 500, nx, ny, x0, y0, xy /* Float32Array, 2 per node */ });
const warp = createWarp({ kind: "grid", grid });
// 2. Per vertex, the same function the shader runs (labels, hit tests, checks):
const { x, y, alpha } = warp.toScreen(lon, lat, camera, u);   // CSS px
// 3. Draw: renderer.setWarp({kind: "grid", grid}); renderer.setScene(scene); renderer.draw(camera, u)
//    (scene from packScene around the same origin, or draw() returns {skipped}).
```

- **Camera.** `camera` comes from `makeCamera(warp.frame, {centerLon, centerLat, zoom, width,
  height, k})`, or `cameraFromMap(map, warp.frame, k)`, and carries its origin.
- **`k`.** For a grid it is the camera's own `ppm`, so layout units are metres.
- **Folds and stretch.** `warp.screenJacobian(x, y, camera, u)` gives fold and stretch for the
  map as it is on screen now.
- **Shader side.** `WARP_GLSL` with `#define WARP_GRID`, or `CPU_TARGETS` for back-end C.
- **No copies.** The renderer keeps no CPU copy of a scene (the lab keeps one, for its checks).

## Gates, fixed before measuring

| Gate | What | Pass |
|------|------|------|
| G1 parity | GPU positions (transform feedback) vs `warp.js`, 1,000 vertices × 2 views × u ∈ {0, ½, 1}; for C, with the worker's own placements | ≤ 0.1 px |
| G2 registration | u = 0 vs `map.project`, at three zooms | ≤ 0.5 px |
| G3 radius | random field points drawn as dots through the line program, centres read back from pixels: \|centre − pin\| vs k·T at u = 1, and the ordinary position at u = 0 | ≤ 1 px; ≤ 0.5 px |
| G4 chord | points on drawn segments and triangles vs the exact warp, sampled by size on screen, away from folds; near folds, near no data, and primitives crossing the outer minute reported on their own | ≥ 99% within 1 px |
| G5 width | widths read back from pixels: a chain of 4 px segments with a 30° kink, single segments at 12 angles, and one segment where the map stretches ≥ 4:1 | each ±0.25 px |
| G6 frames | morph and pan, judged on the phones; desktop records draw() CPU time and GPU time beside the vsync-capped frame interval | phones: p50 ≤ 17 ms, p95 ≤ 33 ms |
| G7 origin change | warm tiles, field ready, bent map → first frame of the new origin with the GPU finished (includes B's texture upload) | p95 ≤ 200 ms (slower phone) |
| G8 new origin | cold tiles → first frame, real tiles only (`?geom=tiles`); a missing tile fails it | p95 ≤ 1.5 s |
| G9 budget | vertices shaded (fill vertices + 6 per line segment per draw; roads draw twice, casing and fill) and tiles per origin | ≤ 5M and ≤ 48, or `overBudget` reported |
| G10 capabilities | each `?caps=` rung draws, no extension required | no console errors |

The reference views for G1, G3 and G4 are 390×844 and 1440×900, with the 45-minute ring at
0.42 of the short side.

**Decision rule (D22, as decided by the owner on 2026-11-13):**

- A back-end passes if it meets G1–G8 on both phones.
- B is the renderer and C is its fallback:
  - The renderer already drops to C on any device where B's start-up probe or its programs fail.
  - If B misses a phone gate that C meets, C is used on that kind of device.
- If neither passes, transit uses the geographic reach view.

The rule first ranked C above B because B needs float textures read in the vertex shader. Those are
core WebGL2, so that need costs nothing on a conformant device.

## Results: desktop

**Setup.**

- Machine: Apple M2, macOS, headless Chrome 157 (ANGLE Metal), DPR 1.
- Geometry: synthetic (the toy rail lines, ring and radial roads, a Thames band, a lake with an
  island, parks).
- Cells: 48 m.
- Origins: Baker Street, Wembley Park, Canary Wharf, Heathrow, Croydon (East Croydon).
- The lab's defaults: half by reach, the bent-map style on, the 5M budget.

**How to read the frame numbers.** Headless Chrome caps animation frames at 60 Hz, so the frame
interval only shows that no frame was missed. The draw-call and GPU times are the useful desktop
numbers.

| Gate | B (gpu) | C (cpu) | A (deck.gl) |
|------|---------|---------|-------------|
| G1 parity | **pass**: max 0.0035 px, 6,000 samples, alphas equal | **pass**: max 0.0001 px, 6,000 samples, alphas equal | not measurable (no evaluator) |
| G2 registration | **pass**: max 0 px | **pass**: max 0 px | **pass**: max 0 px |
| G3 radius (drawn) | **pass**: radius max 0.06 px, ordinary max 0.061 px (300/300 dots) | **pass**: radius max 0.06 px, ordinary max 0.061 px (300/300 dots) | not measured (no pixel read-back) |
| G4 chord, phone view | 100.00% within 1 px, p99 0.305 px | 100.00% within 1 px, p99 0.305 px | 99.83% within 1 px, p99 0.253 px |
| G4 chord, desktop view | 99.40% within 1 px, p99 0.837 px | 99.40% within 1 px, p99 0.837 px | 99.78% within 1 px, p99 0.542 px |
| G4, reported apart | near folds 97.3% (p99 3.525 px); crossing the outer minute: 522 primitives, longest edge at u = ½ 21.1 px | same mesh | same mesh |
| G5 width (px) | **pass**: chain 1.635, kink 1.62, 12 angles 1.46–1.501 (nominal 1.5); stretched 723:1 → 1.814 vs 1.815 | **pass**: chain 1.635, kink 1.62, 12 angles 1.46–1.501 (nominal 1.5); stretched 723:1 → 1.814 vs 1.815 | **fail**: 32% of segments under 75% of their width, 6.1% under 25% (computed) |
| G6 frames (5 origins) | frame p50/p95 16.7/16.7 ms; draw() p95 0.2 ms; GPU ≤ 12.1 ms a frame | frame p50/p95 16.7/16.8 ms; draw() p95 0.2 ms; GPU ≤ 4.96 ms a frame | frame p50/p95 16.7/16.8 ms; draw() p95 3.1 ms (it rebuilds its layers every frame); no GPU timer |
| G7 origin change | **pass**: p50 11.1, p95 14.4 ms | **pass**: p50 12.1, p95 28.2 ms | **pass**: p50 12.8, p95 30.6 ms |
| G8 new origin | not measured (no network); p95 31.1 ms without it | not measured (no network); p95 43.6 ms without it | not measured (no network); p95 58.3 ms without it |
| G9 budget (transit) | **pass**: ≤ 346,195 shaded, ≤ 41 tiles | **pass**: ≤ 346,195 shaded, ≤ 41 tiles | **pass**: ≤ 346,195 shaded, ≤ 41 tiles |

- **G10 rungs:**
  - `?caps=no-vtf` and `?caps=no-r32f` fall back to C.
  - `?caps=no-webgl2` shows the ordinary MapLibre map.
  - Losing and restoring the WebGL context shows the ordinary map, then the mesh again.
- **Probe A's size:** deck.gl adds 577 kB gzipped (`dist.min.js` 9.4.1). Our renderer is 29 kB
  gzipped, plus 21 kB of vendored decoders.
- **B's GPU time** (≤ 12.1 ms a frame, against C's 5.0) is the price of evaluating the warp in the
  shader: each line vertex samples the field six times (both ends and the local scale), each with
  the angle table's `atan`, `cos` and `sin`. On real tiles the two back-ends are level (see
  [Results: real tiles](#results-real-tiles)).

### Stress run

**Setup.** 24 m cells, with nothing shed under the 5M budget. A 390×844 view at DPR 2, with the
CPU throttled 4× by Chrome. The worker's own times don't change under that throttle, so it slows only
the main thread. This run is indicative, not a phone.

| | B (gpu) | C (cpu) |
|---|---|---|
| Shaded per origin | 464,395–728,250 | 464,395–728,250 |
| Frames, worst p50 / p95 | 16.7 / 16.8 ms (worst frame 250 ms) | 16.7 / 16.7 ms (worst frame 50 ms) |
| draw() CPU p95 | 0.8 ms | 0.5 ms |
| GPU time a frame | 6.2–7.89 ms | 2.89–5.53 ms |
| G7 origin change, p95 | 43.6 ms | 60.3 ms |
| Worker pack at Baker Street | 27.8 ms | 59.6 ms |

### Spacing sweep

Baker Street, transit field, phone view, G4's measure (sampled by size on screen).

| Cells | Shaded | Within 1 px | p99 | Near folds within 1 px |
|---|---:|---:|---:|---:|
| 95 m | 167,641 | 99.05% | 0.916 px | 95.3% |
| 48 m | 346,195 | 99.92% | 0.281 px | 98.3% |
| 24 m | 728,250 | 100.00% | 0.088 px | 98.9% |

95 m cells pass only just (99.05%), and only 95% near folds, so the default is 48 m.

### Chord error per field and origin

G4's measure, phone view, away from folds.

| Field | Baker Street | Wembley Park | Canary Wharf | Heathrow | Croydon |
|---|---|---|---|---|---|
| transit | 99.76%, p99 0.268 px | 100.00%, p99 0.179 px | 99.91%, p99 0.258 px | 100.00%, p99 0.133 px | 99.75%, p99 0.616 px |
| walk | 100.00%, p99 0.095 px | 100.00%, p99 0.035 px | 100.00%, p99 0.073 px | 100.00%, p99 0.036 px | 100.00%, p99 0.034 px |
| cliff | 99.96%, p99 0.135 px | 94.92% **fail**, p99 2.117 px | 99.85%, p99 0.091 px | 100.00%, p99 0.298 px | 100.00%, p99 0.071 px |

The cliff field at Wembley Park fails. The river is a 9-minute jump within one field cell, so a
fixed 48 m mesh can't follow it there. The plan named this risk. Its fallback is a mesh
refined per origin where the field jumps (warp-aware re-tessellation in the worker). That
belongs with the real field (W-02), because only #1's field shows where real jumps are.

### Folds and stretch on the synthetic fields

These are renderer diagnostics, not Direction 4's verdict, because the fields are made up.

- **Folded share:** the part of the 45-minute ring covered by folded sheets, counted once where
  sheets stack (a rasterised union).
- **Stacked:** the same with overlaps counted every time.
- **Stretch:** the median over field cells inside the ring, folded ones included, of the largest
  local scale over the smallest.

| Field | Baker Street | Wembley Park | Canary Wharf | Heathrow | Croydon |
|---|---|---|---|---|---|
| transit | 24.8% (stacked 36.7%), 4.9:1 | 19.2% (stacked 30.1%), 3.2:1 | 25.4% (stacked 34.9%), 4.1:1 | 24.1% (stacked 35.4%), 3.1:1 | 19.4% (stacked 27.8%), 3.6:1 |
| walk | 0.0% (stacked 0.0%), 1.0:1 | 0.0% (stacked 0.0%), 1.0:1 | 0.0% (stacked 0.0%), 1.0:1 | 0.0% (stacked 0.0%), 1.0:1 | 0.0% (stacked 0.0%), 1.0:1 |
| cliff | 0.0% (stacked 0.0%), 1.1:1 | 0.0% (stacked 0.0%), 1.1:1 | 0.0% (stacked 0.0%), 1.1:1 | 0.0% (stacked 0.0%), 1.1:1 | 0.0% (stacked 0.0%), 1.1:1 |

Walk and cliff fields don't fold, and their stretch is about 1:1. The toy network misses both
proposed Direction 4 limits (10% folded, 3:1 stretch) at every origin. That is a warning, not a
result: W-02 must run the same numbers on #1's real field.

These are measured at half by reach. With exact bearings the same field folds 8.5–19.7% of the
ring at 4.8–6.7:1. Directions by reach lowers the stretch, and raises the folded share: it makes no
new folds, but the folds lie in the transit directions, which are the sectors it opens.

### Screenshots

Back-end B, synthetic geometry, toy transit field, the lab's defaults (half by reach, the bent-map
style, the network), 1000×700 at DPR 1, taken with `scripts/lab_headless.mjs` (`--size 1000x700`).
URL `/warp/lab/?backend=b&field=transit&origin=…&folds=…`.

| Baker Street, ordinary (u = 0, folds off) | Halfway (u = 0.5, fold layer on) |
|---|---|
| ![Baker Street, u = 0](img/warp-renderer-spike/baker-street-0.jpg) | ![Baker Street, u = 0.5](img/warp-renderer-spike/baker-street-half.jpg) |
| **Baker Street, bent (u = 1, fold layer on)** | **Wembley Park, bent (u = 1, fold layer on)** |
| ![Baker Street, u = 1](img/warp-renderer-spike/baker-street-1.jpg) | ![Wembley Park, u = 1](img/warp-renderer-spike/wembley-park-1.jpg) |

**What the screenshots show:**

- **Halfway:** places past 45 minutes (Clapham Junction, Balham) move towards the outer ring and
  fade. The network fades in; the rings appear only at the end of the morph.
- **Bent:** nothing is drawn outside the 45-minute ring.
- **Baker Street, bent:** Wembley Park is at 20 minutes west-north-west, at the end of the
  Metropolitan line, with the Elizabeth and Piccadilly lines running west and the DLR east.
- **Wembley Park, bent:** the Metropolitan and Jubilee lines pull central London in. Baker Street
  is at 20 minutes, and central London spreads over the south-east quarter rather than a narrow
  wedge, still folded (the pink).
- **Every station** has a dot, with its label beside it. Over synthetic geometry the corner says
  so instead of showing Mapbox's credit.

## Results: real tiles

Mapbox Streets v8 through `/tiles`, the toy transit field, headless Chrome 157 on the M2. Counts only are kept (`docs/warp-renderer-spike-results.json`,
`realTiles`); no tile data or screenshot of it is stored.

**The census:** 110 tiles, none missing.

| Origin | Tiles | Data | Decode (all tiles) | Shaded at 95 m | at 48 m | at 24 m |
|---|---:|---:|---:|---:|---:|---:|
| Baker Street | 27 | 3.3 MB | 84 ms | 1.51M | 3.34M | 8.19M |
| Wembley Park | 28 | 2.3 MB | 71 ms | 0.68M | 1.35M | 3.05M |
| Canary Wharf | 34 | 3.7 MB | 102 ms | 1.45M | 3.18M | 7.76M |
| Heathrow | 20 | 1.8 MB | 52 ms | 0.56M | 1.12M | 2.54M |
| Croydon | 41 | 3.0 MB | 83 ms | 0.80M | 1.60M | 3.65M |

**What the tiles hold:**

- **No `road_label` layer.** Road names are on `road`, so the `/warp/` page's road-label layer draws
  nothing today.
- **Slip roads are common:** 810 `trunk_link`, 451 `primary_link`, 43 `motorway_link`.
- **The Underground is partly there:**
  - 147 `major_rail` features in tunnels (and 52 on bridges), which the mesh draws in the tunnel
    colour;
  - 188 `metro_rail` stations, and 45 `light_rail`;
  - `minor_rail` is rare (45).

  So the deep lines can be drawn from the tiles where they appear, but not reliably everywhere. #1's
  GTFS shapes are the complete source.

**The drawing gates hold on real tiles** (back-end B, Baker Street):

| Gate | Result |
|---|---|
| G1 parity | max 0.0005 px |
| G3 radius | max 0.058 px |
| G4 chord | phone view 99.92%, desktop 99.37% |
| G5 width | chain 1.635, kink 1.62, angles 1.46–1.50 (nominal 1.5); at an 81:1 stretch 1.814 vs 1.815 |

**Real scenes fit the 5M budget** whole, with water, parks and every road class: 1.1M–3.3M
vertices shaded per transit origin at 48 m cells in the census above, and 3.7M for Canary Wharf as
the lab draws it.

**GPU time per bent frame** of the largest, Canary Wharf's 3.7M (median of 12, measured twice per
setting, `bentMapGpu` in the results file):

| | Exact bearings | Half by reach |
|---|---|---|
| B, plain style | 18.8–20.5 ms | 15.0–23.0 ms |
| B, bent-map style | 17.2–23.8 ms | 23.1 ms |
| C, plain style | 16.8–18.1 ms | 19.9 ms |
| C, bent-map style | 15.5–19.2 ms | 17.2–18.7 ms |

Repeats differ more than settings do, so neither directions by reach nor the style costs anything
measurable here, and B and C are level. At about 20 ms a frame the M2 holds 50–60 frames a
second on this scene; a phone will be slower, which is what its G6 run has to show.

**Origin changes** were measured with the earlier 300k budget and 600k worker cache, and not rerun
since the budget became 5M and the cache 8M:

| | B | C |
|---|---|---|
| New origin, warm tiles (G7), p95 | 366 ms (**fail**) | 360 ms (**fail**) |
| New origin, tiles from the browser's cache, p95 | 544 ms | 418 ms |
| Worker tessellation per transit origin | 140–480 ms | 110–360 ms |

**New origin over the network (G8):** 323–613 ms with B. That passes the 1.5 s gate on desktop
broadband, but each origin downloads 1.8–3.7 MB.

- This was measured with a fresh browser profile: Baker Street 425 ms, Wembley Park 323, Canary
  Wharf 613, Heathrow 327, Croydon 493. The server's usage counter rose by exactly the 110 tiles.
- The benchmark's own cold rows were served from the browser's HTTP cache, which the census had
  filled. The DevTools cache switch doesn't reach the module worker.

G7 failed because a real transit scene was bigger than the 600k cache, so going back to an origin
tessellated its tiles again. The 8M cache holds two real scenes, which should remove that; the
phone runs will show it.

**If a phone can't carry a real scene**, the geometry can be simplified first: lines simplified at
the tile's own scale before they are cut, trunk and slip roads only nearer the origin, and the
shed order (which still applies above the budget) with parks and water ahead of minor roads.

## Decision

**B, with C as its fallback** (the owner's call, 2026-11-13).

- B's only extra need, float textures read in the vertex shader, is core WebGL2, and the start-up
  probe checks it on every device. `createMeshRenderer(canvas, {backend: "gpu"})` uses C wherever
  B can't run.
- C costs more:
  - 24 bytes more per fill vertex and per segment;
  - a full repack and re-upload of the scene whenever the field changes (another mode or
    departure time), where B uploads one texture;
  - a per-origin target pass in the worker (a median 5.8 ms for the 53k points of Baker Street's
    48 m scene, timed in node on the M2).
- B is also faster on origin change: 14.4 against 28.2 ms (p95) on desktop, and 43.6 against 60.3
  ms on the throttled stress run. C is cheaper on the GPU on synthetic scenes (5.0 against 12.1 ms
  a frame); on real tiles the two are level.

**Still to confirm:** the phones, for G6–G8, with real scenes of 1.1M–3.7M vertices (see
[Results: real tiles](#results-real-tiles)).

On desktop, B and C pass every gate the lab measures. The exception is G4 on the cliff field at
Wembley Park, which is a question about the mesh and applies to both.

A fails G5: its adapter only translates the corners that deck already extruded, so lines go thin
where the bend turns them (32% of segments under 75% of their width). A hook
that rebuilt the extrusion from extra attributes, or a `LineLayer` subclass with its own vertex
shader, could keep the width. That was not built, because it means writing our line shader again
inside deck, on top of 577 kB.

## Label policy (as built)

- **Drawn on the 2D overlay** in the system font, upright. The mesh makes no glyph requests.
- **Candidates come from our own decoded tiles,** never `queryRenderedFeatures`, which misses
  places that start off screen.
- **Placed every drawn frame,** for the map as it is on screen now (current camera and u). So
  labels never collide mid-morph, and a tap queries the positions being drawn.
- **Priority:**
  1. stations: within a rank, the one reached sooner first;
  2. places by `symbolrank`.

  There are no road names in the mesh view.
- **Stations** get a dot, labelled or not, and their label goes beside the dot: right, else
  left, then further out.
- **Places** are centred on their point.
- **Every box** must fit inside the view and clear the pin and the labels already placed, with a
  4 px gap. At most 40 labels on a phone, 80 on desktop.
- **Hiding:** a place is hidden where the map on screen is folded (det ≤ 0) or stretched beyond
  3:1. Stations are never hidden for that: a fast line folds the map right at its stations, so
  hiding them would hide what transit is about.
- **Tap index:** every candidate, placed or not, goes into a 24 px screen grid. A tap lists all of
  them within 12 px, with minutes. The lab shows the list; the overlap UI is W-02's.

## Float textures (#3)

- **B** uses R32F/RG32F textures with NEAREST filtering, read by `texelFetch` with hand-written
  bilinear interpolation. That is core WebGL2: no extension is asked for.
  - The probe (`caps.js`) uploads a 2×2 texture, runs the real warp code over it through
    transform feedback, and checks the result.
  - The renderer then compiles every program it can need. If that fails it drops a rung (gpu →
    cpu → unsupported); the self-test records `fellBack` and `programsError`.
  - `OES_texture_float_linear` and `EXT_color_buffer_float` are listed but never used.
  - A static test forbids `getExtension` in `static/shared/mesh/`.
- **C** uses no textures at all, so a phone without float vertex textures still gets the bent map.
- **No WebGL2, or a lost context:** the lab shows the ordinary MapLibre map, which is the
  geographic view.

## Tiles and budget

**The cover** spans what the field reaches within the outer minute, plus 1 km for the fade-out:

- walking needs 10–24 tiles per origin;
- the toy transit field needs 20–41.

**Rings:** z14 within 1 km, z12 within 10 km, z10 beyond.

- A tile is split into four, so the cover also holds z11 and z13 tiles.
- Over 48 tiles, every ring shrinks by 20% at a time, up to 8 rounds, before any far tile is cut.
- The plan's first guess (z14 within 2 km, over ±25 km) wanted 51–78 tiles.

**Cost.** At the default 6,428 tiles a day (`TILES` monthly cap ÷ 28) that is about 150–640 new
origins a day, before the browser's one-hour cache. MapLibre's own tiles for the ordinary view
come on top in the product. The lab's MapLibre style is background-only unless "Show base map"
is on.

**Fetching:**

- A tile is fetched once however many scenes want it. A newer scene aborts only the fetches it
  doesn't need.
- 429 and 503 are retried once, and only when Retry-After is ≤ 2 s. Each retry costs the
  visitor's per-IP quota, and a spent budget answers 503.
- The server's 16-request tile limit is per process, shared by every visitor. The worker keeps 8
  in flight, so two cold loads at once fill it.

**Vertex budget:** 5M vertices shaded per scene, 8M cached in the worker (the owner's call, after
the census: real transit scenes are 1.1M–3.7M). The cache limit is enforced after every scene,
including the current tiles once packed.

- **Over budget, the worker drops**, farthest first within each class:
  1. secondary roads;
  2. the smallest parks;
  3. the smallest water;
  4. minor rail;
  5. primary roads.
- It never drops motorway, trunk or major rail, and reports `overBudget` instead.
- Fully covered blocks share their grid nodes.
- **Synthetic scenes** are at most 346k shaded at 48 m, and real ones at most 3.7M, so nothing is
  shed at the five origins today. Shedding is there for a denser city or a bigger reach.

## Changes from the plan

- **Coordinates (D9).** The plan had tile-local vertices plus a uniform array of tile origins.
  Built instead: cached tile meshes are repacked into local metres for each origin, and every
  scene carries its origin.
- **Tessellation (D11).** One ground cell at every zoom (48 m), not tile size / 64 per tile.
  - Per-tile spacing left non-matching vertices where zooms meet, and was coarsest (381 m) in
    the far field.
  - 95 m cells fail the size-weighted chord check.
- **Tile cover (D10).** Rings at z14 within 1 km, not 2 km. They shrink over the cap, and the
  cover spans the reachable area, not ±25 km.
- **No data and past the outer minute (D16).** Polar places move onto the outer ring while they
  fade, instead of staying put. Grid places stay put.
- **Labels (D17).**
  - Stations are not hidden on folds.
  - Placement runs every frame, judged on the map as it is on screen now.
  - Station dots, the pin as an obstacle, and whole boxes inside the view.
- **Budget (D19).**
  - 5M vertices shaded and 8M cached, not 300k and 600k (the owner's call, after the census).
  - Counted in vertices shaded (six per line segment for each pass that draws it, so roads count
    twice), not "fill vertices plus segment instances".
  - The shed order adds secondary roads first.
  - Water is dropped smallest first rather than simplified.
  - Farthest first within a class.
- **Secondary roads.** Kept within 2 km of the origin by distance, not by tile zoom.
- **deck.gl probe (D4).** `LineLayer` instead of `PathLayer`, because it takes our per-segment
  buffers directly. Naming LineLayer's own attributes in the hook stopped the shader compiling,
  so the hook reads the segment end from `geometry.uv`.
- **Decision rule (D22).** B first with C as its fallback, not C first (the owner's call; see
  Decision).
- **Lab geometry.** Synthetic by default. `?geom=tiles` uses real tiles and is required for the
  census.
- **Not in the plan:** directions by reach (half by reach is the lab's default, the owner's call),
  the bent-map style, and the lab's schematic network. See
  [On the bent map](#on-the-bent-map-directions-by-reach-the-style-the-network).

## What is left

1. **Phones (owner).**
   - On the iPhone (Safari) and the mid-range Android (Chrome), open `/warp/lab/?backend=b`,
     then `?backend=c`, and the same with `&geom=tiles` for a real scene (it spends tile budget).
   - Each time press "Self-test", then "Run benchmark" (a few minutes; don't touch the screen),
     then "Copy results", and paste the JSON here.
   - Results accumulate in the page's storage until "Clear results". Record the exact models and
     browser versions.
   - If a phone misses G6 or G7 on real tiles, simplify the geometry first (see
     [Results: real tiles](#results-real-tiles)).
2. **W-02** takes over:
   - the real field (#1's composition and the walking near field), including where it jumps
     (the cliff case above). The lab's walk field is straight-line distance and ignores the
     Thames; a real one crosses only at bridges and tunnels;
   - whether the product bends directions by reach, and how much;
   - drawing #1's network on the bent map, as the lab draws the toy lines;
   - the two views;
   - Direction 4's checks on real fields;
   - the overlap UI on top of the tap index;
   - what to show past the outer ring;
   - fold ordering (folded sheets overlap in draw order today);
   - the content gap at u = 0: the mesh has no buildings, minor streets or road names, while
     the ordinary map does, so a morph from the full MapLibre map would pop;
   - retiring the radial lookup table.

## Risks and things not checked

- **Mapbox terms.** Mapbox's terms for drawing Streets tiles with our own renderer weren't read
  (see `docs/provider-terms.md`). The spike stores no Mapbox tiles: the test tile is encoded by
  hand, and the lab's default geometry is made up. Confirm the terms before shipping.
- **Rail in Streets v8 is only partly there.**
  - The census found 147 tunnelled `major_rail` features and 188 `metro_rail` stations across the
    five covers, but not every line.
  - The complete rail geometry is #1's GTFS shapes, through the same adapter.
- **Real tiles may still join badly where zooms meet,** because they are generalised
  differently. The mesh can't crack there any more, but the geometry itself may not match. The
  synthetic geometry is the same at every zoom, so it can't show this.
- **Fading lines double-blend at their joints.**
  - Opaque lines measure 1.635 px on a 4 px-segment chain against 1.5 nominal (+0.13 px; within
    G5).
  - Lines fading out (crossing the outer minute, during the morph) blend twice where segments
    overlap. That shows as faint beads on those lines while they fade.
  - Fixing it needs joins built from each segment's neighbours. That means two more warp
    evaluations per vertex, so it waits for the phone numbers.
- **Holes inside the reachable area** (#1's unreachable cells, if they come as "no data") would
  move onto the outer ring while fading, and make faint radial slivers mid-morph. W-02 should
  give unreachable places a time past the outer minute, or decide how holes look.
- **One worker** decodes and tessellates every tile.
  - Real transit scenes take 110–480 ms of tessellation on the M2. A phone will be several times
    slower.
  - Simplifying the geometry cuts this first. If a phone still misses G7 or G8 after that, add a
    small worker pool.
- **Bearings are not true with directions by reach:** up to 20–48° off at the outer ring at half
  by reach. Order and minutes are kept, and the rings stay exact.
- **Desktop numbers say little about phones,** and headless Chrome caps frames at 60 Hz.
- **Real tiles are heavy on a phone's network:** 1.8–3.7 MB per transit origin.
- **Real scenes are heavy on a phone's GPU and memory:** up to 3.7M vertices a frame and 8M
  cached. The owner expects modern phones to carry it; the phone runs decide.
- **The lab spends tile budget only with `?geom=tiles`.** It is unlinked and `noindex`; the
  per-IP and monthly caps still apply.
