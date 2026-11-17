// Whole city's travel modes (#23, L-03): which combination a set of toggles means, how the map
// moves from one to another, and the readout's words.
import { test } from "node:test";
import assert from "node:assert/strict";
import { PASSES, STYLE_ID } from "../../static/shared/mesh/schema-streets-v8.js";
import { PASS_OF, comboOf, pullWords, switchPlan } from "../../static/whole-city/carry.js";

test("a set of modes is one combination, and none is the real map", () => {
  assert.equal(comboOf(new Set(["train"])), "train");
  assert.equal(comboOf(new Set(["car"])), "car");
  assert.equal(comboOf(new Set(["train", "car"])), "car-train");
  assert.equal(comboOf(new Set(["car", "train"])), "car-train");
  assert.equal(comboOf(new Set()), "none");
});

test("a switch goes straight across only where the build checked that blend", () => {
  const cross = { "train|car-train": [null, { ok: true }, { ok: false }], "car|car-train": [null, { ok: true }, { ok: true }] };
  assert.equal(switchPlan(cross, "train", "car-train", 1), "straight");
  assert.equal(switchPlan(cross, "car-train", "train", 1), "straight");       // either order
  assert.equal(switchPlan(cross, "train", "car-train", 2), "via-real");
  assert.equal(switchPlan(cross, "car", "car-train", 2), "straight");
  assert.equal(switchPlan(cross, "train", "car-train", 0), "swap");           // the real map is every combination's
  assert.equal(switchPlan(cross, "train", "none", 2), "down");
  assert.equal(switchPlan(cross, "none", "car", 2), "up");
  assert.equal(switchPlan(cross, "train", "train", 2), "same");
  assert.equal(switchPlan(cross, "train", "car", 1), "via-real");             // no checked blend between these two
  assert.equal(switchPlan(null, "train", "car-train", 1), "via-real");
});

test("the readout says how many springs pull and where, or that a mode moves nothing", () => {
  assert.equal(pullWords("train", { n: 52, of: 830, share: 0.0627, top: ["Wembley Park – Finchley Road"] }, 10),
    "Train and tube: 52 of 830 springs pulling (6%), hardest Wembley Park – Finchley Road.");
  assert.equal(pullWords("car", { n: 120, of: 945, share: 0.127, top: ["A40", "A406"] }, 10, 5),
    "Car: 120 of 945 road springs pulling (13%), mostly A40, A406. Car times include 5 minutes once a trip for " +
    "parking and getting to the car.");
  assert.match(pullWords("car", { n: 0, of: 945, share: 0 }, 3, 5), /^Car moves nothing at this strength/);
  assert.match(pullWords("train", { n: 0, of: 830, share: 0 }, 0), /on the real map no spring pulls/);
  assert.equal(pullWords("car", { n: 1, of: 1000, share: 0.001 }, 4), "Car: 1 of 1000 road springs pulling (1%).");
});

test("the car's roads go to the renderer's road pass, in styles it has", () => {
  for (const s of ["motorway", "trunk", "primary"]) {
    assert.equal(PASS_OF[s], "road");
    assert.ok(Number.isInteger(STYLE_ID[s]), s);
  }
  assert.ok(PASSES.some((p) => p.name === "road") && PASSES.some((p) => p.buffer === "road" && p.casing));
});
