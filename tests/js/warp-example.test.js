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
// Measured after the fix: one single-contour spike (49.7 minutes over a way out at 34) at the
// 45 ring, which the uncertain-patch rule leaves alone (docs/warp-time-polar.md). Held here.
const FADE_AT_45 = 1;

test("nothing walled in by ground well inside the ring is sent onto it", () => {
  const g = geometryField(decodeField(example));
  for (const ring of [15, 30, 60]) assert.equal(enclosedFade(g, ring).count, 0, `ring ${ring}`);
  assert.ok(enclosedFade(g, 45).count <= FADE_AT_45);
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
