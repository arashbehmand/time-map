// A travel-time field: minutes on a regular grid in local metres around an origin.
// Pure. The GLSL twin in warp.js samples it the same way.
//
//   node (i, j) sits at (x0 + i·cell, y0 + j·cell); minutes[j·nx + i]
//   NO_DATA (−1) means unknown. GLSL ES doesn't guarantee NaN, so no NaN anywhere.
//   A bilinear sample that touches a NO_DATA corner, or falls outside the grid, is NO_DATA;
//   one that touches a BEYOND corner (no time within the tables' limit) is BEYOND. Neither is
//   ever blended into a time.

export const NO_DATA = -1;
// No time within the tables' limit (the server's 0xFFFE): past every outer ring, it fades onto
// it like no data.
export const BEYOND = 1e4;
// A grid node with no target: its x is this (GLSL ES has no NaN either).
export const GRID_NO_DATA = -1e30;

export function createField({ origin, cell, nx, ny, x0, y0, minutes }) {
  if (!(cell > 0) || nx < 2 || ny < 2) throw new Error("field: bad grid");
  if (!(minutes instanceof Float32Array) || minutes.length !== nx * ny) throw new Error("field: bad minutes");
  return { kind: "field", origin, cell, nx, ny, x0, y0, minutes };
}

// Bilinear minutes at local (x, y), or NO_DATA. Mirrors fieldTarget() in WARP_GLSL.
export function sampleField(f, x, y) {
  const fx = (x - f.x0) / f.cell, fy = (y - f.y0) / f.cell;
  const i = Math.floor(fx), j = Math.floor(fy);
  if (i < 0 || j < 0 || i + 1 >= f.nx || j + 1 >= f.ny) return NO_DATA;
  const a = fx - i, b = fy - j;
  const m = f.minutes, k = j * f.nx + i;
  const c00 = m[k], c10 = m[k + 1], c01 = m[k + f.nx], c11 = m[k + f.nx + 1];
  if (c00 < 0 || c10 < 0 || c01 < 0 || c11 < 0) return NO_DATA;
  if (c00 >= BEYOND || c10 >= BEYOND || c01 >= BEYOND || c11 >= BEYOND) return BEYOND;
  const top = c00 + (c10 - c00) * a, bottom = c01 + (c11 - c01) * a;
  return top + (bottom - top) * b;
}

// Minutes at local (x, y), or NO_DATA where there is no time to show (unknown, beyond, or a
// cell touching an uncertain patch): for labels and taps, which must never print a sentinel or
// a patch's interpolated time.
export function knownMinutes(f, x, y) {
  if (touchesUncertain(f, x, y)) return NO_DATA;
  const t = sampleField(f, x, y);
  return t >= 0 && t < BEYOND ? t : NO_DATA;
}

// Fill a field from a function of local metres (the lab's synthetic fields use this).
export function fieldFromFunction({ origin, cell, halfSize }, minutesAt) {
  const n = Math.round((2 * halfSize) / cell) + 1;
  const x0 = -halfSize, y0 = -halfSize;
  const minutes = new Float32Array(n * n);
  for (let j = 0; j < n; j++) {
    const y = y0 + j * cell;
    for (let i = 0; i < n; i++) {
      const t = minutesAt(x0 + i * cell, y);
      minutes[j * n + i] = Number.isFinite(t) && t >= 0 ? t : NO_DATA;
    }
  }
  return createField({ origin, cell, nx: n, ny: n, x0, y0, minutes });
}

// A grid of target positions (layout units, two floats per node) for the grid warp, the
// shape the London map (#6) rasterises its layout into. Same node convention as a field.
export function createTargetGrid({ origin, cell, nx, ny, x0, y0, xy }) {
  if (!(cell > 0) || nx < 2 || ny < 2) throw new Error("grid: bad grid");
  if (!(xy instanceof Float32Array) || xy.length !== 2 * nx * ny) throw new Error("grid: bad targets");
  return { kind: "grid", origin, cell, nx, ny, x0, y0, xy };
}

export function gridFromFunction({ origin, cell, halfSize }, targetAt) {
  const n = Math.round((2 * halfSize) / cell) + 1;
  const x0 = -halfSize, y0 = -halfSize;
  const xy = new Float32Array(2 * n * n);
  for (let j = 0; j < n; j++) {
    for (let i = 0; i < n; i++) {
      const [tx, ty] = targetAt(x0 + i * cell, y0 + j * cell);
      xy[2 * (j * n + i)] = tx;
      xy[2 * (j * n + i) + 1] = ty;
    }
  }
  return createTargetGrid({ origin, cell, nx: n, ny: n, x0, y0, xy });
}

// Bilinear target at local (x, y), or null outside the grid or touching a GRID_NO_DATA node.
// Mirrors WARP_GLSL.
export function sampleGrid(g, x, y) {
  const fx = (x - g.x0) / g.cell, fy = (y - g.y0) / g.cell;
  const i = Math.floor(fx), j = Math.floor(fy);
  if (i < 0 || j < 0 || i + 1 >= g.nx || j + 1 >= g.ny) return null;
  const a = fx - i, b = fy - j;
  const k = 2 * (j * g.nx + i), r = 2 * g.nx, d = g.xy;
  if (Math.min(d[k], d[k + 2], d[k + r], d[k + r + 2]) < GRID_NO_DATA / 10) return null;
  const out = [0, 0];
  for (let c = 0; c < 2; c++) {
    const top = d[k + c] + (d[k + 2 + c] - d[k + c]) * a;
    const bottom = d[k + r + c] + (d[k + r + 2 + c] - d[k + r + c]) * a;
    out[c] = top + (bottom - top) * b;
  }
  return out;
}

// True when the cell holding local (x, y) has a corner in an uncertain patch.
export function touchesUncertain(f, x, y) {
  const u = f.uncertain;
  if (!u) return false;
  const i = Math.floor((x - f.x0) / f.cell), j = Math.floor((y - f.y0) / f.cell);
  if (i < 0 || j < 0 || i + 1 >= f.nx || j + 1 >= f.ny) return false;
  const k = j * f.nx + i;
  return !!(u[k] || u[k + 1] || u[k + f.nx] || u[k + f.nx + 1]);
}

// The field the bent map's geometry is drawn by: the minutes, with each uncertain patch node
// at its drawn minutes (a smooth fill from the ground around it, from the server), so a patch
// takes only the room between that ground. Colours, bands, taps, label minutes and counts keep
// the field's own minutes. The same field when there are no patches.
export function geometryField(f) {
  if (!f.uncertain || !f.drawn || f.drawn === f.minutes) return f;
  if (f.geometry) return f.geometry;
  const g = createField({ ...f, minutes: f.drawn });
  g.filled = f.filled; g.uncertain = f.uncertain; g.drawn = f.drawn;
  f.geometry = g;
  return g;
}

// ── The server's travel-time field (POST /api/travel-time-field) ─────
// minutes_b64: uint16 little-endian, tenths of a minute; 0xFFFF no data, 0xFFFE beyond (no
// time within the tables' limit), bit 15 set on a node filled in from its neighbours (water,
// parkland: drawn, but nowhere anyone was routed to). uncertain (optional): the uncertain
// patches, {nodes_b64: uint32 little-endian node indices, increasing; drawn_b64: uint16
// tenths, one per node}. See server/warp/field.py.
const WIRE_NO_DATA = 0xffff, WIRE_BEYOND = 0xfffe, WIRE_MAX = 0x7ffd;

function base64Bytes(b64) {
  const s = atob(b64);
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

// → a field (createField) with `filled` (Uint8Array, 1 = filled in), `uncertain` (Uint8Array,
// 1 = in an uncertain patch; null when there are none) and `drawn` (the minutes with each
// patch node at its drawn minutes; the minutes themselves when there are none).
export function decodeField(json) {
  const n = json.nx * json.ny;
  const bytes = base64Bytes(json.minutes_b64);
  if (bytes.length !== 2 * n) throw new Error("field: bad payload");
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const minutes = new Float32Array(n), filled = new Uint8Array(n);
  for (let k = 0; k < n; k++) {
    const v = dv.getUint16(2 * k, true);
    if (v === WIRE_NO_DATA) minutes[k] = NO_DATA;
    else if (v === WIRE_BEYOND) minutes[k] = BEYOND;
    else { minutes[k] = (v & 0x7fff) / 10; filled[k] = v >> 15; }
  }
  const f = createField({ origin: json.origin, cell: json.cell, nx: json.nx, ny: json.ny, x0: json.x0, y0: json.y0, minutes });
  f.filled = filled;
  f.uncertain = null;
  f.drawn = minutes;
  const U = json.uncertain;
  if (U) {
    const nodes = base64Bytes(U.nodes_b64), tenths = base64Bytes(U.drawn_b64);
    const m = nodes.length / 4;
    if (!Number.isInteger(m) || tenths.length !== 2 * m) throw new Error("field: bad payload");
    const nv = new DataView(nodes.buffer, nodes.byteOffset, nodes.byteLength);
    const tv = new DataView(tenths.buffer, tenths.byteOffset, tenths.byteLength);
    const uncertain = new Uint8Array(n), drawn = Float32Array.from(minutes);
    for (let q = 0, last = -1; q < m; q++) {
      const k = nv.getUint32(4 * q, true), t = tv.getUint16(2 * q, true);
      if (k <= last || k >= n || t > WIRE_MAX) throw new Error("field: bad payload");
      last = k;
      uncertain[k] = 1;
      drawn[k] = t / 10;
    }
    if (m > 0) { f.uncertain = uncertain; f.drawn = drawn; }
  }
  return f;
}

// Nearest-node lookup: {minutes, filled, uncertain} at local (x, y), or null outside the grid.
// For taps on the ordinary map, where "this is water" matters more than a bilinear blend.
export function nodeAt(f, x, y) {
  const i = Math.round((x - f.x0) / f.cell), j = Math.round((y - f.y0) / f.cell);
  if (i < 0 || j < 0 || i >= f.nx || j >= f.ny) return null;
  const k = j * f.nx + i;
  return { minutes: f.minutes[k], filled: !!(f.filled && f.filled[k]), uncertain: !!(f.uncertain && f.uncertain[k]) };
}
