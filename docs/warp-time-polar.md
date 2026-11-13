# Warp v2: the travel-time field, the two views and Direction 4

Issue #5, part W-02. Written 2026-11-13 on top of W-01 (#12, `docs/warp-renderer-spike.md`).

**Status.**

- `/warp/` draws a real travel-time field in two views. The normal map with travel-time bands
  ships. The bent (time-polar) view is labelled experimental.
- Public transport is built and drawn from the London tables (T-01, #14), behind the licence
  flag. Its bent view stays hidden, because Direction 4's numeric checks fail at all five
  origins (see [Direction 4](#direction-4-on-real-fields)).
- **Not measured:**
  - the phones;
  - the comprehension test;
  - anything with Mapbox's walking near field, because no routing token was available. Every
    transit number below is the tables alone.

The raw numbers are in `docs/warp-time-polar-results.json`. No field, table or real-tile
image is stored.

## What was built

| Path | What it is |
|------|-----------|
| `server/warp/field.py` | The field's maths: frame.js's local frame in Python, contours → minutes, the 2 km merge, dequantising, cropping, the wire format |
| `server/warp/travel_field.py` | `POST /api/travel-time-field`: one field per (pin, mode, window), cached and coalesced |
| `server/transit/provider.py` | The London tables at runtime: validation, #1's composition, the raster of H3 cells, coverage, status |
| `static/shared/mesh/contours.js` | Bands for the normal map (masked marching squares, islands and holes) and the band layer for the bent map |
| `static/shared/mesh/refine.js` | Mesh refinement where the field bends an edge, applied per origin at pack time |
| `static/shared/mesh/hit.js` | Every sheet under a finger on the bent map, folds included |
| `static/shared/mesh/rings.js`, `fit.js`, `diagnostics.js` | Ring plans and band colours; framing; Direction 4's measures (W-01's fold measure, moved here) |
| `static/warp/app.js`, `views.js`, `words.js` | The page: the two views, the morph, taps, controls, every sentence |
| `static/warp/lab/` | `?field=real`, `__lab.directionChecks()`, G3 at 1,000 points, refinement on |

## The field

Minutes on a regular grid in local metres around the pin (W-01's `field.js` shape). Cells are
100 m for walking and transit, 200 m for cycling and 400 m for driving. The pin is rounded to
4 decimal places (about 11 m), and the page snaps to it.

- **Walking**, in walk mode and as transit's near field:
  - two Mapbox distance isochrones, contours at 333/667/1000/1333 m and 2000/2667/3333/4000 m,
    read at 4 km/h as 5–60 minutes;
  - 4 km/h is R5's walking speed, so walking agrees with the tables, and one cache entry
    serves both modes.
- **Cycling and driving:** two Mapbox time isochrones, 5–60 minutes, at Mapbox's speeds.
- **Every contour piece counts.** The requests send `denoise=0`, because Mapbox's default
  returns only the largest piece of each contour. Holes and islands are kept, and the contours
  are made to nest.
- **Between two contours,** a node's time follows its distance to each boundary:
  t = t(k−1) + Δt · d_in / (d_in + d_out), with the pin as the zero contour.
- **Transit** follows #1's formula, with two additions for table points:
  - **Access:** `compose_row` over the stations and table cells whose walksheds reach the pin's
    res-10 cell. A pin within 100 m of a station also starts from that station's own row.
  - **The last walk:** a group-min over every walkshed entry onto res-10 cells, then a 50 m
    raster of cell indexes onto the grid.
  - **Table points:** the node nearest each usable table point takes that point's own time.
    Within 150 m of a station, a node also takes the station's time plus the straight walk at
    4 km/h, when that is quicker.
  - **Within 2 km of the pin:** the faster of walking and the tables. Beyond 2 km, the tables
    alone.
- **Whole minutes:** two [1 2 1] passes, each node clamped to within 0.5 minute of its own
  value, so nothing leaves its rounding interval. Direction 4 is reported both ways.
- **Gaps:** water and parkland up to about 300 m from a known node are filled from their
  neighbours and flagged. They are drawn, but excluded from taps and checks.
- **Wire format:** uint16 tenths of a minute, base64 in JSON.
  - 0xFFFF means no data.
  - 0xFFFE means beyond: no time within the tables' 180 minutes.
  - Bit 15 marks a filled node.

  A transit field is 577 × 511 nodes, 290 kB gzipped, and builds in about 95 ms on the M2.

**Transit is served only from a build that passes every check:**
- complete, unexpired, and matching its manifest's hashes;
- marked `licences_cleared: true` in the manifest.

`TRANSIT_ALLOW_UNCLEARED=1` serves an uncleared build for local development; it is refused on
Railway and puts a "development only" line on the page. Outside London, transit answers
`outside_transit_area` before calling Mapbox.

## The two views

- **Normal map (always).**
  - MapLibre with bands under the labels, one colour ramp, rings every 5 minutes up to 30,
    every 10 up to 60 and every 15 beyond.
  - Outer-ring choices: 15/30/45/60 for walk, cycle and drive (default 30); 30/45/60/90/120 for
    transit (default 45).
  - A tap gives the minutes at that spot, or "no street here" for water and parkland.
- **Bent map (experimental).**
  - "Bend the map" dissolves the normal map into the unbent mesh (300 ms), then morphs it
    (1.8 s). With reduced motion it jumps straight to the end.
  - The bands come from the field's own cells and move with the map, so they become the rings.
  - Directions are "By reach" by default (spread 0.5, the owner's choice) or "True compass";
    a line under the choice says that by reach moves compass directions.
  - Past the outer ring, places fade onto it (W-01's default), and the legend says so.
  - In transit, folds are tinted.
  - A tap lists every place drawn under the finger with its minutes, named by the nearest
    station or place within 400 m, or by distance and direction from the pin.
- **Which modes may bend** is one table in `static/warp/views.js`:
  - experimental for walk, cycle and drive;
  - hidden for transit; `?polar=1` shows it, for the comprehension test.

## Direction 4 on real fields

Folded share of the 45-minute ring, then median local stretch, for the weekday 08–09
window, dequantised. Sampled at 200 m, with 800 m in brackets. Limits: 10% and 3:1.

| Origin | By reach (0.5) | True compass |
|---|---|---|
| Baker Street | 52.2%, 5.5:1 (16.4%, 3.2:1) | 50.3%, 5.7:1 (15.1%, 3.4:1) |
| Wembley Park | 44.1%, 4.6:1 (15.9%, 3.2:1) | 36.4%, 6.0:1 (11.5%, 4.1:1) |
| Canary Wharf | 57.4%, 5.7:1 (26.7%, 3.6:1) | 53.3%, 6.1:1 (23.8%, 3.7:1) |
| Heathrow | 36.7%, 5.4:1 (18.4%, 3.2:1) | 28.4%, 7.4:1 (12.2%, 4.0:1) |
| Croydon | 44.4%, 6.1:1 (13.0%, 4.4:1) | 40.1%, 6.5:1 (10.0%, 4.4:1) |

**Across all four windows,** dequantised: 28–57% folded at 4.6–7.4:1, or 10–27% at 3.0–4.5:1
at 800 m. On the tables' whole minutes: 30–62% at 5.0–8.0:1. **No origin, window or setting
passes either check.**

- **Coarser sampling and dequantising both help, but not enough.** Part of the 200 m figure is
  whole-minute noise (dequantising takes off 3–5 points). Every scale still misses both limits.
- **By reach compared with true compass:** it lowers the stretch at most origins and raises
  the folded share, because the folds sit in the directions it opens up.
- **These are the tables alone.** The 2 km walking near field would change the first 2 km
  only; the folds sit mostly beyond it, along the fast lines.
- **The radius check** (|drawn target| = minutes at 1,000 random nodes) is exact to 1e-14
  minutes everywhere.

**Decision: public transport keeps the normal map.** Its bent view stays hidden
(`TIME_POLAR.transit = "hidden"`), as Direction 4 says. The comprehension test still needs a
run: with `?polar=1` the overlap and the tap list are there to test. A fold-free layout is
#6's question, not W-02's.

## Done when

| Done when | Result |
|---|---|
| 1,000 random cells within 1 px | **Met in the lab.** G3, dots drawn and read back from pixels, is at most 0.062 px at all five origins (real transit fields, back-end B), and 0.062 px on back-end C at Baker Street |
| Wembley Park from Baker Street within #1's limits | **Met on the accepted line, not on the issue's.** TfL station to station: 13 min (median of six 08:00–08:50 departures, Metropolitan line). Table row 20 (+7); the field at the station 20.9 (+7.9). Accepted line ±8, issue's line ±3 |
| Any number of rings, no 60-minute cap for transit | **Met.** Transit offers up to 120 minutes (13 rings); the tables reach 180 |
| No Matrix calls; at most two isochrone requests per new origin | **Met in the tests.** 2 for a new transit origin, 0 more for walk ↔ transit at the same pin or another window within the 5-minute cache, 0 Matrix (`tests/test_field_api.py`) |
| After a pan, an update within 500 ms p95, no blocking overlay | **Met on desktop.** 50 pans: next drawn frame p95 20.6 ms (normal map) and 19.3 ms (bent), 0 API requests. A new transit pin shows its field in p95 222 ms, with nothing covering the map |
| Smooth on a mid-range phone, iOS Safari included | **Not measured** (owner) |
| Direction 4 reported for all five origins, with a decision | **Met.** Table above: transit's bent view stays hidden |

## The renderer on real fields

Back-end B, the lab's synthetic geometry, real transit fields (`?field=real&window=wd08`), with
refinement on:

| Origin | G1 parity | G3 radius (1,000) | G4 phone / desktop | Refined |
|---|---|---|---|---|
| Baker Street | 0.0013 px | 0.061 px | 100% / 100% | 14,303 |
| Wembley Park | 0.0011 px | 0.061 px | 100% / 100% | 6,841 |
| Canary Wharf | 0.0029 px | 0.060 px | 100% / 100% | 15,784 |
| Heathrow | 0.0039 px | 0.061 px | 100% / 99.3% | 2,349 |
| Croydon | 0.0010 px | 0.060 px | 100% / 100% | 8,810 |

- **G4 without refinement** was 92.8–98.7% on the phone view and 87–94% on the desktop view.
  Refinement against minutes alone fixed every origin but Canary Wharf with directions by
  reach (97.9% on the desktop view): the remap bends bearings where reach concentrates. Testing
  the whole bent position when a remap is on fixed that too.
- **The lab's synthetic default** now gets G4 at 100% on both views (W-01: 99.4% on the
  desktop view).
- **G5's scale-aware sub-check fails at Canary Wharf.** The plain width checks pass.
  - It takes the most stretched segment on screen, which on a real transit field lies on a
    fold line.
  - The drawn coverage there is 0.40 of the expected, exactly the fold fade. That fits the GPU
    (32-bit floats) and JS (64-bit) disagreeing on the fold's sign, not a width error.

## Comprehension test (owner)

- **Setup:** `/warp/?polar=1&mode=transit&window=wd08`, pin on Baker Street, bent, by reach.
  Five people who haven't seen it, five seconds each.
- **Ask:**
  1. "What do the rings mean?"
  2. "What does it mean where the map overlaps itself?"
  3. "Show me every place under this spot." (a tap on a folded spot)
- **Pass:** at least 4 of 5 explain the overlap, and the tap lists every place.

| Person | Rings | Overlap | Tap |
|---|---|---|---|
| 1 | | | |
| 2 | | | |
| 3 | | | |
| 4 | | | |
| 5 | | | |

## What is left

1. **The phones (owner).** iPhone Safari and a mid-range Android, on `/warp/` with real tiles:
   pans, the morph, a new pin. Run W-01's lab protocol too.
2. **A run with a routing token.**
   - Regenerate `static/warp/example-field.json` with `python scripts/make_examples.py <server>`.
     Today's is converted from the old saved answer, with no islands or holes.
   - Rerun `__lab.directionChecks()`, so the near field and its seam are measured.
3. **The comprehension test** (above).
4. **Before transit goes public:**
   - the rail and TfL licences (`docs/transit-sources.md`);
   - Mapbox's terms for drawing Streets tiles with our own renderer, for every view that bends.
5. **Network lines** on the bent map need a transit-build export of stop sequences (not in the
   runtime build).

## Risks and things not checked

- **Mapbox's distance isochrones were not tried live.** `denoise=0` may return noise. The
  interpolation tolerates specks, crossings and several features per contour.
- **Walk mode is slower than the old page.** It moved to 4 km/h from Mapbox's 5.1 km/h, so
  walking reach is about 22% smaller. That is deliberate, and the page says 4 km/h.
- **The near-field seam is unmeasured** (no token). The composition walks through table
  points and walksheds, so a small seam is expected.
- **A wide river stays a hole.** Gaps beyond about 300 m from a known node, such as the wider
  Thames downstream, fade onto the outer ring mid-morph.
- **The bent view's scene timing** on a new pin wasn't measured (no tiles token). W-01
  measured 323–613 ms cold on desktop broadband.
- **Contouring and the overlap index on a phone are unmeasured.** The overlap index runs in
  the worker; the contouring runs on the main thread.
- **Tables expire on 2026-12-12.** Transit turns itself off until the next monthly rebuild.
