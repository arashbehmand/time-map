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

export function createWarp(spec) {
  const kind = spec ? spec.kind : "none";
  let target;
  if (kind === "polar") {
    const f = spec.field, max = spec.maxMinutes ?? Infinity;
    target = (x, y) => {
      const t = sampleField(f, x, y);
      if (t === NO_DATA || t > max) return null;
      const r = Math.hypot(x, y);
      return r > DIR_EPS ? [(x / r) * t, (y / r) * t] : [0, 0];
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
      const r = Math.hypot(x, y);
      return r > DIR_EPS ? [(x / r) * ringMinutes, (y / r) * ringMinutes, 0.5] : [0, 0, 0.5];
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

  return { kind, spec, frame, target, placement, screenOfLocal, toScreen, jacobian, screenJacobian };
}

// Per-vertex placements for the CPU path (C): three floats per point (x, y, flag).
export function targetsFor(warp, xy, out = new Float32Array((xy.length / 2) * 3)) {
  for (let i = 0, n = xy.length / 2; i < n; i++) {
    const p = warp.placement(xy[2 * i], xy[2 * i + 1]);
    out[3 * i] = p[0]; out[3 * i + 1] = p[1]; out[3 * i + 2] = p[2];
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
// Unknown: onto the outer ring along the bearing, fading (flag 0.5), as placement() does.
vec3 polarUnknown(vec2 p) {
  if (u_warpGrid.w >= ${NO_RING.toExponential()}) return vec3(0.0);
  float r = length(p);
  return vec3(r > ${DIR_EPS.toExponential()} ? (p / r) * u_warpGrid.w : vec2(0.0), 0.5);
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
  float r = length(p);
  return vec3(r > ${DIR_EPS.toExponential()} ? (p / r) * t : vec2(0.0), 1.0);
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
