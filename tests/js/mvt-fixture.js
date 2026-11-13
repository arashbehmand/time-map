// Encode a tiny Mapbox Vector Tile by hand (no Mapbox data is stored in the repo), with the
// vendored pbf writer, so the tests can run the real decode → classify → tessellate steps.
// layers: { name: [{ type: 1|2|3, props: {...}, geometry: [[[x, y], ...], ...] }] }
import { PbfWriter } from "../../static/shared/vendor/pbf/index.js";

const zz = (n) => (n << 1) ^ (n >> 31);

function commands(type, parts) {
  const out = [];
  let cx = 0, cy = 0;
  for (const part of parts) {
    const pts = type === 3 ? part.slice(0, -1) : part;   // polygons: ClosePath, not a repeat
    const [x0, y0] = pts[0];
    out.push((1 & 7) | (1 << 3), zz(x0 - cx), zz(y0 - cy));
    cx = x0; cy = y0;
    if (type === 1) continue;
    out.push((2 & 7) | ((pts.length - 1) << 3));
    for (const [x, y] of pts.slice(1)) { out.push(zz(x - cx), zz(y - cy)); cx = x; cy = y; }
    if (type === 3) out.push((7 & 7) | (1 << 3));
  }
  return out;
}

export function encodeTile(layers, extent = 4096) {
  const pbf = new PbfWriter();
  for (const [name, features] of Object.entries(layers)) {
    pbf.writeMessage(3, (_, p) => {
      const keys = [], values = [];
      const key = (k) => { let i = keys.indexOf(k); if (i < 0) { keys.push(k); i = keys.length - 1; } return i; };
      const value = (v) => { let i = values.indexOf(v); if (i < 0) { values.push(v); i = values.length - 1; } return i; };
      p.writeVarintField(15, 2);
      p.writeStringField(1, name);
      for (const f of features) {
        p.writeMessage(2, (__, q) => {
          const tags = [];
          for (const [k, v] of Object.entries(f.props || {})) tags.push(key(k), value(v));
          q.writePackedVarint(2, tags);
          q.writeVarintField(3, f.type);
          q.writePackedVarint(4, commands(f.type, f.geometry));
        });
      }
      for (const k of keys) p.writeStringField(3, k);
      for (const v of values) {
        p.writeMessage(4, (__, q) => {
          if (typeof v === "string") q.writeStringField(1, v);
          else if (Number.isInteger(v)) q.writeVarintField(5, v);
          else q.writeDoubleField(3, v);
        });
      }
      p.writeVarintField(5, extent);
    });
  }
  return pbf.finish();
}
