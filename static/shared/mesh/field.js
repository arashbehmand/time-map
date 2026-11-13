// A travel-time field: minutes on a regular grid in local metres around an origin.
// Pure. The GLSL twin in warp.js samples it the same way.
//
//   node (i, j) sits at (x0 + i·cell, y0 + j·cell); minutes[j·nx + i]
//   NO_DATA (−1) means unknown. GLSL ES doesn't guarantee NaN, so no NaN anywhere.
//   A bilinear sample that touches a NO_DATA corner, or falls outside the grid, is NO_DATA.

export const NO_DATA = -1;
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
  const top = c00 + (c10 - c00) * a, bottom = c01 + (c11 - c01) * a;
  return top + (bottom - top) * b;
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
