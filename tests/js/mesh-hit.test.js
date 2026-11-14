// Every sheet under a finger (static/shared/mesh/hit.js), against a brute force over every
// triangle, on the lab's toy transit field, which folds.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fieldFromFunction, sampleField } from "../../static/shared/mesh/field.js";
import { closestBary, meshSheetIndex, sheetIndex } from "../../static/shared/mesh/hit.js";
import { bandMesh } from "../../static/shared/mesh/contours.js";
import { makeEdgeTest, refineFills } from "../../static/shared/mesh/refine.js";
import { angularSpread, createWarp } from "../../static/shared/mesh/warp.js";
import { transitField } from "../../static/warp/lab/fields.js";

const network = JSON.parse(readFileSync(new URL("../../static/warp/lab/stations.json", import.meta.url)));
const origin = network.origins.find((o) => o.id === "baker-street");
const fine = transitField(origin, network);
// Coarser, so the brute force stays quick.
const field = fieldFromFunction({ origin, cell: 400, halfSize: 16000 }, (x, y) => sampleField(fine, x, y));

function rng(seed) {
  let a = seed >>> 0;
  return () => { a = (a * 1664525 + 1013904223) >>> 0; return a / 4294967296; };
}

for (const spread of [0, 0.5]) {
  test(`every triangle under a point is found (spread ${spread})`, () => {
    const spec = { kind: "polar", field, maxMinutes: 45 };
    if (spread) spec.angles = angularSpread(field, 45, { spread });
    const warp = createWarp(spec);
    const index = sheetIndex(field, warp, { maxMinutes: 45, merge: 0 });
    // Brute force: the same cells and triangles.
    const { nx, ny, x0, y0, cell, minutes } = field;
    const T = [];
    for (let j = 0; j + 1 < ny; j++) for (let i = 0; i + 1 < nx; i++) {
      const ids = [j * nx + i, j * nx + i + 1, (j + 1) * nx + i + 1, (j + 1) * nx + i];
      if (ids.some((k) => !(minutes[k] >= 0) || minutes[k] > 45)) continue;
      const p = ids.map((k) => warp.target(x0 + (k % nx) * cell, y0 + Math.floor(k / nx) * cell));
      if (p.some((q) => !q)) continue;
      T.push([p[0], p[1], p[2]], [p[0], p[2], p[3]]);
    }
    assert.equal(index.triangles, T.length);
    const rand = rng(7 + spread * 10);
    let stacked = 0;
    for (let n = 0; n < 500; n++) {
      const r = 45 * Math.sqrt(rand()), a = 2 * Math.PI * rand();
      const lx = r * Math.cos(a), ly = r * Math.sin(a);
      const want = T.filter(([p, q, s]) => closestBary(lx, ly, p[0], p[1], q[0], q[1], s[0], s[1]).d === 0).length;
      const got = index.query(lx, ly, 0).length;
      assert.equal(got, want, `${lx},${ly}`);
      if (want > 1) stacked++;
    }
    assert.ok(stacked > 0, "the toy field folds, so some points lie under several sheets");
  });
}

test("hits merge into sheets by distance between their sources", () => {
  const spec = { kind: "polar", field, maxMinutes: 45 };
  const index = sheetIndex(field, createWarp(spec), { maxMinutes: 45 });
  const sheets = index.query(10, 0, 0.5);
  for (let i = 0; i < sheets.length; i++) for (let j = i + 1; j < sheets.length; j++) {
    assert.ok(Math.hypot(sheets[i].x - sheets[j].x, sheets[i].y - sheets[j].y) > 300);
  }
  for (const s of sheets) assert.ok(Math.abs(Math.hypot(...createWarp(spec).target(s.x, s.y)) - s.minutes) < 2);
});

test("the index reads any mesh: the band layer, refined, against a brute force", () => {
  const spec = { kind: "polar", field, maxMinutes: 45 };
  const warp = createWarp(spec);
  const bands = bandMesh(field);
  const n = bands.pos.length / 2;
  const r = refineFills({ pos: bands.pos, index: bands.index, style: new Float32Array(n), zoom: new Uint8Array(n) },
    makeEdgeTest(field, 45, 0.05), { maxDepth: 2 });
  assert.ok(r.added > 0);
  const minutes = new Float32Array(r.pos.length / 2);
  for (let v = 0; v < minutes.length; v++) minutes[v] = sampleField(field, r.pos[2 * v], r.pos[2 * v + 1]);
  const index = meshSheetIndex({ pos: r.pos, minutes, index: r.index }, warp, { merge: 0 });
  const T = [];
  for (let i = 0; i < r.index.length; i += 3) {
    const p = [r.index[i], r.index[i + 1], r.index[i + 2]].map((v) => warp.target(r.pos[2 * v], r.pos[2 * v + 1]));
    if (p.every(Boolean)) T.push(p);
  }
  assert.equal(index.triangles, T.length);
  const rand = rng(11);
  for (let k = 0; k < 200; k++) {
    const rr = 45 * Math.sqrt(rand()), a = 2 * Math.PI * rand();
    const lx = rr * Math.cos(a), ly = rr * Math.sin(a);
    const want = T.filter(([p, q, t]) => closestBary(lx, ly, p[0], p[1], q[0], q[1], t[0], t[1]).d === 0).length;
    assert.equal(index.query(lx, ly, 0).length, want);
  }
});

test("hits near each other with different minutes stay separate (squash draws them together)", () => {
  // Two triangles 150 m apart drawn on the same spot (as squash can draw a pocket's edge next to
  // slower ground): 20 and 35 minutes. Within 300 m of each other, but not the same place.
  const pos = Float32Array.from([0, 0, 100, 0, 0, 100, 150, 0, 250, 0, 150, 100]);
  const minutes = Float32Array.from([20, 20, 20, 35, 35, 35]);
  const index = Uint32Array.from([0, 1, 2, 3, 4, 5]);
  const warp = { target: (x, y) => [(x >= 150 ? x - 150 : x) / 10, y / 10] };
  const hits = meshSheetIndex({ pos, minutes, index }, warp).query(2, 2, 0.1);
  assert.deepEqual(hits.map((h) => Math.round(h.minutes)).sort(), [20, 35]);
  assert.equal(meshSheetIndex({ pos, minutes, index }, warp, { mergeMinutes: Infinity }).query(2, 2, 0.1).length, 1);
});
