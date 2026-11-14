// Another way to bend a transit field onto rings (#5, W-02 trial). Pure.
//
// A transit field has pockets: around each station the minutes drop, then rise again as you
// walk away. The plain time-polar warp ("fold") draws every place at radius = its minutes, so
// each pocket folds over its station and the map overlaps itself.
//
//   squashField(field)   "squash": along each ray from the pin, radius = the place's own
//                        minutes while they keep rising; where they drop below the slowest
//                        place met so far (a pocket's shadow), the radius keeps climbing at
//                        `minSlope` minutes per metre instead, until the minutes catch up. So
//                        nothing folds along a ray, and a field without pockets is drawn
//                        exactly as the fold, with round rings. Colour still shows each
//                        place's own minutes.
//   bentRings(field, warp, levels)   squash's rings: its distance from the pin isn't minutes, so
//                        each ring is bent instead. Along each of `bearings` directions it
//                        passes through the first place that many minutes away (on the
//                        field's own minutes), drawn where the warp puts it: one closed,
//                        deformed circle per level, every point of it exactly that many
//                        minutes away, everything inside it quicker. Quick places further out
//                        (near stations) show by their colour. Layout-unit polylines with
//                        NaN breaks where a direction has no such place on the map.
//   squashReach(field, R, maxMinutes)   how far out squash draws the places within maxMinutes
//                        (the 99th percentile of R over them), so the view can reach that far.
//   cutPeaks(field)      enclosed hills (walled-off ground) lowered to the way out around them,
//                        so squash implodes them instead of pushing what lies behind outward
//   squashLayout(field, maxMinutes, opts)   R, its reach, the radius to frame on and the polar
//                        warp spec over R: what the page and both workers build from the same
//                        field. With `scale: "ground"` (groundScale) the rings are spaced by the
//                        ground reached instead of the minutes.
import { BEYOND, createField, sampleField } from "./field.js";

const WALK_M_PER_MIN = 4000 / 60;

const known = (t) => t >= 0 && t < BEYOND;

// `mode: "implode"` (a trial: ?squashmode=implode) goes the other way round. Along each ray, a place
// with nothing quicker beyond it is an anchor and sits at its own minutes; slow ground between
// two anchors (a walled-off railway, the stretch between stations) is spread evenly over the
// time between them, so it implodes to nothing when the anchor beyond is no slower than the one
// before, and never pushes what lies behind it outward. A quick place beyond slow ground stays
// at its own minutes. R ≤ minutes everywhere, and R never decreases going out.
export function squashField(f, { minSlope = 0.003, mode = "explode" } = {}) {
  const { nx, ny, x0, y0, cell, minutes } = f;
  const n = nx * ny;
  const R = new Float32Array(n).fill(-1);
  // Nodes in order of distance from the pin, in buckets a quarter cell wide. A corner of the
  // point one cell along the ray can still be the node itself or a neighbour not yet done, so
  // only corners already done are read.
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

  // Bilinear values of `arrays` one cell along the ray from node k (dir −1: toward the pin,
  // +1: outward), from the corners where `ok` holds; null where none does.
  const along = (k, dir, arrays, ok) => {
    const r = rad[k], s = 1 + (dir * cell) / r;
    const px = (x0 + (k % nx) * cell) * s, py = (y0 + Math.floor(k / nx) * cell) * s;
    const fx = (px - x0) / cell, fy = (py - y0) / cell;
    const i = Math.floor(fx), j = Math.floor(fy), a = fx - i, b = fy - j;
    const sum = arrays.map(() => 0);
    let den = 0;
    for (const [di, dj, w] of [[0, 0, (1 - a) * (1 - b)], [1, 0, a * (1 - b)], [0, 1, (1 - a) * b], [1, 1, a * b]]) {
      const ii = i + di, jj = j + dj;
      if (ii < 0 || jj < 0 || ii >= nx || jj >= ny || w <= 0) continue;
      const q = jj * nx + ii;
      if (!ok(q)) continue;
      arrays.forEach((arr, m) => { sum[m] += w * arr[q]; });
      den += w;
    }
    return den > 0 ? sum.map((v) => v / den) : null;
  };

  if (mode !== "implode") {
    for (const k of order) {
      const t = minutes[k];
      if (!known(t) || rad[k] <= cell) { R[k] = t; continue; }
      const p = along(k, -1, [R], (q) => known(R[q]));
      // Rising (or level): its own minutes. In a pocket's shadow: keep climbing.
      R[k] = p ? (t >= p[0] ? t : p[0] + minSlope * cell) : t;
    }
  } else {
    // Outward-in: E, the quickest time at or beyond each place, and D, the distance out to the
    // next anchor (0 at an anchor). Then inward-out: P, the last anchor's minutes, and L, the
    // distance back to it; R spreads E − P over L + D.
    const E = new Float32Array(n), D = new Float32Array(n), P = new Float32Array(n), Ln = new Float32Array(n);
    const done1 = new Uint8Array(n), done2 = new Uint8Array(n);
    for (let o = n - 1; o >= 0; o--) {
      const k = order[o], t = minutes[k];
      if (!known(t)) continue;
      const nx1 = rad[k] > cell ? along(k, +1, [E, D], (q) => done1[q] === 1) : null;
      if (!nx1 || t <= nx1[0]) { E[k] = t; D[k] = 0; } else { E[k] = nx1[0]; D[k] = nx1[1] + cell; }
      done1[k] = 1;
    }
    for (const k of order) {
      const t = minutes[k];
      if (!known(t)) { R[k] = t; continue; }
      done2[k] = 1;
      if (D[k] === 0 || rad[k] <= cell) { P[k] = E[k]; Ln[k] = 0; R[k] = E[k]; continue; }
      const pv = along(k, -1, [P, Ln], (q) => done2[q] === 1 && q !== k);
      P[k] = pv ? Math.min(pv[0], E[k]) : E[k];
      Ln[k] = pv ? pv[1] + cell : 0;
      R[k] = P[k] + ((E[k] - P[k]) * Ln[k]) / Math.max(1e-9, Ln[k] + D[k]);
    }
  }
  const out = createField({ origin: f.origin, cell, nx, ny, x0, y0, minutes: R });
  if (f.filled) out.filled = f.filled;
  return out;
}

// Hills cut down (squash's first step): a place slower than all the ground around it (a station's
// platforms and tracks, a railway yard: walled off from walking) is lowered to the level of the
// way out around it, so it implodes to a line instead of pushing everything behind it outward.
// Slow ground that leads on outward to slower ground (between stations) is kept: only enclosed
// hills are cut. Each place's value is the highest, over every path from it to the edge of the
// known ground, of the lowest time along that path (a priority flood from the edge). Unknown
// ground enclosed by known ground (a hole) can't be crossed; unknown ground reaching the grid's
// edge is outside, and the known ground next to it is the edge.
export function cutPeaks(f) {
  const { nx, ny, minutes } = f;
  const n = nx * ny;
  const val = (k) => (minutes[k] >= BEYOND ? Infinity : minutes[k]);
  const outside = new Uint8Array(n);
  const stack = [];
  for (let i = 0; i < nx; i++) for (const j of [0, ny - 1]) stack.push(j * nx + i);
  for (let j = 0; j < ny; j++) for (const i of [0, nx - 1]) stack.push(j * nx + i);
  while (stack.length) {
    const k = stack.pop();
    if (outside[k] || minutes[k] >= 0) continue;
    outside[k] = 1;
    const i = k % nx, j = (k - i) / nx;
    if (i > 0) stack.push(k - 1);
    if (i + 1 < nx) stack.push(k + 1);
    if (j > 0) stack.push(k - nx);
    if (j + 1 < ny) stack.push(k + nx);
  }
  // A binary max-heap of [value, node].
  const hv = [], hk = [];
  const push = (v, k) => {
    let i = hv.length; hv.push(v); hk.push(k);
    while (i > 0) { const p = (i - 1) >> 1; if (hv[p] >= hv[i]) break; [hv[p], hv[i]] = [hv[i], hv[p]]; [hk[p], hk[i]] = [hk[i], hk[p]]; i = p; }
  };
  const pop = () => {
    const v = hv[0], k = hk[0], lv = hv.pop(), lk = hk.pop();
    if (hv.length) {
      hv[0] = lv; hk[0] = lk;
      for (let i = 0; ;) {
        const l = 2 * i + 1, r = l + 1;
        let m = i;
        if (l < hv.length && hv[l] > hv[m]) m = l;
        if (r < hv.length && hv[r] > hv[m]) m = r;
        if (m === i) break;
        [hv[m], hv[i]] = [hv[i], hv[m]]; [hk[m], hk[i]] = [hk[i], hk[m]]; i = m;
      }
    }
    return [v, k];
  };
  const cut = new Float32Array(minutes);
  const seen = new Uint8Array(n);
  for (let k = 0; k < n; k++) {
    if (!(minutes[k] >= 0)) continue;
    const i = k % nx, j = (k - i) / nx;
    const edge = i === 0 || j === 0 || i === nx - 1 || j === ny - 1 ||
      outside[k - 1] || outside[k + 1] || outside[k - nx] || outside[k + nx];
    if (edge) { seen[k] = 1; push(val(k), k); }
  }
  while (hv.length) {
    const [v, k] = pop();
    const i = k % nx, j = (k - i) / nx;
    for (const q of [i > 0 ? k - 1 : -1, i + 1 < nx ? k + 1 : -1, j > 0 ? k - nx : -1, j + 1 < ny ? k + nx : -1]) {
      if (q < 0 || seen[q] || !(minutes[q] >= 0)) continue;
      seen[q] = 1;
      const c = Math.min(val(q), v);
      if (c < val(q)) cut[q] = c;
      push(c, q);
    }
  }
  const out = createField({ ...f, minutes: cut });
  if (f.filled) out.filled = f.filled;
  return out;
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

// `last`: through the last place that many minutes away along each direction instead of the
// first (imploding: every place within T is then inside the T ring, which comes out round).
export function bentRings(f, warp, levels, { bearings = 720, step = 25, smooth = 2, last = false } = {}) {
  const lv = [...levels].sort((a, b) => a - b);
  const rMax = Math.hypot(Math.max(-f.x0, f.x0 + (f.nx - 1) * f.cell), Math.max(-f.y0, f.y0 + (f.ny - 1) * f.cell));
  // pts[level][bearing]: the layout point of the first place that many minutes away, or null.
  const pts = lv.map(() => new Array(bearings).fill(null));
  for (let b = 0; b < bearings; b++) {
    const a = (2 * Math.PI * b) / bearings, c = Math.cos(a), s = Math.sin(a);
    let next = 0, r0 = 0, t0 = 0;
    const at = last ? new Float64Array(lv.length).fill(NaN) : null;
    for (let r = step; r <= rMax && (last || next < lv.length); r += step) {
      let t1 = sampleField(f, r * c, r * s);
      if (t1 < 0) break;                              // off the map: no place further out here
      if (t1 >= BEYOND) t1 = Infinity;                // no time within the tables' limit
      if (last) {
        // Every upward crossing of each level; the last one stands.
        for (let i = 0; i < lv.length; i++) {
          const T = lv[i];
          if (t0 <= T && t1 > T) at[i] = Number.isFinite(t1) ? r0 + ((T - t0) / (t1 - t0)) * (r - r0) : r;
        }
      } else {
        while (next < lv.length && t1 >= lv[next]) {
          const T = lv[next];
          const rr = Number.isFinite(t1) && t1 > t0 ? r0 + ((T - t0) / (t1 - t0)) * (r - r0) : r;
          pts[next][b] = warp.target(rr * c, rr * s);
          next++;
        }
      }
      r0 = r; t0 = t1;
    }
    if (last) for (let i = 0; i < lv.length; i++) if (!Number.isNaN(at[i])) pts[i][b] = warp.target(at[i] * c, at[i] * s);
  }
  return lv.map((T, i) => {
    const P = pts[i];
    // A light median on the radius over neighbouring directions, so one ray that threads a
    // fast corridor doesn't spike the ring.
    const rad = P.map((p) => (p ? Math.hypot(p[0], p[1]) : NaN));
    const out = [];
    let first = null, open = false;
    for (let b = 0; b <= bearings; b++) {
      const k = b % bearings, p = P[k];
      if (!p) { if (open) { out.push(NaN, NaN); open = false; } continue; }
      const near = [];
      for (let d = -smooth; d <= smooth; d++) { const v = rad[(k + d + bearings) % bearings]; if (!Number.isNaN(v)) near.push(v); }
      near.sort((x, y) => x - y);
      const r = near[near.length >> 1], s = r / Math.max(1e-9, rad[k]);
      const q = [p[0] * s, p[1] * s];
      if (b === bearings) { if (open && first) out.push(first[0], first[1]); break; }
      if (!first && b === 0) first = q;
      out.push(q[0], q[1]);
      open = true;
    }
    if (open) out.push(NaN, NaN);
    return { minutes: T, xy: Float32Array.from(out) };
  });
}

// Ring radius by ground reached (`scale: "ground"`, the warp page's default): radius = R inside
// `inner` minutes, so the middle is drawn exactly as it is; beyond, the area inside a ring grows
// with the ground drawn within it, at the inner disc's own ground per area: inner × √(A(R) ÷
// A(inner)). Each band's area is then the ground it adds. Where ground grows as the square of
// the minutes (walking at one speed), this is R itself: round rings, nothing changed.
export function groundScale(R, inner = 15) {
  const ts = [];
  for (let k = 0; k < R.minutes.length; k++) {
    const t = R.minutes[k];
    if (known(t) && !(R.filled && R.filled[k])) ts.push(t);
  }
  ts.sort((a, b) => a - b);
  const count = (t) => { let lo = 0, hi = ts.length; while (lo < hi) { const m = (lo + hi) >> 1; if (ts[m] <= t) lo = m + 1; else hi = m; } return lo; };
  const a0 = Math.max(1, count(inner));
  return (t) => (t <= inner ? t : inner * Math.sqrt(count(t) / a0));
}

// `frame`: the radius the view is framed on: the reach for `scale: "minutes"`; with the ground
// scale, where places two thirds of maxMinutes away are drawn (the outer rings sit far out:
// zoom out for them).
export function squashLayout(f, maxMinutes, { minSlope, angles = null, scale = "minutes", mode = "explode" } = {}) {
  const implode = mode === "implode";
  const cut = implode ? f : cutPeaks(f);
  let R = squashField(cut, { mode, ...(minSlope > 0 ? { minSlope } : {}) });
  let reach = squashReach(f, R, maxMinutes), frame = reach;
  if (scale === "ground") {
    // Ground counted where it is drawn: imploding draws every place within T inside the T ring.
    const g = groundScale(implode ? f : R);
    const m = new Float32Array(R.minutes.length);
    for (let k = 0; k < m.length; k++) m[k] = known(R.minutes[k]) ? g(R.minutes[k]) : R.minutes[k];
    R = createField({ ...R, minutes: m });
    if (f.filled) R.filled = f.filled;
    reach = g(reach);
    // Framed on where places two thirds of the way out are typically drawn (pockets push that
    // ring out beyond g(T) in many directions).
    const T = Math.round((2 * maxMinutes) / 3), at = [];
    for (let k = 0; k < m.length; k++) {
      if (Math.abs(f.minutes[k] - T) <= 0.5 && !(f.filled && f.filled[k]) && known(m[k])) at.push(m[k]);
    }
    at.sort((a, b) => a - b);
    frame = implode ? g(T) : Math.min(reach, at.length ? at[at.length >> 1] : g(T));
  }
  return { R, reach, frame, cut, implode, spec: { kind: "polar", field: R, maxMinutes: reach, angles } };
}
