// Pure maths for the time map. The fragment shader in app.js mirrors inverseRadius and
// lutSample exactly; the overlay, the labels and the tests use these functions.
//
// Terms, all per angle θ around the pin, in screen pixels:
//   S[i]  source radius: where the i-th travel-time line really is on the ordinary map
//   T[i]  target radius: the circle it becomes on the time map
//   E[i]  what is drawn now: mix(S[i], T[i], u), u = 0 ordinary map … 1 time map
//   sup   the support radius: past it the map is untouched

export const LUT_SIZE = 2048;
export const MAX_RINGS = 4;
export const MAX_MINUTES = 60;
export const SUPPORT_PAD = 128;
export const MAX_TARGET_FRAC = 0.42;   // the outer circle never exceeds 42% of the short side
export const FIT_FRAC = 0.35;          // framing puts the outer line at 35% of the short side

// Evenly spaced, so the legend can say "each ring is N more minutes".
export const RING_TIMES = {
  walking: [5, 10, 15, 20],
  cycling: [5, 10, 15, 20],
  driving: [10, 20, 30, 40],
};

// The zoom each answer is computed at. Radii scale by 2^(zoom − refZoom) on screen.
export const REF_ZOOM = { walking: 14, cycling: 12, driving: 11 };

export const MODE_WORDS = {
  walking: { verb: "walk", ing: "walking" },
  cycling: { verb: "cycle", ing: "cycling" },
  driving: { verb: "drive", ing: "driving" },
};

export function answerKey(lon, lat, mode) {
  return `${lon.toFixed(5)},${lat.toFixed(5)},${mode}`;
}

export function scaleFor(refZoom, zoom) {
  return Math.pow(2, zoom - refZoom);
}

// Angle → position in [0, 1). Same convention as the server (y-down canvas).
function lutT(theta) {
  return (theta + Math.PI) / (2 * Math.PI);
}

// Linear interpolation between neighbouring angular bins, wrapping at the seam.
// Bin i holds the value for the centre of [i, i+1) / N, as the server builds it.
export function lutSample(row, theta) {
  const n = row.length;
  const f = lutT(theta) * n - 0.5;
  const i = Math.floor(f);
  const a = f - i;
  const i0 = ((i % n) + n) % n;
  const i1 = (i0 + 1) % n;
  return row[i0] * (1 - a) + row[i1] * a;
}

// Distance from (px, py) along θ to the viewport padded by SUPPORT_PAD, but never less
// than `floor`, so the blend back to the ordinary map always has room.
export function supportRadius(px, py, theta, w, h, floor) {
  const dx = Math.cos(theta), dy = Math.sin(theta);
  const tx = dx > 1e-9 ? (w + SUPPORT_PAD - px) / dx : dx < -1e-9 ? (-SUPPORT_PAD - px) / dx : Infinity;
  const ty = dy > 1e-9 ? (h + SUPPORT_PAD - py) / dy : dy < -1e-9 ? (-SUPPORT_PAD - py) / dy : Infinity;
  const t = Math.max(0, Math.min(tx, ty));
  return Math.max(t, floor);
}

export function supportFloor(sOuter, tOuter) {
  return Math.max(sOuter, tOuter) + SUPPORT_PAD;
}

export function median(row) {
  const sorted = Float64Array.from(row).sort();
  return sorted[Math.floor(sorted.length / 2)];
}

// Circle radii for the time map: the outer circle is the typical size of the outer
// travel-time line, capped at MAX_TARGET_FRAC of the short side; the others scale by minutes.
export function targetRadii(medianOuterPx, minutes, w, h) {
  const outer = Math.min(MAX_TARGET_FRAC * Math.min(w, h), medianOuterPx);
  const last = minutes[minutes.length - 1];
  return minutes.map((m) => outer * m / last);
}

export function effectiveTargets(S, T, u) {
  return S.map((s, i) => s + (T[i] - s) * u);
}

// Screen radius → ordinary-map radius. The shader runs the same steps per pixel.
export function inverseRadius(rt, S, E, sup) {
  const k = S.length;
  const eps = 1e-6;
  if (rt <= E[0]) return S[0] * rt / Math.max(E[0], eps);
  for (let i = 1; i < k; i++) {
    if (rt <= E[i]) {
      const a = (rt - E[i - 1]) / Math.max(E[i] - E[i - 1], eps);
      return S[i - 1] + (S[i] - S[i - 1]) * a;
    }
  }
  if (rt < sup) {
    const a = (rt - E[k - 1]) / Math.max(sup - E[k - 1], eps);
    return S[k - 1] + (sup - S[k - 1]) * a;
  }
  return rt;
}

// Ordinary-map radius → screen radius (used for labels).
export function forwardRadius(r, S, E, sup) {
  const k = S.length;
  const eps = 1e-6;
  if (r <= S[0]) return E[0] * r / Math.max(S[0], eps);
  for (let i = 1; i < k; i++) {
    if (r <= S[i]) {
      const a = (r - S[i - 1]) / Math.max(S[i] - S[i - 1], eps);
      return E[i - 1] + (E[i] - E[i - 1]) * a;
    }
  }
  if (r < sup) {
    const a = (r - S[k - 1]) / Math.max(sup - S[k - 1], eps);
    return E[k - 1] + (sup - E[k - 1]) * a;
  }
  return r;
}

// The zoom that puts the outer line (median radius `medianOuterRef` at refZoom) at
// FIT_FRAC of the short side.
export function fitZoom(medianOuterRef, refZoom, w, h) {
  const want = FIT_FRAC * Math.min(w, h);
  const z = refZoom + Math.log2(want / Math.max(medianOuterRef, 1));
  return Math.max(4, Math.min(17, z));
}

function listMinutes(minutes) {
  if (minutes.length === 1) return `${minutes[0]}`;
  return `${minutes.slice(0, -1).join(", ")} and ${minutes[minutes.length - 1]}`;
}

function evenStep(minutes) {
  const step = minutes[0];
  return minutes.every((m, i) => m === step * (i + 1)) ? step : null;
}

export function legendSentence(mode, minutes, bent) {
  const w = MODE_WORDS[mode] || MODE_WORDS.walking;
  if (!bent) {
    return `The lines show how far you can ${w.verb} from the pin in ${listMinutes(minutes)} minutes.`;
  }
  const step = evenStep(minutes);
  return step
    ? `Distance from the pin now means travel time. Each ring is ${step} more minutes ${w.ing}.`
    : `Distance from the pin now means travel time. The rings are ${listMinutes(minutes)} minutes ${w.ing}.`;
}

// A plain sentence when a warp answer can't be drawn, otherwise null.
export function validLut(p) {
  const bad = "This answer can't be drawn. Please try again.";
  if (!p || !Array.isArray(p.contour_minutes) || !Array.isArray(p.source_radii)) return bad;
  const mins = p.contour_minutes;
  if (mins.length < 1 || mins.length > MAX_RINGS || mins.some((m) => !Number.isInteger(m) || m < 1 || m > MAX_MINUTES)) {
    return `The map draws at most ${MAX_RINGS} rings, each up to ${MAX_MINUTES} minutes.`;
  }
  if (p.source_radii.length !== mins.length) return bad;
  for (const row of p.source_radii) {
    if (!Array.isArray(row) || row.length !== LUT_SIZE) return bad;
    if (row.some((v) => typeof v !== "number" || !Number.isFinite(v) || v < 0)) return bad;
  }
  return null;
}
