// Refining the mesh where the field bends it (#5, W-02; the fallback W-01 named for its cliff
// case). Pure. Applied per origin at pack time, after cached tile meshes are moved into local
// metres; tile caches stay independent of the origin.
//
//   the test   an edge is split when the field at its midpoint is more than `eps` minutes
//              off the mean of its ends: the drawn chord would miss the warped curve. eps is
//              the caller's: 0.5 px divided by the largest px per minute of the views drawn.
//              Edges with an end the field doesn't place (unknown, or past the outer ring,
//              which fades onto it) are left alone.
//   no cracks  the test reads only the edge's two end positions, so the two triangles that
//              share an edge always agree; midpoints are shared through one map per pass.
//              Fills split by the 1-, 2- and 3-edge patterns, recursively, up to `maxDepth`.
//   budget     each new fill vertex counts one shaded vertex, each new segment `segCost`.
//              Out of room, an edge splits only if its midpoint already exists (so the
//              neighbour that made it still matches), and the rest are counted as skipped.
//
// The bearing's own curvature is not tested: on a 48 m edge it moves the chord by under
// 0.01 minute beyond 500 m from the pin. Jumps in the field (a river, a fast line's edge) are
// what this catches.
import { BEYOND, sampleField } from "./field.js";

export function makeEdgeTest(field, maxMinutes, eps) {
  const t = (x, y) => {
    const v = sampleField(field, x, y);
    return v >= 0 && v < BEYOND && v <= maxMinutes ? v : NaN;
  };
  return {
    t,
    split(ax, ay, ta, bx, by, tb) {
      if (!(ta >= 0) || !(tb >= 0)) return false;
      const tm = t((ax + bx) / 2, (ay + by) / 2);
      return tm >= 0 && Math.abs(tm - (ta + tb) / 2) > eps;
    },
  };
}

// pos (x, y per vertex), index (triangles), per-vertex `style` and `zoom` → new arrays.
export function refineFills({ pos, index, style, zoom }, test, { maxDepth = 3, room = Infinity } = {}) {
  const n0 = pos.length / 2;
  const px = [], py = [], pt = [], pstyle = [], pzoom = [];
  const tv = new Float64Array(n0).fill(-2);           // −2: not computed yet
  const X = (v) => (v < n0 ? pos[2 * v] : px[v - n0]);
  const Y = (v) => (v < n0 ? pos[2 * v + 1] : py[v - n0]);
  const T = (v) => {
    if (v >= n0) return pt[v - n0];
    if (tv[v] === -2) tv[v] = test.t(pos[2 * v], pos[2 * v + 1]);
    return tv[v];
  };
  const mids = new Map();
  let added = 0, skipped = 0;
  const key = (a, b) => (a < b ? a * 0x4000000 + b : b * 0x4000000 + a);
  // The midpoint vertex of edge ab, or -1 if it doesn't split.
  function mid(a, b, depth) {
    if (depth >= maxDepth) return -1;
    const k = key(a, b);
    const m = mids.get(k);
    if (m !== undefined) return m;
    if (!test.split(X(a), Y(a), T(a), X(b), Y(b), T(b))) return -1;
    if (added >= room) { skipped++; return -1; }
    const v = n0 + px.length;
    const x = (X(a) + X(b)) / 2, y = (Y(a) + Y(b)) / 2;
    px.push(x); py.push(y); pt.push(test.t(x, y));
    pstyle.push(a < n0 ? style[a] : pstyle[a - n0]);
    pzoom.push(a < n0 ? zoom[a] : pzoom[a - n0]);
    mids.set(k, v);
    added++;
    return v;
  }
  const out = [];
  function tri(a, b, c, depth) {
    const ab = mid(a, b, depth), bc = mid(b, c, depth), ca = mid(c, a, depth);
    const d = depth + 1;
    if (ab < 0 && bc < 0 && ca < 0) { out.push(a, b, c); return; }
    if (ab >= 0 && bc >= 0 && ca >= 0) {
      tri(a, ab, ca, d); tri(ab, b, bc, d); tri(ca, bc, c, d); tri(ab, bc, ca, d);
    } else if (ab >= 0 && bc >= 0) {
      tri(ab, b, bc, d); tri(a, ab, bc, d); tri(a, bc, c, d);
    } else if (bc >= 0 && ca >= 0) {
      tri(bc, c, ca, d); tri(b, bc, ca, d); tri(b, ca, a, d);
    } else if (ca >= 0 && ab >= 0) {
      tri(ca, a, ab, d); tri(c, ca, ab, d); tri(c, ab, b, d);
    } else if (ab >= 0) {
      tri(a, ab, c, d); tri(ab, b, c, d);
    } else if (bc >= 0) {
      tri(b, bc, a, d); tri(bc, c, a, d);
    } else {
      tri(c, ca, b, d); tri(ca, a, b, d);
    }
  }
  for (let i = 0; i < index.length; i += 3) tri(index[i], index[i + 1], index[i + 2], 0);
  if (!added) return { pos, index, style, zoom, added: 0, skipped };
  const n = n0 + added;
  const P = new Float32Array(2 * n), S = new Float32Array(n), Z = new Uint8Array(n);
  P.set(pos); S.set(style); Z.set(zoom);
  for (let i = 0; i < added; i++) {
    P[2 * (n0 + i)] = px[i]; P[2 * (n0 + i) + 1] = py[i];
    S[n0 + i] = pstyle[i]; Z[n0 + i] = pzoom[i];
  }
  return { pos: P, index: Uint32Array.from(out), style: S, zoom: Z, added, skipped };
}

// seg (ax, ay, bx, by per segment), per-segment `style` and `zoom` → new arrays.
export function refineLines({ seg, style, zoom }, test, { maxDepth = 3, room = Infinity, segCost = 6 } = {}) {
  const outSeg = [], outStyle = [], outZoom = [];
  let added = 0, skipped = 0, spent = 0;
  function piece(ax, ay, ta, bx, by, tb, s, z, depth) {
    if (depth < maxDepth && test.split(ax, ay, ta, bx, by, tb)) {
      if (spent + segCost <= room) {
        spent += segCost;
        added++;
        const mx = (ax + bx) / 2, my = (ay + by) / 2, tm = test.t(mx, my);
        piece(ax, ay, ta, mx, my, tm, s, z, depth + 1);
        piece(mx, my, tm, bx, by, tb, s, z, depth + 1);
        return;
      }
      skipped++;
    }
    outSeg.push(ax, ay, bx, by); outStyle.push(s); outZoom.push(z);
  }
  const ns = seg.length / 4;
  for (let i = 0; i < ns; i++) {
    const ax = seg[4 * i], ay = seg[4 * i + 1], bx = seg[4 * i + 2], by = seg[4 * i + 3];
    piece(ax, ay, test.t(ax, ay), bx, by, test.t(bx, by), style[i], zoom[i], 0);
  }
  if (!added) return { seg, style, zoom, count: ns, added: 0, skipped };
  return { seg: Float32Array.from(outSeg), style: Float32Array.from(outStyle), zoom: Uint8Array.from(outZoom),
           count: outStyle.length, added, skipped };
}

// eps for a warp drawn at most `kMax` px per minute: half a pixel.
export const epsFor = (kMax) => 0.5 / kMax;
