# Warp v2: the travel-time field, the two views and Direction 4

Issue #5, part W-02. Written 2026-11-13 on top of W-01 (#12, `docs/warp-renderer-spike.md`).

**Status.**

- `/warp/` draws a real travel-time field in two views. The normal map with travel-time bands
  ships. The bent (time-polar) view is labelled experimental.
- Public transport is built and drawn from the London tables (T-01, #14), behind the licence
  flag. The owner holds the rail and TfL licences (2026-11-13), so the flag is set for new
  builds. Its bent view stays hidden, because Direction 4's numeric checks fail at all five
  origins (see [Direction 4](#direction-4-on-real-fields)).
- Measured with live Mapbox routing and tiles: the walking, cycling and driving fields, the
  transit near field and its seam, Direction 4 with the near field, the lab's gates on real
  Streets tiles, and the page's pans, pins and bend.
- **Not measured:** the phones and the comprehension test (both the owner's).

The raw numbers are in `docs/warp-time-polar-results.json`. No field, table or real-tile
image is stored.

## What was built

| Path | What it is |
|------|-----------|
| `server/localgrid.py` | frame.js's local frame in Python, and the grid (shared by the field and the transit provider) |
| `server/warp/field.py` | The field's maths: contours → minutes, the 2 km merge, dequantising, cropping, the wire format |
| `server/warp/travel_field.py` | `POST /api/travel-time-field`: one field per (pin, mode, window), cached and coalesced |
| `server/transit/provider.py` | The London tables at runtime: validation, #1's composition, the raster of H3 cells, coverage, status |
| `static/shared/mesh/contours.js` | Bands for the normal map (masked marching squares, islands and holes) and the band layer for the bent map |
| `static/shared/mesh/refine.js` | Mesh refinement where the field bends an edge, applied per origin at pack time |
| `static/shared/mesh/hit.js` | Every sheet under a finger on the bent map, folds included |
| `static/shared/mesh/rings.js`, `fit.js`, `diagnostics.js` | Ring plans and band colours; framing; Direction 4's measures (W-01's fold measure, moved here) |
| `static/shared/mesh/worker.js` | Adds the page's field work: bands, the refined band layer and the overlap index |
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
- **Live replies (King's Cross).** Distance contours come back with `metric: distance` and the
  metre value in `contour`. Each contour is one feature (a polygon with its holes), and each
  holds the one inside it. The walking pair is 3 kB and 15 kB, with 2 and 54 holes. Cycling's
  30–60 minute set is 310 kB, with 15,017 vertices and 1,299 holes.
- **Between two contours,** a node's time follows its distance to each boundary:
  t = t(k−1) + Δt · d_in / (d_in + d_out), with the pin as the zero contour.
- **Transit** follows #1's formula, with two additions for table points:
  - **Access:** `compose_row` over the stations and table cells whose walksheds reach the pin's
    res-10 cell. A pin within 100 m of a station starts from that station's own row, with
    nothing added.
  - **The last walk:** a group-min over every walkshed entry onto res-10 cells, then a 50 m
    raster of cell indexes onto the grid.
  - **Table points:** a table point's own time lands on the node nearest the street point R5
    measured it from (`snap_lon`, `snap_lat`). A table cell only lowers a node that already
    holds a time. A station's time lands even over water (a station on a bridge). Points that
    share a node keep the quickest time.
  - **The last steps from a station:** within 150 m, a node that already holds a time takes
    the station's time plus the straight walk at 4 km/h, when that is quicker. Water and
    places no walkshed reaches stay empty, so this never paints reach across a river.
  - **Within 2 km of the pin:** the faster of walking and the tables. Beyond 2 km, the tables
    alone.
  - **The grid spans the whole coverage box**, whatever the pin: 640 × 511 nodes, so nothing
    the tables reach is cut off.
- **Whole minutes:** after the merge, two [1 2 1] passes over the nodes the tables won, each
  clamped to within 0.5 minute of its own value, so nothing leaves its rounding interval.
  Walking times stay as they are. Direction 4 is reported both ways.
- **Gaps:** water and parkland up to about 300 m from a known node are filled from their
  neighbours and flagged. They are drawn, but excluded from taps and checks.
- **Wire format:** uint16 tenths of a minute, base64 in JSON.
  - 0xFFFF means no data.
  - 0xFFFE means beyond: no time within the tables' 180 minutes. It is a status everywhere
    (the CPU and GPU samplers, labels, taps), never blended into minutes.
  - Bit 15 marks a filled node.

| Field (King's Cross / five transit origins) | Grid | gzip | Server maths | Endpoint, cold |
|---|---|---|---|---|
| Walking | 90 × 87 @ 100 m | 5 kB | 22 ms | 829 ms |
| Cycling | 162 × 169 @ 200 m | 23 kB | 174 ms | 1,120 ms |
| Driving | 207 × 151 @ 400 m | 19 kB | 125 ms | 1,453 ms |
| Transit, weekday 08–09 | about 640 × 511 @ 100 m | 294–299 kB | | 204–951 ms |

"Endpoint, cold" includes the two Mapbox calls. A field built without its walking near field
(a refused isochrone call) is served with a note and not cached.

**Transit is served only from a build that passes every check:**
- complete, unexpired, and matching its manifest's hashes;
- marked `licences_cleared: true` in the manifest.

`TRANSIT_ALLOW_UNCLEARED=1` serves an uncleared build for local development: only to requests
from this machine with no forwarding header, whatever the host, and never on Railway. The page
then shows a "development only" line.

**Coverage** is one polygon, used by the page's chip and the server alike: 1.2 km around every
usable table point. Outside it, transit answers `outside_transit_area` before calling Mapbox,
and the page switches to walking with "Public transport times cover London only". The notice
stays with the limits until the next choice. Inside it, a pin that no walkshed or station
reaches (a reservoir, an airfield) answers `no_transit_access`: "Move the pin onto a street."

## The two views

- **Normal map (always).**
  - MapLibre with bands under the labels, one colour ramp. Rings fall every 5 minutes up to
    30, then every 10 up to 60 when the outer ring is a multiple of 10 (else every 15), and
    every 15 beyond. The legend states that spacing.
  - Outer-ring choices: 15/30/45/60 for walk, cycle and drive (default 30); 30/45/60/90/120 for
    transit (default 45).
  - The bands are contoured in the worker, so the page doesn't stall on a 300k-node field
    (on the main thread only without WebGL2, where there is no worker).
  - A tap gives the minutes at that spot, or "no street here" for water and parkland.
- **Bent map (experimental).**
  - "Bend the map" dissolves the normal map into the unbent mesh (300 ms), then morphs it
    (1.8 s). With reduced motion it jumps straight to the end.
  - While bent, the normal map's layers are hidden, so MapLibre fetches no tiles for a map
    nobody sees (measured: its streets source holds 0 tiles while bent). The bent view may
    zoom in one level past its framing, and the mesh is refined for that zoom.
  - The scene is prepared only while the map is bent, and only where bending is offered. A
    scene missing tiles is shown and asked for once more after 5 s.
  - The bands come from the field's own cells, refined like the map, and move with it, so
    they become the rings.
  - Directions are "By reach" by default (spread 0.5, the owner's choice) or "True compass";
    a line under the choice says that by reach moves compass directions.
  - Past the outer ring, places fade onto it (W-01's default), and the legend says so.
  - In transit, folds are tinted.
  - A tap lists every place drawn under the finger with its minutes, named by the nearest
    station or place within 400 m, or by distance and direction from the pin. The overlap
    index is built from the same refined triangles the bands are drawn with.
- **Which modes may bend** is one table in `static/warp/views.js`:
  - experimental for walk, cycle and drive;
  - hidden for transit; `?polar=1` shows it, for the comprehension test.

## Direction 4 on real fields

Folded share of the 45-minute ring, then median local stretch, for the weekday 08–09
window, with the 2 km walking near field, dequantised. Sampled at 200 m, with 800 m in
brackets. Limits: 10% and 3:1.

| Origin | By reach (0.5) | True compass |
|---|---|---|
| Baker Street | 52.0%, 5.5:1 (16.4%, 3.2:1) | 50.1%, 5.7:1 (15.2%, 3.4:1) |
| Wembley Park | 43.5%, 4.6:1 (15.5%, 3.2:1) | 35.8%, 5.9:1 (11.2%, 4.1:1) |
| Canary Wharf | 57.2%, 5.7:1 (26.6%, 3.6:1) | 53.1%, 6.1:1 (23.7%, 3.7:1) |
| Heathrow | 36.4%, 5.3:1 (18.4%, 3.6:1) | 28.3%, 7.3:1 (12.2%, 4.0:1) |
| Croydon | 43.9%, 6.3:1 (21.4%, 3.8:1) | 39.6%, 6.6:1 (17.6%, 3.5:1) |

**Across all four windows,** dequantised: 28–57% folded at 4.6–7.3:1, or 11–27% at 3.0–4.2:1
at 800 m. On the tables' whole minutes: 30–62% at 5.0–7.9:1. **No origin, window or setting
passes either check.**

- **The folds are not in the near field.** With the inner 2 km left out, 19–52% is still
  folded, at 4.8–7.6:1.
- **There is no seam at 2 km.** The 100 m step across the 2 km circle has a median of 1.0–1.2
  minutes and a p90 of 2.0–3.6, the same as the same step at 1.5 km and at 2.5 km.
- **Coarser sampling and dequantising both help, but not enough.** Part of the 200 m figure is
  whole-minute noise (dequantising takes off 3–5 points). Every scale still misses both limits.
- **By reach compared with true compass:** it lowers the stretch at most origins and raises
  the folded share, because the folds sit in the directions it opens up.

**Decision: public transport keeps the normal map.** Its bent view stays hidden
(`TIME_POLAR.transit = "hidden"`), as Direction 4 says. The comprehension test still needs a
run: with `?polar=1` the overlap and the tap list are there to test. A fold-free layout is
#6's question, not W-02's.

## Trial: squash, another layout for transit's pockets

Around each station the minutes drop, then rise again as you walk away, so along a line out
from the pin the time doesn't only grow. The fold draws every place at its own minutes, so
each pocket folds over its station. Squash is on the bent map behind `?layouts=1`
(`static/shared/mesh/layouts.js`); the default is still the fold.

- **The bend.** Along each line out from the pin, a place sits at its own minutes while they
  keep rising. Where they drop below the slowest place met so far (a pocket's shadow), the
  distance keeps climbing at 3 minutes per km instead, until the minutes catch up. So places
  never come back closer and nothing overlaps, and a field without pockets is drawn exactly
  as the fold, with round rings (walking from King's Cross: round, but for a notch where the
  railway lands block the way).
- **The rings bend instead of the map overlapping.** Along each of 720 directions a ring
  passes through the first place that many minutes away, so every point of it is exactly
  that many minutes away and everything inside it is quicker. Quick spots further out, near
  stations, show by their colour. No single ring per level can split "within" from "beyond"
  in a field with pockets: these rings leave 26–77% of the places within 30 or 45 minutes
  outside them; rings through the last such place would let in 21–61% slower places.
- **Rings spaced by ground reached.** Inside 15 minutes the map is drawn as it is (radius =
  minutes). Beyond, the area inside a ring grows with the ground drawn within it, at the
  middle's own ground per area, so the space between two rings is the ground they add: a wide
  band means many more places. Walking at one speed comes out unchanged (its ground grows as
  the square of the minutes). Public transport reaches far more ground by 45 minutes, so at
  Baker Street the 45 ring sits about 9 times further out than the 15 ring. `?ringscale=minutes`
  shows the rings spaced by minutes instead.
- **The view** opens on where places two thirds of the way out are typically drawn (the 30
  ring for 45 minutes), may zoom in two steps (the mesh is refined to half a pixel one step in,
  a pixel two in) and out as far as wanted. Places within the chosen time are all on the map
  (to the 99th percentile), and the refinement tolerance grows beyond the framed ring, where the
  map is only seen whole zoomed out. The colour bar gives each band the length of the ground it
  adds, on both views.
- **Both workers build it** from the field (the scene's and the band layer's, side by side),
  so the page never stalls on it; the tap index is built just after the band layer is sent. At
  Baker Street, switching to squash takes 1.0–1.4 s and a new pin while bent 1.05–1.35 s (fold:
  0.65–0.7 s); taps answer in under 10 ms.
- **Walled-off ground is cut down first.** A place slower than all the ground around it (a
  station's platforms and tracks, a railway yard) is lowered to the way out around it, so it
  implodes instead of pushing everything behind it outward. Walking from King's Cross, the
  platforms 300 m north read 28–50 minutes against 10–12 for the streets beyond them, and
  pushed the whole sector past the outer ring; cut, they sit in a small bump inside the 15
  ring. With the cut, squash over the whole view at the five transit origins is 0.2–0.6%
  folded at 1.6–2.7:1 (weekday 08–09, by reach), inside both of Direction 4's limits.
- **Pulling every pocket in instead (`?squashmode=implode`)**: a place sits at the quickest time
  at or beyond it, so stations keep their own minutes, but the ordinary streets between
  stations get squeezed into spokes: 7.6–12.4% folded at 17–24:1. Kept as a trial, not used.
- **Tried and dropped for the ring spacing:** area in proportion to ground everywhere (the
  15-minute middle shrank to a dot and streets streaked outward), halfway between that and
  minutes (no better), and spacing each ring so places keep their shape (the rings grew out of
  control).

| Origin, weekday 08–09 | Fold, 45-minute ring | Squash, 45-minute ring | Squash, whole view |
|---|---|---|---|
| Baker Street | 52.0%, 5.5:1 | 1.0%, 3.4:1 | 1.1%, 2.6:1 |
| Wembley Park | 43.5%, 4.6:1 | 0.9%, 4.1:1 | 1.6%, 3.4:1 |
| Canary Wharf | 57.2%, 5.7:1 | 1.5%, 3.6:1 | 1.3%, 2.5:1 |
| Heathrow | 36.4%, 5.3:1 | 0.2%, 5.6:1 | 0.9%, 4.6:1 |
| Croydon | 43.9%, 6.3:1 | 1.5%, 3.6:1 | 1.5%, 3.4:1 |

Folded share, then median stretch (limits 10% and 3:1). A gentler climb in the pockets puts
more places at their own minutes (up to 27% within 45 minutes, against 6–14% at 3 min/km), but
squeezes the pockets: at 1 min/km the stretch is 8–12:1 and 3–5% folds. An earlier version
that climbed everywhere, not just in pockets, bent the rings along fast lines with no pockets.
A third layout, islands (each pocket cut out as a small map on its ring), passed both checks
but drew the islands as specks, and was dropped.

## Done when

| Done when | Result |
|---|---|
| 1,000 random cells within 1 px | **Met in the lab.** G3, dots drawn and read back from pixels, is at most 0.062 px at all five origins (live transit fields, real tiles, back-end B), and 0.062 px on back-end C at Baker Street |
| Wembley Park from Baker Street within #1's limits | **Met on the accepted line by 0.1 minute, on one pair; not on the issue's line.** See [TfL journeys](#tfl-journeys) |
| Any number of rings, no 60-minute cap for transit | **Met.** Transit offers up to 120 minutes (13 rings); the tables reach 180, and the grid spans the whole coverage box |
| No Matrix calls; at most two isochrone requests per new origin | **Met, live and in the tests.** Live usage counter: a new transit pin +2 isochrone, 0 Matrix; walking and another window at that pin +0. Concurrent walk and transit requests share the calls in flight (`tests/test_field_api.py`) |
| After a pan, an update within 500 ms p95, no blocking overlay | **Met on desktop with real tiles.** 50 pans: next drawn frame p95 21 ms (normal map) and 22 ms (bent), 0 API requests. A new walking pin draws its bands in p50 583 ms (max 658), with nothing covering the map; while bent, the new pin's bent view arrives in p50 838 ms (max 911) |
| Smooth on a mid-range phone, iOS Safari included | **Not measured** (owner) |
| Direction 4 reported for all five origins, with a decision | **Met.** Table above: transit's bent view stays hidden |

### TfL journeys

The field at the destination station, from a pin at the start station, weekday 08–09. "No
endings" is the walkshed composition alone, without the table-point and station endings.

| Journey | TfL | Table row | Field | No endings |
|---|---|---|---|---|
| Baker Street → Wembley Park | 13.0 | 20 | 20.9 (+7.9) | 22.7 (+9.7) |
| Paddington → Heathrow T2&3 | 20.0 | 24 | 25.1 (+5.1) | 28.7 (+8.7) |
| London Bridge → East Croydon | 16.0 | 18 | 19.1 (+3.1) | 20.0 (+4.0) |

TfL is station to station by NaPTAN id. Wembley Park's 13.0 is the median of six departures
08:00–08:50; the other two are T-01's go/no-go medians (`gonogo.md`). All three are inside
the accepted ±8 line and none inside the issue's ±3. Without the endings, two of the three
would miss ±8. The endings were added because of Wembley Park, so that row is not independent
evidence. The other two journeys are, and they pass too.

## The renderer on real fields

Back-end B, real Streets tiles (`?geom=tiles`), live transit fields with the near field
(`?field=real&window=wd08`), refinement on:

| Origin | G1 parity | G3 radius (1,000) | G4 phone / desktop | G5 | Refined | Shaded |
|---|---|---|---|---|---|---|
| Baker Street | 0.0006 px | 0.062 px | 100% / 100% | pass | 53,047 | 4.67M |
| Wembley Park | 0.0016 px | 0.060 px | 100% / 100% | pass | 63,512 | 3.06M |
| Canary Wharf | 0.0016 px | 0.060 px | 100% / 100% | pass | 70,720 | 4.68M |
| Heathrow | 0.0030 px | 0.060 px | 100% / 99.7% | pass | 51,867 | 1.64M |
| Croydon | 0.0020 px | 0.061 px | 100% / 100% | pass | 101,996 | 3.70M |

- **A tenth of the budget is kept for refinement.** On real tiles, central London's geometry
  alone fills the 5M budget. Before the reserve, Baker Street got 8 refined vertices and G4
  was 92.3% on the desktop view. With it, the farthest parks and secondary roads are dropped
  instead (4,814 park and 64 secondary-road pieces at Baker Street), and G4 is 100%.
- **Four refinement levels, not three.** With three, Heathrow's desktop view had 98.4% within
  1 px, with long runway and road edges across the cliffs. Four (an edge becomes at most 16)
  gives 99.7%, for about 10% more refined vertices.
- **Fill refinement is all or nothing per pass.** Tile and block meshes share positions but
  not vertex indexes, so splitting only some edges when room runs out could leave a crack.
  With the reserve, no pass was skipped at any origin.
- **G5 passes everywhere.** Its scale-aware sub-check compares drawn coverage near the middle
  of a stretched span with one expectation from the span's midpoint. It now skips spans that
  cross a fold, where the drawn pieces fade and that expectation doesn't. The old Canary Wharf
  failure (0.40 of the expected) was such a span: the fold measure's determinant runs from
  −0.0026 to +0.0106 along it. The earlier guess, that the GPU and JS disagreed on the fold's
  sign, is withdrawn.

**The bent transit view's first scene is slow:** 2.06 s for Baker Street on desktop, with 30
tiles (3.7 MB), 4.77M shaded vertices, 664 ms packing and 451 ms tessellation. Its band layer
(224k refined vertices) takes 903 ms in the same worker. With tiles cold at the server too, the
whole bend took 6.7 s, the morph included. Walking's first bend is 0.44 s plus the morph. The
transit bent view is hidden, so this is for #6 to weigh.

## Comprehension test (owner)

- **Setup:** `/warp/?polar=1&mode=transit&window=wd08`, pin on Baker Street, bent, by reach.
  Five people who haven't seen it, five seconds each.
- **Ask:**
  1. "What do the rings mean?"
  2. "What does it mean where the map overlaps itself?"
  3. "Show me every place under this spot." (a tap on a folded spot)
- **Pass:** at least 4 of 5 explain the overlap, and the tap lists every place.

In the headless run, taps on four folded spots near Baker Street listed 5, 9, 10 and 17
places, each with its minutes.

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
2. **The comprehension test** (above).
3. **Before transit goes public:** a build made with the licence flag set (the next monthly
   rebuild; build 2026-11-12 predates it), and, for every view that bends, Mapbox's terms for
   drawing Streets tiles with our own renderer.
4. **Network lines** on the bent map need a transit-build export of stop sequences (not in the
   runtime build).

## Risks and things not checked

- **Walk mode is slower than the old page.** It moved to 4 km/h from Mapbox's 5.1 km/h, so
  walking reach is about 22% smaller. That is deliberate, and the page says 4 km/h.
- **A wide river stays a hole.** Gaps beyond about 300 m from a known node, such as the wider
  Thames downstream, fade onto the outer ring mid-morph. Gap filling also draws up to 300 m
  past London's edge; those nodes are flagged.
- **The raster** gives each node the res-10 cell under the nearest 50 m pixel, so a node up to
  about 35 m from a cell boundary can take its neighbour's time.
- **A transit field is 294–299 kB gzipped** per pin and window, on top of 1.8–3.7 MB of tiles
  on the first bend.
- **Contouring, the band layer and the overlap index on a phone are unmeasured.** All three run
  in the worker; without WebGL2 there is no worker, and the bands are contoured on the main
  thread.
- **Tables expire on 2026-12-12.** Transit turns itself off until the next monthly rebuild; an
  open page drops its cached transit fields after that date and checks the status again when
  the tab comes back.
