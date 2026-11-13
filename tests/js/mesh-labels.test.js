import { test } from "node:test";
import assert from "node:assert/strict";
import { buildTapIndex, DOT_OFFSET, placeLabels, sortCandidates } from "../../static/shared/mesh/labels.js";

const labels = sortCandidates([
  { type: "place", text: "Hampstead", rank: 11, x: 0, y: 0 },
  { type: "station", text: "Finchley Road", rank: 1, x: 2, y: 0 },
  { type: "place", text: "Camden", rank: 9, x: 400, y: 0 },
  { type: "place", text: "Folded", rank: 1, x: 800, y: 0 },
  { type: "place", text: "Stretched", rank: 2, x: 1200, y: 0 },
  { type: "station", text: "Wembley Park", rank: 1, x: 800, y: 300 },
  { type: "place", text: "Off screen", rank: 1, x: 99999, y: 0 },
]);
const folded = (x, y) => (x === 800 ? { det: -1, stretch: 2 } : x === 1200 ? { det: 1, stretch: 5 } : { det: 1, stretch: 1.5 });
const base = {
  screenOf: (x, y) => ({ x: 100 + x / 2, y: 100 + y / 2, alpha: 1 }),
  jacobianOf: folded,
  measure: (text) => 7 * text.length, minutesOf: () => 12.4, width: 1000, height: 600, cap: 40,
};

test("stations come first, then places by rank, and the order is stable", () => {
  assert.deepEqual(labels.map((l) => l.text), ["Finchley Road", "Wembley Park", "Folded", "Off screen", "Stretched", "Camden", "Hampstead"]);
});

test("a station gets a dot and its label beside it, with minutes", () => {
  const placed = placeLabels(labels, base);
  const fr = placed.find((l) => l.text === "Finchley Road");
  assert.equal(fr.label, "Finchley Road · 12 min");
  assert.ok(fr.dot);
  assert.equal(fr.box[0], fr.sx + DOT_OFFSET);
  assert.deepEqual(placeLabels(labels, base), placed);
});

test("places on a fold or stretched past 3:1 get no label; stations always can", () => {
  const texts = placeLabels(labels, base).map((l) => l.text);
  assert.ok(!texts.includes("Folded") && !texts.includes("Stretched"));
  assert.ok(texts.includes("Wembley Park"), "a station on a fold keeps its label");
  assert.ok(texts.includes("Camden"));
  assert.equal(placeLabels(labels, { ...base, cap: 1 }).length, 1);
});

test("labels clear the pin and stay inside the view", () => {
  // The pin sits on Finchley Road's dot: its label goes beside the pin, not across it.
  const pin = [101 - 10, 100 - 10, 101 + 10, 100 + 10];
  const placed = placeLabels(labels, { ...base, obstacles: [pin] });
  for (const l of placed) {
    assert.ok(!(l.box[0] < pin[2] && l.box[2] > pin[0] && l.box[1] < pin[3] && l.box[3] > pin[1]), l.text);
  }
  // Near the right edge a station's label flips to the left; a place that can't fit is dropped.
  const edge = placeLabels([{ type: "station", text: "Abbey Wood", rank: 1, x: 0, y: 0 }, { type: "place", text: "Thamesmead", rank: 1, x: 0, y: 200 }],
    { ...base, screenOf: (x, y) => ({ x: 990, y: 100 + y, alpha: 1 }) });
  assert.equal(edge.length, 1);
  assert.ok(edge[0].box[2] <= 990 - DOT_OFFSET && edge[0].box[0] >= 0);
});

test("a tap lists every place under the finger, placed or not", () => {
  const tap = buildTapIndex(labels, base.screenOf);
  assert.deepEqual(tap.query(100, 100).map((h) => h.text), ["Hampstead", "Finchley Road"]);
  assert.deepEqual(tap.query(300, 100).map((h) => h.text), ["Camden"]);
  assert.deepEqual(tap.query(150, 150), []);
});

test("among stations of one rank, the one reached sooner goes first", () => {
  const two = [
    { type: "station", text: "Dollis Hill", rank: 1, x: 10, y: 0 },
    { type: "station", text: "Wembley Park", rank: 1, x: 20, y: 0 },
  ];
  const minutes = (x) => (x === 20 ? 20 : 22);
  assert.deepEqual(sortCandidates(two, minutes).map((l) => l.text), ["Wembley Park", "Dollis Hill"]);
  assert.deepEqual(sortCandidates(two).map((l) => l.text), ["Dollis Hill", "Wembley Park"]);
});
