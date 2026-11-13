import { test } from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_TILE_CAP, tileCover } from "../../static/shared/mesh/cover.js";
import { localFrame } from "../../static/shared/mesh/frame.js";

const ORIGINS = [
  { lon: -0.1571, lat: 51.5226 }, { lon: -0.2795, lat: 51.5635 }, { lon: -0.0195, lat: 51.5035 },
  { lon: -0.4542, lat: 51.4713 }, { lon: -0.0928, lat: 51.3755 },
];
const BOX = [-25000, -25000, 25000, 25000];

const contains = (a, b) => b.z > a.z && (b.x >> (b.z - a.z)) === a.x && (b.y >> (b.z - a.z)) === a.y;

test("tiles never overlap, and cover the field's extent", () => {
  for (const o of ORIGINS) {
    const { tiles, capped } = tileCover(localFrame(o), BOX);
    assert.equal(capped, false);
    for (const a of tiles) for (const b of tiles) {
      if (a !== b) assert.ok(!contains(a, b) && !(a.z === b.z && a.x === b.x && a.y === b.y));
    }
    // Area check at zoom 14 units: the tiles' area is at least the box's.
    const frame = localFrame(o);
    const area = tiles.reduce((s, t) => s + (frame.m / 2 ** t.z) ** 2, 0);
    assert.ok(area >= 50_000 * 50_000);
  }
});

test("zoom falls with distance from the origin", () => {
  for (const o of ORIGINS) {
    const { tiles, rings } = tileCover(localFrame(o), BOX);
    for (const t of tiles) {
      const want = rings.find((r) => t.dist <= r.within).z;
      assert.ok(t.z >= want, `${t.z}/${t.x}/${t.y} at ${Math.round(t.dist)} m`);
    }
    assert.ok(tiles[0].z >= 14 || tileCover(localFrame(o), BOX).coarsened > 0);
  }
});

test("over the cap the rings shrink before any tile is cut", () => {
  const frame = localFrame(ORIGINS[0]);
  const loose = tileCover(frame, BOX, { cap: 1000 });
  const tight = tileCover(frame, BOX, { cap: 20 });
  assert.ok(tight.tiles.length <= 20);
  assert.ok(tight.coarsened > 0);
  assert.ok(tight.tiles.length < loose.tiles.length);
  assert.ok(tileCover(frame, BOX).tiles.length <= DEFAULT_TILE_CAP);
});
