// What is under a finger on the bent map (#5, W-02): every sheet drawn at a spot, even where
// folds stack several, with its source place and minutes. Pure.
//
// meshSheetIndex(mesh, warp) maps a triangle mesh ({pos, minutes, index}, local metres) into
// layout units (minutes, the polar target) and buckets its triangles in a 0.5-minute grid. The
// warp page passes the band layer's own mesh, refined like the map (refine.js), so a tap reads
// the same triangles the bands are drawn with. A vertex the warp doesn't place (unknown, past
// the outer ring) drops its triangles. query(lx, ly, r) returns every triangle within r layout
// units of the point, as source places, merged into sheets: hits whose sources are within
// `merge` metres of each other are one sheet (the one nearest the finger is kept).
//
// sheetIndex(field, warp, {maxMinutes}) does the same over the field's own cells (two
// triangles each; cells with a corner unknown, past the ring or filled in are left out).
const BUCKET = 0.5;

export function meshSheetIndex({ pos, minutes, index }, warp, { merge = 300 } = {}) {
  const n = pos.length / 2;
  const tx = new Float64Array(n), ty = new Float64Array(n), ok = new Uint8Array(n);
  for (let v = 0; v < n; v++) {
    const p = warp.target(pos[2 * v], pos[2 * v + 1]);
    if (!p) continue;
    tx[v] = p[0]; ty[v] = p[1]; ok[v] = 1;
  }
  const buckets = new Map();
  const tris = [];
  for (let i = 0; i + 2 < index.length; i += 3) {
    const a = index[i], b = index[i + 1], c = index[i + 2];
    if (!(ok[a] && ok[b] && ok[c])) continue;
    const id = tris.length / 3;
    tris.push(a, b, c);
    const bx0 = Math.floor(Math.min(tx[a], tx[b], tx[c]) / BUCKET), bx1 = Math.floor(Math.max(tx[a], tx[b], tx[c]) / BUCKET);
    const by0 = Math.floor(Math.min(ty[a], ty[b], ty[c]) / BUCKET), by1 = Math.floor(Math.max(ty[a], ty[b], ty[c]) / BUCKET);
    for (let bx = bx0; bx <= bx1; bx++) for (let by = by0; by <= by1; by++) {
      const key = `${bx},${by}`;
      let list = buckets.get(key);
      if (!list) buckets.set(key, (list = []));
      list.push(id);
    }
  }
  const tri = Int32Array.from(tris);

  function query(lx, ly, r = 0) {
    const seen = new Set();
    const hits = [];
    const b0 = Math.floor((lx - r) / BUCKET), b1 = Math.floor((lx + r) / BUCKET);
    const c0 = Math.floor((ly - r) / BUCKET), c1 = Math.floor((ly + r) / BUCKET);
    for (let bx = b0; bx <= b1; bx++) for (let by = c0; by <= c1; by++) {
      for (const id of buckets.get(`${bx},${by}`) || []) {
        if (seen.has(id)) continue;
        seen.add(id);
        const ia = tri[3 * id], ib = tri[3 * id + 1], ic = tri[3 * id + 2];
        const h = closestBary(lx, ly, tx[ia], ty[ia], tx[ib], ty[ib], tx[ic], ty[ic]);
        if (h.d > r) continue;
        hits.push({
          x: h.u * pos[2 * ia] + h.v * pos[2 * ib] + h.w * pos[2 * ic],
          y: h.u * pos[2 * ia + 1] + h.v * pos[2 * ib + 1] + h.w * pos[2 * ic + 1],
          minutes: h.u * minutes[ia] + h.v * minutes[ib] + h.w * minutes[ic],
          distance: h.d,
        });
      }
    }
    hits.sort((p, q) => p.distance - q.distance || p.minutes - q.minutes);
    const sheets = [];
    for (const hit of hits) {
      if (!sheets.some((s) => Math.hypot(s.x - hit.x, s.y - hit.y) <= merge)) sheets.push(hit);
    }
    return sheets;
  }

  return { triangles: tri.length / 3, query };
}

export function sheetIndex(f, warp, { maxMinutes = Infinity, merge = 300 } = {}) {
  const { nx, ny, x0, y0, cell, minutes } = f;
  const filled = f.filled || null;
  const pos = new Float64Array(2 * nx * ny);
  const good = new Uint8Array(nx * ny);
  for (let j = 0; j < ny; j++) {
    for (let i = 0; i < nx; i++) {
      const k = j * nx + i, t = minutes[k];
      pos[2 * k] = x0 + i * cell; pos[2 * k + 1] = y0 + j * cell;
      good[k] = t >= 0 && t <= maxMinutes && !(filled && filled[k]) ? 1 : 0;
    }
  }
  const index = [];
  for (let j = 0; j + 1 < ny; j++) {
    for (let i = 0; i + 1 < nx; i++) {
      const a = j * nx + i, b = a + 1, c = a + nx + 1, d = a + nx;
      if (!(good[a] && good[b] && good[c] && good[d])) continue;
      index.push(a, b, c, a, c, d);
    }
  }
  return meshSheetIndex({ pos, minutes, index }, warp, { merge });
}

// The point of triangle abc nearest p, as barycentric weights (u, v, w) and its distance d.
export function closestBary(px, py, ax, ay, bx, by, cx, cy) {
  const v0x = bx - ax, v0y = by - ay, v1x = cx - ax, v1y = cy - ay, v2x = px - ax, v2y = py - ay;
  const d00 = v0x * v0x + v0y * v0y, d01 = v0x * v1x + v0y * v1y, d11 = v1x * v1x + v1y * v1y;
  const d20 = v2x * v0x + v2y * v0y, d21 = v2x * v1x + v2y * v1y;
  const den = d00 * d11 - d01 * d01;
  if (Math.abs(den) > 1e-18) {
    const v = (d11 * d20 - d01 * d21) / den, w = (d00 * d21 - d01 * d20) / den, u = 1 - v - w;
    if (u >= 0 && v >= 0 && w >= 0) return { u, v, w, d: 0 };
  }
  // Outside (or a degenerate triangle): the nearest point on an edge.
  let best = null;
  for (const [p, q, wp, wq] of [[[ax, ay], [bx, by], 0, 1], [[bx, by], [cx, cy], 1, 2], [[cx, cy], [ax, ay], 2, 0]]) {
    const ex = q[0] - p[0], ey = q[1] - p[1], L = ex * ex + ey * ey;
    const s = L > 0 ? Math.min(1, Math.max(0, ((px - p[0]) * ex + (py - p[1]) * ey) / L)) : 0;
    const d = Math.hypot(p[0] + s * ex - px, p[1] + s * ey - py);
    if (!best || d < best.d) {
      const wts = [0, 0, 0];
      wts[wp] = 1 - s; wts[wq] += s;
      best = { u: wts[0], v: wts[1], w: wts[2], d };
    }
  }
  return best;
}
