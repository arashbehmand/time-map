import { test } from "node:test";
import assert from "node:assert/strict";
import { fieldFromFunction } from "../../static/shared/mesh/field.js";
import { localFrame, tilePlacement } from "../../static/shared/mesh/frame.js";
import { packScene, tessellateTile, vectorTileFeatures } from "../../static/shared/mesh/pipeline.js";
import { classify, pickName, STYLE_ID } from "../../static/shared/mesh/schema-streets-v8.js";
import { createWarp } from "../../static/shared/mesh/warp.js";
import { encodeTile } from "./mvt-fixture.js";

const TILE = { z: 12, x: 2046, y: 1361 };     // central London
const square = (x0, y0, x1, y1) => [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]];

const bytes = encodeTile({
  water: [{ type: 3, props: { class: "river" }, geometry: [square(0, 0, 1024, 1024), square(256, 768, 512, 256)] }],
  landuse: [
    { type: 3, props: { class: "park" }, geometry: [square(2048, 2048, 2560, 2560)] },
    { type: 3, props: { class: "residential" }, geometry: [square(0, 2048, 512, 2560)] },
  ],
  road: [
    { type: 2, props: { class: "major_rail", structure: "tunnel" }, geometry: [[[0, 3000], [4096, 3000]]] },
    { type: 2, props: { class: "motorway" }, geometry: [[[-100, 100], [4200, 4000]]] },
    { type: 2, props: { class: "service" }, geometry: [[[0, 10], [100, 10]]] },
    { type: 2, props: { class: "secondary" }, geometry: [[[0, 20], [100, 20]]] },
  ],
  transit_stop_label: [
    { type: 1, props: { name: "Baker Street", stop_type: "station", mode: "metro_rail", filterrank: 1 }, geometry: [[[1500, 1500]]] },
    { type: 1, props: { name: "Bus stop", stop_type: "stop", mode: "bus" }, geometry: [[[1600, 1500]]] },
  ],
  place_label: [{ type: 1, props: { name: "Marylebone", class: "settlement_subdivision", symbolrank: 11 }, geometry: [[[4200, 10]]] }],
});

test("the schema adapter picks layers, classes and labels", () => {
  assert.deepEqual(classify("water", {}, 3, 12), { kind: "fill", style: STYLE_ID.water, cat: "water" });
  assert.equal(classify("landuse", { class: "residential" }, 3, 12), null);
  assert.equal(classify("road", { class: "major_rail", structure: "tunnel" }, 2, 12).style, STYLE_ID.rail_tunnel);
  assert.equal(classify("road", { class: "secondary" }, 2, 11), null);            // decoded from z12
  assert.equal(classify("road", { class: "secondary" }, 2, 12).style, STYLE_ID.secondary);
  assert.equal(classify("transit_stop_label", { name: "X", stop_type: "stop", mode: "bus" }, 1, 12), null);
  assert.equal(classify("building", {}, 3, 12), null);
  assert.equal(pickName({ name_en: "Paddington" }), "Paddington");
});

test("a real tile goes through decode → classify → tessellate", () => {
  const mesh = tessellateTile(vectorTileFeatures(bytes), { ...TILE, spacingDiv: 64 });
  assert.equal(mesh.extent, 4096);
  assert.deepEqual(mesh.fills.map((f) => f.cat).sort(), ["park", "water"]);
  const water = mesh.fills.find((f) => f.cat === "water");
  assert.ok(Math.abs(water.area - (1024 * 1024 - 256 * 512)) < 1e-6, "the hole is kept");
  assert.deepEqual(mesh.lines.map((l) => l.cat).sort(), ["major_rail", "motorway", "secondary"]);
  for (const l of mesh.lines) {
    for (let i = 0; i < l.segs.length; i += 4) {
      assert.ok(Math.hypot(l.segs[i + 2] - l.segs[i], l.segs[i + 3] - l.segs[i + 1]) <= 64 + 1e-6);
      for (const v of l.segs.subarray(i, i + 4)) assert.ok(v >= 0 && v <= 4096, "clipped to the tile");
    }
  }
  // The place label sits in the buffer zone (x = 4200): the neighbouring tile owns it.
  assert.deepEqual(mesh.labels.map((l) => l.text), ["Baker Street"]);
});

test("packing puts each tile in local metres and fills the CPU targets", () => {
  const mesh = tessellateTile(vectorTileFeatures(bytes), { ...TILE, spacingDiv: 64 });
  const origin = { lon: -0.1571, lat: 51.5226 };
  const frame = localFrame(origin);
  const scene = packScene([mesh], frame);
  const p = tilePlacement(frame, TILE.z, TILE.x, TILE.y, 4096);
  const w = scene.passes.water;
  assert.ok(Math.abs(w.pos[0] - (p.ox + mesh.fills.find((f) => f.cat === "water").verts[0] * p.scale)) < 1e-2);
  // The secondary road is in the tile's corner, more than 2 km from Baker Street: dropped.
  const secondary = mesh.lines.find((l) => l.cat === "secondary").segs.length / 4;
  assert.equal(scene.passes.rail.count + scene.passes.road.count, mesh.stats.segments - secondary);
  assert.equal(scene.stats.shaded, scene.stats.fillVerts + 6 * scene.passes.rail.count + 12 * scene.passes.road.count);
  assert.deepEqual(scene.origin, origin);
  assert.equal(scene.labels[0].text, "Baker Street");
  assert.ok(scene.stats.bytes > 0);

  const field = fieldFromFunction({ origin, cell: 100, halfSize: 25000 }, (x, y) => Math.hypot(x, y) / 80);
  const warp = createWarp({ kind: "polar", field, maxMinutes: 300 });   // the whole tile in reach
  const c = packScene([mesh], frame, { warp });
  assert.equal(c.passes.water.targets.length, (c.passes.water.pos.length / 2) * 3);
  assert.equal(c.passes.road.targets.length, c.passes.road.count * 6);
  const t = warp.target(c.passes.road.seg[2], c.passes.road.seg[3]);
  assert.ok(Math.abs(c.passes.road.targets[3] - t[0]) < 1e-4 && c.passes.road.targets[5] === 1);

  const tight = packScene([mesh], frame, { budget: 10 });
  assert.equal(tight.stats.overBudget, true);
  assert.ok(tight.stats.dropped.park === 1 && tight.stats.dropped.water === 1);
});
