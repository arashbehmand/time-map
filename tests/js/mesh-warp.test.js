import { test } from "node:test";
import assert from "node:assert/strict";
import { createField, createTargetGrid, fieldFromFunction, gridFromFunction, NO_DATA, sampleField, sampleGrid } from "../../static/shared/mesh/field.js";
import { localFrame, makeCamera } from "../../static/shared/mesh/frame.js";
import { ANGLE_BINS, ANGLE_LEVELS, angularSpread, createWarp, DIR_EPS, dotScale, jacobiansFor, lineEmphasis, remapBearing, STYLE, targetsFor, WARP_GLSL, warpDefines } from "../../static/shared/mesh/warp.js";

const ORIGIN = { lon: -0.1571, lat: 51.5226 };
const frame = localFrame(ORIGIN);
const cam = makeCamera(frame, { centerLon: -0.16, centerLat: 51.52, zoom: 12, width: 390, height: 844, k: 4 });
// 1 minute per 100 m, out to 45 minutes; no data past 4 km east.
const field = fieldFromFunction({ origin: ORIGIN, cell: 100, halfSize: 5000 }, (x, y) => (x > 4000 ? -1 : Math.hypot(x, y) / 100));
const polar = createWarp({ kind: "polar", field, maxMinutes: 45 });

function rand(seed) { let a = seed; return () => ((a = (a * 1664525 + 1013904223) >>> 0) / 4294967296); }

test("at u = 1 a place sits k·T from the pin, with its bearing kept", () => {
  const r = rand(1);
  for (let i = 0; i < 1000; i++) {
    const x = -4000 + 7900 * r(), y = -4000 + 8000 * r();
    const t = sampleField(field, x, y);
    if (t === NO_DATA || t > 45) continue;
    const s1 = polar.screenOfLocal(x, y, cam, 1);
    assert.ok(Math.abs(Math.hypot(s1.x - cam.pin[0], s1.y - cam.pin[1]) - cam.k * t) < 1e-9);
    const want = Math.atan2(y, x);
    for (const u of [0, 0.5, 1]) {
      const s = polar.screenOfLocal(x, y, cam, u);
      assert.ok(Math.abs(Math.atan2(s.y - cam.pin[1], s.x - cam.pin[0]) - want) < 1e-9, `u=${u}`);
    }
  }
});

test("u = 0.5 is halfway between the ordinary and the bent position", () => {
  const a = polar.screenOfLocal(1234, -567, cam, 0), b = polar.screenOfLocal(1234, -567, cam, 1);
  const m = polar.screenOfLocal(1234, -567, cam, 0.5);
  assert.ok(Math.abs(m.x - (a.x + b.x) / 2) < 1e-9 && Math.abs(m.y - (a.y + b.y) / 2) < 1e-9);
});

test("no data, or past the outer minute, moves onto the outer ring and fades out", () => {
  for (const [x, y] of [[4500, 0], [-4600, 0]]) {   // no data; 46 min
    const s = polar.screenOfLocal(x, y, cam, 0.7), s1 = polar.screenOfLocal(x, y, cam, 1);
    assert.ok(Math.abs(s.alpha - 0.3) < 1e-9);
    assert.ok(Math.abs(Math.hypot(s1.x - cam.pin[0], s1.y - cam.pin[1]) - cam.k * 45) < 1e-9);
  }
  // Without an outer minute there is no ring: an unknown place fades where it is.
  const open = createWarp({ kind: "polar", field });
  const g = open.screenOfLocal(4500, 0, cam, 0), s = open.screenOfLocal(4500, 0, cam, 0.7);
  assert.ok(Math.abs(s.x - g.x) < 1e-9 && Math.abs(s.alpha - 0.3) < 1e-9);
  // A sample touching a no-data corner is no data.
  const f = createField({ origin: ORIGIN, cell: 1, nx: 2, ny: 2, x0: 0, y0: 0, minutes: new Float32Array([1, 2, 3, -1]) });
  assert.equal(sampleField(f, 0.5, 0.5), NO_DATA);
  assert.equal(sampleField(f, -0.1, 0.5), NO_DATA);
});

test("u = 0 is the ordinary map, for lon/lat input too", () => {
  const s = polar.toScreen(-0.2795, 51.5635, cam, 0);
  const [x, y] = frame.toLocal(-0.2795, 51.5635);
  assert.ok(Math.abs(s.x - ((x - cam.center[0]) * cam.ppm + cam.width / 2)) < 1e-9);
  assert.ok(Math.abs(s.y - ((y - cam.center[1]) * cam.ppm + cam.height / 2)) < 1e-9);
});

test("a grid layout is exact at its nodes, and the identity grid changes nothing", () => {
  const id = createWarp({ kind: "grid", grid: gridFromFunction({ origin: ORIGIN, cell: 500, halfSize: 5000 }, (x, y) => [x, y]) });
  const c = { ...cam, k: cam.ppm };
  for (const [x, y] of [[0, 0], [123, -456], [3999, 2500]]) {
    for (const u of [0, 0.3, 1]) {
      const s = id.screenOfLocal(x, y, c, u), g = id.screenOfLocal(x, y, c, 0);
      assert.ok(Math.abs(s.x - g.x) < 1e-6 && Math.abs(s.y - g.y) < 1e-6);
    }
  }
  const g = createTargetGrid({ origin: ORIGIN, cell: 10, nx: 2, ny: 2, x0: 0, y0: 0, xy: new Float32Array([0, 0, 7, 1, 2, 9, 4, 4]) });
  assert.deepEqual(sampleGrid(g, 0, 0), [0, 0]);
  assert.deepEqual(sampleGrid(g, 5, 5), [3.25, 3.5]);
  assert.equal(sampleGrid(g, 11, 0), null);
});

test("the jacobian finds folds and measures stretch", () => {
  // T rises to 10 min at 1 km, then falls to 5 min at 1.5 km: a fold between.
  const fold = fieldFromFunction({ origin: ORIGIN, cell: 50, halfSize: 3000 }, (x, y) => {
    const r = Math.hypot(x, y);
    return r < 1000 ? r / 100 : r < 1500 ? 10 - (r - 1000) / 100 : 5 + (r - 1500) / 100;
  });
  const w = createWarp({ kind: "polar", field: fold });
  assert.ok(w.jacobian(500, 300).det > 0);
  assert.ok(w.jacobian(1250, 0).det < 0);
  assert.ok(w.jacobian(2000, 100).det > 0);
  // Along the ray 1 min per 100 m; across it, 10 min over a radius of 1 km: stretch 1:1 at 1 km.
  assert.ok(Math.abs(w.jacobian(500, 0, 10).stretch - 1) < 0.02);
});

test("CPU targets match placement(), with its flag", () => {
  const t = targetsFor(polar, new Float32Array([300, 400, 4500, 0]));
  assert.ok(Math.abs(t[0] - 3) < 1e-6 && Math.abs(t[1] - 4) < 1e-6 && t[2] === 1);
  assert.deepEqual([...t.subarray(3)], [45, 0, 0.5]);   // no data: onto the 45-minute ring
});

test("the shader twin uses the same constants and defines", () => {
  assert.ok(WARP_GLSL.includes(DIR_EPS.toExponential()));
  for (const name of ["u_cam", "u_ppm", "u_view", "u_pin", "u_k", "u_u", "u_warpTex", "u_warpGrid", "u_warpSize"]) {
    assert.ok(WARP_GLSL.includes(name), name);
  }
  assert.equal(warpDefines("polar", false), "#define WARP_POLAR\n");
  assert.equal(warpDefines("grid", false), "#define WARP_GRID\n");
  assert.equal(warpDefines("polar", true), "#define CPU_TARGETS\n");
  // No float filtering anywhere: every lookup is texelFetch.
  assert.ok(!/\btexture\s*\(/.test(WARP_GLSL));
  // GLSL ES won't compare a float with an integer literal: every number interpolated into a
  // comparison must read as a float (this once made the GPU programs fail to compile).
  assert.ok(!/(>=|<=|[<>])\s*-?\d+\s*\)/.test(WARP_GLSL), WARP_GLSL.match(/(>=|<=|[<>])\s*-?\d+\s*\)/)?.[0]);
});

// A target Jacobian that, at u = 1, scales the ordinary map by sx along x and sy along y.
const jtFor = (sx, sy) => [(sx * cam.ppm) / cam.k, 0, 0, (sy * cam.ppm) / cam.k];

test("the scale-aware style changes nothing on the ordinary map", () => {
  for (const jt of [jtFor(1, 1 / 9), jtFor(3, -2), null]) assert.deepEqual(lineEmphasis(jt, 1, 0, cam, 0), { width: 1, alpha: 1 });
  assert.equal(dotScale(jtFor(1, 1 / 9), cam, 0), 1);
});

test("a line squeezed sideways thins; along the squeeze, or where the map widens, it keeps its width", () => {
  const jt = jtFor(1, 1 / 9);                         // squeezed 9× across x-direction lines
  assert.equal(lineEmphasis(jt, 1, 0, cam, 1).width, STYLE.WF_MIN);   // √(1/9) = 0.33, clamped
  assert.equal(lineEmphasis(jtFor(1, 0.25), 1, 0, cam, 1).width, 0.5);
  assert.ok(Math.abs(lineEmphasis(jt, 0, 1, cam, 1).width - 1) < 1e-9);
  const half = lineEmphasis(jt, 1, 0, cam, 0.5).width;
  assert.ok(half > STYLE.WF_MIN && half < 1);
  assert.deepEqual(lineEmphasis(jtFor(4, 4), 1, 0, cam, 1), { width: 1, alpha: 1 });
});

test("a line on a fold fades", () => {
  assert.equal(lineEmphasis(jtFor(1, 1), 1, 0, cam, 1).alpha, 1);
  assert.equal(lineEmphasis(jtFor(1, -1), 1, 0, cam, 1).alpha, STYLE.FOLD_FADE);
});

test("station dots shrink where the map is squeezed, and never grow", () => {
  assert.ok(Math.abs(dotScale(jtFor(0.5, 0.5), cam, 1) - Math.SQRT1_2) < 1e-9);
  assert.equal(dotScale(jtFor(0.1, 0.1), cam, 1), 0.6);
  assert.equal(dotScale(jtFor(9, 9), cam, 1), 1);
});

test("the target Jacobian matches the field, and is zero where it is unknown", () => {
  // T = r / 100 m: the target is (x, y) / 100, so its Jacobian is 0.01 · I.
  const jt = polar.targetJacobian(1000, 500);
  for (const [v, want] of [[jt[0], 0.01], [jt[1], 0], [jt[2], 0], [jt[3], 0.01]]) assert.ok(Math.abs(v - want) < 2e-4, `${v}`);
  const j = jacobiansFor(polar, new Float32Array([900, 500, 1100, 500, 4400, 0, 4600, 0]));
  assert.ok(Math.abs(j[0] - 0.01) < 2e-4);
  assert.deepEqual([...j.subarray(4)], [0, 0, 0, 0]);  // next to the no-data region
});

test("the shader twin of the style uses the same constants", () => {
  for (const name of ["fieldJacobian", "lineEmphasis"]) assert.ok(WARP_GLSL.includes(`${name}(`), name);
  for (const k of ["WF_MIN", "FOLD_FADE"]) assert.ok(WARP_GLSL.includes(STYLE[k].toFixed(2)), k);
});

// Directions by reach. A field that is quick to the east: 100 m a minute there, 40 m
// elsewhere, so the east holds more of the reachable area on every ring.
const eastFast = fieldFromFunction({ origin: ORIGIN, cell: 100, halfSize: 5000 }, (x, y) => Math.hypot(x, y) / (x > 0 && Math.abs(y) < x ? 100 : 40));
const row = (ang, m) => Array.from(ang.a.subarray(m * (ANGLE_BINS + 1), (m + 1) * (ANGLE_BINS + 1)));

test("directions by reach keep every bearing at spread 0", () => {
  const ang = angularSpread(eastFast, 45, { spread: 0 });
  const w0 = createWarp({ kind: "polar", field: eastFast, maxMinutes: 45 });
  const w1 = createWarp({ kind: "polar", field: eastFast, maxMinutes: 45, angles: ang });
  const r = rand(7);
  for (let i = 0; i < 500; i++) {
    const x = -4000 + 8000 * r(), y = -4000 + 8000 * r();
    const a = w0.target(x, y), b = w1.target(x, y);
    if (!a) { assert.equal(b, null); continue; }
    assert.ok(Math.hypot(a[0] - b[0], a[1] - b[1]) < 1e-4, `${x},${y}`);
  }
});

test("directions by reach keep minutes as radius, keep order, and add no folds", () => {
  const ang = angularSpread(eastFast, 45);
  for (let m = 0; m < ANGLE_LEVELS; m++) {
    const a = row(ang, m);
    for (let b = 1; b < a.length; b++) assert.ok(a[b] > a[b - 1], `level ${m} bin ${b}`);
    assert.ok(Math.abs(a[ANGLE_BINS] - a[0] - 2 * Math.PI) < 1e-5);
  }
  const w0 = createWarp({ kind: "polar", field: eastFast, maxMinutes: 45 });
  const w1 = createWarp({ kind: "polar", field: eastFast, maxMinutes: 45, angles: ang });
  const r = rand(9);
  for (let i = 0; i < 500; i++) {
    const x = -4000 + 8000 * r(), y = -4000 + 8000 * r();
    const t = sampleField(eastFast, x, y);
    const p = w1.target(x, y);
    if (!p) continue;
    assert.ok(Math.abs(Math.hypot(p[0], p[1]) - t) < 1e-9);
    const j0 = w0.jacobian(x, y), j1 = w1.jacobian(x, y);
    if (j0 && j1 && Math.abs(j0.det) > 1e-6) assert.equal(Math.sign(j1.det), Math.sign(j0.det), `${x},${y}`);
  }
});

test("on the outer ring each direction gets its share of the reachable area", () => {
  const ang = angularSpread(eastFast, 45, { spread: 1, floor: 0, smooth: 0 });
  const area = new Float64Array(ANGLE_BINS);
  const f = eastFast;
  for (let j = 0; j < f.ny; j++) for (let i = 0; i < f.nx; i++) {
    const t = f.minutes[j * f.nx + i], x = f.x0 + i * f.cell, y = f.y0 + j * f.cell;
    if (t < 0 || t > 45 || Math.hypot(x, y) <= DIR_EPS) continue;
    area[Math.min(ANGLE_BINS - 1, Math.floor(((Math.atan2(y, x) + Math.PI) / (2 * Math.PI)) * ANGLE_BINS))] += 1;
  }
  const total = area.reduce((s, v) => s + v, 0);
  const a = row(ang, ANGLE_LEVELS - 1);
  for (let b = 0; b < ANGLE_BINS; b++) assert.ok(Math.abs((a[b + 1] - a[b]) / (2 * Math.PI) - area[b] / total) < 1e-6, `bin ${b}`);
  // The quick east (bins around 0) gets well over its even share; the west well under.
  const east = (a[ANGLE_BINS / 2 + 1] - a[ANGLE_BINS / 2]) * ANGLE_BINS / (2 * Math.PI);
  const west = (a[1] - a[0]) * ANGLE_BINS / (2 * Math.PI);
  assert.ok(east > 2 && west < 0.5, `${east} ${west}`);
});

test("near the origin, inside the walk, bearings barely move", () => {
  const ang = angularSpread(eastFast, 45);
  for (const th of [-3, -1.5, 0.2, 2.5]) assert.ok(Math.abs(remapBearing(ang, th, 0) - th) < 1e-6);
});

test("the shader twin of directions by reach uses the same table layout", () => {
  for (const n of ["polarDir(", "u_angles[", "u_angleOn", "u_angleTMax"]) assert.ok(WARP_GLSL.includes(n), n);
  assert.ok(WARP_GLSL.includes(`${ANGLE_BINS + 1}`) && WARP_GLSL.includes(`${(ANGLE_LEVELS - 1).toFixed(1)}`));
});
