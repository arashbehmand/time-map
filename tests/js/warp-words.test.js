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

test("transit stays hidden on the bent map unless ?polar=1", () => {
  assert.equal(V.bendOffered("walking"), true);
  assert.equal(V.bendOffered("transit"), false);
  assert.equal(V.bendOffered("transit", "?polar=1"), true);
  assert.equal(V.DIRECTIONS[V.DEFAULT_DIRECTIONS], 0.5);
});

test("tables expire the day after valid_until, in London", () => {
  assert.equal(V.expired("2026-12-12", new Date("2026-12-12T23:30:00Z")), false);   // GMT: still the 12th
  assert.equal(V.expired("2026-12-12", new Date("2026-12-13T00:30:00Z")), true);
  assert.equal(V.expired(null), false);
  assert.ok(Math.abs(V.refineEps(8.4) - 0.5 / 16.8) < 1e-12);
});
