import { test } from "node:test";
import assert from "node:assert/strict";
import { fieldFromFunction } from "../../static/shared/mesh/field.js";
import { localFrame, makeCamera } from "../../static/shared/mesh/frame.js";
import { clipSegment, enforceBudget, gridFill, lineSegments, ringArea } from "../../static/shared/mesh/tessellate.js";
import { createWarp } from "../../static/shared/mesh/warp.js";

const EXT = 4096, CELL = 64;
const triArea = (v, i) => {
  const [a, b, c] = [i[0], i[1], i[2]];
  return ((v[2 * b] - v[2 * a]) * (v[2 * c + 1] - v[2 * a + 1]) - (v[2 * b + 1] - v[2 * a + 1]) * (v[2 * c] - v[2 * a])) / 2;
};

function fillStats(rings) {
  const verts = [], index = [];
  const kept = gridFill(rings, EXT, CELL, verts, index);
  let sum = 0, maxEdge = 0, minArea = Infinity, oneCell = true;
  for (let t = 0; t < index.length; t += 3) {
    const tri = index.slice(t, t + 3);
    const a = triArea(verts, tri);
    sum += a;
    minArea = Math.min(minArea, a);
    const xs = tri.map((k) => verts[2 * k]), ys = tri.map((k) => verts[2 * k + 1]);
    for (let e = 0; e < 3; e++) {
      const p = tri[e], q = tri[(e + 1) % 3];
      maxEdge = Math.max(maxEdge, Math.hypot(verts[2 * p] - verts[2 * q], verts[2 * p + 1] - verts[2 * q + 1]));
    }
    // All three corners inside one cell (shared cell edges count for both sides).
    const cx = Math.floor((Math.min(...xs) + 1e-6) / CELL), cy = Math.floor((Math.min(...ys) + 1e-6) / CELL);
    if (Math.max(...xs) > (cx + 1) * CELL + 1e-6 || Math.max(...ys) > (cy + 1) * CELL + 1e-6) oneCell = false;
  }
  return { kept, sum, maxEdge, minArea, oneCell, verts, index };
}

test("a segment is clipped to the tile", () => {
  assert.deepEqual(clipSegment(-100, 100, 100, 100, 0, EXT), [0, 100, 100, 100]);
  assert.equal(clipSegment(-100, -100, -1, -5, 0, EXT), null);
  const segs = lineSegments([[-500, 10], [5000, 10]], EXT, CELL);
  for (let i = 0; i < segs.length; i += 4) {
    assert.ok(Math.hypot(segs[i + 2] - segs[i], segs[i + 3] - segs[i + 1]) <= CELL + 1e-9);
    for (const v of segs.slice(i, i + 4)) assert.ok(v >= 0 && v <= EXT);
  }
  assert.equal(segs.length / 4, EXT / CELL);
});

test("fills keep their area and holes, cut into single cells, wound one way", () => {
  // A concave outer ring (an L) with a square hole, partly outside the tile (the buffer zone).
  const outer = [[-200, -200], [3000, -200], [3000, 1500], [1200, 1500], [1200, 3300], [-200, 3300], [-200, -200]];
  const hole = [[300, 300], [300, 900], [900, 900], [900, 300], [300, 300]];
  const s = fillStats([outer, hole]);
  const want = 3000 * 1500 + 1200 * 1800 - 600 * 600;     // clipped to [0, 4096]²
  assert.ok(Math.abs(s.kept - want) / want < 1e-6, `${s.kept} vs ${want}`);
  assert.ok(Math.abs(s.sum - want) / want < 1e-6);
  assert.ok(s.minArea > 0, "every triangle has positive area in y-down coordinates");
  assert.ok(s.oneCell);
  assert.ok(s.maxEdge <= CELL * Math.SQRT2 + 1e-6);
  // Nothing inside the hole.
  for (let t = 0; t < s.index.length; t += 3) {
    const cx = (s.verts[2 * s.index[t]] + s.verts[2 * s.index[t + 1]] + s.verts[2 * s.index[t + 2]]) / 3;
    const cy = (s.verts[2 * s.index[t] + 1] + s.verts[2 * s.index[t + 1] + 1] + s.verts[2 * s.index[t + 2] + 1]) / 3;
    assert.ok(!(cx > 300 && cx < 900 && cy > 300 && cy < 900));
  }
});

test("a polygon covering the whole tile becomes plain cells", () => {
  const s = fillStats([[[-100, -100], [5000, -100], [5000, 5000], [-100, 5000], [-100, -100]]]);
  assert.equal(s.kept, EXT * EXT);
  assert.equal(s.index.length / 3, 2 * (EXT / CELL) ** 2);
});

test("neighbouring cells share bit-identical vertices on their common edge", () => {
  // A diagonal edge crosses many cell lines; every vertex on a cell line must appear in
  // both cells it borders (else the warp could open a crack).
  const s = fillStats([[[100, 100], [4000, 900], [3900, 4000], [100, 100]]]);
  const onLine = new Map();
  for (let i = 0; i < s.verts.length; i += 2) {
    const x = s.verts[i], y = s.verts[i + 1];
    if (x % CELL === 0 && x > 0 && x < EXT && y % CELL !== 0) onLine.set(`${x},${y}`, (onLine.get(`${x},${y}`) || 0) + 1);
  }
  assert.ok(onLine.size > 0);
  for (const [k, n] of onLine) assert.ok(n >= 2, `vertex ${k} is used by one cell only`);
});

test("a fold flips exactly the triangles where the jacobian is negative", () => {
  const origin = { lon: -0.1571, lat: 51.5226 };
  const field = fieldFromFunction({ origin, cell: 25, halfSize: 3000 }, (x, y) => {
    const r = Math.hypot(x, y);
    return r < 1000 ? r / 100 : r < 1500 ? 10 - (r - 1000) / 100 : 5 + (r - 1500) / 100;
  });
  const warp = createWarp({ kind: "polar", field });
  const cam = makeCamera(localFrame(origin), { centerLon: origin.lon, centerLat: origin.lat, zoom: 13, width: 800, height: 800, k: 10 });
  let checked = 0;
  for (let gx = -2400; gx < 2400; gx += 60) {
    for (let gy = -2400; gy < 2400; gy += 60) {
      const tri = [[gx, gy], [gx + 60, gy], [gx + 60, gy + 60]];
      const rs = tri.map(([x, y]) => Math.hypot(x, y));
      // Skip triangles that straddle a crease (r = 1000 or 1500) or the origin.
      if (rs.some((r) => r < 100) || [1000, 1500].some((c) => Math.min(...rs) < c + 30 && Math.max(...rs) > c - 30)) continue;
      const s = tri.map(([x, y]) => warp.screenOfLocal(x, y, cam, 1));
      const a = (s[1].x - s[0].x) * (s[2].y - s[0].y) - (s[1].y - s[0].y) * (s[2].x - s[0].x);
      const j = warp.jacobian(gx + 40, gy + 20, 5);
      assert.equal(Math.sign(a), Math.sign(j.det), `triangle at ${gx},${gy}`);
      checked++;
    }
  }
  assert.ok(checked > 1000);
});

test("the budget sheds the least important geometry first and never the main lines", () => {
  const f = [
    { cat: "motorway", count: 100, area: 0 }, { cat: "major_rail", count: 100, area: 0 },
    { cat: "park", count: 50, area: 10 }, { cat: "park", count: 50, area: 1000 },
    { cat: "water", count: 80, area: 5 }, { cat: "primary", count: 60, area: 0 }, { cat: "secondary", count: 40, area: 0 },
  ];
  const r = enforceBudget(f, 400);   // total 480: secondary (40), then the smaller park (50)
  assert.deepEqual(r.keep, [true, true, false, true, true, true, false]);
  assert.deepEqual(r.dropped, { secondary: 1, park: 1 });
  assert.equal(r.overBudget, false);
  const tight = enforceBudget(f, 150);
  assert.ok(tight.keep[0] && tight.keep[1]);
  assert.equal(tight.overBudget, true);
  assert.equal(ringArea([0, 0, 2, 0, 2, 2, 0, 2]), 4);
});
