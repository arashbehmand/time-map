// Travel-time bands for the ordinary map (#5, W-02): masked marching squares over a field,
// as GeoJSON in lon/lat, with every island and hole. Pure.
//
//   region(T)   the nodes known to be reached within T minutes (no data, beyond and anything
//               slower are outside), traced as closed rings; the grid is padded with outside
//               nodes, so every ring closes. A crossing between two known nodes is placed by
//               linear interpolation; next to an unknown node, half way.
//   saddles     a cell with two inside corners on a diagonal joins them when the mean of its
//               corners is inside
//   band k      region(T_k) minus region(T_{k−1}): region(T_k)'s rings plus region(T_{k−1})'s
//               rings reversed. Rings are oriented so that, in lon/lat, outer rings run
//               counter-clockwise and holes clockwise (RFC 7946); holes go in the smallest
//               outer ring that holds them.
import { NO_DATA, BEYOND } from "./field.js";

const known = (t) => t !== NO_DATA && t >= 0 && t < BEYOND;

// Rings (arrays of [x, y] in local metres, closed) of region(T). Orientation: outer rings
// have negative shoelace area in local metres (y south), holes positive.
export function regionRings(f, T, { skipFilled = false } = {}) {
  const { nx, ny, x0, y0, cell, minutes } = f;
  const filled = skipFilled && f.filled ? f.filled : null;
  const val = (i, j) => {
    if (i < 0 || j < 0 || i >= nx || j >= ny) return Infinity;
    const k = j * nx + i, t = minutes[k];
    if (!known(t) || (filled && filled[k])) return Infinity;
    return t;
  };
  const inside = (i, j) => val(i, j) <= T;
  // Crossing on the edge between nodes a and b (b one step right or down of a).
  const point = (ia, ja, ib, jb) => {
    const ta = val(ia, ja), tb = val(ib, jb);
    let s = 0.5;
    if (Number.isFinite(ta) && Number.isFinite(tb) && ta !== tb) s = Math.min(1, Math.max(0, (T - ta) / (tb - ta)));
    return [x0 + (ia + (ib - ia) * s) * cell, y0 + (ja + (jb - ja) * s) * cell];
  };
  // Edge keys: horizontal edge (i,j)-(i+1,j) and vertical edge (i,j)-(i,j+1), padded by one.
  const W = nx + 2;
  const hKey = (i, j) => 2 * ((j + 1) * W + (i + 1));
  const vKey = (i, j) => 2 * ((j + 1) * W + (i + 1)) + 1;
  const next = new Map();
  const pos = new Map();
  for (let j = -1; j < ny; j++) {
    for (let i = -1; i < nx; i++) {
      const tl = inside(i, j), tr = inside(i + 1, j), br = inside(i + 1, j + 1), bl = inside(i, j + 1);
      if (tl === tr && tr === br && br === bl) continue;
      // Clockwise around the cell (on screen, y down): top, right, bottom, left.
      const sides = [
        [tl, tr, hKey(i, j), () => point(i, j, i + 1, j)],
        [tr, br, vKey(i + 1, j), () => point(i + 1, j, i + 1, j + 1)],
        [br, bl, hKey(i, j + 1), () => point(i, j + 1, i + 1, j + 1)],
        [bl, tl, vKey(i, j), () => point(i, j, i, j + 1)],
      ];
      const cross = [];
      for (const [a, b, key, at] of sides) {
        if (a === b) continue;
        cross.push({ enter: !a && b, key });
        if (!pos.has(key)) pos.set(key, at());
      }
      if (cross.length === 2) {
        const n = cross.find((c) => c.enter), e = cross.find((c) => !c.enter);
        next.set(n.key, e.key);
      } else {
        // Saddle: four crossings alternating. Joined (centre inside): each entry pairs with
        // the exit before it; separate: with the exit after it.
        const vals = [val(i, j), val(i + 1, j), val(i + 1, j + 1), val(i, j + 1)];
        const centreIn = vals.every(Number.isFinite) && vals.reduce((s, v) => s + v, 0) / 4 <= T;
        for (let k = 0; k < 4; k++) {
          if (!cross[k].enter) continue;
          const e = centreIn ? cross[(k + 3) % 4] : cross[(k + 1) % 4];
          next.set(cross[k].key, e.key);
        }
      }
    }
  }
  const rings = [];
  const seen = new Set();
  for (const start of next.keys()) {
    if (seen.has(start)) continue;
    const ring = [];
    let k = start;
    while (!seen.has(k)) {
      seen.add(k);
      ring.push(pos.get(k));
      k = next.get(k);
      if (k === undefined) break;
    }
    if (ring.length >= 3) { ring.push(ring[0]); rings.push(ring); }
  }
  return rings;
}

export function signedArea(ring) {
  let a = 0;
  for (let i = 0; i + 1 < ring.length; i++) a += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1];
  return a / 2;
}

function bbox(ring) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of ring) { if (x < x0) x0 = x; if (y < y0) y0 = y; if (x > x1) x1 = x; if (y > y1) y1 = y; }
  return [x0, y0, x1, y1];
}

function contains(ring, x, y) {
  let inside = false;
  for (let i = 0, j = ring.length - 2; i < ring.length - 1; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

// Rings → polygons [[outer, ...holes], ...] (local metres), by orientation and containment.
export function nestRings(rings) {
  const outers = [], holes = [];
  for (const r of rings) {
    const a = signedArea(r);
    if (a < 0) outers.push({ ring: r, area: -a, box: bbox(r), holes: [] });
    else if (a > 0) holes.push(r);
  }
  outers.sort((p, q) => p.area - q.area);
  for (const h of holes) {
    // A point just inside the hole: the midpoint of its first edge, nudged to the side away
    // from the region (the region is on every ring's visual left, so the hole is right).
    const [ax, ay] = h[0], [bx, by] = h[1];
    const len = Math.hypot(bx - ax, by - ay) || 1;
    const px = (ax + bx) / 2 - ((by - ay) / len) * 1e-3, py = (ay + by) / 2 + ((bx - ax) / len) * 1e-3;
    const o = outers.find((p) => px >= p.box[0] && px <= p.box[2] && py >= p.box[1] && py <= p.box[3] && contains(p.ring, px, py));
    if (o) o.holes.push(h);
  }
  return outers.map((o) => [o.ring, ...o.holes]);
}

// GeoJSON FeatureCollection: one MultiPolygon per band, quickest first. levels: ring minutes,
// increasing. frame: localFrame(field.origin). Properties: band, from, to (minutes).
export function bandPolygons(f, frame, levels, opts = {}) {
  const ll = ([x, y]) => frame.fromLocal(x, y).map((v) => Math.round(v * 1e6) / 1e6);
  const features = [];
  let prev = [];
  levels.forEach((T, k) => {
    const cur = regionRings(f, T, opts);
    const rings = [...cur, ...prev.map((r) => [...r].reverse())];
    const polys = nestRings(rings);
    if (polys.length) {
      features.push({
        type: "Feature",
        properties: { band: k, from: k ? levels[k - 1] : 0, to: T },
        geometry: { type: "MultiPolygon", coordinates: polys.map((p) => p.map((r) => r.map(ll))) },
      });
    }
    prev = cur;
  });
  return { type: "FeatureCollection", features };
}

// The band layer for the bent map (renderer.setBandMesh): the field's own cells inside `box`
// (local metres [x0, y0, x1, y1]) whose four corners hold a time (not beyond, not filled in),
// as triangles over the nodes, with each node's minutes. The renderer colours them by band.
// `mask` (one byte per node, optional): only nodes set in it (layouts.js islands: not torn).
export function bandMesh(f, box = null, { skipFilled = true, mask = null } = {}) {
  const { nx, ny, x0, y0, cell, minutes } = f;
  const filled = skipFilled && f.filled ? f.filled : null;
  let i0 = 0, j0 = 0, i1 = nx - 1, j1 = ny - 1;
  if (box) {
    i0 = Math.max(0, Math.floor((box[0] - x0) / cell)); j0 = Math.max(0, Math.floor((box[1] - y0) / cell));
    i1 = Math.min(nx - 1, Math.ceil((box[2] - x0) / cell)); j1 = Math.min(ny - 1, Math.ceil((box[3] - y0) / cell));
  }
  // A node the warp places: the warp samples the cell to its lower right (field.js), and a
  // cell with an unknown corner sends the node onto the outer ring as it fades, which would
  // streak the band. So the node and that cell's other three corners must all hold a time.
  const hasTime = (k) => known(minutes[k]) && !(filled && filled[k]) && !(mask && !mask[k]);
  const ok = (k) => {
    const i = k % nx, j = Math.floor(k / nx);
    return i + 1 < nx && j + 1 < ny && hasTime(k) && hasTime(k + 1) && hasTime(k + nx) && hasTime(k + nx + 1);
  };
  const id = new Int32Array(nx * ny).fill(-1);
  const pos = [], mins = [], index = [];
  const vert = (k) => {
    if (id[k] < 0) {
      id[k] = mins.length;
      pos.push(x0 + (k % nx) * cell, y0 + Math.floor(k / nx) * cell);
      mins.push(minutes[k]);
    }
    return id[k];
  };
  for (let j = j0; j < j1; j++) {
    for (let i = i0; i < i1; i++) {
      const a = j * nx + i, b = a + 1, c = a + nx + 1, d = a + nx;
      if (!(ok(a) && ok(b) && ok(c) && ok(d))) continue;
      const va = vert(a), vb = vert(b), vc = vert(c), vd = vert(d);
      index.push(va, vb, vc, va, vc, vd);
    }
  }
  return { pos: Float32Array.from(pos), minutes: Float32Array.from(mins), index: Uint32Array.from(index) };
}
