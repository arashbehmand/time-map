// Whole city's travel modes (#23): which combination a set of toggles means, how the map
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

import { comboName, keepRecent, modeAvailability, runSteps, switchSteps } from "../../static/whole-city/carry.js";

// Run a switch's steps against a fake map: each glide moves along the current combination, each
// cross blends between two combinations at a solved position. Returns every blend drawn.
function drive(steps, start) {
  let combo = start.combo, v = start.v;
  const drawn = [];
  let done = false;
  runSteps(steps, {
    glide(x, next) { if (x !== v) drawn.push(`${combo}: ${v} → ${x}`); v = x; next(); },
    cross({ from, to, at }, next) { drawn.push(`${from} ⇄ ${to} at ${at}`); next(); },
    enter(id, x) { combo = id; v = x; },
    done() { done = true; },
  });
  return { combo, v, drawn, done };
}

test("each plan's steps draw only blends the build checked, and end where they should", () => {
  const cross = { "train|car-train": [null, { ok: true }, { ok: true }, { ok: false }] };
  const go = (from, to, v, remembered = 0) => {
    const i = Math.round(v);
    return drive(switchSteps(switchPlan(cross, from, to, i), from, to, i, v, remembered), { combo: from, v });
  };
  let r = go("train", "car-train", 2.4);                     // straight: glide to 2, blend across
  assert.deepEqual(r.drawn, ["train: 2.4 → 2", "train ⇄ car-train at 2"]);
  assert.equal(r.combo, "car-train"); assert.equal(r.v, 2); assert.ok(r.done);
  r = go("car-train", "train", 3);                           // not checked at 3: down and up again
  assert.deepEqual(r.drawn, ["car-train: 3 → 0", "train: 0 → 3"]);
  assert.equal(r.combo, "train"); assert.equal(r.v, 3);
  r = go("train", "none", 2.6);                              // the last mode off: down to the real map
  assert.deepEqual(r.drawn, ["train: 2.6 → 0"]); assert.equal(r.combo, "none");
  r = go("none", "car", 0, 2.6);                             // the first on again: up to where it was
  assert.deepEqual(r.drawn, ["car: 0 → 2.6"]); assert.equal(r.combo, "car");
  r = go("train", "car-train", 0.4);                         // near the real map: glide to it, then swap
  assert.deepEqual(r.drawn, ["train: 0.4 → 0"]); assert.equal(r.combo, "car-train"); assert.equal(r.v, 0);
  r = go("train", "train", 2);                               // nothing to do
  assert.deepEqual(r.drawn, []); assert.equal(r.combo, "train"); assert.ok(r.done);
});

test("steps wait for each glide and blend to end before the next", () => {
  const log = [];
  let pending = null;
  runSteps([{ glide: 1 }, { cross: { from: "a", to: "b", at: 1 } }, { enter: "b", at: 1 }], {
    glide(x, next) { log.push("glide"); pending = next; },
    cross(c, next) { log.push("cross"); pending = next; },
    enter() { log.push("enter"); },
    done() { log.push("done"); },
  });
  assert.deepEqual(log, ["glide"]);
  pending(); assert.deepEqual(log, ["glide", "cross"]);
  pending(); assert.deepEqual(log, ["glide", "cross", "enter", "done"]);
});

test("a mode is live only where the build has it; Train and tube always", () => {
  const none = modeAvailability(null);
  assert.equal(none.walk.on, false); assert.equal(none.bus.on, false);
  assert.equal(none.train.on, true); assert.equal(none.car.on, false);
  assert.ok(none.walk.why && none.bus.why && none.car.why);
  const carOnly = modeAvailability({ modes: [{ id: "walk", available: false, why: "No walking here." },
    { id: "bus", available: false, why: "Later." }, { id: "train", available: true }, { id: "car", available: true }] });
  assert.equal(carOnly.walk.on, false); assert.equal(carOnly.walk.why, "No walking here."); assert.equal(carOnly.car.on, true);
  const all = modeAvailability({ modes: ["walk", "bus", "train", "car"].map((id) => ({ id, available: true, why: "" })) });
  for (const m of ["walk", "bus", "train", "car"]) { assert.equal(all[m].on, true); assert.equal(all[m].why, ""); }
});

test("every set of the four modes is one combination, its id the build's (sorted)", () => {
  const modes = ["walk", "bus", "train", "car"];
  const ids = new Set();
  for (let mask = 0; mask < 16; mask++) {
    const on = new Set(modes.filter((_, k) => mask & (1 << k)));
    const id = comboOf(on);
    ids.add(id);
    assert.equal(id, on.size ? [...on].sort().join("-") : "none");
  }
  assert.equal(ids.size, 16);
  assert.equal(comboOf(new Set(["walk", "car", "bus", "train"])), "bus-car-train-walk");
  assert.equal(comboName("car-train"), "Car + train and tube");
  assert.equal(comboName("bus-walk"), "Bus + walking");
  assert.equal(comboName("none"), "No way of travelling");
});

test("walking and bus say why they move nothing, judged by their taut springs, and name where one acts", () => {
  for (const p of [undefined, { n: 0, taut: 0, of: 2600, share: 0 }]) {
    for (const i of [0, 12]) {
      assert.equal(pullWords("walk", p, i, 0, 17.2), "Walking moves nothing here: at this map's scale (about 17 km/h " +
        "door to door) walking is too slow to pull two places together.");
      assert.equal(pullWords("bus", p, i, 0, 17.2), "Bus moves nothing here: door to door, with the walk to the stop " +
        "and the wait, no bus between neighbouring places beats this map's scale (about 17 km/h).");
    }
  }
  // A spring at its rest length acts on the map though it is under 1% past it: it is named.
  assert.equal(pullWords("walk", { n: 0, taut: 1, of: 2611, share: 0, top: ["Bank – Bank"] }, 24, 0, 17.2),
    "Walking: 1 of 2611 walking springs taut, 0 more than 1% past its time, hardest Bank – Bank. Elsewhere it is " +
    "slower than the map.");
  assert.equal(pullWords("bus", { n: 1, taut: 2, of: 1384, share: 0.001, top: ["TUBE"],
    top_links: ["London Victoria – Marble Arch", "Pimlico – Victoria"] }, 20),
    "Bus: 2 of 1384 bus springs taut, 1 more than 1% past their time, hardest London Victoria – Marble Arch " +
    "(route TUBE). Elsewhere it is slower than the map.");
  assert.match(pullWords("bus", { n: 40, taut: 41, of: 900, share: 0.044, top: ["25", "207"] }, 20), /\(routes 25, 207\)/);
  // An older modes.json with no taut count: its pulling count stands in.
  assert.match(pullWords("bus", { n: 3, of: 900, share: 0.003, top: ["N8"] }, 20), /^Bus: 3 of 900 bus springs taut/);
});

test("only the few most recently drawn combinations keep their placed land", () => {
  const list = [];
  assert.deepEqual(keepRecent(list, "a", 3), []);
  keepRecent(list, "b", 3); keepRecent(list, "c", 3);
  assert.deepEqual(keepRecent(list, "a", 3), []);            // a used again: now the most recent
  assert.deepEqual(keepRecent(list, "d", 3), ["b"]);         // the least recently used goes
  assert.deepEqual(list, ["c", "a", "d"]);
});

import { THIN_MARK, thinPositions } from "../../static/whole-city/carry.js";

test("positions with a very thin triangle are marked; the real map never is", () => {
  const frames = [12, 3, 18, 73.1, 140.1].map((r) => ({ metrics: { stretch_max: r } }));
  frames[0].metrics.stretch_max = 999;                       // the real map: never marked
  assert.deepEqual(thinPositions(frames), [{ i: 3, ratio: 73.1 }, { i: 4, ratio: 140.1 }]);
  assert.equal(THIN_MARK, 50);
  assert.deepEqual(thinPositions([{ metrics: { stretch_max: 1 } }, { metrics: {} }]), []);
});

import { madeWith, sha256Hex } from "../../static/whole-city/carry.js";

test("travel-mode files are used only with the map and land they were made with", async () => {
  const enc = (s) => new TextEncoder().encode(s).buffer;
  const map = await sha256Hex(enc('{"build":"b"}\n'));
  const land = await sha256Hex(new ArrayBuffer(0));
  assert.equal(land, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");   // sha256 of nothing
  const doc = { build: "b", table_sha256: "t" };
  const mode = { build: "b", table_sha256: "t", timemap_sha256: map, land_sha256: land };
  assert.ok(madeWith(mode, doc, map, land));
  assert.ok(!madeWith({ ...mode, timemap_sha256: "0" }, doc, map, land));     // an earlier build of the map
  assert.ok(!madeWith({ ...mode, land_sha256: "0" }, doc, map, land));
  assert.ok(!madeWith(mode, doc, null, null));                                // no Web Crypto: no car
  assert.ok(!madeWith(null, doc, map, land));
});
