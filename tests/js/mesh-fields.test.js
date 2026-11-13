// The lab's synthetic fields: not travel times, but they must have the shapes the renderer
// is tested against (a fast line that pulls a far place in, a cliff, plain walking).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { NO_DATA, sampleField } from "../../static/shared/mesh/field.js";
import { localFrame } from "../../static/shared/mesh/frame.js";
import * as F from "../../static/warp/lab/fields.js";

const network = JSON.parse(readFileSync(new URL("../../static/warp/lab/stations.json", import.meta.url)));
const example = JSON.parse(readFileSync(new URL("../../static/warp/example.json", import.meta.url)));
const byId = (id) => network.origins.find((o) => o.id === id);

test("on the toy network, Wembley Park from Baker Street is far quicker than walking", () => {
  const o = byId("baker-street"), wp = byId("wembley-park");
  const field = F.transitField(o, network);
  const [x, y] = localFrame(o).toLocal(wp.lon, wp.lat);
  const t = sampleField(field, x, y);
  assert.ok(t > 12 && t < 25, `${t}`);
  assert.ok(t < F.walkMinutes(Math.hypot(x, y)) / 5);
});

test("walking time rises along every ray", () => {
  const field = F.walkField(byId("croydon"), network);
  for (let k = 0; k < 16; k++) {
    const a = (k * Math.PI) / 8;
    let prev = -1;
    for (let r = 0; r < 20000; r += 250) {
      const t = sampleField(field, r * Math.cos(a), r * Math.sin(a));
      assert.ok(t > prev);
      prev = t;
    }
  }
});

test("the cliff field jumps across the river, away from the bridge", () => {
  const field = F.cliffField(byId("canary-wharf"), network);
  const south = sampleField(field, -1000, F.RIVER_Y + 60), north = sampleField(field, -1000, F.RIVER_Y - 60);
  assert.ok(north - south > 5, `${south} → ${north}`);
});

test("the contours field comes from the saved King's Cross answer", () => {
  const field = F.contoursField(example);
  assert.ok(Math.abs(sampleField(field, 1, 1)) < 0.1);
  assert.ok(sampleField(field, 300, 0) < sampleField(field, 800, 0));
  assert.equal(sampleField(field, 5000, 0), NO_DATA);
});

test("each test origin gets a warp spec", () => {
  for (const o of network.origins) {
    for (const name of [...F.POLAR_FIELDS.filter((f) => f !== "contours"), ...F.GRID_FIELDS]) {
      const spec = F.makeWarpSpec(name, o, network);
      assert.equal(spec.kind, F.GRID_FIELDS.includes(name) ? "grid" : "polar");
    }
  }
});

test("fold diagnostics count stacked sheets once in the folded share", () => {
  const walk = F.foldDiagnostics(F.makeWarpSpec("walk", byId("baker-street"), network));
  assert.equal(walk.foldedShare, 0);
  assert.ok(Math.abs(walk.medianStretch - 1) < 0.01);
  const transit = F.foldDiagnostics(F.makeWarpSpec("transit", byId("baker-street"), network));
  assert.ok(transit.foldedShare > 0 && transit.foldedShare <= 1);
  assert.ok(transit.foldedSheets >= transit.foldedShare, "sheets with overlaps ≥ their union");
});
