// Regressions for defects found in review of the mesh renderer spike.
import { test } from "node:test";
import assert from "node:assert/strict";
import { fieldFromFunction, GRID_NO_DATA, gridFromFunction, sampleGrid } from "../../static/shared/mesh/field.js";
import { localFrame, makeCamera } from "../../static/shared/mesh/frame.js";
import { packScene } from "../../static/shared/mesh/pipeline.js";
import { classify, STYLE_ID } from "../../static/shared/mesh/schema-streets-v8.js";
import { enforceBudget, gridFill } from "../../static/shared/mesh/tessellate.js";
import { createWarp, targetsFor } from "../../static/shared/mesh/warp.js";

const ORIGIN = { lon: -0.1571, lat: 51.5226 };
const frame = localFrame(ORIGIN);
const cam = makeCamera(frame, { centerLon: ORIGIN.lon, centerLat: ORIGIN.lat, zoom: 12, width: 390, height: 844, k: 4 });
// 1 minute per 100 m; the outer minute (45) is at 4.5 km.
const field = fieldFromFunction({ origin: ORIGIN, cell: 100, halfSize: 8000 }, (x, y) => Math.hypot(x, y) / 100);
const polar = createWarp({ kind: "polar", field, maxMinutes: 45 });

test("a primitive across the outer minute stays short while it morphs (no streak)", () => {
  // Two neighbours 200 m apart on one bearing: 44 min (inside) and 46 min (past the ring).
  for (const u of [0.25, 0.5, 0.75, 1]) {
    const a = polar.screenOfLocal(4400, 0, cam, u), b = polar.screenOfLocal(4600, 0, cam, u);
    const ordinary = 200 * cam.ppm, bent = 2 * cam.k;
    assert.ok(Math.hypot(a.x - b.x, a.y - b.y) <= Math.max(ordinary, bent) + 1e-6, `u=${u}`);
    assert.ok(Math.abs(b.alpha - (1 - u)) < 1e-9 && a.alpha === 1);
  }
  // Past the ring, a place moves to the outer ring along its own bearing.
  const p = polar.screenOfLocal(3000, 4000, cam, 1);
  assert.ok(Math.abs(Math.hypot(p.x - cam.pin[0], p.y - cam.pin[1]) - cam.k * 45) < 1e-9);
  assert.ok(Math.abs(Math.atan2(p.y - cam.pin[1], p.x - cam.pin[0]) - Math.atan2(4000, 3000)) < 1e-9);
  // CPU targets carry the same anchor, flagged as "fading" (0.5), not "valid" (1).
  const t = targetsFor(polar, new Float32Array([3000, 4000]));
  assert.ok(Math.abs(t[0] - 27) < 1e-4 && Math.abs(t[1] - 36) < 1e-4 && t[2] === 0.5);
});

test("a grid layout can say 'no data' inside its extent", () => {
  const g = gridFromFunction({ origin: ORIGIN, cell: 500, halfSize: 2000 }, (x, y) => (x > 900 && y > 900 ? [GRID_NO_DATA, 0] : [x, y]));
  assert.equal(sampleGrid(g, 1200, 1200), null);
  assert.deepEqual(sampleGrid(g, -1000, -1000), [-1000, -1000]);
  const w = createWarp({ kind: "grid", grid: g });
  const s = w.screenOfLocal(1200, 1200, { ...cam, k: cam.ppm }, 0.4);
  assert.ok(Math.abs(s.alpha - 0.6) < 1e-9);
});

test("the budget drops the farthest geometry first within a class", () => {
  const f = [
    { cat: "primary", count: 10, area: 0, dist: 100 },
    { cat: "primary", count: 10, area: 0, dist: 9000 },
    { cat: "primary", count: 10, area: 0, dist: 3000 },
    { cat: "park", count: 10, area: 50, dist: 100 },
    { cat: "park", count: 10, area: 50, dist: 8000 },
  ];
  // Total 50: both parks go (smaller first, then farther), then the farthest primary.
  const r = enforceBudget(f, 25);
  assert.deepEqual(r.keep, [true, false, true, false, false]);
  // Same-size parks: the far one goes first.
  assert.deepEqual(enforceBudget(f, 40).keep, [true, true, true, true, false]);
});

test("motorway and trunk slip roads draw with their road", () => {
  assert.equal(classify("road", { class: "motorway_link" }, 2, 12).style, STYLE_ID.motorway);
  assert.equal(classify("road", { class: "trunk_link" }, 2, 12).style, STYLE_ID.trunk);
  assert.equal(classify("road", { class: "primary_link" }, 2, 12).style, STYLE_ID.primary);
});

test("a fully covered block shares its grid nodes", () => {
  const verts = [], index = [];
  gridFill([[[-10, -10], [5000, -10], [5000, 5000], [-10, 5000], [-10, -10]]], 4096, 64, verts, index);
  assert.equal(index.length / 3, 2 * 64 * 64);
  assert.equal(verts.length / 2, 65 * 65);
});

test("concave rings cut into several pieces per cell keep their area and stay inside", () => {
  // A comb: five teeth 100 units wide crossing many cell lines, and a U with a deep notch.
  const comb = [[0, 0], [1100, 0], [1100, 900]];
  for (let k = 5; k >= 0; k--) comb.push([k * 200 + 100, 900], [k * 200 + 100, 100], [k * 200, 100], [k * 200, 900]);
  comb.push([0, 900], [0, 0]);
  const shapes = [
    { ring: comb.filter((p, i, a) => i === 0 || p[0] !== a[i - 1][0] || p[1] !== a[i - 1][1]), cell: 64 },
    { ring: [[100, 100], [900, 100], [900, 900], [700, 900], [700, 300], [300, 300], [300, 900], [100, 900], [100, 100]], cell: 256 },
  ];
  for (const { ring, cell } of shapes) {
    let want = 0;
    for (let i = 0; i < ring.length - 1; i++) want += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1];
    want = Math.abs(want) / 2;
    const verts = [], index = [];
    const got = gridFill([ring], 4096, cell, verts, index);
    assert.ok(Math.abs(got - want) / want < 1e-6, `${got} vs ${want}`);
    const inside = (x, y) => {
      let c = false;
      for (let i = 0, j = ring.length - 2; i < ring.length - 1; j = i++) {
        const [xi, yi] = ring[i], [xj, yj] = ring[j];
        if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) c = !c;
      }
      return c;
    };
    for (let t = 0; t < index.length; t += 3) {
      const cx = (verts[2 * index[t]] + verts[2 * index[t + 1]] + verts[2 * index[t + 2]]) / 3;
      const cy = (verts[2 * index[t] + 1] + verts[2 * index[t + 1] + 1] + verts[2 * index[t + 2] + 1]) / 3;
      assert.ok(inside(cx, cy), `triangle centroid ${cx},${cy} outside`);
    }
  }
});

test("a scene says which origin its buffers are measured from", () => {
  const scene = packScene([], frame);
  assert.deepEqual(scene.origin, ORIGIN);
});

test("tiles of different zooms put the same vertices on their shared edge", async () => {
  const { spacingForZoom } = await import("../../static/shared/mesh/pipeline.js");
  // A z12 tile and the z13 tile touching its right edge; one polygon covers both (tile units
  // of each), so every vertex on the shared edge is a cell corner on both sides.
  const sharedEdge = (z, x, y, side) => {
    const verts = [];
    const div = spacingForZoom(z);
    gridFill([[[-100, -100], [4196, -100], [4196, 4196], [-100, 4196], [-100, -100]]], 4096, 4096 / div, verts, []);
    const n = 2 ** z, out = new Set();
    for (let i = 0; i < verts.length; i += 2) {
      const ex = verts[i], ey = verts[i + 1];
      if (ex !== (side === "right" ? 4096 : 0)) continue;
      // World mercator y of the vertex, as an exact fraction string.
      out.add(((y + ey / 4096) / n).toFixed(12));
    }
    return out;
  };
  const coarse = sharedEdge(12, 100, 50, "right");               // x edge at 101/4096
  const fineA = sharedEdge(13, 202, 100, "left");                // the two z13 tiles beside it
  const fineB = sharedEdge(13, 202, 101, "left");
  const fine = new Set([...fineA, ...fineB]);
  assert.deepEqual([...coarse].sort(), [...fine].sort());
});

test("roads count twice in the budget: casing and fill are two draws", async () => {
  const { packScene: pack } = await import("../../static/shared/mesh/pipeline.js");
  const mesh = {
    z: 12, x: 2046, y: 1361, extent: 4096, fills: [], labels: [],
    lines: [
      { style: 5, cat: "motorway", pass: "road", segs: new Float32Array([0, 0, 10, 0, 10, 0, 20, 0]) },
      { style: 2, cat: "major_rail", pass: "rail", segs: new Float32Array([0, 5, 10, 5]) },
    ],
    stats: { fillVerts: 0, segments: 3 },
  };
  const s = pack([mesh], frame);
  assert.equal(s.stats.shaded, 2 * 6 * 2 + 1 * 6);
  assert.equal(pack([mesh], frame, { budget: 20 }).stats.overBudget, true);
});

test("the census times decoding the whole tile, geometry included", async () => {
  const { censusTile } = await import("../../static/shared/mesh/pipeline.js");
  const { encodeTile } = await import("./mvt-fixture.js");
  const bytes = encodeTile({ road: [{ type: 2, props: { class: "major_rail" }, geometry: [[[0, 0], [100, 0], [200, 50]]] }],
    water: [{ type: 3, props: {}, geometry: [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]] }] });
  const c = censusTile(bytes, 12);
  assert.equal(c.points, 3 + 5);
  assert.ok(c.decodeMs >= 0);
});
