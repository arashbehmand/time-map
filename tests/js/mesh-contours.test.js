// Bands for the ordinary map (static/shared/mesh/contours.js) and the ring plan.
import { test } from "node:test";
import assert from "node:assert/strict";
import { bandPolygons, nestRings, regionRings, signedArea } from "../../static/shared/mesh/contours.js";
import { BEYOND, fieldFromFunction, NO_DATA } from "../../static/shared/mesh/field.js";
import { localFrame } from "../../static/shared/mesh/frame.js";
import { bandColor, ringPlan } from "../../static/shared/mesh/rings.js";

const origin = { lon: -0.1233, lat: 51.5308 };
const WALK = 4000 / 60;
const disc = (fn, half = 3000, cell = 50) => fieldFromFunction({ origin, cell, halfSize: half }, fn);
const polyArea = (poly) => -signedArea(poly[0]) - poly.slice(1).reduce((s, h) => s + signedArea(h), 0);

test("a walking disc gives one ring per level, of the right area", () => {
  const f = disc((x, y) => Math.hypot(x, y) / WALK);
  const polys = nestRings(regionRings(f, 15));             // 15 min = 1 km
  assert.equal(polys.length, 1);
  assert.equal(polys[0].length, 1);
  const area = polyArea(polys[0]);
  assert.ok(Math.abs(area / (Math.PI * 1000 * 1000) - 1) < 0.01, `${area}`);
  assert.ok(signedArea(polys[0][0]) < 0);                  // outer rings: negative in local metres
});

test("a band between two levels is an annulus, wound like GeoJSON", () => {
  const f = disc((x, y) => Math.hypot(x, y) / WALK);
  const fc = bandPolygons(f, localFrame(origin), [10, 20]);
  assert.equal(fc.features.length, 2);
  const [inner, ring] = fc.features.map((g) => g.geometry.coordinates);
  assert.equal(inner.length, 1);
  assert.equal(inner[0].length, 1);
  assert.equal(ring.length, 1);
  assert.equal(ring[0].length, 2);                         // outer and one hole
  const llArea = (r) => r.slice(0, -1).reduce((s, p, i) => s + p[0] * r[i + 1][1] - r[i + 1][0] * p[1], 0) / 2;
  assert.ok(llArea(ring[0][0]) > 0, "outer ring counter-clockwise in lon/lat");
  assert.ok(llArea(ring[0][1]) < 0, "hole clockwise in lon/lat");
  assert.deepEqual(fc.features[1].properties, { band: 1, from: 10, to: 20 });
});

test("islands and holes survive", () => {
  // Quick near the pin and around a far station; a lake with no data inside the near disc.
  const f = disc((x, y) => {
    if (Math.hypot(x - 300, y) < 150) return NO_DATA;
    const near = Math.hypot(x, y) / WALK, far = 6 + Math.hypot(x - 2200, y) / WALK;
    return Math.min(near, far);
  });
  const polys = nestRings(regionRings(f, 10));
  assert.equal(polys.length, 2);                           // the pin's disc and the far island
  const pin = polys.find((p) => p[0].some(([x, y]) => Math.hypot(x, y) < 700));
  assert.equal(pin.length, 2);                             // with the lake as a hole
  assert.ok(Math.abs(polyArea(pin) - (Math.PI * 666.7 ** 2 - Math.PI * 150 ** 2)) / (Math.PI * 666.7 ** 2) < 0.03);
});

test("beyond and filled nodes are outside every band", () => {
  const f = disc((x) => (x > 0 ? BEYOND : 1), 500, 100);
  const polys = nestRings(regionRings(f, 30));
  assert.equal(polys.length, 1);
  assert.ok(polys[0][0].every(([x]) => x <= 50 + 1e-9));
  f.filled = new Uint8Array(f.nx * f.ny).fill(1);
  assert.equal(regionRings(f, 30, { skipFilled: true }).length, 0);
  assert.ok(regionRings(f, 30).length > 0);
});

test("a saddle joins its diagonal when the cell's mean is inside", () => {
  const make = (centre) => {
    const m = new Float32Array([0, 10, 10, 0]);              // tl, tr / bl, br on a 2×2 grid
    if (centre !== undefined) m[1] = m[2] = centre;
    return { kind: "field", origin, cell: 100, nx: 2, ny: 2, x0: 0, y0: 0, minutes: m };
  };
  // tl and br are 0 minutes, tr and bl 6: the cell's mean is 3.
  assert.equal(nestRings(regionRings(make(6), 5)).length, 1);      // 3 ≤ 5: one piece
  assert.equal(nestRings(regionRings(make(6), 2.9)).length, 2);    // 3 > 2.9: two pieces
});

test("the ring plan: every 5 to 30, 10 to 60, 15 beyond, and the outer ring", () => {
  assert.deepEqual(ringPlan(30).map((r) => r.minutes), [5, 10, 15, 20, 25, 30]);
  assert.deepEqual(ringPlan(45).map((r) => r.minutes), [5, 10, 15, 20, 25, 30, 40, 45]);
  assert.deepEqual(ringPlan(90).map((r) => r.minutes), [5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 90]);
  assert.deepEqual(ringPlan(90).filter((r) => r.major).map((r) => r.minutes), [15, 30, 45, 60, 75, 90].filter((m) => [15, 30, 60, 75, 90].includes(m)));
  assert.equal(ringPlan(120).length, 13);
  assert.match(bandColor(0, 11), /^#[0-9a-f]{6}$/);
  assert.notEqual(bandColor(0, 11), bandColor(10, 11));
});

test("ninety minutes gives eleven bands", () => {
  const f = disc((x, y) => Math.hypot(x, y) / 100, 9500, 250);          // 6 km/h
  const levels = ringPlan(90).map((r) => r.minutes);
  assert.equal(bandPolygons(f, localFrame(origin), levels).features.length, 11);
});
