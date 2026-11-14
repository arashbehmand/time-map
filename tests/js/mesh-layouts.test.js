// The two trial layouts for transit's pockets (static/shared/mesh/layouts.js).
import { test } from "node:test";
import assert from "node:assert/strict";
import { fieldFromFunction } from "../../static/shared/mesh/field.js";
import { foldDiagnostics } from "../../static/shared/mesh/diagnostics.js";
import { bentRings, islandLayout, squashField, squashReach } from "../../static/shared/mesh/layouts.js";
import { createWarp } from "../../static/shared/mesh/warp.js";
import { sampleField } from "../../static/shared/mesh/field.js";

const WALK = 4000 / 60;
// Walking from the pin, and a station 4 km east reached in 10 minutes: a pocket.
const origin = { lon: -0.12, lat: 51.5 };
const field = fieldFromFunction({ origin, cell: 100, halfSize: 6000 },
  (x, y) => Math.min(Math.hypot(x, y) / WALK, 10 + Math.hypot(x - 4000, y) / WALK));
const fold = { kind: "polar", field, maxMinutes: 45 };

test("squash never shrinks outward, never undercuts a time, and unfolds the pocket", () => {
  const R = squashField(field);
  for (let k = 0; k < field.minutes.length; k++) assert.ok(R.minutes[k] >= field.minutes[k] - 1e-4);
  const j = Math.round(-field.y0 / field.cell);
  for (let i = Math.round(-field.x0 / field.cell) + 1; i < field.nx; i++) {
    assert.ok(R.minutes[j * field.nx + i] > R.minutes[j * field.nx + i - 1], "rises along the ray east");
  }
  assert.ok(foldDiagnostics(fold).foldedShare > 0.01, "the fold overlaps itself");
  assert.ok(foldDiagnostics({ ...fold, field: R }).foldedShare < 0.002, "squash doesn't");
});

test("islands: the pin's ground is time-polar, the pocket an undistorted map on its ring", () => {
  const L = islandLayout(field, { maxMinutes: 45 });
  assert.equal(L.stats.islands, 1);
  assert.ok(L.stats.torn > 0, "torn at the border");
  const { nx, minutes } = field, xy = L.grid.xy, piece = L.cut.label;
  const pin = Math.round(-field.y0 / field.cell) * nx + Math.round(-field.x0 / field.cell);
  let checkedPin = 0, checkedIsland = 0;
  for (let k = 0; k + 1 < minutes.length; k++) {
    if (piece[k] < 0) continue;
    if (piece[k] === piece[pin]) {
      if (minutes[k] > 1) { assert.ok(Math.abs(Math.hypot(xy[2 * k], xy[2 * k + 1]) - minutes[k]) < 1e-3); checkedPin++; }
    } else if (piece[k + 1] === piece[k]) {
      // Neighbours one cell apart sit `scale` × one cell's walk apart: one scale, no bending.
      assert.ok(Math.abs(xy[2 * k + 2] - xy[2 * k] - (L.stats.scale * field.cell) / WALK) < 1e-3);
      assert.ok(Math.abs(xy[2 * k + 3] - xy[2 * k + 1]) < 1e-3);
      checkedIsland++;
    }
  }
  assert.ok(checkedPin > 100 && checkedIsland > 100);
  // The station's own place sits on its ring (10 minutes).
  const st = Math.round(-field.y0 / field.cell) * nx + Math.round((4000 - field.x0) / field.cell);
  assert.ok(Math.abs(Math.hypot(xy[2 * st], xy[2 * st + 1]) - 10) < 1e-3);
  assert.ok(foldDiagnostics({ kind: "grid", grid: L.grid, field, maxMinutes: 45 }).foldedShare < foldDiagnostics(fold).foldedShare);
});

test("squash's rings: one closed bent circle per level, every point that many minutes away", () => {
  const R = squashField(field);
  const reach = squashReach(field, R, 45);
  assert.ok(reach > 45, "the pocket's far side is drawn past 45");
  const warp = createWarp({ kind: "polar", field: R, maxMinutes: reach });
  const rings = bentRings(field, warp, [20, 10], { smooth: 0 });
  assert.deepEqual(rings.map((r) => r.minutes), [10, 20]);
  for (const { minutes, xy } of rings) {
    const n = xy.length / 2;
    assert.ok(Number.isNaN(xy[xy.length - 1]) && n === 720 + 2, "720 directions, closed, one piece");
    assert.ok(Math.abs(xy[0] - xy[2 * 720]) < 1e-6 && Math.abs(xy[1] - xy[2 * 720 + 1]) < 1e-6);
    let prev = null;
    for (let i = 0; i < 720; i++) {
      const ang = Math.atan2(xy[2 * i + 1], xy[2 * i]);
      if (prev !== null) assert.ok(((ang - prev + 3 * Math.PI) % (2 * Math.PI)) - Math.PI > 0, "goes round the pin once");
      prev = ang;
      assert.ok(Math.hypot(xy[2 * i], xy[2 * i + 1]) >= minutes - 0.6, "squash never draws a place nearer than its minutes");
    }
  }
  // East, the 20-minute ring stops at the first 20-minute place: before the pocket, not past it.
  const east = Math.hypot(rings[1].xy[0], rings[1].xy[1]);
  assert.ok(east < 25, `east radius ${east}`);
  assert.ok(Math.abs(sampleField(field, 20 * WALK, 0) - 20) < 0.5);
});
