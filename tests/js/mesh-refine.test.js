// Refining the mesh where the field bends it (static/shared/mesh/refine.js), on the lab's
// cliff field: a river 1.5 km north of the pin that one bridge crosses.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { bandMesh } from "../../static/shared/mesh/contours.js";
import { makeEdgeTest, refineFills, refineLines } from "../../static/shared/mesh/refine.js";
import { angularSpread } from "../../static/shared/mesh/warp.js";
import { cliffField, RIVER_Y, transitField } from "../../static/warp/lab/fields.js";

const network = JSON.parse(readFileSync(new URL("../../static/warp/lab/stations.json", import.meta.url)));
const origin = network.origins.find((o) => o.id === "canary-wharf");
const field = cliffField(origin, network);
const EPS = 0.05;
const STEP = 48;

// A 48 m grid of squares, two triangles each, across the river: like a tessellated park.
function gridMesh(x0, y0, n) {
  const pos = [], index = [];
  for (let j = 0; j <= n; j++) for (let i = 0; i <= n; i++) pos.push(x0 + i * STEP, y0 + j * STEP);
  for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) {
    const a = j * (n + 1) + i, b = a + 1, c = a + n + 2, d = a + n + 1;
    index.push(a, b, c, a, c, d);
  }
  const nv = pos.length / 2;
  return { pos: Float32Array.from(pos), index: Uint32Array.from(index), style: new Float32Array(nv), zoom: new Uint8Array(nv) };
}

test("fills split where the river jumps, and every inner edge stays shared (no cracks)", () => {
  const n = 40, x0 = -1000, y0 = RIVER_Y - 20 * STEP;
  const mesh = gridMesh(x0, y0, n);
  const t = makeEdgeTest(field, 60, EPS);
  const r = refineFills(mesh, t, { maxDepth: 3 });
  assert.ok(r.added > 0, "the river makes the mesh split");
  const uses = new Map();
  for (let i = 0; i < r.index.length; i += 3) {
    for (const [a, b] of [[r.index[i], r.index[i + 1]], [r.index[i + 1], r.index[i + 2]], [r.index[i + 2], r.index[i]]]) {
      const k = a < b ? `${a},${b}` : `${b},${a}`;
      uses.set(k, (uses.get(k) || 0) + 1);
    }
  }
  const X = (v) => r.pos[2 * v], Y = (v) => r.pos[2 * v + 1];
  const x1 = x0 + n * STEP, y1 = y0 + n * STEP, on = (v, a) => Math.abs(v - a) < 1e-3;
  for (const [k, c] of uses) {
    assert.ok(c <= 2, k);
    if (c === 1) {
      const [a, b] = k.split(",").map(Number);
      const border = (on(X(a), x0) && on(X(b), x0)) || (on(X(a), x1) && on(X(b), x1)) ||
        (on(Y(a), y0) && on(Y(b), y0)) || (on(Y(a), y1) && on(Y(b), y1));
      assert.ok(border, `an inner edge used once: a crack at ${X(a)},${Y(a)}`);
    }
  }
  // The chord error falls: with room to split deep enough, every edge is within eps.
  const worstOf = (m) => {
    let worst = 0;
    const px = (v) => m.pos[2 * v], py = (v) => m.pos[2 * v + 1];
    for (let i = 0; i < m.index.length; i += 3) {
      for (const [a, b] of [[m.index[i], m.index[i + 1]], [m.index[i + 1], m.index[i + 2]], [m.index[i + 2], m.index[i]]]) {
        const ta = t.t(px(a), py(a)), tb = t.t(px(b), py(b)), tm = t.t((px(a) + px(b)) / 2, (py(a) + py(b)) / 2);
        if (ta >= 0 && tb >= 0 && tm >= 0) worst = Math.max(worst, Math.abs(tm - (ta + tb) / 2));
      }
    }
    return worst;
  };
  const before = worstOf(mesh), at3 = worstOf(r), deep = worstOf(refineFills(mesh, t, { maxDepth: 10 }));
  assert.ok(before > 0.3, `${before}`);               // a 48 m edge across the jump misses by 0.4 min
  assert.ok(at3 < before * 0.6, `${at3} vs ${before}`);
  assert.ok(deep <= EPS + 1e-3, `${deep}`);           // positions are float32
  // The area is unchanged.
  let area = 0;
  for (let i = 0; i < r.index.length; i += 3) {
    const [a, b, c] = [r.index[i], r.index[i + 1], r.index[i + 2]];
    area += ((X(b) - X(a)) * (Y(c) - Y(a)) - (Y(b) - Y(a)) * (X(c) - X(a))) / 2;
  }
  assert.ok(Math.abs(area - (n * STEP) ** 2) < 1, `${area}`);
});

test("a smooth field doesn't split anything", () => {
  const mesh = gridMesh(-1000, -1000, 10);
  const r = refineFills(mesh, makeEdgeTest(field, 60, EPS));
  assert.equal(r.added, 0);
  assert.equal(r.index, mesh.index);
});

test("lines split across the river and keep their ends", () => {
  const seg = Float32Array.from([-800, RIVER_Y + 30, -800, RIVER_Y - 30, -800, -500, -700, -500]);
  const r = refineLines({ seg, style: new Float32Array(2), zoom: new Uint8Array(2) }, makeEdgeTest(field, 60, EPS));
  assert.ok(r.count > 2);
  assert.deepEqual([r.seg[0], r.seg[1]], [-800, RIVER_Y + 30]);
  const last = r.count - 2;
  assert.deepEqual([r.seg[4 * last + 2], r.seg[4 * last + 3]], [-800, RIVER_Y - 30]);
  assert.deepEqual(Array.from(r.seg.slice(-4)), [-800, -500, -700, -500]);
});

test("refinement stays inside the room it is given", () => {
  const mesh = gridMesh(-1000, RIVER_Y - 10 * STEP, 20);
  const t = makeEdgeTest(field, 60, EPS);
  assert.equal(refineFills(mesh, t, { room: 0 }).added, 0);
  // Not enough room for the whole pass: nothing is split (a partial pass could leave a
  // T-junction where two meshes share a boundary but not vertex indexes).
  const some = refineFills(mesh, t, { room: 25 });
  assert.equal(some.added, 0);
  assert.ok(some.skipped > 25);
  assert.equal(some.index, mesh.index);
  const all = refineFills(mesh, t);
  assert.ok(refineFills(mesh, t, { room: all.added }).added === all.added);
  const lines = refineLines({ seg: Float32Array.from([-800, RIVER_Y + 30, -800, RIVER_Y - 30]), style: new Float32Array(1), zoom: new Uint8Array(1) },
    t, { room: 6, segCost: 6 });
  assert.equal(lines.added, 1);
});

test("the band mesh takes only cells whose corners the warp can place", () => {
  const m = bandMesh(field, [-500, -500, 500, 500]);
  assert.equal(m.index.length / 6, 10 * 10);
  assert.equal(m.pos.length / 2, 11 * 11);
  assert.equal(m.minutes.length, 11 * 11);
  // Next to a node with no time, the cell up and to the left of it goes too: its far corner
  // samples the unknown node.
  const holed = { ...field, minutes: Float32Array.from(field.minutes) };
  const k = Math.round((0 - field.y0) / field.cell) * field.nx + Math.round((0 - field.x0) / field.cell);
  holed.minutes[k] = -1;
  const h = bandMesh(holed, [-500, -500, 500, 500]);
  assert.equal(h.index.length / 6, 10 * 10 - 9);
});

test("with directions by reach, the test reads the bent position, not minutes alone", () => {
  const o = network.origins.find((p) => p.id === "canary-wharf");
  const f = transitField(o, network);
  const angles = angularSpread(f, 45, { spread: 0.5 });
  const mesh = gridMesh(-3000, -3000, 125);
  const plain = refineFills(mesh, makeEdgeTest(f, 45, EPS));
  const bent = refineFills(mesh, makeEdgeTest(f, 45, EPS, angles));
  assert.ok(bent.added > plain.added, `${bent.added} vs ${plain.added}`);
});
