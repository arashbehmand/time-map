// The stations-only layout (transit/layout/frame.py) writes positions in frame.js's local frame;
// tests/test_layout.py pins the same numbers on the Python side.
import { test } from "node:test";
import assert from "node:assert/strict";
import { localFrame } from "../../static/shared/mesh/frame.js";

const ORIGIN = { lon: -0.1276, lat: 51.5073 };   // transit/layout/frame.py ORIGIN
const PINNED = [
  [-0.27883, 51.56384, -10478.259699, -6297.913424],
  [-0.0928, 51.3754, 2411.184537, 14661.841866],
  [0.1, 51.65, 15769.701167, -15910.235562],
];

test("frame.js local coordinates match the numbers the layout's Python twin is pinned to", () => {
  const f = localFrame(ORIGIN);
  for (const [lon, lat, x, y] of PINNED) {
    const [gx, gy] = f.toLocal(lon, lat);
    assert.ok(Math.abs(gx - x) < 1e-5 && Math.abs(gy - y) < 1e-5, `${lon},${lat}: ${gx},${gy}`);
  }
});
