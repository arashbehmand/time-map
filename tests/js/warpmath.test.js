import { test } from "node:test";
import assert from "node:assert/strict";
import * as W from "../../static/warp/warpmath.js";

const S = [40, 90, 130, 200];
const T = [50, 100, 150, 200];
const SUP = 500;

test("u = 0 leaves the ordinary map alone", () => {
  const E = W.effectiveTargets(S, T, 0);
  for (const r of [0, 10, 40, 75, 150, 199, 260, 499, 600]) {
    assert.ok(Math.abs(W.inverseRadius(r, S, E, SUP) - r) < 1e-9, `r=${r}`);
    assert.ok(Math.abs(W.forwardRadius(r, S, E, SUP) - r) < 1e-9, `r=${r}`);
  }
});

test("inverse undoes forward, for every amount of bend", () => {
  for (const u of [0.25, 0.5, 1]) {
    const E = W.effectiveTargets(S, T, u);
    for (let r = 0; r < 700; r += 7) {
      const back = W.inverseRadius(W.forwardRadius(r, S, E, SUP), S, E, SUP);
      assert.ok(Math.abs(back - r) < 0.5, `u=${u} r=${r} back=${back}`);
    }
  }
});

test("on the time map each travel-time line lands on its circle", () => {
  const E = W.effectiveTargets(S, T, 1);
  S.forEach((s, i) => assert.ok(Math.abs(W.forwardRadius(s, S, E, SUP) - T[i]) < 1e-9));
});

test("past the support radius nothing moves", () => {
  const E = W.effectiveTargets(S, T, 1);
  assert.equal(W.forwardRadius(SUP + 1, S, E, SUP), SUP + 1);
  assert.equal(W.inverseRadius(SUP + 1, S, E, SUP), SUP + 1);
});

test("the scale doubles per zoom step", () => {
  assert.equal(W.scaleFor(14, 14), 1);
  assert.equal(W.scaleFor(14, 15), 2);
  assert.equal(W.scaleFor(14, 12), 0.25);
});

test("lutSample interpolates between bins and across the seam", () => {
  const n = W.LUT_SIZE;
  const row = Array.from({ length: n }, (_, i) => i);
  const centre = (i) => (i + 0.5) / n * 2 * Math.PI - Math.PI;   // angle of bin i's centre
  assert.ok(Math.abs(W.lutSample(row, centre(10)) - 10) < 1e-6);
  const between = (centre(10) + centre(11)) / 2;
  assert.ok(Math.abs(W.lutSample(row, between) - 10.5) < 1e-6);
  // Halfway between the last bin and the first: the mean of the two.
  assert.ok(Math.abs(W.lutSample(row, Math.PI) - (n - 1) / 2) < 1e-6);
});

test("support always leaves room past the outer line", () => {
  const floor = W.supportFloor(300, 250);
  // Pin near the right edge, looking right: the screen edge is close, the floor wins.
  assert.equal(W.supportRadius(790, 300, 0, 800, 600, floor), floor);
  // Pin in the middle, looking left: the padded edge is far.
  assert.equal(W.supportRadius(400, 300, Math.PI, 800, 600, 0), 400 + W.SUPPORT_PAD);
  for (let a = -Math.PI; a < Math.PI; a += 0.3) {
    assert.ok(W.supportRadius(10, 10, a, 800, 600, floor) > 300);
  }
});

test("targets scale with minutes and are capped by the screen", () => {
  assert.deepEqual(W.targetRadii(200, [5, 10, 15, 20], 1000, 1000), [50, 100, 150, 200]);
  const capped = W.targetRadii(5000, [5, 10, 15, 20], 1000, 800);
  assert.equal(capped[3], W.MAX_TARGET_FRAC * 800);
});

test("fitZoom frames the outer line at a fixed share of the screen", () => {
  const z = W.fitZoom(100, 14, 800, 800);
  assert.ok(Math.abs(100 * W.scaleFor(14, z) - W.FIT_FRAC * 800) < 1e-6);
});

test("the legend says what a ring means", () => {
  assert.equal(
    W.legendSentence("walking", [5, 10, 15, 20], true),
    "Distance from the pin now means travel time. Each ring is 5 more minutes walking.",
  );
  assert.equal(
    W.legendSentence("driving", [10, 20, 30, 40], false),
    "The lines show how far you can drive from the pin in 10, 20, 30 and 40 minutes.",
  );
  for (const mode of Object.keys(W.RING_TIMES)) {
    assert.match(W.legendSentence(mode, W.RING_TIMES[mode], true), /Each ring is \d+ more minutes/);
  }
});

test("validLut refuses what the shader can't draw", () => {
  const row = new Array(W.LUT_SIZE).fill(10);
  const ok = { contour_minutes: [5, 10, 15, 20], source_radii: [row, row, row, row] };
  assert.equal(W.validLut(ok), null);
  assert.match(W.validLut({ ...ok, contour_minutes: [5, 10, 15, 20, 25], source_radii: [row, row, row, row, row] }), /at most 4 rings/);
  assert.match(W.validLut({ ...ok, contour_minutes: [5, 10, 15, 61] }), /up to 60 minutes/);
  const nan = row.slice(); nan[3] = NaN;
  assert.ok(W.validLut({ ...ok, source_radii: [row, row, row, nan] }));
  assert.ok(W.validLut({ ...ok, source_radii: [row, row, row, row.slice(1)] }));
  assert.ok(W.validLut(null));
});

test("ring times stay inside the routing limits", () => {
  for (const times of Object.values(W.RING_TIMES)) {
    assert.ok(times.length <= W.MAX_RINGS);
    assert.ok(times.every((m) => m >= 1 && m <= W.MAX_MINUTES));
  }
});
