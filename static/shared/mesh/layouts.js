// Squash: another way to bend a transit field onto rings (#5, W-02 trial, behind ?layouts=1). Pure.
//
// A transit field has pockets: around each station the minutes drop, then rise again as you
// walk away. The plain time-polar warp ("fold") draws every place at radius = its minutes, so
// each pocket folds over its station and the map overlaps itself. Squash gives up "radius =
// minutes" to keep the map from overlapping:
//
//   cutPeaks(field)      places much slower than all the ground around them are lowered to the
//                        way out around them, so they don't push everything behind them
//                        outward. A layout filter only: colours, labels, taps and the rings keep
//                        the true minutes. The worst of them in a walking field (a station's
//                        platforms and tracks, which the contour interpolation gives the next
//                        contour's times) come from the server as uncertain patches with drawn
//                        minutes already (field.js geometryField): squash bends from those, so
//                        the cut is left the slow ridges of transit's tables.
//   squashField(field)   along each ray from the pin, radius = the place's own (cut) minutes
//                        while they keep rising; where they drop below the slowest place met
//                        so far (a pocket's shadow), the radius keeps climbing at `minSlope` (MIN_SLOPE)
//                        minutes per metre instead, until the minutes catch up. Holes in the
//                        known ground are stepped over, not restarted from. A field without
//                        pockets is drawn exactly as the fold, with round rings.
//   groundScale(field)   beyond `inner` minutes, the radius grows with the number of places
//                        reached (the true minutes, as the colour bar counts them), at the
//                        middle's own places per area; inside, radius = minutes.
//   squashReach          how far out squash draws the places within the chosen time (99th
//                        percentile, capped at 3 × the time): the view reaches that far.
//   bentRings(field, warp, levels)   the rings: along each of `bearings` directions, the first
//                        place that many minutes away on the true minutes, drawn where the warp
//                        puts it. Every vertex is a place exactly that many minutes away (no
//                        smoothing), and every place nearer the pin along that direction is
//                        quicker. Quicker places further out (near stations) sit beyond the ring.
//                        `src` holds each vertex's place (local metres), for checking. Squash
//                        passes `ringSource`: the true minutes with the cut patches unknown, so
//                        a ring steps over walled-off ground instead of spiking into it (those
//                        patches sit inside a ring, in their own colour).
//   squashLayout(field, maxMinutes, opts)   all of it: R, its reach, the radius to frame on,
//                        the by-reach bearing table (built on R, the quantity the warp looks it
//                        up with) and the polar warp spec. Both workers build it from the same
//                        field, filled-node flags and uncertain patches included. Uncertain
//                        patches count as unknown for the rings, the ground scale and the reach.
import { BEYOND, createField, geometryField, NO_DATA, sampleField } from "./field.js";
import { angularSpread } from "./warp.js";

const known = (t) => t >= 0 && t < BEYOND;

// The cut leaves a place alone unless it stands at least this far above its way out: real slow
// streets between stations (the cut's median drop was 1 minute, p90 3) keep their minutes,
// walled-off ground (15–40 minutes above the streets around it) is lowered.
export const CUT_MIN_DROP = 5;

// The climb in a pocket's shadow: 5 minutes per km (W-03, #27). At 3 it was too gentle: places
// behind a pocket were drawn almost on top of each other (median stretch up to 4.7:1 at
// Heathrow); at 5 every test origin, window and directions setting is under 2.4:1.
export const MIN_SLOPE = 0.005;

export function squashField(f, { minSlope = MIN_SLOPE, maxSteps = 30 } = {}) {
  const { nx, ny, x0, y0, cell, minutes } = f;
  const n = nx * ny;
  const R = new Float32Array(n).fill(-1);
  // Nodes in order of distance from the pin, in buckets a quarter cell wide. A corner of the
  // point one cell back toward the pin can still be a node not yet done (or the node itself),
  // so only corners already done are read.
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

  // Bilinear R one cell (or, across a hole, a few cells) back toward the pin along the ray from
  // node k, from the corners already done; null if none within `maxSteps` cells.
  const inward = (k) => {
    const r = rad[k], x = x0 + (k % nx) * cell, y = y0 + Math.floor(k / nx) * cell;
    for (let m = 1; m <= maxSteps && m * cell < r; m++) {
      const s = 1 - (m * cell) / r;
      const fx = (x * s - x0) / cell, fy = (y * s - y0) / cell;
      const i = Math.floor(fx), j = Math.floor(fy), a = fx - i, b = fy - j;
      let sum = 0, den = 0;
      for (const [di, dj, w] of [[0, 0, (1 - a) * (1 - b)], [1, 0, a * (1 - b)], [0, 1, (1 - a) * b], [1, 1, a * b]]) {
        const ii = i + di, jj = j + dj;
        if (ii < 0 || jj < 0 || ii >= nx || jj >= ny || w <= 0) continue;
        const v = R[jj * nx + ii];
        if (known(v)) { sum += w * v; den += w; }
      }
      if (den > 0) return [sum / den, m];
    }
    return null;
  };

  for (const k of order) {
    const t = minutes[k];
    if (!known(t) || rad[k] <= cell) { R[k] = t; continue; }
    const p = inward(k);
    // Rising (or level): its own minutes. In a pocket's shadow: keep climbing.
    R[k] = p ? (t >= p[0] ? t : p[0] + minSlope * cell * p[1]) : t;
  }
  const out = createField({ origin: f.origin, cell, nx, ny, x0, y0, minutes: R });
  if (f.filled) out.filled = f.filled;
  return out;
}

// Each known place's value becomes the highest, over every path from it to the edge of the known
// ground, of the lowest time along that path (a priority flood from the edge), but only where that
// is at least `minDrop` below its own minutes. The edge is known ground next to the grid's edge,
// to unknown ground that reaches the grid's edge, or to "beyond" ground (no time within the
// tables' limit). Unknown holes and beyond ground are never crossed, and keep their sentinels.
export function cutPeaks(f, { minDrop = CUT_MIN_DROP } = {}) {
  const { nx, ny, minutes } = f;
  const n = nx * ny;
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
  const exit = (q) => outside[q] || minutes[q] >= BEYOND;
  for (let k = 0; k < n; k++) {
    if (!known(minutes[k])) continue;
    const i = k % nx, j = (k - i) / nx;
    const edge = i === 0 || j === 0 || i === nx - 1 || j === ny - 1 ||
      exit(k - 1) || exit(k + 1) || exit(k - nx) || exit(k + nx);
    if (edge) { seen[k] = 1; push(minutes[k], k); }
  }
  while (hv.length) {
    const [v, k] = pop();
    const i = k % nx, j = (k - i) / nx;
    for (const q of [i > 0 ? k - 1 : -1, i + 1 < nx ? k + 1 : -1, j > 0 ? k - nx : -1, j + 1 < ny ? k + nx : -1]) {
      if (q < 0 || seen[q] || !known(minutes[q])) continue;
      seen[q] = 1;
      const c = Math.min(minutes[q], v);
      if (minutes[q] - c >= minDrop) cut[q] = c;
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
    if (known(t) && t <= maxMinutes && known(R.minutes[k]) && !(f.uncertain && f.uncertain[k])) r.push(R.minutes[k]);
  }
  if (!r.length) return maxMinutes;
  r.sort((a, b) => a - b);
  return Math.min(cap * maxMinutes, Math.max(maxMinutes, r[Math.min(r.length - 1, Math.floor(share * r.length))]));
}

export function bentRings(f, warp, levels, { bearings = 720, step = 25 } = {}) {
  const lv = [...levels].sort((a, b) => a - b);
  const xMin = f.x0, xMax = f.x0 + (f.nx - 1) * f.cell, yMin = f.y0, yMax = f.y0 + (f.ny - 1) * f.cell;
  const rMax = Math.hypot(Math.max(-xMin, xMax), Math.max(-yMin, yMax));
  const pts = lv.map(() => new Array(bearings).fill(null));
  for (let b = 0; b < bearings; b++) {
    const a = (2 * Math.PI * b) / bearings, c = Math.cos(a), s = Math.sin(a);
    let next = 0, r0 = 0, t0 = 0;
    for (let r = step; r <= rMax && next < lv.length; r += step) {
      const x = r * c, y = r * s;
      if (x < xMin || x > xMax || y < yMin || y > yMax) break;      // off the grid
      const t1 = sampleField(f, x, y);
      if (!known(t1)) continue;          // a hole or beyond: step over it, keep the last known
      while (next < lv.length && t1 >= lv[next]) {
        const T = lv[next];
        // Interpolated between neighbouring samples; across a hole, at the first known one.
        const rr = r - r0 <= step + 1e-9 && t1 > t0 ? r0 + ((T - t0) / (t1 - t0)) * (r - r0) : r;
        const p = warp.target(rr * c, rr * s);
        pts[next][b] = p ? [p[0], p[1], rr * c, rr * s] : null;
        next++;
      }
      r0 = r; t0 = t1;
    }
  }
  return lv.map((T, i) => {
    const P = pts[i], out = [], src = [];
    let open = false;
    for (let b = 0; b <= bearings; b++) {
      const p = P[b % bearings];
      if (!p) { if (open) { out.push(NaN, NaN); src.push(NaN, NaN); open = false; } continue; }
      out.push(p[0], p[1]); src.push(p[2], p[3]);
      open = true;
    }
    if (open) { out.push(NaN, NaN); src.push(NaN, NaN); }
    return { minutes: T, xy: Float32Array.from(out), src: Float32Array.from(src) };
  });
}

// Radius by places reached (`scale: "ground"`, the page's default for squash): radius = minutes
// inside `inner`; beyond, inner × √(places within t ÷ places within inner), counting the true
// minutes of known ground that isn't filled in or uncertain (as the colour bar does). Where places grow as
// the square of the minutes (walking at one speed), it is the identity.
export function groundScale(f, inner = 15) {
  const ts = [];
  for (let k = 0; k < f.minutes.length; k++) {
    const t = f.minutes[k];
    if (known(t) && !(f.filled && f.filled[k]) && !(f.uncertain && f.uncertain[k])) ts.push(t);
  }
  ts.sort((a, b) => a - b);
  const count = (t) => { let lo = 0, hi = ts.length; while (lo < hi) { const m = (lo + hi) >> 1; if (ts[m] <= t) lo = m + 1; else hi = m; } return lo; };
  const a0 = Math.max(1, count(inner));
  return (t) => (t <= inner ? t : inner * Math.sqrt(count(t) / a0));
}

// `frame`: the radius the view is framed on: the reach for `scale: "minutes"`; with the ground
// scale, the median radius at which places two thirds of maxMinutes away are drawn (the outer
// rings sit far out: zoom out for them). `spread`: directions by reach (warp.js angularSpread).
// `inner`: the ground scale's middle, drawn at its own minutes (15 by default).
export function squashLayout(f, maxMinutes, { minSlope, spread = 0, scale = "minutes", inner = 15 } = {}) {
  const unsure = f.uncertain || null;
  const cut = cutPeaks(geometryField(f));
  let R = squashField(cut, minSlope > 0 ? { minSlope } : {});
  let reach = squashReach(f, R, maxMinutes), frame = reach;
  if (scale === "ground") {
    const g = groundScale(f, inner);
    const m = new Float32Array(R.minutes.length);
    for (let k = 0; k < m.length; k++) m[k] = known(R.minutes[k]) ? g(R.minutes[k]) : R.minutes[k];
    R = createField({ ...R, minutes: m });
    if (f.filled) R.filled = f.filled;
    reach = g(reach);
    const T = Math.round((2 * maxMinutes) / 3), at = [];
    for (let k = 0; k < m.length; k++) {
      if (Math.abs(f.minutes[k] - T) <= 0.5 && !(f.filled && f.filled[k]) && !(unsure && unsure[k]) && known(m[k])) at.push(m[k]);
    }
    at.sort((a, b) => a - b);
    frame = Math.min(reach, at.length ? at[at.length >> 1] : g(T));
  }
  R.uncertain = unsure;
  const angles = spread > 0 ? angularSpread(R, reach, { spread }) : null;
  const ringMinutes = Float32Array.from(f.minutes);
  for (let k = 0; k < ringMinutes.length; k++) {
    if (cut.minutes[k] !== f.minutes[k] || (unsure && unsure[k])) ringMinutes[k] = NO_DATA;
  }
  const ringSource = createField({ ...f, minutes: ringMinutes });
  return { R, reach, frame, cut, ringSource, spec: { kind: "polar", field: R, maxMinutes: reach, angles } };
}

// One flag per node: squash's cut patches (ground lowered by cutPeaks), the nodes its rings step
// over. Uncertain patches aren't flagged: every check leaves them out already.
export function cutMask(f, sq) {
  const m = new Uint8Array(f.minutes.length), unsure = f.uncertain || null;
  for (let k = 0; k < m.length; k++) m[k] = sq.cut.minutes[k] !== f.minutes[k] && !(unsure && unsure[k]) ? 1 : 0;
  return m;
}
