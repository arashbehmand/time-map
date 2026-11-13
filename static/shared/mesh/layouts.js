// Two other ways to bend a transit field onto rings (#5, W-02 prototypes). Pure.
//
// A transit field has pockets: around each station the minutes drop, then rise again as you
// walk away. The plain time-polar warp ("fold") draws every place at radius = its minutes, so
// each pocket folds over its station and the map overlaps itself.
//
//   squashField(field)   "squash": radius = the running maximum of the minutes along the ray
//                        from the pin, climbing at least `minSlope` minutes per metre. It
//                        never decreases outward, so nothing folds along a ray; colour still
//                        shows each place's own minutes. Pockets are not pulled in: a quick
//                        station beyond slow ground sits on the ring of the slow ground.
//
//   islandLayout(field)  "islands": the field is cut into basins (each node runs downhill to
//                        a local minimum; shallow basins merge into their neighbour). The
//                        pin's basin is drawn time-polar (radius = minutes). Every other basin
//                        is an island: a small undistorted map of its ground, centred on its
//                        quickest place, which
//                        sits on the ring of its own minutes. Islands that would overlap slide
//                        along their ring (up to `maxSlide` degrees). The islands' scale (ring
//                        minutes per minute walked) is set so that together they cover
//                        `fill` of the outer disc: their ground is far larger than the disc
//                        at walking scale. Basins are torn apart at their borders (no-data
//                        nodes on the slower side), so nothing is smeared between islands.
//                        Returns a target grid (field.js) for a "grid" warp.
//
//   timeLines(field, warp, levels)   the real minute lines (region boundaries, contours.js)
//                        carried through a warp into layout units, as one polyline set per
//                        level with NaN breaks where the warp doesn't place a point. Squash
//                        draws these instead of rings: its distance from the pin isn't minutes.
//   squashReach(field, R, maxMinutes)   how far out squash draws the places within maxMinutes
//                        (the 99th percentile of R over them), so the view can reach that far.
import { regionRings } from "./contours.js";
import { BEYOND, createField, createTargetGrid, GRID_NO_DATA } from "./field.js";

const WALK_M_PER_MIN = 4000 / 60;

const known = (t) => t >= 0 && t < BEYOND;

export function squashField(f, { minSlope = 0.003 } = {}) {
  const { nx, ny, x0, y0, cell, minutes } = f;
  const n = nx * ny;
  const R = new Float32Array(n).fill(-1);
  // Nodes in order of distance from the pin, in buckets a quarter cell wide: the point one
  // cell back toward the pin has all four bilinear corners in earlier buckets.
  const bw = cell / 4;
  const rad = new Float64Array(n);
  let maxB = 0;
  for (let k = 0; k < n; k++) {
    rad[k] = Math.hypot(x0 + (k % nx) * cell, y0 + Math.floor(k / nx) * cell);
    maxB = Math.max(maxB, Math.floor(rad[k] / bw));
  }
  const count = new Uint32Array(maxB + 2);
  for (let k = 0; k < n; k++) count[Math.floor(rad[k] / bw) + 1]++;
  for (let b = 1; b < count.length; b++) count[b] += count[b - 1];
  const order = new Uint32Array(n);
  const at = count.slice();
  for (let k = 0; k < n; k++) order[at[Math.floor(rad[k] / bw)]++] = k;

  for (const k of order) {
    const t = minutes[k];
    if (!known(t)) { R[k] = t; continue; }
    const r = rad[k];
    if (r <= cell) { R[k] = t; continue; }
    // R one cell back toward the pin, from the corners that hold a running maximum.
    const s = 1 - cell / r;
    const px = (x0 + (k % nx) * cell) * s, py = (y0 + Math.floor(k / nx) * cell) * s;
    const fx = (px - x0) / cell, fy = (py - y0) / cell;
    const i = Math.floor(fx), j = Math.floor(fy), a = fx - i, b = fy - j;
    let num = 0, den = 0;
    for (const [di, dj, w] of [[0, 0, (1 - a) * (1 - b)], [1, 0, a * (1 - b)], [0, 1, (1 - a) * b], [1, 1, a * b]]) {
      const ii = i + di, jj = j + dj;
      if (ii < 0 || jj < 0 || ii >= nx || jj >= ny || w <= 0) continue;
      const v = R[jj * nx + ii];
      if (known(v)) { num += w * v; den += w; }
    }
    R[k] = den > 0 ? Math.max(t, num / den + minSlope * cell) : t;
  }
  const out = createField({ origin: f.origin, cell, nx, ny, x0, y0, minutes: R });
  if (f.filled) out.filled = f.filled;
  return out;
}

export function islandLayout(f, { maxMinutes, persistence = 4, minNodes = 25, fill = 0.3, maxScale = 0.5,
                                   maxSlide = 60, slack = 3 } = {}) {
  const { nx, ny, x0, y0, cell, minutes } = f;
  const n = nx * ny;
  const show = maxMinutes + slack;
  const ok = (k) => known(minutes[k]);
  const less = (p, q) => minutes[p] < minutes[q] || (minutes[p] === minutes[q] && p < q);

  // 1. Downhill: each node points at its lowest 8-neighbour lower than itself.
  const parent = new Int32Array(n).fill(-1);
  for (let k = 0; k < n; k++) {
    if (!ok(k)) continue;
    const i = k % nx, j = (k - i) / nx;
    let best = k;
    for (let dj = -1; dj <= 1; dj++) for (let di = -1; di <= 1; di++) {
      const ii = i + di, jj = j + dj;
      if ((!di && !dj) || ii < 0 || jj < 0 || ii >= nx || jj >= ny) continue;
      const q = jj * nx + ii;
      if (ok(q) && less(q, best)) best = q;
    }
    parent[k] = best;
  }
  const label = new Int32Array(n).fill(-1);
  const stack = [];
  for (let k = 0; k < n; k++) {
    if (!ok(k) || label[k] >= 0) continue;
    let q = k;
    while (label[q] < 0 && parent[q] !== q) { stack.push(q); q = parent[q]; }
    const root = label[q] >= 0 ? label[q] : q;
    label[q] = root;
    while (stack.length) label[stack.pop()] = root;
  }

  // 2. Merge shallow basins (persistence) and small ones, lowest saddles first.
  const saddles = new Map();
  for (let k = 0; k < n; k++) {
    if (label[k] < 0) continue;
    const i = k % nx;
    for (const q of [i + 1 < nx ? k + 1 : -1, k + nx < n ? k + nx : -1]) {
      if (q < 0 || label[q] < 0 || label[q] === label[k]) continue;
      const a = Math.min(label[k], label[q]), b = Math.max(label[k], label[q]);
      const key = a * n + b, s = Math.max(minutes[k], minutes[q]);
      const old = saddles.get(key);
      if (old === undefined || s < old) saddles.set(key, s);
    }
  }
  const edges = [...saddles].map(([key, s]) => [Math.floor(key / n), key % n, s]).sort((p, q) => p[2] - q[2]);
  const up = new Map();
  const find = (a) => { let r = a; while (up.has(r)) r = up.get(r); while (up.has(a)) { const nx2 = up.get(a); up.set(a, r); a = nx2; } return r; };
  const size = new Map();
  for (let k = 0; k < n; k++) if (label[k] >= 0 && minutes[k] <= show) size.set(label[k], (size.get(label[k]) || 0) + 1);
  const merge = (lo, hi) => { up.set(hi, lo); size.set(lo, (size.get(lo) || 0) + (size.get(hi) || 0)); };
  for (const [a, b, s] of edges) {
    const ra = find(a), rb = find(b);
    if (ra === rb) continue;
    const [lo, hi] = less(ra, rb) ? [ra, rb] : [rb, ra];
    if (s - minutes[hi] <= persistence) merge(lo, hi);
  }
  for (const [a, b] of edges) {
    const ra = find(a), rb = find(b);
    if (ra === rb) continue;
    const sa = size.get(ra) || 0, sb = size.get(rb) || 0;
    if (sa < minNodes || sb < minNodes) {
      const [lo, hi] = less(ra, rb) ? [ra, rb] : [rb, ra];
      merge(lo, hi);
    }
  }
  for (let k = 0; k < n; k++) if (label[k] >= 0) label[k] = find(label[k]);

  // 3. The pin's basin, and the islands with their ground's reach from their quickest place.
  const pinNode = Math.round(-y0 / cell) * nx + Math.round(-x0 / cell);
  const pinBasin = label[pinNode];
  const pos = (k) => [x0 + (k % nx) * cell, y0 + Math.floor(k / nx) * cell];
  const islands = new Map();
  for (let k = 0; k < n; k++) {
    const b = label[k];
    if (b < 0 || b === pinBasin || minutes[k] > show || minutes[b] > maxMinutes) continue;
    let isl = islands.get(b);
    if (!isl) {
      const [sx, sy] = pos(b);
      isl = { root: b, sx, sy, t: minutes[b], nodes: 0, extent: 0 };
      islands.set(b, isl);
    }
    const [x, y] = pos(k);
    isl.nodes++;
    isl.extent = Math.max(isl.extent, Math.hypot(x - isl.sx, y - isl.sy) / WALK_M_PER_MIN);
  }
  let islandNodes = 0;
  for (const isl of islands.values()) islandNodes += isl.nodes;
  const nodeArea = (cell / WALK_M_PER_MIN) ** 2;            // ring minutes² per node at scale 1
  const scale = islandNodes ? Math.min(maxScale, Math.sqrt((fill * Math.PI * maxMinutes ** 2) / (islandNodes * nodeArea))) : maxScale;
  for (const isl of islands.values()) isl.extent *= scale;

  // 4. Place them: on their own ring, at their bearing, slid along the ring where they would
  // overlap the pin's sheet or an island already placed (biggest first).
  const BIN = 1;                                   // degrees
  const occupied = Array.from({ length: 360 / BIN }, () => []);
  const binOf = (deg) => ((Math.round(deg / BIN) % occupied.length) + occupied.length) % occupied.length;
  for (let k = 0; k < n; k++) {
    if (label[k] !== pinBasin || minutes[k] > show) continue;
    const [x, y] = pos(k);
    const b = binOf((Math.atan2(y, x) * 180) / Math.PI);
    const list = occupied[b];
    if (!list.length) list.push([0, minutes[k]]);
    else list[0][1] = Math.max(list[0][1], minutes[k]);
  }
  const free = (deg, half, r0, r1) => {
    for (let d = -half; d <= half; d += BIN) {
      for (const [a, b] of occupied[binOf(deg + d)]) if (r0 < b && r1 > a) return false;
    }
    return true;
  };
  const placed = [...islands.values()].sort((p, q) => q.nodes - p.nodes);
  let slid = 0, unplaced = 0;
  for (const isl of placed) {
    const deg0 = (Math.atan2(isl.sy, isl.sx) * 180) / Math.PI;
    const half = Math.ceil(((isl.t > isl.extent ? Math.asin(isl.extent / isl.t) : Math.PI / 2) * 180) / Math.PI);
    const r0 = isl.t - isl.extent, r1 = isl.t + isl.extent;
    let deg = deg0, found = false;
    for (let off = 0; off <= maxSlide && !found; off += 2) {
      for (const sgn of off ? [1, -1] : [1]) {
        if (free(deg0 + sgn * off, half, r0, r1)) { deg = deg0 + sgn * off; found = true; break; }
      }
    }
    if (!found) unplaced++;
    else if (deg !== deg0) slid++;
    for (let d = -half; d <= half; d += BIN) occupied[binOf(deg + d)].push([r0, r1]);
    const a = (deg * Math.PI) / 180;
    isl.deg = deg;
    isl.ax = isl.t * Math.cos(a);
    isl.ay = isl.t * Math.sin(a);
  }

  // 5. Targets, and the tears: a node on the border of a slower basin than its neighbour's.
  const xy = new Float32Array(2 * n).fill(GRID_NO_DATA);
  const mask = new Uint8Array(n);
  const piece = new Int32Array(n).fill(-1);           // for pipeline.js's cut: one id per piece
  let torn = 0;
  for (let k = 0; k < n; k++) {
    const b = label[k];
    if (b < 0 || minutes[k] > show) continue;
    const isl = b === pinBasin ? null : islands.get(b);
    if (b !== pinBasin && !isl) continue;
    const i = k % nx, j = (k - i) / nx;
    let border = false;
    for (let dj = -1; dj <= 1 && !border; dj++) for (let di = -1; di <= 1; di++) {
      const ii = i + di, jj = j + dj;
      if (ii < 0 || jj < 0 || ii >= nx || jj >= ny) continue;
      const q = jj * nx + ii, bq = label[q];
      if (bq >= 0 && bq !== b && minutes[bq] < minutes[b]) { border = true; break; }
    }
    if (border) { torn++; continue; }
    const [x, y] = pos(k);
    if (!isl) {
      const r = Math.hypot(x, y), t = minutes[k];
      xy[2 * k] = r > 1e-6 ? (x / r) * t : 0;
      xy[2 * k + 1] = r > 1e-6 ? (y / r) * t : 0;
    } else {
      xy[2 * k] = isl.ax + (scale * (x - isl.sx)) / WALK_M_PER_MIN;
      xy[2 * k + 1] = isl.ay + (scale * (y - isl.sy)) / WALK_M_PER_MIN;
    }
    mask[k] = 1;
    piece[k] = b;
  }
  const grid = createTargetGrid({ origin: f.origin, cell, nx, ny, x0, y0, xy });
  return {
    grid, mask,
    cut: { x0, y0, cell, nx, ny, label: piece },
    stats: { islands: islands.size, slid, unplaced, torn, scale, persistence },
    islands: placed.map((p) => ({ t: p.t, nodes: p.nodes, extent: p.extent, deg: p.deg })),
  };
}

export function squashReach(f, R, maxMinutes, { share = 0.99, cap = 3 } = {}) {
  const r = [];
  for (let k = 0; k < f.minutes.length; k++) {
    const t = f.minutes[k];
    if (known(t) && t <= maxMinutes && known(R.minutes[k])) r.push(R.minutes[k]);
  }
  if (!r.length) return maxMinutes;
  r.sort((a, b) => a - b);
  return Math.min(cap * maxMinutes, Math.max(maxMinutes, r[Math.min(r.length - 1, Math.floor(share * r.length))]));
}

// Loops shorter than `minLoop` layout minutes all round (a single grid node's pocket) are left
// out: they are noise at a 100 m grid, and they clutter the outer lines.
export function timeLines(f, warp, levels, { minStep = 0.1, minLoop = 1.5 } = {}) {
  return levels.map((T) => {
    const out = [];
    for (const ring of regionRings(f, T)) {
      const piece = [];
      let lx = NaN, ly = NaN, open = false, length = 0;
      for (const [x, y] of ring) {
        const p = warp.target(x, y);
        if (!p) { if (open) { piece.push(NaN, NaN); open = false; } continue; }
        const d = open ? Math.hypot(p[0] - lx, p[1] - ly) : 0;
        if (open && d < minStep) continue;
        piece.push(p[0], p[1]);
        length += d;
        lx = p[0]; ly = p[1]; open = true;
      }
      if (open) piece.push(NaN, NaN);
      if (length >= minLoop) out.push(...piece);
    }
    return { minutes: T, xy: Float32Array.from(out) };
  });
}
