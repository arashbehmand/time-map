// Squash, the trial layout for transit's pockets (static/shared/mesh/layouts.js).
import { test } from "node:test";
import assert from "node:assert/strict";
import { fieldFromFunction } from "../../static/shared/mesh/field.js";
import { foldDiagnostics } from "../../static/shared/mesh/diagnostics.js";
import { bentRings, squashField, squashLayout, squashReach } from "../../static/shared/mesh/layouts.js";
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

test("without pockets, squash is the fold: its own minutes and round rings", () => {
  const plain = fieldFromFunction({ origin, cell: 100, halfSize: 4000 }, (x, y) => Math.hypot(x, y) / WALK);
  const { R, reach, spec } = squashLayout(plain, 45);
  let worst = 0;
  for (let k = 0; k < plain.minutes.length; k++) worst = Math.max(worst, Math.abs(R.minutes[k] - plain.minutes[k]));
  assert.ok(worst < 1e-4, `R departs from the minutes by ${worst}`);
  assert.ok(Math.abs(reach - 45) < 0.5);
  for (const { minutes, xy } of bentRings(plain, createWarp(spec), [15, 30])) {
    for (let i = 0; i < 720; i++) {
      const r = Math.hypot(xy[2 * i], xy[2 * i + 1]);
      assert.ok(Math.abs(r - minutes) < 0.05, `ring ${minutes} at radius ${r}`);
    }
  }
});

test("squash climbs only in a pocket's shadow, and rejoins the minutes after it", () => {
  // The same pocket on a wider field: walking out of it catches up with the climb near 6.5 km.
  const wide = fieldFromFunction({ origin, cell: 100, halfSize: 9000 },
    (x, y) => Math.min(Math.hypot(x, y) / WALK, 10 + Math.hypot(x - 4000, y) / WALK));
  const R = squashField(wide);
  const j = Math.round(-wide.y0 / wide.cell), i0 = Math.round(-wide.x0 / wide.cell);
  const at = (m) => j * wide.nx + i0 + Math.round(m / wide.cell);
  assert.ok(Math.abs(R.minutes[at(1000)] - wide.minutes[at(1000)]) < 1e-4, "before the pocket: its own minutes");
  assert.ok(R.minutes[at(4000)] > wide.minutes[at(4000)] + 1, "at the station: pushed out");
  assert.ok(Math.abs(R.minutes[at(8000)] - wide.minutes[at(8000)]) < 1e-4, "past the pocket: its own minutes again");
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

test("the ground scale keeps the middle as it is, and walking at one speed unchanged", () => {
  // Walking only: ground grows as the square of the minutes, so the scale is the identity.
  const plain = fieldFromFunction({ origin, cell: 100, halfSize: 4000 }, (x, y) => Math.hypot(x, y) / WALK);
  const a = squashLayout(plain, 45), b = squashLayout(plain, 45, { scale: "ground" });
  let worst = 0;
  for (let k = 0; k < plain.minutes.length; k++) {
    if (plain.minutes[k] <= 45) worst = Math.max(worst, Math.abs(b.R.minutes[k] - a.R.minutes[k]));
  }
  assert.ok(worst < 1, `walking moved by ${worst} minutes`);
  // With a pocket: inside 15 minutes nothing moves; beyond, the rings spread out by ground, and
  // the view is framed on the 30-minute ring, not the outer edge.
  const m = squashLayout(field, 45), g = squashLayout(field, 45, { scale: "ground" });
  for (let k = 0; k < field.minutes.length; k++) {
    if (m.R.minutes[k] >= 0 && m.R.minutes[k] <= 15) assert.equal(g.R.minutes[k], m.R.minutes[k]);
  }
  assert.ok(g.frame < g.reach && g.frame > 25, `frame ${g.frame}, reach ${g.reach}`);
  for (let k = 1; k < g.R.minutes.length; k++) {
    if (m.R.minutes[k] > 15 && m.R.minutes[k - 1] > 15 && m.R.minutes[k] > m.R.minutes[k - 1]) {
      assert.ok(g.R.minutes[k] >= g.R.minutes[k - 1], "the order outward is kept");
    }
  }
});

test("imploding: slow ground gives way, quick places keep their minutes, rings come out round", () => {
  const sq = squashLayout(field, 45, { mode: "implode" });
  const R = sq.R.minutes, t = field.minutes, nx = field.nx;
  for (let k = 0; k < t.length; k++) if (t[k] >= 0) assert.ok(R[k] <= t[k] + 1e-4, "never further out than its minutes");
  const j = Math.round(-field.y0 / field.cell), i0 = Math.round(-field.x0 / field.cell);
  const at = (m) => j * nx + i0 + Math.round(m / field.cell);
  assert.ok(Math.abs(R[at(4000)] - 10) < 1e-4, "the station stays at its 10 minutes");
  assert.ok(R[at(3000)] < t[at(3000)] - 5, "the slow ground before it is pulled in");
  for (let i = i0 + 1; i < nx; i++) assert.ok(R[j * nx + i] >= R[j * nx + i - 1] - 1e-4, "never back closer going out");
  // A walled-off hill beside the pin (a station's tracks) implodes; nothing behind it moves.
  const hill = fieldFromFunction({ origin, cell: 100, halfSize: 3000 },
    (x, y) => Math.hypot(x, y) / WALK + (Math.hypot(x, y + 300) < 150 ? 40 : 0));
  const h = squashLayout(hill, 30, { mode: "implode" }).R;
  const north = (m) => Math.round((-m - hill.y0) / hill.cell) * hill.nx + Math.round(-hill.x0 / hill.cell);
  assert.ok(h.minutes[north(300)] < 6, "the hill is drawn at the streets' minutes");
  assert.ok(Math.abs(h.minutes[north(1500)] - hill.minutes[north(1500)]) < 1e-4, "behind it: its own minutes");
  // Rings through the last place N minutes away: round where nothing quicker lies beyond.
  const plain = fieldFromFunction({ origin, cell: 100, halfSize: 4000 }, (x, y) => Math.hypot(x, y) / WALK);
  const p = squashLayout(plain, 45, { mode: "implode" });
  for (const { minutes, xy } of bentRings(plain, createWarp(p.spec), [15, 30], { last: true })) {
    for (let i = 0; i < 720; i++) assert.ok(Math.abs(Math.hypot(xy[2 * i], xy[2 * i + 1]) - minutes) < 0.05);
  }
});
