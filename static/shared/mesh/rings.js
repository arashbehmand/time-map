// Rings and band colours, shared by both views of the warp page (#5, W-02). Pure.
//
//   ringPlan(outer)   the ring minutes up to the outer ring: every 5 minutes up to 30, then
//                     every 10 up to 60 when the outer ring is a multiple of 10 (else every 15),
//                     every 15 beyond 60; it always ends on the outer ring. A ring is "major"
//                     (labelled) on a quarter hour and at the outer ring
//   spacing(plan)     the steps in order, [{from, step}], for the legend's sentence
//   bandColor(i, n)   band i of n, quickest darkest: one hue, so more bands only add steps

export function ringPlan(outer) {
  const out = [];
  const mid = outer % 10 === 0 ? 10 : 15;
  let m = 0;
  for (;;) {
    m += m < 30 ? 5 : m < 60 ? mid : 15;
    if (m >= outer) break;
    out.push(m);
  }
  out.push(outer);
  return out.map((minutes) => ({ minutes, major: minutes % 15 === 0 || minutes === outer }));
}

export function spacing(plan) {
  const out = [];
  let prev = 0;
  for (const { minutes } of plan) {
    const step = minutes - prev;
    if (!out.length || out[out.length - 1].step !== step) out.push({ from: prev, step });
    prev = minutes;
  }
  return out;
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
