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
//   squashLayout(field, maxMinutes, opts)   R, its reach, the radius to frame on and the polar
//                        warp spec over R: what the page and both workers build from the same
//                        field. With `scale: "ground"` (groundScale) the rings are spaced by the
//                        ground reached instead of the minutes.
import { BEYOND, createField, sampleField } from "./field.js";

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
    // R one cell back toward the pin, from the corners that hold one.
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
    // Rising (or level): its own minutes. In a pocket's shadow: keep climbing.
    const prev = num / den;
    R[k] = den > 0 ? (t >= prev ? t : prev + minSlope * cell) : t;
  }
  const out = createField({ origin: f.origin, cell, nx, ny, x0, y0, minutes: R });
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

export function bentRings(f, warp, levels, { bearings = 720, step = 25, smooth = 2 } = {}) {
  const lv = [...levels].sort((a, b) => a - b);
  const rMax = Math.hypot(Math.max(-f.x0, f.x0 + (f.nx - 1) * f.cell), Math.max(-f.y0, f.y0 + (f.ny - 1) * f.cell));
  // pts[level][bearing]: the layout point of the first place that many minutes away, or null.
  const pts = lv.map(() => new Array(bearings).fill(null));
  for (let b = 0; b < bearings; b++) {
    const a = (2 * Math.PI * b) / bearings, c = Math.cos(a), s = Math.sin(a);
    let next = 0, r0 = 0, t0 = 0;
    for (let r = step; r <= rMax && next < lv.length; r += step) {
      let t1 = sampleField(f, r * c, r * s);
      if (t1 < 0) break;                              // off the map: no place further out here
      if (t1 >= BEYOND) t1 = Infinity;                // no time within the tables' limit
      while (next < lv.length && t1 >= lv[next]) {
        const T = lv[next];
        const rr = Number.isFinite(t1) && t1 > t0 ? r0 + ((T - t0) / (t1 - t0)) * (r - r0) : r;
        pts[next][b] = warp.target(rr * c, rr * s);
        next++;
      }
      r0 = r; t0 = t1;
    }
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
export function squashLayout(f, maxMinutes, { minSlope, angles = null, scale = "minutes" } = {}) {
  let R = squashField(f, minSlope > 0 ? { minSlope } : {});
  let reach = squashReach(f, R, maxMinutes), frame = reach;
  if (scale === "ground") {
    const g = groundScale(R);
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
    frame = Math.min(reach, at.length ? at[at.length >> 1] : g(T));
  }
  return { R, reach, frame, spec: { kind: "polar", field: R, maxMinutes: reach, angles } };
}
