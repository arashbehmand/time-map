// Squash, the trial layout for transit's pockets (static/shared/mesh/layouts.js).
import { test } from "node:test";
import assert from "node:assert/strict";
import { BEYOND, createField, fieldFromFunction, NO_DATA, sampleField } from "../../static/shared/mesh/field.js";
import { foldDiagnostics, LAYOUT_LIMITS, layoutChecks, ringShares } from "../../static/shared/mesh/diagnostics.js";
import { bentRings, CUT_MIN_DROP, cutMask, cutPeaks, squashField, squashLayout, squashReach } from "../../static/shared/mesh/layouts.js";
import { createWarp } from "../../static/shared/mesh/warp.js";

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

test("squash's rings: one closed bent circle per level, every vertex a place that many minutes away", () => {
  const R = squashField(field);
  const reach = squashReach(field, R, 45);
  assert.ok(reach > 45, "the pocket's far side is drawn past 45");
  const warp = createWarp({ kind: "polar", field: R, maxMinutes: reach });
  const rings = bentRings(field, warp, [20, 10]);
  assert.deepEqual(rings.map((r) => r.minutes), [10, 20]);
  for (const { minutes, xy, src } of rings) {
    const n = xy.length / 2;
    assert.ok(Number.isNaN(xy[xy.length - 1]) && n === 720 + 2, "720 directions, closed, one piece");
    assert.ok(Math.abs(xy[0] - xy[2 * 720]) < 1e-6 && Math.abs(xy[1] - xy[2 * 720 + 1]) < 1e-6);
    let prev = null;
    for (let i = 0; i < 720; i++) {
      const ang = Math.atan2(xy[2 * i + 1], xy[2 * i]);
      if (prev !== null) assert.ok(((ang - prev + 3 * Math.PI) % (2 * Math.PI)) - Math.PI > 0, "goes round the pin once");
      prev = ang;
      // The vertex is a place `minutes` away on the true field, drawn where the warp puts it.
      assert.ok(Math.abs(sampleField(field, src[2 * i], src[2 * i + 1]) - minutes) < 0.3);
      const t = warp.target(src[2 * i], src[2 * i + 1]);
      assert.ok(Math.abs(t[0] - xy[2 * i]) < 1e-3 && Math.abs(t[1] - xy[2 * i + 1]) < 1e-3);
    }
  }
  // East, the 20-minute ring stops at the first 20-minute place: before the pocket, not past it.
  assert.ok(Math.abs(rings[1].src[0] - 20 * WALK) < 30, `east at ${rings[1].src[0]} m`);
});

test("the hill cut: walled-off ground is lowered to the way round it, nothing else moves", () => {
  // Walking from the pin, with a 40-minute dome 300 m north (a station's platforms), a 3-minute
  // bump east (a slow street: under the cut's threshold), a slow ridge west running out to the
  // edge, an unknown hole beside the dome, and a strip of "beyond" ground far south.
  const walk = (x, y) => Math.hypot(x, y) / WALK;
  const f = fieldFromFunction({ origin, cell: 100, halfSize: 3000 }, (x, y) => {
    if (Math.hypot(x, y + 300) < 150) return walk(x, y) + 40;
    if (Math.hypot(x - 1000, y) < 150) return walk(x, y) + 3;
    if (x < -800 && Math.abs(y) < 100) return walk(x, y) + 30;
    if (y > 2500) return BEYOND;
    return walk(x, y);
  });
  const hole = (x, y) => Math.hypot(x - 400, y + 300) < 120;
  for (let j = 0; j < f.ny; j++) for (let i = 0; i < f.nx; i++) {
    if (hole(f.x0 + i * f.cell, f.y0 + j * f.cell)) f.minutes[j * f.nx + i] = NO_DATA;
  }
  const c = cutPeaks(f);
  const at = (x, y) => Math.round((y - f.y0) / f.cell) * f.nx + Math.round((x - f.x0) / f.cell);
  assert.ok(f.minutes[at(0, -300)] > 40 && c.minutes[at(0, -300)] < 8, "the dome is cut to the streets round it");
  assert.equal(c.minutes[at(1000, 0)], f.minutes[at(1000, 0)], "a bump under the threshold stays");
  assert.equal(c.minutes[at(-1500, 0)], f.minutes[at(-1500, 0)], "a slow ridge open to the edge stays");
  assert.equal(c.minutes[at(400, -300)], NO_DATA, "the hole stays unknown");
  assert.equal(c.minutes[at(0, 2800)], BEYOND, "beyond stays beyond");
  // Squash's rings step over the cut dome: north, the 15 ring is at the streets' 15 minutes
  // (1 km), not at the dome's edge 150 m out.
  const sq = squashLayout(f, 30);
  const [ring] = bentRings(sq.ringSource, createWarp(sq.spec), [15]);
  const north = 540;                                             // 720 bearings; y is south
  assert.ok(Math.abs(ring.src[2 * north + 1] + 1000) < 60, `north at ${ring.src[2 * north + 1]} m`);
  for (let k = 0; k < f.minutes.length; k++) {
    if (f.minutes[k] >= 0 && f.minutes[k] < BEYOND) {
      assert.ok(c.minutes[k] <= f.minutes[k], "never raised");
      assert.ok(c.minutes[k] === f.minutes[k] || f.minutes[k] - c.minutes[k] >= CUT_MIN_DROP, "lowered by the threshold or not at all");
    }
  }
});

test("holes: squash steps over them, and the rings carry on beyond them", () => {
  // The pocket field with an unknown strip 3 cells wide across the ray east, at 2.5 km.
  const h = createField({ ...field, minutes: Float32Array.from(field.minutes) });
  for (let j = 0; j < h.ny; j++) for (let i = 0; i < h.nx; i++) {
    const x = h.x0 + i * h.cell;
    if (x >= 2450 && x <= 2750) h.minutes[j * h.nx + i] = NO_DATA;
  }
  const R = squashField(h).minutes;
  const j = Math.round(-h.y0 / h.cell), i0 = Math.round(-h.x0 / h.cell);
  const before = R[j * h.nx + i0 + 24], after = R[j * h.nx + i0 + 28];
  assert.ok(after >= before, `beyond the hole (${after}) is not drawn nearer than before it (${before})`);
  const warp = createWarp({ kind: "polar", field: squashField(h), maxMinutes: 200 });
  // Nothing before the hole is 37 minutes away (the ridge is 35); beyond it, the walk out of the
  // pocket reaches 37 at about 5.8 km. Stopping at the hole would leave no point east.
  const [ring] = bentRings(h, warp, [37]);
  assert.ok(Math.abs(ring.src[1]) < 1 && Math.abs(ring.src[0] - 5800) < 60, `the 37 ring east at ${ring.src[0]} m`);
});

test("both workers build one layout: the filled flags travel with the field", () => {
  const f = createField({ ...field, minutes: Float32Array.from(field.minutes) });
  f.filled = new Uint8Array(f.minutes.length);
  for (let k = 0; k < f.filled.length; k += 7) f.filled[k] = 1;
  const wire = { origin: f.origin, cell: f.cell, nx: f.nx, ny: f.ny, x0: f.x0, y0: f.y0, minutes: f.minutes, filled: f.filled };
  const rebuilt = createField(wire);
  rebuilt.filled = wire.filled;                                  // worker.js fieldOf
  const a = squashLayout(f, 45, { scale: "ground", spread: 0.5 }), b = squashLayout(rebuilt, 45, { scale: "ground", spread: 0.5 });
  assert.deepEqual([a.reach, a.frame], [b.reach, b.frame]);
  assert.deepEqual(a.R.minutes, b.R.minutes);
  const bare = squashLayout(createField(wire), 45, { scale: "ground", spread: 0.5 });
  assert.notEqual(bare.frame, a.frame, "without the flags the ground scale counts differently");
  assert.ok(a.spec.angles, "the by-reach table is built on R");
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

test("the reader-facing checks: what each layout gets right and wrong about T minutes", () => {
  const plain = fieldFromFunction({ origin, cell: 100, halfSize: 4000 }, (x, y) => Math.hypot(x, y) / WALK);
  const p = squashLayout(plain, 45);
  const pc = layoutChecks(p.spec, plain, 30, bentRings(plain, createWarp(p.spec), [30])[0]);
  assert.ok(pc.overlap < 0.01 && pc.outside < 0.01 && pc.slowInside < 0.01, JSON.stringify(pc));
  assert.ok(pc.stretch < 1.1);
  // The pocket: the fold overlaps; squash doesn't, but draws the pocket's quick places beyond
  // the 30 ring.
  const foldSpec = { kind: "polar", field, maxMinutes: 60 };
  const fc = layoutChecks(foldSpec, field, 30, bentRings(field, createWarp(foldSpec), [30])[0]);
  const sq = squashLayout(field, 30);
  const sc = layoutChecks(sq.spec, field, 30, bentRings(field, createWarp(sq.spec), [30])[0]);
  assert.ok(fc.overlap > 0.02, JSON.stringify(fc));
  assert.ok(sc.overlap < fc.overlap / 5, JSON.stringify(sc));
  assert.ok(sc.outside > 0.05, "the pocket lies beyond the 30 ring");
});

test("W-03: the page's ring counts are the checks' counts, and cut patches are counted apart", () => {
  // The pocket field: ringShares (what the page states) agrees with layoutChecks (what the lab
  // gates) on the same warp and ring.
  const sq = squashLayout(field, 30);
  const warp = createWarp(sq.spec);
  const [ring] = bentRings(sq.ringSource, warp, [30]);
  const lc = layoutChecks(sq.spec, field, 30, ring);
  const rs = ringShares(warp, field, 30, ring);
  assert.equal(rs.outside, lc.outside);
  assert.equal(rs.slowInside, lc.slowInside);
  assert.equal(rs.omitted, lc.omitted);
  assert.ok(rs.counts.judged > 100 && rs.bearings > 0.99, JSON.stringify(rs));
  assert.ok(rs.outside > 0.05, "the pocket's quick places are past the ring");

  // A 40-minute dome 300 m north of the pin (a station's platforms), walled off: squash cuts it,
  // so it is drawn inside the 15 ring though slower. Counted as cut, it leaves C3's share.
  const walk = (x, y) => Math.hypot(x, y) / WALK;
  const f = fieldFromFunction({ origin, cell: 100, halfSize: 3000 },
    (x, y) => (Math.hypot(x, y + 300) < 150 ? walk(x, y) + 40 : walk(x, y)));
  const s2 = squashLayout(f, 30);
  const w2 = createWarp(s2.spec);
  const [r15] = bentRings(s2.ringSource, w2, [15]);
  const cut = cutMask(f, s2);
  assert.ok(cut.reduce((a, b) => a + b, 0) > 3, "the dome is flagged");
  const c = ringShares(w2, f, 15, r15, { step: 1, cut });
  assert.ok(c.counts.slowCut > 0 && c.counts.slowCut === c.counts.slowInside, JSON.stringify(c.counts));
  assert.ok(c.slowInside > 0 && c.slowInsideExCut === 0);
  assert.ok(c.counts.insideCut >= c.counts.slowCut);
});

test("W-03: nothing to count is no share, never a pass at 0%", () => {
  const empty = fieldFromFunction({ origin, cell: 100, halfSize: 1000 }, () => NO_DATA);
  const warp = createWarp({ kind: "polar", field, maxMinutes: 45 });
  const rs = ringShares(warp, empty, 30, { xy: new Float32Array(0) });
  assert.equal(rs.outside, null);
  assert.equal(rs.slowInside, null);
  assert.equal(rs.bearings, 0);
  assert.deepEqual(LAYOUT_LIMITS, { overlap: 0.05, stretch: 4, slowInside: 0.025, omitted: 0.02 });
});

test("W-03: the ring is read as drawn: segments cover the gaps by reach opens, a spike is thin", () => {
  // Directions by reach move the ring's vertices apart in drawn bearing; the segments between
  // them still cover every bearing, so no place goes unjudged.
  const sq = squashLayout(field, 30, { spread: 0.5 });
  const warp = createWarp(sq.spec);
  const [ring] = bentRings(sq.ringSource, warp, [30]);
  const rs = ringShares(warp, field, 30, ring);
  assert.ok(rs.bearings > 0.99, `bearings ${rs.bearings}`);
  assert.equal(rs.counts.unjudged, 0);

  // A circle of radius 10 with one spike out to 20 at bearing 0. A place beside the spike's base
  // at radius 15 is outside the ring; one in the spike is inside. (A largest-radius-per-bearing
  // reading put both inside.) The mock warp draws every place where it is.
  const n = 720, xy = [];
  for (let b = 0; b <= n; b++) {
    const a = (2 * Math.PI * b) / n, r = b % n === 0 ? 20 : 10;
    xy.push(r * Math.cos(a), r * Math.sin(a));
  }
  const at = (deg, r) => [r * Math.cos((deg * Math.PI) / 180), r * Math.sin((deg * Math.PI) / 180)];
  const places = [at(0, 15), at(0.5, 15), at(90, 5), at(90, 12)];
  // Two rows, the second no data, since a field needs at least 2 × 2 nodes.
  const f = createField({ origin, cell: 1, nx: places.length, ny: 2, x0: 0, y0: 0,
                          minutes: new Float32Array([20, 20, 40, 20, NO_DATA, NO_DATA, NO_DATA, NO_DATA]) });
  const mock = { target: (x) => places[Math.round(x)] };
  const s = ringShares(mock, f, 30, { xy: Float32Array.from(xy) }, { step: 1 });
  // Within 30: places 0, 1 and 3; 0 is in the spike (inside), 1 beside it and 3 past the circle
  // (outside). Place 2 (40 minutes) is inside and slower.
  assert.deepEqual([s.counts.judged, s.counts.outside, s.counts.inside, s.counts.slowInside], [3, 2, 2, 1]);
});
