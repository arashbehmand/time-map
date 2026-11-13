import { test } from "node:test";
import assert from "node:assert/strict";
import * as F from "../../static/shared/mesh/frame.js";

test("mercator round-trips", () => {
  for (const [lon, lat] of [[-0.1571, 51.5226], [0, 0], [151.2, -33.9], [-122.4, 37.8]]) {
    const [mx, my] = F.mercator(lon, lat);
    const [lon2, lat2] = F.unmercator(mx, my);
    assert.ok(Math.abs(lon2 - lon) < 1e-9 && Math.abs(lat2 - lat) < 1e-9);
  }
});

test("the ordinary map matches a 512-px world at zoom z", () => {
  // At zoom 0 the world is 512 px wide: lon 90 is a quarter of it right of lon 0.
  const frame = F.localFrame({ lon: 0, lat: 0 });
  const cam = F.makeCamera(frame, { centerLon: 0, centerLat: 0, zoom: 0, width: 512, height: 512, k: 1 });
  const [x, y] = F.geoScreen(...frame.toLocal(90, 0), cam);
  assert.ok(Math.abs(x - 384) < 1e-9 && Math.abs(y - 256) < 1e-9);
  // At zoom 3 a point's offset from the centre is 2^3 times its zoom-0 offset.
  const london = F.localFrame({ lon: -0.1571, lat: 51.5226 });
  const c3 = F.makeCamera(london, { centerLon: -0.1571, centerLat: 51.5226, zoom: 3, width: 400, height: 300, k: 1 });
  const [mx0, my0] = F.mercator(-0.1571, 51.5226), [mx, my] = F.mercator(-0.2795, 51.5635);
  const [sx, sy] = F.geoScreen(...london.toLocal(-0.2795, 51.5635), c3);
  assert.ok(Math.abs(sx - (200 + (mx - mx0) * 512 * 8)) < 1e-6);
  assert.ok(Math.abs(sy - (150 + (my - my0) * 512 * 8)) < 1e-6);
  // The pin is the origin's own screen position.
  assert.deepEqual(c3.pin.map((v) => Math.round(v * 1e6)), [200e6, 150e6]);
});

test("a tile's placement agrees with mercator", () => {
  const frame = F.localFrame({ lon: -0.1571, lat: 51.5226 });
  const p = F.tilePlacement(frame, 12, 2046, 1361, 4096);
  const [lon, lat] = F.unmercator(2046 / 4096, 1361 / 4096);
  const [x, y] = frame.toLocal(lon, lat);
  assert.ok(Math.abs(p.ox - x) < 1e-6 && Math.abs(p.oy - y) < 1e-6);
  assert.ok(Math.abs(p.scale * 4096 - F.tileSizeMetres(frame, 12)) < 1e-9);
});
