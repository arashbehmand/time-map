// Rings and band colours, shared by both views of the warp page (#5, W-02). Pure.
//
//   ringPlan(outer)   the ring minutes up to the outer ring: every 5 minutes up to 30, every
//                     10 up to 60, every 15 beyond, and always the outer ring itself; a ring is
//                     "major" (labelled) on a quarter hour and at the outer ring
//   bandColor(i, n)   band i of n, quickest darkest: one hue, so more bands only add steps

export function ringPlan(outer) {
  const out = [];
  let m = 0;
  for (;;) {
    m += m < 30 ? 5 : m < 60 ? 10 : 15;
    if (m >= outer) break;
    out.push(m);
  }
  out.push(outer);
  return out.map((minutes) => ({ minutes, major: minutes % 15 === 0 || minutes === outer }));
}

// Blue ramp, dark (quick) to light (slow), sampled for any number of bands.
const RAMP = [[31, 78, 156], [61, 127, 217], [127, 178, 234], [196, 222, 246]];

export function bandRgb(i, n) {
  const t = n <= 1 ? 0 : i / (n - 1);
  const f = t * (RAMP.length - 1), k = Math.min(RAMP.length - 2, Math.floor(f)), a = f - k;
  return RAMP[k].map((c, j) => Math.round(c + (RAMP[k + 1][j] - c) * a));
}

export function bandColor(i, n) {
  const [r, g, b] = bandRgb(i, n);
  return `#${((1 << 24) | (r << 16) | (g << 8) | b).toString(16).slice(1)}`;
}
