// The server's field payload (server/warp/field.py encode) decoded by field.js, and the
// frame vector tests/test_field.py checks on the Python side.
import { test } from "node:test";
import assert from "node:assert/strict";
import { BEYOND, decodeField, knownMinutes, NO_DATA, nodeAt, sampleField } from "../../static/shared/mesh/field.js";
import { createWarp } from "../../static/shared/mesh/warp.js";
import { localFrame } from "../../static/shared/mesh/frame.js";

test("decodeField reads tenths, no data, beyond and the filled mark", () => {
  const raw = new Uint16Array([0, 123, 0xffff, 0xfffe, 1790 | 0x8000, 32765]);
  const b64 = Buffer.from(new Uint8Array(raw.buffer)).toString("base64");
  const f = decodeField({ origin: { lon: 0, lat: 51 }, cell: 100, nx: 3, ny: 2, x0: -100, y0: 0, minutes_b64: b64 });
  assert.deepEqual(Array.from(f.minutes).map((v) => Math.round(v * 10) / 10), [0, 12.3, NO_DATA, BEYOND, 179, 3276.5]);
  assert.deepEqual(Array.from(f.filled), [0, 0, 0, 0, 1, 0]);
  assert.deepEqual(nodeAt(f, 0, 0), { minutes: f.minutes[1], filled: false });
  assert.equal(nodeAt(f, 500, 0), null);
  assert.throws(() => decodeField({ origin: {}, cell: 100, nx: 4, ny: 2, x0: 0, y0: 0, minutes_b64: b64 }));
});

test("frame.js agrees with the Python twin's vector", () => {
  const [x, y] = localFrame({ lon: -0.1233, lat: 51.5308 }).toLocal(-0.0195, 51.5035);
  assert.ok(Math.abs(x - 7188.271472) < 1e-3 && Math.abs(y - 3038.111369) < 1e-3);
});

test("beyond is a status, never blended into a time", () => {
  const f = { kind: "field", origin: { lon: 0, lat: 51 }, cell: 100, nx: 2, ny: 2, x0: 0, y0: 0,
              minutes: new Float32Array([30, BEYOND, 30, 30]) };
  assert.equal(sampleField(f, 1, 1), BEYOND);                  // next to a beyond corner
  assert.equal(knownMinutes(f, 1, 1), NO_DATA);                // labels and taps show nothing
  assert.equal(createWarp({ kind: "polar", field: f, maxMinutes: 45 }).target(1, 1), null);
  const g = { ...f, minutes: new Float32Array([30, 32, 30, 30]) };
  assert.ok(Math.abs(knownMinutes(g, 50, 0) - 31) < 1e-9);
});
