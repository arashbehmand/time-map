// The warp: where a place is drawn, as a function of the morph u (0 ordinary map … 1 bent).
//
//   screen = mix(geo(p), pin + k · target(p), u)        p = local metres (frame.js)
//
// target() is the layout. Two kinds:
//   polar  target = bearing(p) · minutes(p): keeps the compass bearing from the origin and
//          puts the place at a distance proportional to its travel time (exact at every vertex).
//   grid   target = bilinear target positions from a grid: any layout, e.g. the London
//          map's (#6), rasterised into a grid of positions.
// Where target() is unknown it fades out as u → 1, and it still moves, so a triangle or a
// segment with one known and one unknown corner stays short instead of streaking:
//   polar  no data, or past maxMinutes: it moves onto the outer ring along its own bearing
//   grid   no data (outside the grid, or a GRID_NO_DATA node): it stays on the ordinary map
// placement() gives the position used, with a flag: 1 known, 0.5 fading to an anchor,
// 0 fading where it is.
//
// toScreen() is the "lon/lat → screen" function #6 reuses. WARP_GLSL is the same arithmetic
// for the vertex shaders; both use the constants below, and the tests hold them together.
import { GRID_NO_DATA, NO_DATA, sampleField, sampleGrid } from "./field.js";
import { geoScreen, localFrame } from "./frame.js";

export const DIR_EPS = 1e-6;    // metres: inside this radius the bearing is undefined (target 0)
export const NO_RING = 1e8;     // maxMinutes at or above this: no outer ring to fade onto
export const JAC_H = 25;        // metres: the finite-difference step for the local scale
// Directions by reach (see angularSpread): a table of bent bearings, ANGLE_BINS + 1 per
// time level, ANGLE_LEVELS levels from 0 to the outer minute.
export const ANGLE_BINS = 36;
export const ANGLE_LEVELS = 10;
export const ANGLE_VEC4 = Math.ceil(((ANGLE_BINS + 1) * ANGLE_LEVELS) / 4);

// Scale-aware style: how the bent map's local scale changes a line (lineEmphasis) and a
// folded fill. All 1 at u = 0, so the ordinary map is unchanged. (Each class also recedes
// to its own strength on the bent map: `bent` in the schema's STYLES.)
export const STYLE = {
  WF_MIN: 0.35,                // line width × √(scale across the line), between WF_MIN and 1:
                               // thinner where the map squeezes it sideways, never wider
                               // (where the bend magnifies, wider lines only add weight)
  FOLD_FADE: 0.4,              // lines where the map is folded
  FILL_FOLD_FADE: 0.45,        // folded parks and water
};

export function createWarp(spec) {
  const kind = spec ? spec.kind : "none";
  let target;
  const dir = kind === "polar" ? polarDirection(spec.angles) : null;
  if (kind === "polar") {
    const f = spec.field, max = spec.maxMinutes ?? Infinity;
    target = (x, y) => {
      const t = sampleField(f, x, y);
      if (t === NO_DATA || t > max) return null;
      const [dx, dy] = dir(x, y, t);
      return [dx * t, dy * t];
    };
  } else if (kind === "grid") {
    const g = spec.grid;
    target = (x, y) => sampleGrid(g, x, y);
  } else {
    target = () => null;
  }
  const ringMinutes = kind === "polar" && spec.maxMinutes < NO_RING ? spec.maxMinutes : null;
  // [x, y, flag] in layout units (see the header).
  function placement(x, y) {
    const t = target(x, y);
    if (t) return [t[0], t[1], 1];
    if (ringMinutes !== null) {
      const [dx, dy] = dir(x, y, ringMinutes);
      return [dx * ringMinutes, dy * ringMinutes, 0.5];
    }
    return [0, 0, 0];
  }
  const source = kind === "polar" ? spec.field : kind === "grid" ? spec.grid : null;
  const frame = source ? localFrame(source.origin) : spec && spec.origin ? localFrame(spec.origin) : null;

  // Local metres → {x, y, alpha} in CSS px.
  function screenOfLocal(x, y, cam, u) {
    const [gx, gy] = geoScreen(x, y, cam);
    const [tx, ty, flag] = placement(x, y);
    if (flag === 0) return { x: gx, y: gy, alpha: 1 - u };
    const bx = cam.pin[0] + cam.k * tx, by = cam.pin[1] + cam.k * ty;
    return { x: gx + (bx - gx) * u, y: gy + (by - gy) * u, alpha: flag === 1 ? 1 : 1 - u };
  }

  // lon/lat → {x, y, alpha} in CSS px: the function the London map (#6) calls per vertex.
  function toScreen(lon, lat, cam, u) {
    const [x, y] = frame.toLocal(lon, lat);
    return screenOfLocal(x, y, cam, u);
  }

  // Local scale of target() around p, layout units per metre. det ≤ 0 means the map is
  // folded there; stretch is the largest local scale over the smallest (Infinity at a fold
  // line). null where target() is unknown at p or a neighbour.
  function jacobian(x, y, h = 25) {
    const px = target(x + h, y), mx = target(x - h, y), py = target(x, y + h), my = target(x, y - h);
    if (!px || !mx || !py || !my) return null;
    const a = (px[0] - mx[0]) / (2 * h), c = (px[1] - mx[1]) / (2 * h);
    const b = (py[0] - my[0]) / (2 * h), d = (py[1] - my[1]) / (2 * h);
    const det = a * d - b * c;
    const s = a * a + b * b + c * c + d * d;
    const disc = Math.sqrt(Math.max(0, s * s - 4 * det * det));
    const big = Math.sqrt((s + disc) / 2), small = Math.sqrt(Math.max(0, (s - disc) / 2));
    return { det, stretch: small > 1e-12 ? big / small : Infinity };
  }

  // The same for what is on screen now: (1 − u)·ppm·I + u·k·J. A place is folded on screen
  // when this det ≤ 0, which at small u it is not yet. null where the target is unknown.
  function screenJacobian(x, y, cam, u, h = 25) {
    const j = jacobian(x, y, h);
    if (!j) return null;
    const px = target(x + h, y), mx = target(x - h, y), py = target(x, y + h), my = target(x, y - h);
    const g = (1 - u) * cam.ppm, k = u * cam.k;
    const a = g + (k * (px[0] - mx[0])) / (2 * h), c = (k * (px[1] - mx[1])) / (2 * h);
    const b = (k * (py[0] - my[0])) / (2 * h), d = g + (k * (py[1] - my[1])) / (2 * h);
    const det = a * d - b * c;
    const s = a * a + b * b + c * c + d * d;
    const disc = Math.sqrt(Math.max(0, s * s - 4 * det * det));
    const big = Math.sqrt((s + disc) / 2), small = Math.sqrt(Math.max(0, (s - disc) / 2));
    return { det, stretch: small > 1e-12 ? big / small : Infinity };
  }

  // ∂target/∂x and ∂target/∂y at p as [a, c, b, d] (layout units per metre), or null where
  // the target is unknown nearby. Mirrors fieldJacobian() in WARP_GLSL.
  function targetJacobian(x, y, h = JAC_H) {
    const px = target(x + h, y), mx = target(x - h, y), py = target(x, y + h), my = target(x, y - h);
    if (!px || !mx || !py || !my) return null;
    return [(px[0] - mx[0]) / (2 * h), (px[1] - mx[1]) / (2 * h), (py[0] - my[0]) / (2 * h), (py[1] - my[1]) / (2 * h)];
  }

  return { kind, spec, frame, target, placement, screenOfLocal, toScreen, jacobian, screenJacobian, targetJacobian };
}

// The bent direction of a place at local (x, y) reached in t minutes, as a unit vector (zero
// at the origin). Without a table, its own bearing. With one, the bearing is remapped for
// t's ring: bilinear in (bearing, t) over the table, as polarDir() in WARP_GLSL.
export function polarDirection(angles) {
  return (x, y, t) => {
    const r = Math.hypot(x, y);
    if (r <= DIR_EPS) return [0, 0];
    if (!angles) return [x / r, y / r];
    const a = remapBearing(angles, Math.atan2(y, x), t);
    return [Math.cos(a), Math.sin(a)];
  };
}

export function remapBearing({ a, tMax }, th, t) {
  const N = ANGLE_BINS;
  const fx = ((th + Math.PI) / (2 * Math.PI)) * N;
  const i = Math.min(N - 1, Math.max(0, Math.floor(fx)));
  const ax = fx - i;
  const fy = Math.min(1, Math.max(0, t / tMax)) * (ANGLE_LEVELS - 1);
  const j = Math.min(ANGLE_LEVELS - 2, Math.max(0, Math.floor(fy)));
  const ay = fy - j;
  const r0 = j * (N + 1), r1 = r0 + N + 1;
  const a0 = a[r0 + i] + (a[r0 + i + 1] - a[r0 + i]) * ax;
  const a1 = a[r1 + i] + (a[r1 + i + 1] - a[r1 + i]) * ax;
  return a0 + (a1 - a0) * ay;
}

// Directions by reach: on each ring of minutes, every direction gets the share of the 360°
// that it holds of the area reachable within that time. Within a short walk the reach is a
// disc and bearings stay as they are; further out, directions a fast line reaches into open
// up and the rest close. Order is kept (north-west stays between north and west), and at each
// ring the remap only stretches angles, so it adds no folds; radius is still minutes.
//   spread  0 keeps every bearing, 1 is the area share alone (mixed linearly between)
//   floor   no direction gets less than this × its even share
//   smooth  passes of a [1 2 1] filter over the shares, so the bend has no sharp kinks
// Returns {a, tMax} for createWarp's polar spec (`angles`).
export function angularSpread(field, maxMinutes, { spread = 1, floor = 0.25, smooth = 2 } = {}) {
  const N = ANGLE_BINS, M = ANGLE_LEVELS;
  let tMax = maxMinutes;
  if (!(tMax < NO_RING)) { tMax = 0; for (const t of field.minutes) if (t > tMax) tMax = t; }
  // Area reached by level (cumulative later), per direction.
  const hist = new Float64Array(M * N);
  const cellArea = field.cell * field.cell;
  for (let j = 0; j < field.ny; j++) {
    const y = field.y0 + j * field.cell;
    for (let i = 0; i < field.nx; i++) {
      const t = field.minutes[j * field.nx + i];
      if (t < 0 || t > tMax) continue;
      const x = field.x0 + i * field.cell;
      if (Math.hypot(x, y) <= DIR_EPS) continue;
      const b = Math.min(N - 1, Math.floor(((Math.atan2(y, x) + Math.PI) / (2 * Math.PI)) * N));
      const m = Math.max(1, Math.ceil((t / tMax) * (M - 1)));
      hist[m * N + b] += cellArea;
    }
  }
  const a = new Float32Array((N + 1) * M);
  const w = new Float64Array(N);
  for (let m = 0; m < M; m++) {
    if (m > 0) for (let b = 0; b < N; b++) w[b] += hist[m * N + b];   // cumulative: reached by T_m
    let share = Array.from(w);
    let total = share.reduce((s, v) => s + v, 0);
    if (m === 0 || total <= 0) share = share.map(() => 1 / N);
    else {
      share = share.map((v) => v / total);
      for (let k = 0; k < smooth; k++) share = share.map((v, b) => (share[(b + N - 1) % N] + 2 * v + share[(b + 1) % N]) / 4);
      share = share.map((v) => Math.max(floor / N, (1 - spread) / N + spread * v));
      total = share.reduce((s, v) => s + v, 0);
      share = share.map((v) => v / total);
    }
    // Cumulative angle, then turned so that on average no direction moves.
    const row = new Float64Array(N + 1);
    for (let b = 0; b < N; b++) row[b + 1] = row[b] + 2 * Math.PI * share[b];
    let drift = 0;
    for (let b = 0; b < N; b++) drift += row[b] - (2 * Math.PI * b) / N;
    drift /= N;
    for (let b = 0; b <= N; b++) a[m * (N + 1) + b] = -Math.PI + row[b] - drift;
  }
  return { a, tMax };
}

// How a line segment is drawn on the bent map: its width factor and its alpha. jt is
// targetJacobian() at the segment (or null), (dx, dy) its direction in local metres.
// Mirrors lineEmphasis() in WARP_GLSL.
//   width  √ of the map's scale across the line, relative to the ordinary map, clamped to
//          [WF_MIN, 1]: a line squeezed sideways gets thinner
//   alpha  FOLD_FADE where the map is folded, else 1
export function lineEmphasis(jt, dx, dy, cam, u) {
  if (!jt || (jt[0] === 0 && jt[1] === 0 && jt[2] === 0 && jt[3] === 0)) return { width: 1, alpha: 1 };
  const g = (1 - u) * cam.ppm, k = u * cam.k;
  const A = (g + k * jt[0]) / cam.ppm, C = (k * jt[1]) / cam.ppm;
  const B = (k * jt[2]) / cam.ppm, D = (g + k * jt[3]) / cam.ppm;
  const L = Math.hypot(dx, dy) || 1;
  const nx = -dy / L, ny = dx / L;
  const across = Math.hypot(A * nx + B * ny, C * nx + D * ny);
  const width = Math.min(1, Math.max(STYLE.WF_MIN, Math.sqrt(across)));
  return { width, alpha: A * D - B * C < 0 ? STYLE.FOLD_FADE : 1 };
}

// The same for a station dot: a radius factor from the map's local area scale, smaller
// where the bent map is squeezed, never bigger than on the ordinary map.
export function dotScale(jt, cam, u) {
  if (!jt) return 1;
  const g = (1 - u) * cam.ppm, k = u * cam.k;
  const A = (g + k * jt[0]) / cam.ppm, C = (k * jt[1]) / cam.ppm;
  const B = (k * jt[2]) / cam.ppm, D = (g + k * jt[3]) / cam.ppm;
  return Math.min(1, Math.max(0.6, Math.pow(Math.abs(A * D - B * C), 0.25)));
}

// Per-vertex placements for the CPU path (C): three floats per point (x, y, flag).
export function targetsFor(warp, xy, out = new Float32Array((xy.length / 2) * 3)) {
  for (let i = 0, n = xy.length / 2; i < n; i++) {
    const p = warp.placement(xy[2 * i], xy[2 * i + 1]);
    out[3 * i] = p[0]; out[3 * i + 1] = p[1]; out[3 * i + 2] = p[2];
  }
  return out;
}

// The warp's local scale at each segment's midpoint ([x0, y0, x1, y1] per segment, local
// metres), four floats per segment, zero where unknown: a_jac for the CPU path (C).
export function jacobiansFor(warp, seg, out = new Float32Array(seg.length)) {
  for (let j = 0, n = seg.length / 4; j < n; j++) {
    const t = warp.targetJacobian((seg[4 * j] + seg[4 * j + 2]) / 2, (seg[4 * j + 1] + seg[4 * j + 3]) / 2);
    if (t) out.set(t, 4 * j); else out.fill(0, 4 * j, 4 * j + 4);
  }
  return out;
}

// The shader twin. Include after `#define WARP_POLAR`, `WARP_GRID` or `CPU_TARGETS` (or none,
// for the ordinary map only). Positions are local metres; output is CSS px plus alpha.
export const WARP_GLSL = /* glsl */`
uniform vec2  u_cam;       // camera centre, local metres
uniform float u_ppm;       // CSS px per metre (ordinary map)
uniform vec2  u_view;      // viewport, CSS px
uniform vec2  u_pin;       // the origin on screen, CSS px
uniform float u_k;         // CSS px per layout unit (bent map)
uniform float u_u;         // 0 ordinary map ... 1 bent

vec2 geoScreen(vec2 p) { return (p - u_cam) * u_ppm + 0.5 * u_view; }

#if defined(WARP_POLAR) || defined(WARP_GRID)
uniform highp sampler2D u_warpTex;  // R32F minutes (polar) or RG32F targets (grid), NEAREST
uniform vec4  u_warpGrid;           // x0, y0, cell, maxMinutes (≥ ${NO_RING.toExponential()} means none)
uniform ivec2 u_warpSize;           // nx, ny

#if defined(WARP_POLAR)
uniform bool  u_angleOn;                 // directions by reach (angularSpread) on or off
uniform vec4  u_angles[${ANGLE_VEC4}];             // the table, packed four to a vec4
uniform float u_angleTMax;
float angleAt(int k) { return u_angles[k / 4][k % 4]; }
// Mirrors polarDirection() / remapBearing() in warp.js.
vec2 polarDir(vec2 p, float t) {
  float r = length(p);
  if (r <= ${DIR_EPS.toExponential()}) return vec2(0.0);
  if (!u_angleOn) return p / r;
  float fx = (atan(p.y, p.x) + ${Math.PI.toFixed(8)}) / ${(2 * Math.PI).toFixed(8)} * ${ANGLE_BINS.toFixed(1)};
  int i = clamp(int(floor(fx)), 0, ${ANGLE_BINS - 1});
  float ax = fx - float(i);
  float fy = clamp(t / u_angleTMax, 0.0, 1.0) * ${(ANGLE_LEVELS - 1).toFixed(1)};
  int j = clamp(int(floor(fy)), 0, ${ANGLE_LEVELS - 2});
  float ay = fy - float(j);
  int r0 = j * ${ANGLE_BINS + 1};
  int r1 = r0 + ${ANGLE_BINS + 1};
  float a0 = mix(angleAt(r0 + i), angleAt(r0 + i + 1), ax);
  float a1 = mix(angleAt(r1 + i), angleAt(r1 + i + 1), ax);
  float a = mix(a0, a1, ay);
  return vec2(cos(a), sin(a));
}
// Unknown: onto the outer ring along the bearing, fading (flag 0.5), as placement() does.
vec3 polarUnknown(vec2 p) {
  if (u_warpGrid.w >= ${NO_RING.toExponential()}) return vec3(0.0);
  return vec3(polarDir(p, u_warpGrid.w) * u_warpGrid.w, 0.5);
}
#endif

// Placement in .xy and the flag in .z (1 known, 0.5 fading to an anchor, 0 fading in place).
// Mirrors placement() in warp.js, and sampleField / sampleGrid in field.js.
vec3 fieldTarget(vec2 p) {
  vec2 f = (p - u_warpGrid.xy) / u_warpGrid.z;
  vec2 fl = floor(f);
  ivec2 i = ivec2(fl);
#if defined(WARP_POLAR)
  if (i.x < 0 || i.y < 0 || i.x + 1 >= u_warpSize.x || i.y + 1 >= u_warpSize.y) return polarUnknown(p);
  vec2 a = f - fl;
  float c00 = texelFetch(u_warpTex, i, 0).r;
  float c10 = texelFetch(u_warpTex, i + ivec2(1, 0), 0).r;
  float c01 = texelFetch(u_warpTex, i + ivec2(0, 1), 0).r;
  float c11 = texelFetch(u_warpTex, i + ivec2(1, 1), 0).r;
  if (min(min(c00, c10), min(c01, c11)) < 0.0) return polarUnknown(p);
  float t = mix(mix(c00, c10, a.x), mix(c01, c11, a.x), a.y);
  if (t > u_warpGrid.w) return polarUnknown(p);
  return vec3(polarDir(p, t) * t, 1.0);
#else
  if (i.x < 0 || i.y < 0 || i.x + 1 >= u_warpSize.x || i.y + 1 >= u_warpSize.y) return vec3(0.0);
  vec2 a = f - fl;
  vec2 c00 = texelFetch(u_warpTex, i, 0).rg;
  vec2 c10 = texelFetch(u_warpTex, i + ivec2(1, 0), 0).rg;
  vec2 c01 = texelFetch(u_warpTex, i + ivec2(0, 1), 0).rg;
  vec2 c11 = texelFetch(u_warpTex, i + ivec2(1, 1), 0).rg;
  if (min(min(c00.x, c10.x), min(c01.x, c11.x)) < ${(GRID_NO_DATA / 10).toExponential()}) return vec3(0.0);
  return vec3(mix(mix(c00, c10, a.x), mix(c01, c11, a.x), a.y), 1.0);
#endif
}
#define TARGET(p, t) fieldTarget(p)
#elif defined(CPU_TARGETS)
#define TARGET(p, t) (t)
#else
#define TARGET(p, t) vec3(0.0)
#endif

#if defined(WARP_POLAR) || defined(WARP_GRID)
// Mirrors targetJacobian() in warp.js: (∂t/∂x, ∂t/∂y), or zero where unknown nearby.
vec4 fieldJacobian(vec2 p) {
  const float h = ${JAC_H.toFixed(1)};
  vec3 px = fieldTarget(p + vec2(h, 0.0)), mx = fieldTarget(p - vec2(h, 0.0));
  vec3 py = fieldTarget(p + vec2(0.0, h)), my = fieldTarget(p - vec2(0.0, h));
  if (min(min(px.z, mx.z), min(py.z, my.z)) < 0.75) return vec4(0.0);
  return vec4((px.xy - mx.xy) / (2.0 * h), (py.xy - my.xy) / (2.0 * h));
}
#endif

// Mirrors lineEmphasis() in warp.js: (width factor, alpha) for a segment of direction d
// (local metres) with target Jacobian jt.
vec2 lineEmphasis(vec4 jt, vec2 d) {
  if (jt == vec4(0.0)) return vec2(1.0);
  float g = (1.0 - u_u) * u_ppm, k = u_u * u_k;
  float A = (g + k * jt.x) / u_ppm, C = (k * jt.y) / u_ppm;
  float B = (k * jt.z) / u_ppm, D = (g + k * jt.w) / u_ppm;
  float L = length(d);
  vec2 n = L > 0.0 ? vec2(-d.y, d.x) / L : vec2(0.0, 1.0);
  float across = length(vec2(A * n.x + B * n.y, C * n.x + D * n.y));
  float width = clamp(sqrt(across), ${STYLE.WF_MIN.toFixed(2)}, 1.0);
  return vec2(width, A * D - B * C < 0.0 ? ${STYLE.FOLD_FADE.toFixed(2)} : 1.0);
}

// CSS px in .xy, alpha in .z. Mirrors screenOfLocal in warp.js.
vec3 warpScreen(vec2 p, vec3 target) {
  vec2 g = geoScreen(p);
  if (target.z < 0.25) return vec3(g, 1.0 - u_u);
  return vec3(mix(g, u_pin + u_k * target.xy, u_u), target.z > 0.75 ? 1.0 : 1.0 - u_u);
}
`;

// `#define` lines for a warp kind and back-end.
export function warpDefines(kind, cpuTargets) {
  if (cpuTargets) return "#define CPU_TARGETS\n";
  if (kind === "polar") return "#define WARP_POLAR\n";
  if (kind === "grid") return "#define WARP_GRID\n";
  return "";
}
