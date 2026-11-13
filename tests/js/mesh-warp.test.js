import { test } from "node:test";
import assert from "node:assert/strict";
import { createField, createTargetGrid, fieldFromFunction, gridFromFunction, NO_DATA, sampleField, sampleGrid } from "../../static/shared/mesh/field.js";
import { localFrame, makeCamera } from "../../static/shared/mesh/frame.js";
import { createWarp, DIR_EPS, targetsFor, WARP_GLSL, warpDefines } from "../../static/shared/mesh/warp.js";

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
