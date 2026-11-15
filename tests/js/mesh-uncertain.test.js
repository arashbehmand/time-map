// Uncertain patches (server/warp/field.py): ground the contours leave out, kept at the
// provider's minutes but drawn by its drawn minutes on the bent map, and no place anywhere else.
import { test } from "node:test";
import assert from "node:assert/strict";
import { decodeField, geometryField, knownMinutes, NO_DATA, nodeAt } from "../../static/shared/mesh/field.js";
import { bandMesh, regionRings } from "../../static/shared/mesh/contours.js";
import { enclosedFade, wayOut } from "../../static/shared/mesh/diagnostics.js";
import { sheetIndex } from "../../static/shared/mesh/hit.js";
import { squashLayout } from "../../static/shared/mesh/layouts.js";
import { angularSpread, createWarp } from "../../static/shared/mesh/warp.js";
import * as T from "../../static/warp/words.js";

const b64 = (typed) => Buffer.from(new Uint8Array(typed.buffer)).toString("base64");
const N = 21, CELL = 100, HALF = ((N - 1) / 2) * CELL;
const at = (x, y) => ((y + HALF) / CELL) * N + (x + HALF) / CELL;

// A walk from the pin at 4 km/h, with King's Cross's platforms 200–300 m north at 50 minutes
// (the contour interpolation's time) and their drawn minutes between the ground around them.
function kingsCross({ patches = true, minutes = null } = {}) {
  const t = new Float32Array(N * N);
  for (let j = 0; j < N; j++) for (let i = 0; i < N; i++) t[j * N + i] = Math.hypot(i * CELL - HALF, j * CELL - HALF) / 66.667;
  t[at(0, -200)] = 50; t[at(0, -300)] = 50.1;
  if (minutes) minutes(t);
  const raw = Uint16Array.from(t, (v) => (v < 0 ? 0xffff : Math.round(v * 10)));
  const json = { origin: { lon: -0.1233, lat: 51.5308 }, cell: CELL, nx: N, ny: N, x0: -HALF, y0: -HALF, minutes_b64: b64(raw) };
  if (patches) {
    json.uncertain = { nodes_b64: b64(Uint32Array.from([at(0, -300), at(0, -200)])), drawn_b64: b64(Uint16Array.from([80, 60])) };
  }
  return json;
}

test("a field's uncertain patches decode onto drawn minutes, the minutes kept", () => {
  const f = decodeField(kingsCross());
  assert.equal(f.minutes[at(0, -200)], 50);                      // the provider's estimate
  assert.equal(f.uncertain[at(0, -200)], 1);
  assert.equal(f.uncertain[at(0, -100)], 0);
  assert.equal(Math.round(f.drawn[at(0, -200)] * 10), 60);
  assert.equal(f.drawn[at(0, -100)], f.minutes[at(0, -100)]);
  const g = geometryField(f);
  assert.equal(Math.round(g.minutes[at(0, -300)] * 10), 80);
  assert.equal(g.uncertain, f.uncertain);
  assert.equal(geometryField(f), g);                             // built once
  assert.equal(geometryField(g), g);
  const plain = decodeField(kingsCross({ patches: false }));
  assert.equal(plain.uncertain, null);
  assert.equal(geometryField(plain), plain);
});

test("a bad patch list is a bad payload", () => {
  const ok = kingsCross();
  const bad = (u) => () => decodeField({ ...ok, uncertain: { ...ok.uncertain, ...u } });
  assert.throws(bad({ nodes_b64: b64(Uint32Array.from([at(0, -200), at(0, -300)])).slice(0, 4) }));
  assert.throws(bad({ nodes_b64: b64(Uint32Array.from([N * N, N * N + 1])) }));           // off the grid
  assert.throws(bad({ drawn_b64: b64(Uint16Array.from([80])) }));                          // lengths differ
  assert.throws(bad({ nodes_b64: b64(Uint32Array.from([at(0, -200), at(0, -200)])) }));   // not increasing
});

test("labels and taps claim no time in a patch", () => {
  const f = decodeField(kingsCross());
  assert.equal(knownMinutes(f, 10, -250), NO_DATA);              // a cell with a patch corner
  assert.equal(knownMinutes(f, 10, -390), NO_DATA);
  assert.ok(knownMinutes(f, 10, -650) > 9);                      // the street beyond keeps its time
  assert.equal(nodeAt(f, 0, -200).uncertain, true);
  assert.equal(T.tapGeographic(nodeAt(f, 0, -200), "King's Cross"), T.UNCERTAIN_TAP);
  assert.match(T.tapGeographic(nodeAt(f, 0, -600), "King's Cross"), /^About 9 min from King's Cross\.$/);
});

test("the fold draws a patch between the ground around it, not out on the ring", () => {
  const f = decodeField(kingsCross());
  const wedge = createWarp({ kind: "polar", field: f, maxMinutes: 30 });
  assert.equal(wedge.placement(0, -200)[2], 0.5);                // today: onto the outer ring
  const warp = createWarp({ kind: "polar", field: geometryField(f), maxMinutes: 30 });
  for (const y of [-100, -200, -300, -400]) {
    const [tx, ty, flag] = warp.placement(0, y);
    assert.equal(flag, 1);
    assert.ok(Math.hypot(tx, ty) < 10, `${y}: ${Math.hypot(tx, ty)}`);
  }
  assert.ok(Math.abs(Math.hypot(...warp.target(0, -200)) - 6) < 1e-4);
});

test("enclosedFade counts the wedge, and not a slow inlet open to the edge", () => {
  const f = decodeField(kingsCross());
  assert.deepEqual(enclosedFade(f, 30).where.map(([x, y]) => [x, y]), [[0, -300], [0, -200]]);
  assert.equal(enclosedFade(geometryField(f), 30).count, 0);
  // The same slow strip, open all the way north to the grid's edge: its way out is outward.
  const inlet = decodeField(kingsCross({ patches: false, minutes: (t) => { for (let y = -200; y >= -HALF; y -= CELL) t[at(0, y)] = 50 + (-y - 200) / 100; } }));
  assert.equal(enclosedFade(inlet, 30).count, 0);
  // Both platform nodes get out best along the street north of them (0, −400).
  const w = wayOut(f);
  assert.equal(w[at(0, -200)], f.minutes[at(0, -400)]);
  assert.equal(w[at(0, -300)], f.minutes[at(0, -400)]);
});

test("a patch is no place: bands, the band layer, by reach and the overlap index leave it out", () => {
  const f = decodeField(kingsCross());
  const plain = decodeField(kingsCross({ patches: false }));
  // At 60 minutes the patch's 50 would be coloured in: with the patch, the region has a hole.
  assert.equal(regionRings(plain, 60).length, 1);
  assert.equal(regionRings(f, 60).length, 2);
  const m = bandMesh(f);
  for (let v = 0; v < m.pos.length / 2; v++) assert.ok(!(m.pos[2 * v] === 0 && (m.pos[2 * v + 1] === -200 || m.pos[2 * v + 1] === -300)));
  const u = bandMesh(f, null, { uncertainOnly: true });
  assert.ok(u.index.length > 0 && u.index.length < m.index.length);
  for (let v = 0; v < u.pos.length / 2; v++) assert.ok(u.minutes[v] < 15);
  assert.equal(bandMesh(plain, null, { uncertainOnly: true }).index.length, 0);
  // By reach: the same as if the patch weren't known at all.
  const gone = decodeField(kingsCross({ patches: false, minutes: (t) => { t[at(0, -200)] = -1; t[at(0, -300)] = -1; } }));
  assert.deepEqual(Array.from(angularSpread(f, 60, { spread: 1 }).a), Array.from(angularSpread(gone, 60, { spread: 1 }).a));
  const warp = createWarp({ kind: "polar", field: geometryField(f), maxMinutes: 60 });
  assert.ok(sheetIndex(f, warp, { maxMinutes: 60 }).triangles < sheetIndex(plain, warp, { maxMinutes: 60 }).triangles);
});

test("squash bends a patch by its drawn minutes, and its rings step over it", () => {
  const f = decodeField(kingsCross());
  const sq = squashLayout(f, 30);
  assert.ok(sq.R.minutes[at(0, -200)] < 15 && sq.R.minutes[at(0, -300)] < 15);
  assert.equal(sq.ringSource.minutes[at(0, -200)], NO_DATA);
  assert.equal(sq.ringSource.minutes[at(0, -100)], f.minutes[at(0, -100)]);
});

test("the patch sentence has no jargon and says no time", () => {
  assert.doesNotMatch(T.UNCERTAIN_TAP, /\d/);
  assert.match(T.UNCERTAIN_TAP, /^No reliable travel time here/);
});
