// Tile geometry → small pieces the warp can move without visible error. Pure.
//
// The warp moves vertices; the edges between them stay straight. So no edge may be longer
// than one cell (tile extent / spacingDiv):
//   lines  clipped to the tile, then split into segments of at most one cell;
//   fills  clipped to a grid of cells (a quadtree of axis clips), then earcut per cell.
// Clipping to the tile's own square drops the buffer zone the neighbouring tile also has.
// Every clip writes the boundary coordinate exactly and orders an edge's endpoints before
// interpolating, so two neighbouring cells get bit-identical vertices on their shared edge
// and the mesh has no cracks. The result does not depend on the origin, so it is cached.
import earcut from "../vendor/earcut/earcut.js";

// ── Lines ────────────────────────────────────────────────────────────

// Liang–Barsky: the part of segment a→b inside [lo, hi]², or null.
export function clipSegment(ax, ay, bx, by, lo, hi) {
  let t0 = 0, t1 = 1;
  const dx = bx - ax, dy = by - ay;
  const edges = [[-dx, ax - lo], [dx, hi - ax], [-dy, ay - lo], [dy, hi - ay]];
  for (const [p, q] of edges) {
    if (p === 0) { if (q < 0) return null; continue; }
    const r = q / p;
    if (p < 0) { if (r > t1) return null; if (r > t0) t0 = r; }
    else { if (r < t0) return null; if (r < t1) t1 = r; }
  }
  if (t1 - t0 <= 0) return null;
  return [ax + t0 * dx, ay + t0 * dy, ax + t1 * dx, ay + t1 * dy];
}

// Push the segments of one polyline ([{x, y}] or [[x, y]]), clipped and split, onto `out`.
export function lineSegments(points, extent, cell, out = []) {
  const X = (p) => (Array.isArray(p) ? p[0] : p.x), Y = (p) => (Array.isArray(p) ? p[1] : p.y);
  for (let i = 1; i < points.length; i++) {
    const c = clipSegment(X(points[i - 1]), Y(points[i - 1]), X(points[i]), Y(points[i]), 0, extent);
    if (!c) continue;
    const len = Math.hypot(c[2] - c[0], c[3] - c[1]);
    if (len === 0) continue;
    const n = Math.max(1, Math.ceil(len / cell - 1e-9));
    for (let k = 0; k < n; k++) {
      const s = k / n, e = (k + 1) / n;
      out.push(c[0] + (c[2] - c[0]) * s, c[1] + (c[3] - c[1]) * s,
               c[0] + (c[2] - c[0]) * e, c[1] + (c[3] - c[1]) * e);
    }
  }
  return out;
}

// ── Fills ────────────────────────────────────────────────────────────

// Sutherland–Hodgman against one axis-aligned half-plane. ring: flat [x0, y0, x1, y1, …],
// open (first point not repeated). keep > 0 keeps coord ≥ value, keep < 0 keeps ≤ value.
function clipRing(ring, axis, value, keep) {
  const n = ring.length / 2;
  if (n < 3) return [];
  const out = [];
  const inside = (i) => (keep > 0 ? ring[2 * i + axis] >= value : ring[2 * i + axis] <= value);
  const other = 1 - axis;
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n;
    const pin = inside(i), qin = inside(j);
    if (pin) out.push(ring[2 * i], ring[2 * i + 1]);
    if (pin !== qin) {
      // Canonical order (smaller coordinate on this axis first), so both sides of the line
      // compute the same point whichever way the edge runs.
      const [s, e] = ring[2 * i + axis] < ring[2 * j + axis] ? [i, j] : [j, i];
      const t = (value - ring[2 * s + axis]) / (ring[2 * e + axis] - ring[2 * s + axis]);
      const pt = [0, 0];
      pt[axis] = value;
      pt[other] = ring[2 * s + other] + t * (ring[2 * e + other] - ring[2 * s + other]);
      out.push(pt[0], pt[1]);
    }
  }
  return out.length >= 6 ? out : [];
}

export function ringArea(ring) {
  let a = 0;
  for (let i = 0, n = ring.length / 2; i < n; i++) {
    const j = (i + 1) % n;
    a += ring[2 * i] * ring[2 * j + 1] - ring[2 * j] * ring[2 * i + 1];
  }
  return a / 2;
}

function clipRings(rings, axis, value, keep) {
  const out = [];
  for (let r = 0; r < rings.length; r++) {
    const c = clipRing(rings[r], axis, value, keep);
    if (r === 0 && !c.length) return [];          // the outer ring is gone: nothing left
    if (c.length && Math.abs(ringArea(c)) > 1e-9) out.push(c);
    else if (r === 0) return [];
  }
  return out;
}

function flatRing(points) {
  const out = [];
  const X = (p) => (Array.isArray(p) ? p[0] : p.x), Y = (p) => (Array.isArray(p) ? p[1] : p.y);
  for (const p of points) out.push(X(p), Y(p));
  // MVT rings repeat the first point at the end.
  const n = out.length;
  if (n >= 4 && out[0] === out[n - 2] && out[1] === out[n - 1]) out.length = n - 2;
  return out;
}

// Triangles of one polygon (outer ring then holes), cut into cells of `cell` units inside
// [0, extent]². Appends to verts (x, y pairs) and index; returns the area kept.
// Every triangle is wound with a positive signed area in y-down coordinates.
export function gridFill(rings, extent, cell, verts = [], index = []) {
  const flat = [];
  for (const r of rings) {
    const f = flatRing(r);
    if (f.length >= 6) flat.push(f);
  }
  if (!flat.length) return 0;
  let area = 0;

  // A fully covered block: one vertex per grid node, shared by the cells around it.
  const emitQuads = (x0, y0, x1, y1) => {
    const nx = Math.max(1, Math.round((x1 - x0) / cell)), ny = Math.max(1, Math.round((y1 - y0) / cell));
    const b = verts.length / 2;
    for (let j = 0; j <= ny; j++) {
      const y = j === ny ? y1 : y0 + j * cell;
      for (let i = 0; i <= nx; i++) verts.push(i === nx ? x1 : x0 + i * cell, y);
    }
    for (let j = 0; j < ny; j++) {
      for (let i = 0; i < nx; i++) {
        const v = b + j * (nx + 1) + i;
        index.push(v, v + 1, v + nx + 2, v, v + nx + 2, v + nx + 1);
      }
    }
    area += (x1 - x0) * (y1 - y0);
  };

  const emitCell = (cellRings) => {
    const data = [], holes = [];
    for (let r = 0; r < cellRings.length; r++) {
      if (r > 0) holes.push(data.length / 2);
      for (const v of cellRings[r]) data.push(v);
    }
    const tri = earcut(data, holes.length ? holes : undefined);
    const b = verts.length / 2;
    for (const v of data) verts.push(v);
    for (let t = 0; t < tri.length; t += 3) {
      let i0 = tri[t], i1 = tri[t + 1], i2 = tri[t + 2];
      const a2 = (data[2 * i1] - data[2 * i0]) * (data[2 * i2 + 1] - data[2 * i0 + 1]) -
                 (data[2 * i1 + 1] - data[2 * i0 + 1]) * (data[2 * i2] - data[2 * i0]);
      if (a2 === 0) continue;
      if (a2 < 0) [i1, i2] = [i2, i1];
      index.push(b + i0, b + i1, b + i2);
      area += Math.abs(a2) / 2;
    }
  };

  const visit = (rs, x0, y0, x1, y1) => {
    if (!rs.length) return;
    const size = x1 - x0;
    // Whole square covered: emit plain cells, no clipping or earcut.
    if (rs.length === 1 && Math.abs(Math.abs(ringArea(rs[0])) - size * (y1 - y0)) <= 1e-9 * size * size) {
      emitQuads(x0, y0, x1, y1);
      return;
    }
    if (size <= cell + 1e-9) { emitCell(rs); return; }
    const mx = (x0 + x1) / 2, my = (y0 + y1) / 2;
    const left = clipRings(rs, 0, mx, -1), right = clipRings(rs, 0, mx, 1);
    visit(clipRings(left, 1, my, -1), x0, y0, mx, my);
    visit(clipRings(right, 1, my, -1), mx, y0, x1, my);
    visit(clipRings(left, 1, my, 1), x0, my, mx, y1);
    visit(clipRings(right, 1, my, 1), mx, my, x1, y1);
  };

  // Clip to the tile square first (drops the buffer zone), then subdivide.
  let rs = clipRings(flat, 0, 0, 1);
  rs = clipRings(rs, 0, extent, -1);
  rs = clipRings(rs, 1, 0, 1);
  rs = clipRings(rs, 1, extent, -1);
  visit(rs, 0, 0, extent, extent);
  return area;
}

// ── Budget ───────────────────────────────────────────────────────────

// What goes first when a scene is over budget, in order. Motorway, trunk and major rail
// are never dropped: the scene reports overBudget instead.
export const SHED_ORDER = ["secondary", "park", "water", "minor_rail", "primary"];
const BY_AREA = new Set(["park", "water"]);

// features: [{cat, count, area, dist}] (count = vertices shaded; dist = metres from the
// origin). Within a class the farthest goes first (parks and water: the smallest first, then
// the farthest). Returns the indices kept, what was dropped per class, and whether it still
// doesn't fit.
export function enforceBudget(features, budget) {
  let total = 0;
  for (const f of features) total += f.count;
  const keep = new Array(features.length).fill(true);
  const dropped = {};
  for (const cat of SHED_ORDER) {
    if (total <= budget) break;
    const idx = features.map((f, i) => i).filter((i) => features[i].cat === cat);
    const far = (a, b) => (features[b].dist ?? 0) - (features[a].dist ?? 0);
    idx.sort(BY_AREA.has(cat) ? (a, b) => features[a].area - features[b].area || far(a, b) : far);
    for (const i of idx) {
      if (total <= budget) break;
      keep[i] = false;
      total -= features[i].count;
      dropped[cat] = (dropped[cat] || 0) + 1;
    }
  }
  return { keep, dropped, total, overBudget: total > budget };
}
