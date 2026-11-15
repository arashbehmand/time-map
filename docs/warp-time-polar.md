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
- **Fixed 2026-11-15:** on the bent map the platforms north of King's Cross were drawn as a wedge
  out to the outer ring. Ground the contours leave out is now an uncertain patch, drawn small
  and left uncoloured (see [Uncertain patches](#uncertain-patches-the-wedge-north-of-kings-cross)).
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
- **Uncertain patches:** ground the contours leave out of a hole through two or more
  consecutive contours, rising above its way out by more than walking in from its edge explains
  (King's Cross's platforms). Each keeps its own minutes. It also gets drawn minutes, which only
  the bent map's geometry uses. See [Uncertain patches](#uncertain-patches-the-wedge-north-of-kings-cross).
- **Wire format:** uint16 tenths of a minute, base64 in JSON.
  - 0xFFFF means no data.
  - 0xFFFE means beyond: no time within the tables' 180 minutes. It is a status everywhere
    (the CPU and GPU samplers, labels, taps), never blended into minutes.
  - Bit 15 marks a filled node.
  - `uncertain`, only when there are patches: `nodes_b64` (uint32 node indices, increasing)
    and `drawn_b64` (uint16 tenths, one per node).

| Field (King's Cross / five transit origins) | Grid | gzip | Server maths | Endpoint, cold |
|---|---|---|---|---|
| Walking | 90 × 87 @ 100 m | 5 kB | 22 ms | 829 ms |
| Cycling | 162 × 169 @ 200 m | 23 kB | 174 ms | 1,120 ms |
| Driving | 207 × 151 @ 400 m | 19 kB | 125 ms | 1,453 ms |
| Transit, weekday 08–09 | about 640 × 511 @ 100 m | 294–299 kB | | 204–951 ms |

"Endpoint, cold" includes the two Mapbox calls. Finding uncertain patches adds at most 4 ms to a
walking field and 15–85 ms to a cycling or driving one (measured on live fields, 2026-11-15). A field built without its walking near field
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

## Uncertain patches: the wedge north of King's Cross

**What the owner saw (2026-11-14).** At the opening example (King's Cross, walking, 30
minutes, Fold, By reach), the normal map has a pale notch north of the pin: the platforms and
tracks, which nobody walks into. Bent, the notch became a bright wedge from the pin to the 30
ring, with the tracks fanned up along it.

**Why.**
- Mapbox's walking contours leave the platforms out of every contour up to 50 minutes.
- `contour_field` interpolates them to about 50 minutes; the streets 200 m further north are at
  12. The saved field's north ray read 0, 3.8, 50.0, 50.1, 20.2, 11.7, 11.9, 13.4.
- The fold draws every place at its own minutes, and the fade sends places past the outer ring
  onto it. So the triangles between the station (4 minutes) and the ring stretched into a wedge.
- The station's label read "London King's Cross · 33 min": the patch's interpolated time.

**How it was missed.**
- The same explosion was reported in squash and fixed there only (the cut).
- The second review packet described the cut as a squash step, so every review looked at squash.
- Every numeric check here (Direction 4, G1–G5) trusts the field: a hole interpolated to 50
  minutes counts as a real 50-minute place, and the fade past the outer ring is by design.
- A real-tile screenshot of the walking fold at King's Cross was taken during the timing runs
  and never looked at.

**The rule** (`server/warp/field.py`; the owner accepted it as a heuristic for uncertain
ground, 2026-11-14):
- **Way out:** for each node, the highest level, over paths to the grid's edge, of the lowest
  time along the path. No data counts as +∞. It is the flood of squash's cut.
- **On the grid,** contour k's region is exactly {t ≤ t_k}. So a node is in a hole of contour k
  when its way out ≤ t_k < its time. A node in holes of two or more consecutive contours is a
  candidate (no data counts as slower than every contour).
- **A group of candidates** (4-connected) is an uncertain patch when its largest rise above the
  way out is more than walking in from its edge explains: depth × 2 ÷ speed + 3 minutes. Speed
  is 4 km/h walking, 15 km/h cycling and 24 km/h driving. A park 1.2 km across stays a real
  place; the platforms (one 100 m cell deep, 38 minutes up) don't.
- **Drawn minutes:** a smooth fill from the ground around the patch (neighbour means working
  inward, then Jacobi passes with the rim held). King's Cross's platforms are drawn at 6.1, 8.2
  and 9.6 between 3.8 and 11.7.
- **The provider's minutes stay on the wire** for audit. The patch is not marked filled, which
  means water.
- **Transit:** the walking near field is classified before the 2 km merge. A patch's walking
  time never beats a time from the tables: the tables win there, and the patch is kept only
  where they have none. At the five transit origins the tables cover every walking patch, so
  transit fields carry none.

**What each view does with a patch.**
- **Normal map:** the bands leave it out at every ring, a pale notch as before, and the colour
  bar doesn't count it.
- **Bent map** (fold and squash, CPU and GPU, both workers, the lab): bent by the field with
  each patch node at its drawn minutes, so it is drawn small between the station front and the
  streets beyond. The band layer and the overlap tint leave it uncoloured. By reach gives it no
  room.
- **Labels and taps:**
  - A label whose cell touches a patch shows its name with no minutes.
  - A tap on a patch, normal or bent, says "No reliable travel time here: the route data leaves
    this patch out." A tap on the street 600 m north still says about 12 minutes.
- **Checks:** G3, Direction 4's diagnostics and the radius check leave the patch footprint out,
  as they leave filled ground out. The drawn radius of patch nodes equals their drawn minutes
  (`__lab.uncertainChecks`, error 0).

**Measured on live fields, 2026-11-15** (`__lab.uncertainChecks()`).
- **The wedges:** nodes the warp sends onto the outer ring though every way out crosses ground at
  least 10 minutes inside the ring (`diagnostics.enclosedFade`). They are counted before (the
  field's own minutes, as the fold drew them) and after (what it draws now).
- **The two-contour rule** makes "after" 0 at 15, 30 and 60. At 45 one 10-minute contour step
  can hide a single-contour spike. At 60, Baker Street cycling keeps one deep patch.

| Origin | Mode | Patch nodes (no data) | Patches | Wedges at 15 | 30 | 45 | 60 |
|---|---|---|---|---|---|---|---|
| King’s Cross | Walk | 5 (1) | 3 | 0 → 0 | 2 → 0 | 4 → 1 | 1 → 0 |
| King’s Cross | Cycle | 284 (270) | 131 | 1 → 0 | 27 → 0 | 105 → 1 | 270 → 0 |
| King’s Cross | Drive | 11 (11) | 5 | 0 → 0 | 0 → 0 | 4 → 0 | 11 → 0 |
| Baker Street | Walk | 17 (9) | 9 | 0 → 0 | 5 → 0 | 9 → 0 | 9 → 0 |
| Baker Street | Cycle | 276 (265) | 128 | 0 → 0 | 43 → 0 | 107 → 1 | 266 → 1 |
| Baker Street | Drive | 68 (67) | 31 | 0 → 0 | 0 → 0 | 1 → 0 | 67 → 0 |
| Wembley Park | Walk | 50 (38) | 32 | 0 → 0 | 4 → 0 | 12 → 1 | 38 → 0 |
| Wembley Park | Cycle | 737 (717) | 185 | 0 → 0 | 59 → 0 | 107 → 0 | 717 → 0 |
| Wembley Park | Drive | 225 (208) | 96 | 0 → 0 | 0 → 0 | 15 → 2 | 208 → 0 |
| Canary Wharf | Walk | 6 (5) | 5 | 0 → 0 | 0 → 0 | 2 → 0 | 5 → 0 |
| Canary Wharf | Cycle | 157 (149) | 75 | 0 → 0 | 1 → 0 | 49 → 0 | 149 → 0 |
| Canary Wharf | Drive | 318 (287) | 133 | 0 → 0 | 0 → 0 | 8 → 1 | 287 → 0 |
| Heathrow | Walk | 0 (0) | 0 | 0 → 0 | 0 → 0 | 0 → 0 | 0 → 0 |
| Heathrow | Cycle | 1158 (1139) | 200 | 0 → 0 | 42 → 0 | 327 → 1 | 1139 → 0 |
| Heathrow | Drive | 593 (527) | 223 | 0 → 0 | 33 → 0 | 95 → 3 | 527 → 0 |
| Croydon | Walk | 22 (18) | 17 | 0 → 0 | 2 → 0 | 5 → 1 | 18 → 0 |
| Croydon | Cycle | 711 (691) | 224 | 0 → 0 | 39 → 0 | 142 → 1 | 691 → 0 |
| Croydon | Drive | 125 (107) | 57 | 0 → 0 | 1 → 0 | 17 → 0 | 107 → 0 |

Most patches in cycling and driving fields are enclosed no-data holes: `denoise=0` keeps every
hole Mapbox returns. Before, they faded onto the outer ring from ground well inside it; now they
are drawn at the times around them. A patch with its own minutes is the kind that could take a
real place's colour. There are 0–12 such nodes per walking field and 0–66 per cycling or driving
field. They were not looked at one by one.

**The check that would have caught it.**
- `tests/js/warp-example.test.js` checks the committed example:
  - no wedge at rings 15, 30 and 60;
  - at most the one at 45;
  - from 100 to 700 m north of the pin, every point drawn in place within 15 minutes, By reach
    and True compass.
- `tests/test_ux.py` checks that the platforms are a patch, with minutes kept and drawn
  under 15.
- Run against the example at 81e2106, both fail: at ring 30, and on the first platform node.
- **And looking:** the default bent view at the opening example is opened and read after every
  visual change, not only the view a report names.

**Looked at** (real tiles, desktop, 2026-11-15):

| Origin | Mode | View | Ring | Seen |
|---|---|---|---|---|
| King's Cross | Walk | Normal | 30 | The pale notch north of the pin, as before |
| King's Cross | Walk | Fold, by reach | 30 | No wedge. The platforms are a small uncoloured block between the 5 and 12 minute radii, and the tracks run north from it at the times of the ground around them (Maiden Lane 24 min). Taps say the new sentence on the block and about 12 min 600 m north |
| King's Cross | Walk | Fold, by reach | 15 | No wedge. The tracks north of the block reach the ring at their own times (15+ minutes) |
| King's Cross | Walk | Fold, by reach | 60 | No wedge. A few small slivers 24–40 minutes out near Maiden Lane and Caledonian Road: single-contour spikes, a node one contour above its neighbours |
| King's Cross | Walk | Fold, true compass | 30 | No wedge; the block sits north of the pin |
| King's Cross | Walk | Squash | 30 | No wedge; the block is drawn small inside the 15 ring |
| King's Cross | Cycle | Fold, by reach | 30 | No wedge; "London King's Cross" with no minutes; a few small slivers |
| King's Cross | Drive | Fold, by reach | 30 | No wedge |
| Baker Street | Walk | Fold, by reach | 30 | No wedge; Regent's Park keeps its own times (the allowance) |
| Canary Wharf | Walk | Fold, by reach | 30 | No wedge |

Not looked at: a transit origin with `?polar=1`. The transit fields at the five origins carry no
patches, so their bent view is unchanged.

**Direction 4, rerun 2026-11-15.** The weekday 08–09 smoothed rows at all five origins and both
direction settings are unchanged to the third decimal place, because the transit fields carry no
patches. The whole-minute rows were not rerun: this checkout's build is marked cleared, so the
server smooths every field. The recorded ones stand.

## Trial: squash, another layout for transit's pockets

Around each station the minutes drop, then rise again as you walk away, so along a line out
from the pin the time doesn't only grow. The fold (the issue's time-polar view) draws every
place at its own minutes, so each pocket folds over its station. Squash gives up "distance =
minutes" to stop that. It is on the bent map behind `?layouts=1` (`static/shared/mesh/layouts.js`);
the default is still the fold, and transit's bend stays hidden. It is not the issue's view: if
it is to ship for transit, #5 needs the owner to accept it in place of "distance = minutes".

- **The cut.** A place standing at least 5 minutes above the way out around it is lowered to
  that way out, so it doesn't push everything behind it outward. Since 2026-11-15 the worst
  of these in a walking field, a station's platforms, arrive from the server as uncertain patches
  with their drawn minutes. Squash bends from those, like the fold, so the cut is left with
  transit's slow ridges. Slow streets between stations (most
  of them under 3 minutes above their way out) keep their minutes; unknown holes and "beyond"
  ground are never crossed or changed. Walking from King's Cross, the platforms read 28–50
  minutes against 10–12 for the streets beyond them and blew the north sector past the outer
  ring; cut, they sit in a small bump inside the 15 ring. A layout filter only: colours,
  labels and taps keep each place's own minutes.
- **The bend.** Along each line out from the pin, a place sits at its own (cut) minutes while
  they keep rising; where they drop below the slowest place met so far (a pocket's shadow), the
  distance keeps climbing at 3 minutes per km until the minutes catch up. Unknown ground is
  stepped over, not restarted from. A field without pockets is drawn exactly as the fold.
- **Rings spaced by places reached.** Inside 15 minutes the radius is the minutes. Beyond, it
  grows with the number of places within each time (the true minutes, as the colour bar counts
  them), at the middle's own places per area, so a wide band means many more places. Walking at
  one speed comes out unchanged. `?ringscale=minutes` spaces the rings by minutes instead.
- **The rings bend.** Along each of 720 directions a ring passes through the first place that
  many minutes away (true minutes, stepping over unknown ground and the cut patches), drawn where
  squash puts it, with no smoothing. Every vertex is a place exactly that many minutes away, and
  every place nearer the pin along that direction is quicker except the cut patches. Quicker
  places beyond a ring (near stations) and the cut patches inside one show by their colour; the
  page's note says so.
- **The view** opens on where places two thirds of the way out are typically drawn, may zoom in
  two steps (refined to half a pixel one step in, a pixel two in; beyond the framed ring the
  tolerance grows to at most two pixels at the deepest zoom), and covers the places within the
  chosen time to the 99th percentile (about 1% are past its edge).
- **Both workers build it** from the same field, filled-node flags included, so the map, bands,
  rings, labels and taps share one layout.

**Measured on what a reader reads** (code at 3c4a2f6 + e54f1ad; transit, weekday 08–09, by
reach, T = 45 minutes; `diagnostics.layoutChecks`). These are shares of the places within 45
minutes (true minutes, filled ground left out), drawn wherever the layout puts them. They are
not Direction 4's numbers: that rule measures the fold's 45-minute disc, which squash doesn't
have.

| Origin | Fold: overlap, stretch | Squash: overlap, stretch | Squash: outside the 45 ring | Squash: slower inside it | Squash: off the view |
|---|---|---|---|---|---|
| Baker Street | 56.4%, 5.6:1 | 1.4%, 3.7:1 | 36.4% | 1.0% | 1.0% |
| Wembley Park | 48.5%, 4.6:1 | 1.3%, 3.8:1 | 20.1% | 1.0% | 1.0% |
| Canary Wharf | 64.1%, 5.7:1 | 1.4%, 3.5:1 | 29.3% | 0.9% | 0.9% |
| Heathrow | 60.6%, 5.3:1 | 0.7%, 4.7:1 | 73.3% | 6.0% | 0.9% |
| Croydon | 49.5%, 6.3:1 | 2.2%, 4.0:1 | 57.2% | 0.7% | 1.1% |

- **Overlap** is the share of their drawn area covered twice or more, whatever the orientation.
  The fold's rings are exact (nothing within 45 outside its ring, nothing slower inside), but it
  overlaps itself over half its area. Squash hardly overlaps; its stretch is lower but over 3:1
  at all five origins; and 20–73% of the places within 45 minutes are drawn beyond the 45 ring.
  So squash's rings show the shape of the main reach, not "everything you can get to in 45
  minutes".
- **The renderer on squash** (lab, `?layout=squash`, real tiles, back-end B): G1 ≤ 0.004 px, G2
  pass, G3 ≤ 0.061 px over 1,000 cells against squash's own radius, G4 99.9–100% within 1 px,
  G5 pass, at all five origins.
- **Timing** at Baker Street, warm tiles: switching to squash 1.41 s, a new pin while bent 1.42–
  1.47 s (fold: 0.69–0.74 s); the squash layout itself 140–210 ms per field in each worker.
- **Tried and dropped:** islands (each pocket cut out as a small map on its ring: specks);
  pulling every pocket in (stations at their own minutes, but the streets between them
  squeezed into spokes: 7.6–12.4% folded at 17–24:1); area in proportion to ground everywhere,
  halfway between that and minutes, and spacing each ring so places keep their shape (worse or
  unstable). Earlier figures in this section, measured over the whole view before the cut's
  threshold and the shared field, are withdrawn.

## Done when

Every row is about the shipped path: the normal map, and the fold where the map bends.

| Done when | Result |
|---|---|
| 1,000 random cells within 1 px | **Met in the lab.** G3, dots drawn and read back from pixels, is at most 0.062 px at all five origins (live transit fields, real tiles, back-end B), and 0.062 px on back-end C at Baker Street. On the walking fold at King's Cross after the patch fix: 0.028 px. Cells touching an uncertain patch are left out: they have no reliable time to match |
| Wembley Park from Baker Street within #1's limits | **Met on the accepted line by 0.1 minute, on one pair; not on the issue's line.** See [TfL journeys](#tfl-journeys) |
| Any number of rings, no 60-minute cap for transit | **Met.** Transit offers up to 120 minutes (13 rings); the tables reach 180, and the grid spans the whole coverage box |
| No Matrix calls; at most two isochrone requests per new origin | **Met, live and in the tests.** Live usage counter: a new transit pin +2 isochrone, 0 Matrix; walking and another window at that pin +0. Concurrent walk and transit requests share the calls in flight (`tests/test_field_api.py`) |
| After a pan, an update within 500 ms p95, no blocking overlay | **Met on desktop with real tiles.** 50 pans: next drawn frame p95 21 ms (normal map) and 22 ms (bent), 0 API requests. A new walking pin draws its bands in p50 583 ms (max 658), with nothing covering the map; while bent, the new pin's bent view arrives in p50 838 ms (max 911) |
| Smooth on a mid-range phone, iOS Safari included | **Not measured** (owner) |
| Direction 4 reported for all five origins, with a decision | **Met, for the fold** (the issue's time-polar view): it fails everywhere, so transit's bent view stays hidden. Squash is measured separately above, not against Direction 4 |

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

- **Single-contour spikes stay.** A node one contour above all its neighbours is not an uncertain
  patch: the owner's rule needs two contours. Beyond 30 minutes the contours are 10 minutes
  apart, so it can still be drawn as a thin sliver toward the ring at 45 or 60 (1–3 per field at
  45 above). Widening the rule is the owner's call.
- **An uncertain patch may be real ground.** A gated estate or a yard behind a long fence, in
  holes of two contours and too slow to walk into from its edge, would be left uncoloured and
  drawn at the times around it. Its minutes stay in the field. The thresholds (detour 2, 3
  minutes, 15 and 24 km/h) are first guesses, checked only against the counts above.

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
