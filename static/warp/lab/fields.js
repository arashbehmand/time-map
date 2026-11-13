// SYNTHETIC travel-time fields and layouts for the renderer lab. Pure (the network comes in
// as data), so the node tests build the same fields. None of these are real travel times.
//
//   walk     minutes = distance × detour / walking speed
//   transit  the toy network in stations.json: walk to a station, ride, walk on
//   cliff    walking, with a river 1.5 km north that only one bridge crosses
//   contours the saved King's Cross walking answer (static/warp/example.json), interpolated
//   identity, shear, pull   target-position grids, to show the London map (#6) can use the
//            same renderer with a layout that is not travel time
import { fieldFromFunction, gridFromFunction, NO_DATA, sampleField } from "../../shared/mesh/field.js";
import { localFrame, metresPerUnit, WORLD_PX } from "../../shared/mesh/frame.js";
import { createWarp } from "../../shared/mesh/warp.js";

export const FIELD_CELL = 100;          // metres (D7)
export const FIELD_HALF = 25_000;       // ±25 km
export const GRID_CELL = 500;
export const POLAR_FIELDS = ["walk", "transit", "cliff", "contours"];
export const GRID_FIELDS = ["identity", "shear", "pull"];

const DEFAULT_PARAMS = { walkMetresPerMinute: 80, detour: 1.3, waitMinutes: 4, dwellMinutes: 0.5,
  interchangeMinutes: 3, accessMetres: 2000, egressMetres: 1500 };

export function walkMinutes(metres, p = DEFAULT_PARAMS) {
  return (metres * p.detour) / p.walkMetresPerMinute;
}

// Earliest arrival at every station of the toy network from `origin`, in minutes.
// Dijkstra over (line, stop) nodes: walk to a stop within accessMetres and wait; ride with
// a dwell per stop; change lines at a station of the same name for interchangeMinutes.
export function stationTimes(origin, network) {
  const p = { ...DEFAULT_PARAMS, ...(network.params || {}) };
  const frame = localFrame(origin);
  const nodes = [];
  network.lines.forEach((line, li) => line.stops.forEach(([name, lon, lat], si) => {
    const [x, y] = frame.toLocal(lon, lat);
    nodes.push({ li, si, name, x, y, kmh: line.kmh });
  }));
  const at = (li, si) => nodes.findIndex((n) => n.li === li && n.si === si);
  const dist = new Float64Array(nodes.length).fill(Infinity);
  nodes.forEach((n, i) => {
    const d = Math.hypot(n.x, n.y);
    if (d <= p.accessMetres) dist[i] = walkMinutes(d, p) + p.waitMinutes;
  });
  const done = new Uint8Array(nodes.length);
  for (;;) {
    let u = -1;
    for (let i = 0; i < nodes.length; i++) if (!done[i] && (u < 0 || dist[i] < dist[u])) u = i;
    if (u < 0 || dist[u] === Infinity) break;
    done[u] = 1;
    const n = nodes[u];
    const relax = (v, t) => { if (v >= 0 && t < dist[v]) dist[v] = t; };
    for (const ds of [-1, 1]) {
      const v = at(n.li, n.si + ds);
      if (v < 0) continue;
      const km = Math.hypot(nodes[v].x - n.x, nodes[v].y - n.y) / 1000;
      relax(v, dist[u] + (km / n.kmh) * 60 + p.dwellMinutes);
    }
    nodes.forEach((m, v) => { if (m.name === n.name && m.li !== n.li) relax(v, dist[u] + p.interchangeMinutes); });
  }
  const best = new Map();
  nodes.forEach((n, i) => {
    const prev = best.get(n.name);
    if (!prev || dist[i] < prev.minutes) best.set(n.name, { name: n.name, x: n.x, y: n.y, minutes: dist[i] });
  });
  return [...best.values()];
}

function polar(origin, fn) {
  return fieldFromFunction({ origin, cell: FIELD_CELL, halfSize: FIELD_HALF }, fn);
}

export function walkField(origin, network) {
  const p = { ...DEFAULT_PARAMS, ...(network && network.params) };
  return polar(origin, (x, y) => walkMinutes(Math.hypot(x, y), p));
}

export function transitField(origin, network) {
  const p = { ...DEFAULT_PARAMS, ...(network.params || {}) };
  const stations = stationTimes(origin, network).filter((s) => Number.isFinite(s.minutes));
  const f = polar(origin, (x, y) => walkMinutes(Math.hypot(x, y), p));
  // Then each station improves the cells within walking reach of it (a few thousand each).
  const r = Math.ceil(p.egressMetres / f.cell);
  for (const s of stations) {
    const ci = Math.round((s.x - f.x0) / f.cell), cj = Math.round((s.y - f.y0) / f.cell);
    for (let j = Math.max(0, cj - r); j <= Math.min(f.ny - 1, cj + r); j++) {
      const y = f.y0 + j * f.cell;
      for (let i = Math.max(0, ci - r); i <= Math.min(f.nx - 1, ci + r); i++) {
        const d = Math.hypot(f.x0 + i * f.cell - s.x, y - s.y);
        if (d > p.egressMetres) continue;
        const t = s.minutes + walkMinutes(d, p);
        if (t < f.minutes[j * f.nx + i]) f.minutes[j * f.nx + i] = t;
      }
    }
  }
  return f;
}

export const RIVER_Y = -1500;          // metres north of the origin (y is south)
export const BRIDGE_X = 500;

export function cliffField(origin, network) {
  const p = { ...DEFAULT_PARAMS, ...(network && network.params) };
  const bridge = Math.hypot(BRIDGE_X, RIVER_Y);
  return polar(origin, (x, y) => {
    if (y >= RIVER_Y) return walkMinutes(Math.hypot(x, y), p);
    return walkMinutes(bridge + Math.hypot(x - BRIDGE_X, y - RIVER_Y), p);   // via the bridge
  });
}

// example.json: radii in px at ref_zoom per angle bin (y-down, bin i at θ = (i + ½)/N·2π − π),
// one row per contour. Minutes are interpolated between contours; past the last, no data.
export function contoursField(example) {
  const origin = { lon: example.origin.lon, lat: example.origin.lat };
  const mPerPx = metresPerUnit(origin.lat) / (WORLD_PX * Math.pow(2, example.ref_zoom));
  const rows = example.source_radii.map((r) => r.map((v) => v * mPerPx));
  const mins = example.contour_minutes.map(Number);
  const n = rows[0].length;
  const radiusAt = (row, theta) => {
    const f = ((theta + Math.PI) / (2 * Math.PI)) * n - 0.5;
    const i = Math.floor(f), a = f - i;
    const i0 = ((i % n) + n) % n, i1 = (i0 + 1) % n;
    return row[i0] * (1 - a) + row[i1] * a;
  };
  return polar(origin, (x, y) => {
    const r = Math.hypot(x, y);
    if (r === 0) return 0;
    const th = Math.atan2(y, x);
    let r0 = 0, m0 = 0;
    for (let k = 0; k < rows.length; k++) {
      const rk = radiusAt(rows[k], th);
      if (r <= rk) return m0 + ((mins[k] - m0) * (r - r0)) / Math.max(rk - r0, 1e-6);
      r0 = rk; m0 = mins[k];
    }
    return NO_DATA;
  });
}

// Target-position grids (layout units = local metres, so k = px per metre on screen).
export function gridField(name, origin, network) {
  const frame = localFrame(origin);
  let fn;
  if (name === "identity") fn = (x, y) => [x, y];
  else if (name === "shear") fn = (x, y) => [x + 0.3 * y, y];
  else {
    // Pull: places near a point come 60% of the way towards the origin, smoothly.
    const ref = network.origins.find((o) => o.id === "wembley-park") || network.origins[0];
    const [cx, cy] = frame.toLocal(ref.lon, ref.lat);
    const s2 = 3000 * 3000;
    fn = (x, y) => {
      const w = 0.6 * Math.exp(-((x - cx) ** 2 + (y - cy) ** 2) / s2);
      return [x - w * cx, y - w * cy];
    };
  }
  return gridFromFunction({ origin, cell: GRID_CELL, halfSize: FIELD_HALF }, fn);
}

// The lab's one entry point: a warp spec for createWarp() / the renderer / the worker.
export function makeWarpSpec(fieldName, origin, network, { example, maxMinutes = 45 } = {}) {
  if (GRID_FIELDS.includes(fieldName)) return { kind: "grid", grid: gridField(fieldName, origin, network) };
  let field;
  if (fieldName === "walk") field = walkField(origin, network);
  else if (fieldName === "transit") field = transitField(origin, network);
  else if (fieldName === "cliff") field = cliffField(origin, network);
  else if (fieldName === "contours") { field = contoursField(example); maxMinutes = Math.min(maxMinutes, 20); }
  else throw new Error(`unknown field ${fieldName}`);
  return { kind: "polar", field, maxMinutes };
}

// Renderer diagnostics on a polar field, NOT Direction 4's verdict (that needs real fields):
//   foldedShare    the share of the area inside the outer ring covered by folded sheets on the
//                  bent map: the union of the folded cells' images, rasterised, so overlapping
//                  sheets count once
//   foldedSheets   the same without the union: total area of folded cells' images (can exceed
//                  foldedShare, even 1, where sheets stack)
//   medianStretch  the median over field cells inside the ring (folded ones included) of the
//                  largest local scale over the smallest, sampled per source cell, unweighted
export function foldDiagnostics(spec, { step = 2, raster = 600 } = {}) {
  if (spec.kind !== "polar") return null;
  const f = spec.field, max = spec.maxMinutes ?? 45;
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
      if (q.some((t) => !t)) continue;
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
  };
}
