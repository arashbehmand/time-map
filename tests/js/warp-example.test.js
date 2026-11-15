// The warp page's opening view, on the committed example (static/warp/example-field.json,
// King's Cross walking): the check that would have caught the bent map's wedge (2026-11), when
// the platforms north of the pin, interpolated to 50 minutes, were sent onto the 30 ring.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { decodeField, geometryField } from "../../static/shared/mesh/field.js";
import { enclosedFade } from "../../static/shared/mesh/diagnostics.js";
import { angularSpread, createWarp } from "../../static/shared/mesh/warp.js";

const example = JSON.parse(readFileSync(new URL("../../static/warp/example-field.json", import.meta.url)));
test("nothing walled in by ground well inside the ring is sent onto it", () => {
  const g = geometryField(decodeField(example));
  for (const ring of [15, 30, 45, 60]) assert.equal(enclosedFade(g, ring).count, 0, `ring ${ring}`);
});

// The slivers north of King's Cross (single dots in a gap of one outline, 7 to 16 minutes above
// the dots next to them) are drawn at the times around them, not spiking outward.
test("the slivers around King's Cross are drawn at the times around them", () => {
  const f = decodeField(example), g = geometryField(f);
  const node = (x, y) => Math.round((y - f.y0) / f.cell) * f.nx + Math.round((x - f.x0) / f.cell);
  for (const [x, y] of [[300, -1900], [-1200, -100], [-400, -900], [600, -800]]) {
    const k = node(x, y);
    assert.equal(f.uncertain[k], 1, `${x},${y} is not a patch`);
    const around = [k - 1, k + 1, k - f.nx, k + f.nx].map((q) => f.minutes[q]);
    assert.ok(g.minutes[k] <= Math.max(...around) + 0.05, `${x},${y} still sticks out`);
  }
});

test("the tracks north of King's Cross are drawn between the station and the streets beyond", () => {
  const f = decodeField(example), g = geometryField(f);
  for (const spread of [0.5, 0]) {                  // by reach (the default) and true compass
    const angles = spread > 0 ? angularSpread(f, 30, { spread }) : null;
    const warp = createWarp({ kind: "polar", field: g, maxMinutes: 30, angles });
    for (let y = -100; y >= -700; y -= 25) {
      const [tx, ty, flag] = warp.placement(0, y);
      assert.equal(flag, 1, `${-y} m north: not drawn where it is`);
      assert.ok(Math.hypot(tx, ty) <= 15, `${-y} m north: drawn ${Math.hypot(tx, ty).toFixed(1)} minutes out`);
    }
  }
});
