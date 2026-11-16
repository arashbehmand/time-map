// The full London time map (#6, L-02): the mesh warp, the page's land carrying, the borough style.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createWarp, jacobiansFor, meshLocator, targetsFor, triangleJacobian } from "../../static/shared/mesh/warp.js";
import { PASSES, STYLE_ID, STYLES } from "../../static/shared/mesh/schema-streets-v8.js";
import {
  atDistance, blend, buildLand, decodeLand, distanceAt, ease, keyframes, moveLand, nodesOf, pathLengths, place, placeLine,
  createMotion, createPlacer, lineChains, loadSegment, segmentAt, segmentTriangles, smoothLine, triangleMaps, widthFactors, WIDTH_MIN,
} from "../../static/whole-city/carry.js";

const ORIGIN = { lon: -0.1276, lat: 51.5073 };
// Two triangles sharing the edge (1000, 0)–(0, 1000); y points south, as in the local frame.
const real = [0, 0, 1000, 0, 0, 1000, 1000, 1000];
const tri = [0, 1, 2, 1, 3, 2];
// A layout that bends the square: each triangle gets its own affine map.
const nodes = [0, 0, 1500, 100, -200, 800, 1300, 1400];

test("the mesh warp is exact at the nodes, linear in a triangle and continuous across an edge", () => {
  const w = createWarp({ kind: "mesh", mesh: { real, tri, nodes }, origin: ORIGIN });
  for (let i = 0; i < 4; i++) assert.deepEqual(w.target(real[2 * i], real[2 * i + 1]), [nodes[2 * i], nodes[2 * i + 1]]);
  const mid = w.target(250, 250);
  assert.ok(Math.abs(mid[0] - (0.5 * 0 + 0.25 * 1500 + 0.25 * -200)) < 1e-9);
  assert.ok(Math.abs(mid[1] - (0.5 * 0 + 0.25 * 100 + 0.25 * 800)) < 1e-9);
  for (const t of [0.1, 0.5, 0.9]) {          // either side of the shared edge, the same place
    const x = 1000 * (1 - t), y = 1000 * t, e = 1e-7;
    const a = w.target(x - e, y - e), b = w.target(x + e, y + e);
    assert.ok(Math.hypot(a[0] - b[0], a[1] - b[1]) < 1e-3);
  }
  assert.equal(w.target(-1, 500), null);
  assert.equal(w.target(1200, 500), null);
  assert.deepEqual(w.placement(-1, 500), [0, 0, 0]);
});

test("its local scale is the triangle's own affine map", () => {
  const w = createWarp({ kind: "mesh", mesh: { real, tri, nodes }, origin: ORIGIN });
  const j = w.targetJacobian(200, 300);
  assert.deepEqual(j, triangleJacobian(real, nodes, tri, 0));
  // ∂tx/∂x is how far the target moves east per metre east: node 1 is 1500 east of node 0 at 1000 m
  assert.ok(Math.abs(j[0] - 1.5) < 1e-12 && Math.abs(j[1] - 0.1) < 1e-12);
  assert.ok(Math.abs(j[2] + 0.2) < 1e-12 && Math.abs(j[3] - 0.8) < 1e-12);
  assert.equal(w.targetJacobian(5000, 5000), null);
  const out = targetsFor(w, new Float32Array([250, 250, 5000, 5000]));
  assert.equal(out[2], 1);
  assert.equal(out[5], 0);
  const jac = jacobiansFor(w, new Float32Array([100, 100, 300, 300]));
  assert.ok(Math.abs(jac[0] - 1.5) < 1e-6);
});

test("the locator finds a point's triangle and weights, and nothing outside", () => {
  const find = meshLocator(real, tri);
  const h = find(900, 900);
  assert.equal(h.t, 1);
  assert.ok(Math.abs(h.w0 + h.w1 + h.w2 - 1) < 1e-12);
  assert.equal(find(2000, 0), null);
});

// land.bin as transit/layout/land.py packs it: each array at a 4-byte boundary.
function packLand(parts) {
  const header = { styles: ["water", "park", "boundary", "major_rail", "minor_rail", "rail_tunnel"], arrays: [],
    file: "land.bin", weight_scale: 65535 };
  let size = 0;
  for (const [, , a] of parts) { size += (4 - (size % 4)) % 4; size += a.byteLength; }
  const buf = new ArrayBuffer(size);
  let off = 0;
  for (const [name, dtype, a] of parts) {
    off += (4 - (off % 4)) % 4;
    new Uint8Array(buf, off, a.byteLength).set(new Uint8Array(a.buffer));
    header.arrays.push({ name, dtype, offset: off, length: a.length });
    off += a.byteLength;
  }
  return { buf, header };
}

// Vertices: node 0; a point on the shared edge 1–2 (stored by the edge); an interior point of
// triangle 0 (weights 0.2, 0.2); node 3. The fill is a triangle in mesh triangle 0, the rail run
// goes from the shared-edge point into triangle 1.
const { buf, header } = packLand([
  ["fill_style", "uint8", Uint8Array.from([0])],
  ["fill_nv", "uint32", Uint32Array.from([3])],
  ["fill_nt", "uint32", Uint32Array.from([1])],
  ["fill_c", "uint16", Uint16Array.from([0, 0, 0, 1, 2, 2, 0, 1, 2])],
  ["fill_w", "uint16", Uint16Array.from([0, 0, 32767, 0, 13107, 13107])],
  ["fill_idx", "uint16", Uint16Array.from([0, 1, 2])],
  ["line_style", "uint8", Uint8Array.from([3])],
  ["line_nv", "uint32", Uint32Array.from([2])],
  ["line_c", "uint16", Uint16Array.from([1, 2, 2, 3, 3, 3])],
  ["line_w", "uint16", Uint16Array.from([32767, 0, 0, 0])],
]);

test("carry.js places stored vertices exactly as land.place does in the build", () => {
  const land = decodeLand(buf, header);
  const scene = buildLand(land, header, real, STYLE_ID);
  assert.deepEqual(Object.keys(scene.passes).sort(), ["rail", "water"]);
  assert.equal(scene.passes.water.style[0], STYLE_ID.water);
  assert.equal(scene.passes.rail.style[0], STYLE_ID.major_rail);
  // Pinned from transit/layout/land.py place() (float64), then stored as Float32 as the page does.
  moveLand(scene, nodes);
  assert.deepEqual(Array.from(scene.passes.water.targets),
    [0, 0, 1, 650.0130004882812, 449.9946594238281, 1, 260, 180, 1]);
  assert.deepEqual(Array.from(scene.passes.rail.targets), [650.0130004882812, 449.9946594238281, 1, 1300, 1400, 1]);
  const xy = place(land.fill_c, land.fill_w, 65535, nodes, new Float64Array(6));
  assert.equal(xy[2], 650.0129701686121);
  assert.equal(xy[3], 449.99465934233615);
  // On the real map the scene's own positions are the vertices themselves.
  assert.deepEqual(Array.from(scene.passes.water.pos), [0, 0, 500.00762939453125, 499.99237060546875, 200, 200]);
});

test("a point on a shared mesh edge lands in the same place from both of its triangles", () => {
  const land = decodeLand(buf, header);
  const scene = moveLand(buildLand(land, header, real, STYLE_ID), nodes);
  const fill = scene.passes.water.targets, rail = scene.passes.rail.targets;
  assert.equal(fill[3], rail[0]);
  assert.equal(fill[4], rail[1]);
  // and where the mesh warp itself puts that point, from either side of the edge
  const w = createWarp({ kind: "mesh", mesh: { real, tri, nodes }, origin: ORIGIN });
  const t = 32767 / 65535, p = [1000 * (1 - t), 1000 * t], want = w.target(p[0], p[1]);
  assert.ok(Math.abs(fill[3] - want[0]) < 1e-3 && Math.abs(fill[4] - want[1]) < 1e-3);
});

test("a carried line and a slider position's nodes", () => {
  const line = { c: [0, 0, 0, 1, 2, 2, 3, 3, 3], w: [0, 0, 32767, 0, 0, 0] };
  assert.deepEqual(Array.from(placeLine(line, 65535, real)), [0, 0, 500.00762951094833, 499.99237048905167, 1000, 1000]);
  assert.deepEqual(Array.from(nodesOf({ x: [10, 25], y: [-5, 0] })), [1, -0.5, 2.5, 0]);
});

test("the borough style and pass exist, and no existing style moved", () => {
  const before = ["water", "park", "major_rail", "minor_rail", "rail_tunnel", "motorway", "trunk", "primary", "secondary"];
  before.forEach((name, i) => assert.equal(STYLE_ID[name], i));
  assert.equal(STYLES[STYLE_ID.boundary].name, "boundary");
  const names = PASSES.map((p) => p.name);
  assert.ok(names.indexOf("boundary") > names.indexOf("park") && names.indexOf("boundary") < names.indexOf("roadCase"));
});

// Three solved positions and two in-between keyframes (one of them where the map settles).
const motionDoc = {
  frames: [{ rho: 0, x: [0, 0], y: [0, 0] }, { rho: 1, x: [100, 0], y: [0, 0] }, { rho: 10, x: [400, 0], y: [0, 0] }],
  motion: { between: [{ at: 1.5, rho: 3.16, x: [200, 0], y: [0, 0] }, { at: 0.5, rho: 0.5, x: [50, 0], y: [0, 0] }] },
};

test("keyframes come in slider order, solved positions at whole steps", () => {
  const keys = keyframes(motionDoc);
  assert.deepEqual(keys.map((k) => k.at), [0, 0.5, 1, 1.5, 2]);
  assert.deepEqual(keys.map((k) => k.main), [0, null, 1, null, 2]);
  assert.equal(keys[3].nodes[0], 20);                      // decimetres to metres
});

test("a slider value is a blend of its two neighbouring keyframes", () => {
  const keys = keyframes(motionDoc);
  assert.deepEqual(segmentAt(keys, 0), { k: 0, s: 0 });
  assert.deepEqual(segmentAt(keys, 0.25), { k: 0, s: 0.5 });
  assert.deepEqual(segmentAt(keys, 1), { k: 2, s: 0 });
  assert.deepEqual(segmentAt(keys, 2), { k: 3, s: 1 });
  assert.deepEqual(segmentAt(keys, 9), { k: 3, s: 1 });
  const { k, s } = segmentAt(keys, 1.25);
  assert.deepEqual(Array.from(blend(keys[k].nodes, keys[k + 1].nodes, s)), [15, 0, 0, 0]);
});

test("the animation's pace follows how far the map moves, and inverts", () => {
  const keys = keyframes(motionDoc);
  const cum = pathLengths(keys, [0]);
  assert.deepEqual(Array.from(cum), [0, 5, 10, 20, 40]);
  for (const v of [0, 0.3, 0.5, 1.2, 1.5, 1.9, 2]) assert.ok(Math.abs(atDistance(keys, cum, distanceAt(keys, cum, v)) - v) < 1e-12);
  assert.equal(atDistance(keys, cum, 30), 1.75);           // half way through the biggest move
  assert.equal(ease(0), 0);
  assert.equal(ease(1), 1);
  assert.equal(ease(0.5), 0.5);
});

test("a blend loads positions from one layout and targets from the next", () => {
  const land = decodeLand(buf, header);
  const scene = buildLand(land, header, real, STYLE_ID);
  const placer = createPlacer(scene, [{ nodes: real }, { nodes }]);
  loadSegment(scene, placer.placed(0), placer.placed(1));
  assert.deepEqual(Array.from(scene.passes.water.pos), [0, 0, 500.00762939453125, 499.99237060546875, 200, 200]);
  assert.deepEqual(Array.from(scene.passes.water.targets), [0, 0, 1, 650.0130004882812, 449.9946594238281, 1, 260, 180, 1]);
  assert.deepEqual(Array.from(scene.passes.rail.seg), [500.00762939453125, 499.99237060546875, 1000, 1000]);
  assert.deepEqual(Array.from(scene.passes.rail.targets), [650.0130004882812, 449.9946594238281, 1, 1300, 1400, 1]);
});

test("placed keyframes are kept a few at a time, the latest used last to go", () => {
  const land = decodeLand(buf, header);
  const scene = buildLand(land, header, real, STYLE_ID);
  const keys = [0, 1, 2, 3].map((d) => ({ nodes: real.map((x) => x + d) }));
  const placer = createPlacer(scene, keys, { keep: 2 });
  const first = placer.placed(0);
  assert.equal(placer.placed(0), first);                      // cached, not placed again
  placer.placed(1);
  placer.placed(0);                                           // 0 used again, so 1 goes next
  placer.placed(2);
  assert.ok(placer.has(0) && placer.has(2) && !placer.has(1));
  // the copied positions are the target layout's own Float32 values
  loadSegment(scene, placer.placed(2), placer.placed(3));
  const t = placer.placed(2).water, pos = scene.passes.water.pos;
  for (let i = 0; i < pos.length / 2; i++) assert.deepEqual([pos[2 * i], pos[2 * i + 1]], [t[3 * i], t[3 * i + 1]]);
  assert.equal(scene.passes.water.targets, placer.placed(3).water);
});

test("each line segment's triangle is the one holding the nodes of both its ends", () => {
  const c = Uint16Array.from([
    1, 2, 2, 3, 3, 3,      // shared-edge point to node 3: triangle 1
    0, 0, 0, 1, 2, 2,      // node 0 to the shared edge: triangle 0
    1, 1, 1, 2, 2, 2,      // along the shared edge: a triangle on it
    0, 0, 0, 3, 3, 3,      // no triangle holds nodes 0 and 3
  ]);
  assert.deepEqual(Array.from(segmentTriangles(c, tri)), [1, 0, 0, -1]);
});

test("a triangle's map is its own affine map, the mesh warp's Jacobian", () => {
  const maps = triangleMaps(tri, real, nodes);
  for (const t of [0, 1]) {
    const j = triangleJacobian(real, nodes, tri, t);      // [∂tx/∂x, ∂ty/∂x, ∂tx/∂y, ∂ty/∂y]
    const got = [maps[4 * t], maps[4 * t + 2], maps[4 * t + 1], maps[4 * t + 3]];
    got.forEach((v, k) => assert.ok(Math.abs(v - j[k]) < 1e-12));
  }
  assert.deepEqual(Array.from(triangleMaps(tri, real, real)), [1, 0, 0, 1, 1, 0, 0, 1]);
});

test("a line gets thinner as the map squeezes the ground across it, never wider", () => {
  const seg = [100, 100, 300, 100, 100, 100, 100, 300];   // along x, along y, both in triangle 0
  const segTri = [0, 0];
  const w = (layout) => Array.from(widthFactors(seg, segTri, triangleMaps(tri, real, layout)));
  assert.deepEqual(w(real), [1, 1]);
  assert.deepEqual(w(real.map((v) => v * 0.5)), [0.5, 0.5]);                      // shrunk by half
  assert.deepEqual(w(real.map((v) => v * 0.1)), [WIDTH_MIN, WIDTH_MIN].map(Math.fround));   // never below WIDTH_MIN
  assert.deepEqual(w(real.map((v) => v * 2)), [1, 1]);                            // stretched: unchanged
  const squeezedNS = real.map((v, i) => (i % 2 ? v * 0.5 : v));                     // north–south only
  assert.deepEqual(w(squeezedNS), [0.5, 1]);       // the east–west line is squeezed across, the other along
});

test("smoothing moves a line's bend, not its real shape, and keeps its ends and stations", () => {
  const n = 41, realXY = new Float64Array(2 * n);
  for (let i = 0; i < n; i++) realXY[2 * i] = 100 * i;                 // a straight line, 0 to 4,000 m
  const kinked = Float64Array.from(realXY);
  for (let i = 0; i < n; i++) kinked[2 * i + 1] = Math.max(0, 0.5 * (100 * i - 2000));   // bent at 2,000 m
  const none = new Uint8Array(n);
  assert.deepEqual(Array.from(smoothLine(realXY, realXY, none, 300)), Array.from(realXY));   // nothing moved
  const sm = smoothLine(realXY, kinked, none, 300);
  const bend = (xy, i) => Math.abs(xy[2 * i + 3] - 2 * xy[2 * i + 1] + xy[2 * i - 1]);
  let worst = 0;
  for (let i = 1; i < n - 1; i++) worst = Math.max(worst, bend(sm, i));
  assert.ok(bend(kinked, 20) === 50 && worst < 10);                    // the corner is rounded off
  for (const i of [0, 5, n - 1]) assert.equal(sm[2 * i + 1], kinked[2 * i + 1]);   // ends exact; far from the bend, exact
  const pinned = new Uint8Array(n); pinned[20] = 1;                     // a station at the bend
  assert.equal(smoothLine(realXY, kinked, pinned, 300)[41], kinked[41]);
  // linear: the smoothed blend of two layouts is the blend of the smoothed ones
  const other = Float64Array.from(kinked, (v, k) => (k % 2 ? -v : v + 30));
  const mix = Float64Array.from(kinked, (v, k) => 0.3 * v + 0.7 * other[k]);
  const a = smoothLine(realXY, kinked, none, 300), b = smoothLine(realXY, other, none, 300), m = smoothLine(realXY, mix, none, 300);
  m.forEach((v, k) => assert.ok(Math.abs(v - (0.3 * a[k] + 0.7 * b[k])) < 1e-9));
});

test("the placer gives each line segment its width factor on every keyframe", () => {
  const land = decodeLand(buf, header);
  const scene = buildLand(land, header, real, STYLE_ID);
  const keys = [{ nodes: real }, { nodes: real.map((v) => v * 0.5) }];
  const placer = createPlacer(scene, keys, { triangles: tri });
  assert.deepEqual(Array.from(placer.placed(0).wf.rail), [1]);
  assert.deepEqual(Array.from(placer.placed(1).wf.rail), [0.5]);
  loadSegment(scene, placer.placed(0), placer.placed(1));
  assert.deepEqual(Array.from(scene.passes.rail.wf), [1, 0.5]);         // at u = 0 and at u = 1
  assert.equal(scene.passes.water.wf, undefined);                       // fills have none
});

// Line pieces as land.py stores them: each segment's two ends (three nodes, two weights each).
function pieces(list) {
  const c = [], w = [], runs = [];
  for (const run of list) {
    runs.push(run.length - 1);
    for (let i = 0; i + 1 < run.length; i++) { c.push(...run[i][0], ...run[i + 1][0]); w.push(...run[i][1], ...run[i + 1][1]); }
  }
  return { c: Uint16Array.from(c), w: Uint16Array.from(w), runs: Int32Array.from(runs) };
}
const N0 = [[0, 0, 0], [0, 0]], N3 = [[3, 3, 3], [0, 0]], E = [[1, 2, 2], [100, 0]];

test("pieces cut at a mesh edge are joined back into one line; a junction or a dead end stops it", () => {
  const one = lineChains(pieces([[N0, E], [E, N3]]));
  assert.equal(one.length, 1);
  assert.deepEqual(Array.from(one[0].read), [0, 3, 9]);           // node 0, the edge point, node 3
  assert.deepEqual(Array.from(one[0].pinned), [1, 0, 1]);          // the nodes stay put
  assert.deepEqual(Array.from(one[0].slot).sort((a, b) => a - b), [0, 3, 6, 9]);   // every slot, once
  const reversed = lineChains(pieces([[N0, E], [N3, E]]));          // the second piece stored backwards
  assert.equal(reversed.length, 1);
  assert.equal(reversed[0].read.length, 3);
  const junction = lineChains(pieces([[N0, E], [E, N3], [E, [[2, 2, 2], [0, 0]]]]));
  assert.equal(junction.length, 3);                                 // three pieces meet: no joining
  const P = [[4, 5, 5], [1, 0]], Q = [[4, 6, 6], [2, 0]];
  const loop = lineChains(pieces([[P, Q], [Q, P]]));
  assert.equal(loop.length, 1);
  assert.equal(loop[0].read.length, 3);                             // opened at one point: P, Q, P
});

// ── the page's motion ─────────────────────────────────────────────────
// A fake clock and frame scheduler: advance(ms) runs the callbacks pending then.
function fakeLoop() {
  let t = 0, next = 1;
  const q = new Map();
  return {
    now: () => t,
    schedule: (f) => { const h = next++; q.set(h, f); return h; },
    cancel: (h) => q.delete(h),
    pending: () => q.size,
    advance(ms) { t += ms; const fs = [...q.values()]; q.clear(); fs.forEach((f) => f(t)); },
  };
}
// Five solved positions, each node moving 100 m a step, and a log of what the page is told.
function motionOf(opts = {}) {
  const doc = { frames: [0, 1, 2, 3, 4].map((i) => ({ rho: i, x: [1000 * i, 0], y: [0, 0] })) };
  const keys = keyframes(doc), loop = fakeLoop(), log = { moves: [], settles: 0 };
  const m = createMotion({ keys, cum: pathLengths(keys, [0]), last: 4, now: loop.now, schedule: loop.schedule,
    cancel: loop.cancel, onMove: (v) => log.moves.push(v), onSettle: () => log.settles++, playMs: 4000, stepMs: 400, ...opts });
  return { m, loop, log };
}

test("one move at a time: turning round again and again keeps a single frame callback", () => {
  const { m, loop } = motionOf();
  m.toggle();                                     // bend
  loop.advance(500);
  for (let i = 0; i < 5; i++) { m.toggle(); assert.equal(loop.pending(), 1); }
  for (let i = 0; i < 400 && m.moving; i++) loop.advance(16);
  assert.equal(m.moving, false);
});

test("a key during a play steps from where the map is, the way it says", () => {
  const { m, loop } = motionOf();
  m.toggle();                                     // play to the end (4)
  for (let i = 0; i < 140; i++) loop.advance(16); // part way (2.24 s of 4 s: at about 2.45)
  const here = m.v;
  assert.ok(here > 1 && here < 3);
  m.key("ArrowLeft");
  assert.equal(m.kind, "key");
  for (let i = 0; i < 100 && m.moving; i++) loop.advance(16);
  assert.equal(m.v, Math.ceil(here) - 1);          // back, to the solved position before
  assert.ok(m.v < here);
});

test("keys during a glide step on from its target; a held key waits for the glide", () => {
  const { m, loop } = motionOf();
  m.key("ArrowRight");
  m.key("ArrowRight");                            // a second press: on to 2
  m.key("ArrowRight", true);                      // a key held down: ignored while it glides
  assert.equal(m.back, false);                    // gliding forward is not "back to the real map"
  for (let i = 0; i < 200 && m.moving; i++) loop.advance(16);
  assert.equal(m.v, 2);
  m.key("End");
  assert.equal(m.back, true);                     // now the button would take it back
});

test("a glide takes its share of the path, at least the shortest glide's time", () => {
  const { m, loop } = motionOf();
  m.key("ArrowRight");                            // a quarter of the path: 1,000 ms of 4,000
  let t = 0;
  while (m.moving) { loop.advance(10); t += 10; }
  assert.ok(t >= 1000 && t <= 1010);
  const short = motionOf({ playMs: 400 });
  short.m.key("ArrowRight");                      // its share is 100 ms; the floor is 400
  t = 0;
  while (short.m.moving) { short.loop.advance(10); t += 10; }
  assert.ok(t >= 400 && t <= 410);
});

test("dragging stops a play; stop and reduced motion settle at once", () => {
  const { m, loop, log } = motionOf();
  m.toggle();
  loop.advance(100);
  m.drag(3.5);
  assert.equal(m.moving, false);
  loop.advance(16);
  assert.equal(m.v, 3.5);
  m.toggle();
  m.stop();
  assert.equal(loop.pending(), 0);
  const settled = log.settles;
  assert.ok(settled >= 1);
  const rm = motionOf({ reduceMotion: () => true });
  rm.m.toggle();
  assert.equal(rm.m.v, 4);
  assert.equal(rm.log.settles, 1);
  assert.equal(rm.loop.pending(), 0);
});

test("a frame stamped before the move began leaves the map where it was", () => {
  const { m, loop, log } = motionOf();
  m.key("ArrowRight");
  const cb = loop.schedule;                       // the pending callback runs at a time before the start
  void cb;
  loop.advance(-5);
  assert.equal(log.moves.at(-1), 0);
});
