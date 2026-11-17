// The warp page's sentences and choices (static/warp/words.js, views.js).
import { test } from "node:test";
import assert from "node:assert/strict";
import { ringPlan } from "../../static/shared/mesh/rings.js";
import * as V from "../../static/warp/views.js";
import * as T from "../../static/warp/words.js";

test("the legend says the ring spacing the plan really has", () => {
  assert.equal(T.sentence("walking", 30, ringPlan(30), false),
    "The coloured bands show where you can walk to within 30 minutes, a band every 5 minutes.");
  assert.equal(T.sentence("transit", 90, ringPlan(90), false),
    "The coloured bands show where you can get to within 90 minutes, a band every 5 minutes up to 30, then every 10 up to 60, then every 15.");
  assert.equal(T.sentence("transit", 45, ringPlan(45), true),
    "Distance from the pin now means travel time: a ring every 5 minutes up to 30, then every 15.");
});

test("what a tap on the normal map says", () => {
  assert.equal(T.tapGeographic(null, "X"), "No travel time here.");
  assert.equal(T.tapGeographic({ minutes: -1, filled: false }, "X"), "No travel time here.");
  assert.equal(T.tapGeographic({ minutes: 12, filled: true }, "X"), "No street here (water or parkland).");
  assert.equal(T.tapGeographic({ minutes: 1e4, filled: false }, "X"), "More than 3 hours, or no route in the timetable.");
  assert.equal(T.tapGeographic({ minutes: 12.4, filled: false }, "King's Cross"), "About 12 min from King's Cross.");
});

test("a tap lists every place under the finger, two places with one name included", () => {
  const nameOf = (x) => (x < 2000 ? "Baker Street" : "Marylebone");
  const sheets = [{ x: 100, y: 0, minutes: 4 }, { x: 1020, y: 0, minutes: 21 }, { x: 1040, y: 10, minutes: 33 },
                  { x: 3000, y: 0, minutes: 12 }, { x: 3000, y: 0, minutes: 12 }];
  const lines = T.placeLines(sheets, nameOf);
  assert.equal(lines.length, 4, lines.join(" | "));       // the same sheet twice is one place
  assert.equal(lines[0], "Baker Street: about 4 min");
  assert.equal(lines[1], "1.0 km east of the pin: about 21 min");
  assert.equal(lines[2], "1.0 km east of the pin: about 33 min");   // said the same, kept
  assert.equal(lines[3], "Marylebone: about 12 min");
});

test("an unnamed place is said by distance and direction", () => {
  assert.equal(T.unnamedPlace(10, 10), "next to the pin");
  assert.equal(T.unnamedPlace(0, -800), "800 m north of the pin");
  assert.equal(T.unnamedPlace(2500, 2500), "3.5 km south-east of the pin");
});

test("the default window follows London time, midnight and the weekend included", () => {
  const at = (iso) => V.defaultWindow(new Date(iso));
  assert.equal(at("2026-11-16T08:30:00Z"), "wd08");        // Monday, GMT
  assert.equal(at("2026-11-16T12:00:00Z"), "wd13");
  assert.equal(at("2026-11-16T23:59:00Z"), "wd19");
  assert.equal(at("2026-11-17T00:00:00Z"), "wd08");        // midnight is hour 0, not 24
  assert.equal(at("2026-11-14T10:00:00Z"), "sa13");        // Saturday
  assert.equal(at("2026-11-15T10:00:00Z"), "sa13");        // Sunday
  assert.equal(at("2026-07-06T07:30:00Z"), "wd08");        // BST: 08:30 in London
});

test("inside the coverage polygon, holes excluded", () => {
  const poly = { type: "Polygon", coordinates: [[[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]], [[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]]] };
  assert.ok(V.insideArea(poly, 3, 3));
  assert.ok(!V.insideArea(poly, 1.5, 1.5));
  assert.ok(!V.insideArea(poly, 5, 5));
  assert.ok(V.insideArea({ type: "MultiPolygon", coordinates: [poly.coordinates] }, 3, 3));
  assert.equal(V.fieldKey(-0.12346, 51.5, "walking", "wd08"), "-0.1235,51.5000,walking,");
  assert.equal(V.fieldKey(-0.12346, 51.5, "transit", "wd08"), "-0.1235,51.5000,transit,wd08");
});

test("every mode may bend, public transport included (W-03: with squash)", () => {
  for (const m of ["walking", "cycling", "driving", "transit"]) {
    assert.equal(V.TIME_POLAR[m], "experimental");
    assert.equal(V.bendOffered(m), true);
  }
  assert.equal(V.bendOffered("transit", "?polar=1"), true);
  assert.equal(V.DIRECTIONS[V.DEFAULT_DIRECTIONS], 0.5);
});

test("public transport bends with squash, every other mode with the fold; a pick wins", () => {
  assert.equal(V.layoutFor("transit"), "squash");
  for (const m of ["walking", "cycling", "driving"]) assert.equal(V.layoutFor(m), "fold");
  assert.equal(V.layoutFor("transit", "fold"), "fold");
  assert.equal(V.layoutFor("walking", "squash"), "squash");
  assert.equal(V.layoutFor("transit", "nonsense"), "squash");
});

test("the time map says what squash gives up, with the shares counted for the view drawn", () => {
  assert.equal(T.givesUp(45, 0.374, 0.008),
    "Rings follow the main reach, not every place: 37% of the places within 45 minutes lie past the 45 min ring, and under 1% of the places inside it are slower. Zoom out to see the whole ring. Colours and taps give each place's own time.");
  assert.match(T.givesUp(45, 0.03, 0.021), /: 3% of the places within 45 minutes .* and 2% of the places inside it are slower/);
  assert.match(T.givesUp(60, null, null), /: Some places within 60 minutes lie past the 60 min ring, and a few places inside it can be slower/);
  assert.ok(!/0%/.test(T.givesUp(45, null, null)), "no number before it is counted");
  // Squash doesn't keep distance as minutes, so its direction note doesn't promise it.
  assert.match(T.directionNote("reach", "fold"), /Minutes stay exact/);
  assert.ok(!/exact/i.test(T.directionNote("reach", "squash")));
  assert.ok(!/wedges/.test(T.directionNote("compass", "squash")));
});

test("public transport bends at every ring; past the checked ones a footnote says it bends more", () => {
  assert.equal(V.bendOffered("transit"), true);
  assert.deepEqual(V.CHECKED_OUTERS.transit, [30, 45]);
  assert.equal(T.roughNote(45),
    "Past 45 minutes the time map bends more: rings get rougher, and more places sit on the wrong side of them.");
});

test("tables expire the day after valid_until, in London", () => {
  assert.equal(V.expired("2026-12-12", new Date("2026-12-12T23:30:00Z")), false);   // GMT: still the 12th
  assert.equal(V.expired("2026-12-12", new Date("2026-12-13T00:30:00Z")), true);
  assert.equal(V.expired(null), false);
  assert.ok(Math.abs(V.refineEps(8.4) - 0.5 / (8.4 * 2)) < 1e-12);      // half a pixel one level in
});
