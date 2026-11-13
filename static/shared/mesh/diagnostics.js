// Fold and stretch, and the radius check, for Direction 4 of #5 (W-02). Pure.
//   foldDiagnostics  W-01's measure (moved here from the lab): folded share of the outer
//                    ring (a rasterised union), stacked share, median local stretch
//   directionFour    the two numeric checks at 200 m sampling (and 800 m to show how much
//                    of the stretch is whole-minute noise), against 10% and 3:1
//   ringRadius       |target| = minutes at random known nodes: an identity check of the JS
//                    warp (a polar target is a unit direction times minutes, so it holds by
//                    construction). It is not evidence for the drawn radius: that is G3, which
//                    reads dots back from pixels
import { createWarp } from "./warp.js";

export const LIMITS = { folded: 0.10, stretch: 3 };

// Fold and stretch on a polar field, as Direction 4 of #5 measures them:
//   foldedShare    the share of the area inside the outer ring covered by folded sheets on the
//                  bent map: the union of the folded cells' images, rasterised, so overlapping
//                  sheets count once
//   foldedSheets   the same without the union: total area of folded cells' images (can exceed
//                  foldedShare, even 1, where sheets stack)
//   medianStretch  the median over field cells inside the ring (folded ones included) of the
//                  largest local scale over the smallest, sampled per source cell, unweighted
//   skipped        cells left out: a corner unknown, past the ring, or filled in (water)
//   excludeWithin    metres: leave out cells whose centre is this close to the pin (to show how
//                   much of the folding sits in the walking near field)
export function foldDiagnostics(spec, { step = 2, raster = 600, skipFilled = true, excludeWithin = 0 } = {}) {
  if (spec.kind !== "polar") return null;
  const f = spec.field, max = spec.maxMinutes ?? 45;
  const filled = skipFilled && f.filled ? f.filled : null;
  let cells = 0, skipped = 0;
  const warp = createWarp(spec);
  const px = (2 * max) / raster;                        // minutes per raster pixel
  const covered = new Uint8Array(raster * raster);
  let sheets = 0;
  const stretch = [];
  const fill = (a, b, c) => {
    // Mark raster pixels whose centre is inside triangle abc (layout units).
    const x0 = Math.max(0, Math.floor((Math.min(a[0], b[0], c[0]) + max) / px));
    const x1 = Math.min(raster - 1, Math.ceil((Math.max(a[0], b[0], c[0]) + max) / px));
    const y0 = Math.max(0, Math.floor((Math.min(a[1], b[1], c[1]) + max) / px));
    const y1 = Math.min(raster - 1, Math.ceil((Math.max(a[1], b[1], c[1]) + max) / px));
    const side = (p, q, x, y) => (q[0] - p[0]) * (y - p[1]) - (q[1] - p[1]) * (x - p[0]);
    for (let j = y0; j <= y1; j++) {
      const y = -max + (j + 0.5) * px;
      for (let i = x0; i <= x1; i++) {
        const x = -max + (i + 0.5) * px;
        const d1 = side(a, b, x, y), d2 = side(b, c, x, y), d3 = side(c, a, x, y);
        if ((d1 >= 0 && d2 >= 0 && d3 >= 0) || (d1 <= 0 && d2 <= 0 && d3 <= 0)) covered[j * raster + i] = 1;
      }
    }
  };
  const h = f.cell * step;
  for (let j = 0; j + step < f.ny; j += step) {
    for (let i = 0; i + step < f.nx; i += step) {
      const x = f.x0 + i * f.cell, y = f.y0 + j * f.cell;
      const q = [warp.target(x, y), warp.target(x + h, y), warp.target(x + h, y + h), warp.target(x, y + h)];
      if (excludeWithin > 0 && Math.hypot(x + h / 2, y + h / 2) < excludeWithin) continue;
      cells++;
      if (q.some((t) => !t) || (filled && (filled[j * f.nx + i] || filled[j * f.nx + i + step] ||
          filled[(j + step) * f.nx + i] || filled[(j + step) * f.nx + i + step]))) { skipped++; continue; }
      const jac = warp.jacobian(x + h / 2, y + h / 2, h / 2);
      if (jac) stretch.push(jac.stretch);
      const area2 = (t0, t1, t2) => (t1[0] - t0[0]) * (t2[1] - t0[1]) - (t1[1] - t0[1]) * (t2[0] - t0[0]);
      const a1 = area2(q[0], q[1], q[2]), a2 = area2(q[0], q[2], q[3]);
      if (a1 < 0) { fill(q[0], q[1], q[2]); sheets += -a1 / 2; }
      if (a2 < 0) { fill(q[0], q[2], q[3]); sheets += -a2 / 2; }
    }
  }
  let inRing = 0, foldedIn = 0;
  for (let j = 0; j < raster; j++) for (let i = 0; i < raster; i++) {
    const x = -max + (i + 0.5) * px, y = -max + (j + 0.5) * px;
    if (x * x + y * y > max * max) continue;
    inRing++;
    if (covered[j * raster + i]) foldedIn++;
  }
  stretch.sort((a, b) => a - b);
  return {
    foldedShare: foldedIn / inRing,
    foldedSheets: sheets / (Math.PI * max * max),
    medianStretch: stretch.length ? stretch[Math.floor(stretch.length / 2)] : null,
    cells, skipped,
  };
}


export function directionFour(spec, { raster = 600 } = {}) {
  const cell = spec.field.cell;
  const at200 = foldDiagnostics(spec, { step: Math.max(1, Math.round(200 / cell)), raster });
  const at800 = foldDiagnostics(spec, { step: Math.max(1, Math.round(800 / cell)), raster });
  const outside2km = foldDiagnostics(spec, { step: Math.max(1, Math.round(200 / cell)), raster, excludeWithin: 2000 });
  return {
    ring: spec.maxMinutes, at200, at800, outside2km,
    passFolded: at200.foldedShare <= LIMITS.folded,
    passStretch: at200.medianStretch !== null && at200.medianStretch <= LIMITS.stretch,
  };
}

function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// n random nodes that hold a time within the ring (not filled in): the largest gap between
// the drawn radius (|target|, layout minutes) and the field's minutes.
export function ringRadius(spec, { n = 1000, seed = 20261113 } = {}) {
  const f = spec.field, warp = createWarp(spec), max = spec.maxMinutes ?? Infinity, rand = rng(seed);
  let worst = 0, got = 0;
  for (let tries = 0; got < n && tries < 200 * n; tries++) {
    const k = Math.floor(rand() * f.nx * f.ny), t = f.minutes[k];
    if (!(t > 0) || t > max || (f.filled && f.filled[k])) continue;
    const x = f.x0 + (k % f.nx) * f.cell, y = f.y0 + Math.floor(k / f.nx) * f.cell;
    const p = warp.target(x, y);
    if (!p) continue;
    worst = Math.max(worst, Math.abs(Math.hypot(p[0], p[1]) - t));
    got++;
  }
  return { n: got, maxError: worst };
}
