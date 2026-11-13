// SYNTHETIC travel-time fields and layouts for the renderer lab. Pure (the network comes in
// as data), so the node tests build the same fields. None of these are real travel times,
// except `real`, which comes from the server.
//
//   walk     minutes = distance × detour / walking speed
//   transit  the toy network in stations.json: walk to a station, ride, walk on
//   cliff    walking, with a river 1.5 km north that only one bridge crosses
//   contours the warp page's saved King's Cross walking field (static/warp/example-field.json)
//   identity, shear, pull   target-position grids, to show the London map (#6) can use the
//            same renderer with a layout that is not travel time
//   real     NOT synthetic: the field the warp page gets from POST /api/travel-time-field
//            (?field=real&mode=transit|walking&window=wd08&smooth=1), fetched at boot for every
//            test origin, so the self-test, the benchmark and directionChecks run on it
import { decodeField, fieldFromFunction, gridFromFunction } from "../../shared/mesh/field.js";
import { localFrame, metresPerUnit, WORLD_PX } from "../../shared/mesh/frame.js";

export const FIELD_CELL = 100;          // metres (D7)
export const FIELD_HALF = 25_000;       // ±25 km
export const GRID_CELL = 500;
export const POLAR_FIELDS = ["walk", "transit", "cliff", "contours", "real"];
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

// The warp page's saved field (static/warp/example-field.json, the server's wire format).
export function contoursField(example) {
  return decodeField(example);
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
export function makeWarpSpec(fieldName, origin, network, { example, maxMinutes = 45, real = null } = {}) {
  if (GRID_FIELDS.includes(fieldName)) return { kind: "grid", grid: gridField(fieldName, origin, network) };
  let field;
  if (fieldName === "walk") field = walkField(origin, network);
  else if (fieldName === "transit") field = transitField(origin, network);
  else if (fieldName === "cliff") field = cliffField(origin, network);
  else if (fieldName === "contours") { field = contoursField(example); maxMinutes = Math.min(maxMinutes, 25); }
  else if (fieldName === "real") {
    field = real && real.get(origin.id);
    if (!field) throw new Error(`no real field for ${origin.id}`);
  }
  else throw new Error(`unknown field ${fieldName}`);
  return { kind: "polar", field, maxMinutes };
}

// Fold diagnostics moved to the shared mesh (the warp page reports them on real fields).
export { foldDiagnostics } from "../../shared/mesh/diagnostics.js";
