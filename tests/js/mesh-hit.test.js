// Every sheet under a finger (static/shared/mesh/hit.js), against a brute force over every
// triangle, on the lab's toy transit field, which folds.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fieldFromFunction, sampleField } from "../../static/shared/mesh/field.js";
import { closestBary, sheetIndex } from "../../static/shared/mesh/hit.js";
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
