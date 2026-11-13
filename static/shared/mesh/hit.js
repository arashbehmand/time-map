// What is under a finger on the bent map (#5, W-02): every sheet of the field drawn at a
// spot, even where folds stack several, with its source place and minutes. Pure.
//
// sheetIndex(field, warp, {maxMinutes}) maps each cell of the field into layout units
// (minutes, the polar target) as two triangles and buckets them in a 0.5-minute grid. A cell
// with a corner that is unknown, past the outer ring or filled in (water, parkland) is left
// out: it isn't drawn as a place. query(lx, ly, r) returns every triangle within r layout
// units of the point, as source places, merged into sheets: hits whose sources are within
// `merge` metres of each other are one sheet (the one nearest the finger is kept).
const BUCKET = 0.5;

export function sheetIndex(f, warp, { maxMinutes = Infinity, merge = 300 } = {}) {
  const { nx, ny, x0, y0, cell, minutes } = f;
  const filled = f.filled || null;
  const tx = new Float64Array(nx * ny), ty = new Float64Array(nx * ny);
  const ok = new Uint8Array(nx * ny);
  for (let j = 0; j < ny; j++) {
    for (let i = 0; i < nx; i++) {
      const k = j * nx + i, t = minutes[k];
      if (!(t >= 0) || t > maxMinutes || (filled && filled[k])) continue;
      const p = warp.target(x0 + i * cell, y0 + j * cell);
      if (!p) continue;
      tx[k] = p[0]; ty[k] = p[1]; ok[k] = 1;
    }
  }
  const buckets = new Map();
  const tris = [];
  const add = (a, b, c) => {
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
  };
  for (let j = 0; j + 1 < ny; j++) {
    for (let i = 0; i + 1 < nx; i++) {
      const a = j * nx + i, b = a + 1, c = a + nx + 1, d = a + nx;
      if (!(ok[a] && ok[b] && ok[c] && ok[d])) continue;
      add(a, b, c);
      add(a, c, d);
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
        const sx = (n) => x0 + (n % nx) * cell, sy = (n) => y0 + Math.floor(n / nx) * cell;
        hits.push({
          x: h.u * sx(ia) + h.v * sx(ib) + h.w * sx(ic),
          y: h.u * sy(ia) + h.v * sy(ib) + h.w * sy(ic),
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
