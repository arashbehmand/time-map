// The land of the full London time map (#6, L-02), carried through the mesh. Pure: no DOM and
// no imports, so node tests run it as the page does.
//
// land.bin holds the land already cut along the mesh (transit/layout/land.py). Every vertex is
// stored as three mesh nodes and two weights in 1/Q-ths (the third weight is one minus the
// others), so its place on a layout is w0·X[c0] + w1·X[c1] + w2·X[c2]: exactly the mesh's
// piecewise-linear map. A vertex on a mesh edge is stored by that edge alone, the same way in
// every piece that shares it, so neighbouring pieces meet exactly on every layout. The arithmetic
// here is the order land.place uses, so the build's Float32 check sees what the page draws.

const DTYPES = { uint8: Uint8Array, uint16: Uint16Array, uint32: Uint32Array, int32: Int32Array };

// land.bin → its arrays by name.
export function decodeLand(buffer, header) {
  const out = {};
  for (const a of header.arrays) out[a.name] = new DTYPES[a.dtype](buffer, a.offset, a.length);
  return out;
}

// Stored vertices (nodes c: three per vertex, weights w: two per vertex) placed on a layout
// (nodes: x, y per node, metres), written `stride` floats per vertex; with stride 3 the third is
// the flag 1 (the renderer's cpu targets).
export function place(c, w, Q, nodes, out, stride = 2) {
  const n = c.length / 3;
  for (let i = 0; i < n; i++) {
    const w1 = w[2 * i] / Q, w2 = w[2 * i + 1] / Q, w0 = 1 - w1 - w2;
    const a = c[3 * i], b = c[3 * i + 1], d = c[3 * i + 2];
    out[stride * i] = w0 * nodes[2 * a] + w1 * nodes[2 * b] + w2 * nodes[2 * d];
    out[stride * i + 1] = w0 * nodes[2 * a + 1] + w1 * nodes[2 * b + 1] + w2 * nodes[2 * d + 1];
    if (stride === 3) out[3 * i + 2] = 1;
  }
  return out;
}

// Which renderer pass each land style is drawn in.
export const PASS_OF = {
  water: "water", park: "park", boundary: "boundary", major_rail: "rail", minor_rail: "rail", rail_tunnel: "rail",
  // the car's main roads (#23, L-03: roads.bin), in the renderer's road pass (casing and fill)
  motorway: "road", trunk: "road", primary: "road",
};

// The renderer's scene passes for the land, on the real map (`real`: the first layout), plus what
// moveLand needs to put them on another layout: {passes: {water, park, boundary, rail}, verts}.
// styleId maps land.bin's style names (header.styles) to the renderer's style ids.
export function buildLand(land, header, real, styleId) {
  const names = header.styles, Q = header.weight_scale;
  const passes = {}, verts = {};
  const byPass = {};
  let v0 = 0, t0 = 0;
  for (let p = 0; p < land.fill_style.length; p++) {
    const name = PASS_OF[names[land.fill_style[p]]];
    (byPass[name] ||= []).push({ p, v0, t0, nv: land.fill_nv[p], nt: land.fill_nt[p] });
    v0 += land.fill_nv[p];
    t0 += 3 * land.fill_nt[p];
  }
  for (const [name, list] of Object.entries(byPass)) {
    let nv = 0, ni = 0;
    for (const it of list) { nv += it.nv; ni += 3 * it.nt; }
    const c = new Uint16Array(3 * nv), w = new Uint16Array(2 * nv), style = new Float32Array(nv);
    const index = new Uint32Array(ni);
    let v = 0, k = 0;
    for (const it of list) {
      for (let j = 0; j < 3 * it.nt; j++) index[k++] = land.fill_idx[it.t0 + j] + v;
      const sid = styleId[names[land.fill_style[it.p]]];
      c.set(land.fill_c.subarray(3 * it.v0, 3 * (it.v0 + it.nv)), 3 * v);
      w.set(land.fill_w.subarray(2 * it.v0, 2 * (it.v0 + it.nv)), 2 * v);
      style.fill(sid, v, v + it.nv);
      v += it.nv;
    }
    passes[name] = { kind: "fill", pos: place(c, w, Q, real, new Float32Array(2 * nv)), style, index,
      count: index.length, targets: new Float32Array(3 * nv) };
    verts[name] = { c, w, Q };
  }
  // lines: segments between consecutive vertices of each run, both ends stored
  const runs = {};
  let r0 = 0;
  for (let r = 0; r < land.line_style.length; r++) {
    const sname = names[land.line_style[r]];
    (runs[PASS_OF[sname]] ||= []).push({ r0, nv: land.line_nv[r], sid: styleId[sname] });
    r0 += land.line_nv[r];
  }
  for (const [name, list] of Object.entries(runs)) {
    let ns = 0;
    for (const it of list) ns += it.nv - 1;
    const c = new Uint16Array(6 * ns), w = new Uint16Array(4 * ns), style = new Float32Array(ns);
    let s = 0;
    for (const it of list) {
      for (let j = 0; j < it.nv - 1; j++, s++) {
        const a = it.r0 + j;
        c.set(land.line_c.subarray(3 * a, 3 * a + 6), 6 * s);
        w.set(land.line_w.subarray(2 * a, 2 * a + 4), 4 * s);
        style[s] = it.sid;
      }
    }
    passes[name] = { kind: "line", seg: place(c, w, Q, real, new Float32Array(4 * ns)), style, count: ns,
      targets: new Float32Array(6 * ns) };
    verts[name] = { c, w, Q, runs: Int32Array.from(list, (it) => it.nv - 1) };
  }
  return { passes, verts };
}

// Move every pass to one layout: fills get three floats per vertex, lines six per segment (both
// ends), which is the renderer's cpu-target layout.
export function moveLand(scene, nodes) {
  for (const [name, p] of Object.entries(scene.passes)) {
    const v = scene.verts[name];
    place(v.c, v.w, v.Q, nodes, p.targets, 3);
  }
  return scene;
}

// A carried line from timemap.json ({c, w} flat lists) placed on a layout: x, y per vertex.
export function placeLine(line, Q, nodes) {
  return place(line.c, line.w, Q, nodes, new Float64Array((line.c.length / 3) * 2));
}

// One slider position's node positions in metres, from timemap.json's decimetres.
export function nodesOf(frame) {
  const n = frame.x.length, out = new Float64Array(2 * n);
  for (let i = 0; i < n; i++) { out[2 * i] = frame.x[i] / 10; out[2 * i + 1] = frame.y[i] / 10; }
  return out;
}

// ── smooth motion ───────────────────────────────────────────────────
// The slider runs continuously from 0 (the real map) to the last solved position. The build ships
// keyframes: the solved positions at whole steps, and in-between positions it solved and added
// wherever a straight blend of two neighbours would fold, plus, where the solution jumps, the
// solver's own steps as it settles (springs.sweep; timemap.py, motion_keyframes). Any point
// between two neighbouring keyframes is their straight blend, which the build checked is
// unfolded all the way, so the page can draw every point of the slider.

// Keyframes in slider order: [{at, rho, main (solved position's index, or null), nodes}].
export function keyframes(doc) {
  const keys = doc.frames.map((f, i) => ({ at: i, rho: f.rho, main: i, settling: false, nodes: nodesOf(f) }));
  for (const b of (doc.motion && doc.motion.between) || []) {
    keys.push({ at: b.at, rho: b.rho, main: null, settling: !!b.settling, nodes: nodesOf(b) });
  }
  return keys.sort((a, b) => a.at - b.at);
}

// Slider value v as the blend from keys[k] to keys[k + 1] at s in [0, 1].
export function segmentAt(keys, v) {
  const last = keys.length - 1;
  if (last < 1 || v <= keys[0].at) return { k: 0, s: 0 };
  if (v >= keys[last].at) return { k: last - 1, s: 1 };
  let lo = 0, hi = last;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (keys[mid].at <= v) lo = mid; else hi = mid;
  }
  return { k: lo, s: (v - keys[lo].at) / (keys[lo + 1].at - keys[lo].at) };
}

// The straight blend of two layouts, the way the shader mixes them: a·(1 − s) + b·s.
export function blend(a, b, s, out = new Float64Array(a.length)) {
  for (let i = 0; i < a.length; i++) out[i] = a[i] * (1 - s) + b[i] * s;
  return out;
}

// How far the map has moved at each keyframe along the slider: the mean distance the `which`
// nodes travel from one keyframe to the next, added up. The page's animation goes at an even
// pace along this, so the small early steps and the large late ones each take the time they need.
export function pathLengths(keys, which) {
  const cum = new Float64Array(keys.length);
  for (let k = 1; k < keys.length; k++) {
    const a = keys[k - 1].nodes, b = keys[k].nodes;
    let d = 0;
    for (const i of which) d += Math.hypot(b[2 * i] - a[2 * i], b[2 * i + 1] - a[2 * i + 1]);
    cum[k] = cum[k - 1] + d / Math.max(1, which.length);
  }
  return cum;
}

// Distance along the path at slider value v, and the slider value at a distance (inverse).
export function distanceAt(keys, cum, v) {
  const { k, s } = segmentAt(keys, v);
  return keys.length < 2 ? 0 : cum[k] + (cum[k + 1] - cum[k]) * s;
}

export function atDistance(keys, cum, d) {
  const last = keys.length - 1;
  if (last < 1 || d <= 0) return keys[0].at;
  if (d >= cum[last]) return keys[last].at;
  let lo = 0, hi = last;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (cum[mid] <= d) lo = mid; else hi = mid;
  }
  const span = cum[lo + 1] - cum[lo];
  const s = span > 0 ? (d - cum[lo]) / span : 0;
  return keys[lo].at + (keys[lo + 1].at - keys[lo].at) * s;
}

// Smooth start and stop (the warp page's morph uses the same curve).
export const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);

// ── the page's motion ───────────────────────────────────────────────
// Where the map is on the slider (v, 0 … last) and at most one move at a time, driven by one
// pending frame callback: a play (the button, Home, End), a key glide (one solved position) or a
// drag (the slider's own input, applied once a frame). Pure: the page passes its clock and frame
// scheduler, and is told of every move (onMove) and when the map comes to rest (onSettle).
// A play or glide goes at an even pace along the path (pathLengths), easing in and out; the whole
// move takes playMs, a part its share (at least minMs, or stepMs for a key glide).
const FORWARD = new Set(["ArrowRight", "ArrowUp", "PageUp"]), BACKWARD = new Set(["ArrowLeft", "ArrowDown", "PageDown"]);

export function createMotion({ keys, cum, last, now, schedule, cancel, onMove, onSettle, reduceMotion = () => false,
  playMs = 4500, stepMs = 450, minMs = 350 }) {
  let v = 0, anim = null, pending = null, dragTo = null;
  const total = cum[cum.length - 1] || 1;
  const clamp = (x) => Math.max(0, Math.min(last, x));
  const set = (x) => { v = clamp(x); onMove(v); };
  const wake = () => { if (pending === null) pending = schedule(frame); };
  function frame(t) {
    pending = null;
    if (dragTo !== null) { const x = dragTo; dragTo = null; set(x); }
    if (!anim) return;
    const a = anim, p = Math.max(0, Math.min(1, (t - a.start) / a.ms));
    if (p >= 1) { anim = null; set(a.target); onSettle(); return; }
    const e = ease(p);
    set(a.d1 !== a.d0 ? atDistance(keys, cum, a.d0 + (a.d1 - a.d0) * e) : a.v0 + (a.target - a.v0) * e);
    wake();
  }
  // Move to `target`: a play ("play") or a key glide ("key"). It replaces any move under way and
  // goes on from where the map is.
  function playTo(target, kind = "play") {
    target = clamp(target);
    dragTo = null;
    if (reduceMotion() || target === v) { anim = null; set(target); onSettle(); return; }
    const d0 = distanceAt(keys, cum, v), d1 = distanceAt(keys, cum, target);
    const share = (playMs * Math.abs(d1 - d0)) / total;
    anim = { kind, d0, d1, v0: v, target, start: now(), ms: Math.max(kind === "key" ? stepMs : minMs, share) };
    wake();
  }
  return {
    get v() { return v; },
    get moving() { return anim !== null; },
    get kind() { return anim ? anim.kind : null; },
    // Whether the button now means "back to the real map": the map is at the end, or heading there.
    get back() { return (anim ? anim.target : v) >= last - 1e-6; },
    playTo,
    toggle() { playTo(this.back ? 0 : last); },
    // A key: arrows and page keys glide to the next or previous solved position (stepping on from a
    // glide's target, or from where the map is during a play); Home and End play to the ends. A
    // key held down steps on as each glide ends. Returns whether the key was one of these.
    key(name, repeat = false) {
      const fwd = FORWARD.has(name), bwd = BACKWARD.has(name);
      if (!fwd && !bwd && name !== "Home" && name !== "End") return false;
      if (repeat && anim) return true;
      if (name === "Home" || name === "End") { playTo(name === "Home" ? 0 : last); return true; }
      const here = anim && anim.kind === "key" ? anim.target : v;
      playTo(fwd ? Math.min(last, Math.floor(here + 1e-6) + 1) : Math.max(0, Math.ceil(here - 1e-6) - 1), "key");
      return true;
    },
    // The slider dragged to x: any move stops, and x is applied on the next frame (the last one wins).
    drag(x) { anim = null; dragTo = x; wake(); },
    // Stop where the map is.
    stop() { if (pending !== null) { cancel(pending); pending = null; } dragTo = null; if (anim) { anim = null; onSettle(); } },
    // Put the map at x at once (load, a restored context).
    jump(x) { anim = null; dragTo = null; set(x); },
  };
}

// ── line widths ─────────────────────────────────────────────────────
// Lines get thinner where the map squeezes the ground across them. land.py cuts every line along
// the mesh, so each segment lies in one triangle, and the triangle's linear map F (real map →
// layout) is exact there. A thin band along a segment of real direction d comes out det F ÷ |F·d|
// times as thick: that, never above 1 (a stretched line keeps its width) and never below
// WIDTH_MIN (so a line stays visible), is the segment's width factor.
export const WIDTH_MIN = 0.3;

// The triangle each segment lies in, from the nodes its two ends are stored by (c: six per
// segment): the triangle holding them all. -1 when none does.
export function segmentTriangles(c, triangles) {
  const tri = new Map(), edge = new Map(), node = new Map();
  const nT = triangles.length / 3;
  for (let t = 0; t < nT; t++) {
    const v = [triangles[3 * t], triangles[3 * t + 1], triangles[3 * t + 2]].sort((a, b) => a - b);
    tri.set(v.join(","), t);
    for (const [a, b] of [[v[0], v[1]], [v[1], v[2]], [v[0], v[2]]]) if (!edge.has(`${a},${b}`)) edge.set(`${a},${b}`, t);
    for (const a of v) if (!node.has(a)) node.set(a, t);
  }
  const n = c.length / 6, out = new Int32Array(n);
  for (let s = 0; s < n; s++) {
    const u = [...new Set(c.subarray ? c.subarray(6 * s, 6 * s + 6) : c.slice(6 * s, 6 * s + 6))].sort((a, b) => a - b);
    const k = u.join(",");
    out[s] = (u.length === 3 ? tri.get(k) : u.length === 2 ? edge.get(k) : u.length === 1 ? node.get(u[0]) : undefined) ?? -1;
  }
  return out;
}

// Each triangle's linear map from the real map to a layout, [a, b, c, d]: (x, y) → (a·x + b·y,
// c·x + d·y). Nodes: x, y per node.
export function triangleMaps(triangles, real, nodes, out = new Float64Array((triangles.length / 3) * 4)) {
  for (let t = 0; t < triangles.length / 3; t++) {
    const i = triangles[3 * t], j = triangles[3 * t + 1], k = triangles[3 * t + 2];
    const e1x = real[2 * j] - real[2 * i], e1y = real[2 * j + 1] - real[2 * i + 1];
    const e2x = real[2 * k] - real[2 * i], e2y = real[2 * k + 1] - real[2 * i + 1];
    const f1x = nodes[2 * j] - nodes[2 * i], f1y = nodes[2 * j + 1] - nodes[2 * i + 1];
    const f2x = nodes[2 * k] - nodes[2 * i], f2y = nodes[2 * k + 1] - nodes[2 * i + 1];
    const det = e1x * e2y - e1y * e2x;
    out[4 * t] = (f1x * e2y - f2x * e1y) / det;
    out[4 * t + 1] = (f2x * e1x - f1x * e2x) / det;
    out[4 * t + 2] = (f1y * e2y - f2y * e1y) / det;
    out[4 * t + 3] = (f2y * e1x - f1y * e2x) / det;
  }
  return out;
}

// Width factors of segments (seg: x0, y0, x1, y1 per segment on the real map, segTri their
// triangles) on a layout given by its triangle maps.
export function widthFactors(seg, segTri, maps, out = new Float32Array(seg.length / 4)) {
  for (let s = 0; s < out.length; s++) {
    const t = segTri[s];
    const dx = seg[4 * s + 2] - seg[4 * s], dy = seg[4 * s + 3] - seg[4 * s + 1], L = Math.hypot(dx, dy);
    if (t < 0 || !(L > 0)) { out[s] = 1; continue; }
    const a = maps[4 * t], b = maps[4 * t + 1], c = maps[4 * t + 2], d = maps[4 * t + 3];
    const fx = (a * dx + b * dy) / L, fy = (c * dx + d * dy) / L;
    out[s] = Math.min(1, Math.max(WIDTH_MIN, (a * d - b * c) / Math.hypot(fx, fy)));
  }
  return out;
}

// ── smoothing lines ─────────────────────────────────────────────────
// The mesh's map is linear inside each triangle, so a line bends a little wherever it crosses a
// mesh edge. Smoothing takes each line's displacement from the real map (not its shape: the
// real map is left exactly as it is) and averages it along the line over a window of ±h real
// metres, three times (close to a Gaussian of spread h). The window stays centred: it narrows
// toward a run's ends and toward points at mesh nodes (where stations sit), so those stay exact,
// lines still meet and pass through their stations, and a stretch the map moves without bending
// stays exactly as it is. Every step is linear in the layout, so the smoothed blend of two
// keyframes is the blend of their smoothed lines: the renderer's morph stays exact. Smoothing is
// for lines only: water, parks and the mesh keep the exact map.
export const SMOOTH_M = 200;

// One run: real and placed vertices (x, y each), `pinned` (bool per vertex) → the smoothed
// placed vertices.
export function smoothLine(real, xy, pinned, h = SMOOTH_M, passes = 3) {
  const n = real.length / 2, out = Float64Array.from(xy);
  if (n < 3 || !(h > 0)) return out;
  const s = new Float64Array(n);
  for (let i = 1; i < n; i++) s[i] = s[i - 1] + Math.hypot(real[2 * i] - real[2 * i - 2], real[2 * i + 1] - real[2 * i - 1]);
  // each vertex's half window: h, or less, so it reaches neither a run end nor a pinned point
  const hw = new Float64Array(n);
  let before = 0;
  for (let i = 0; i < n; i++) { if (i === 0 || pinned[i]) before = s[i]; hw[i] = s[i] - before; }
  let after = s[n - 1];
  for (let i = n - 1; i >= 0; i--) { if (i === n - 1 || pinned[i]) after = s[i]; hw[i] = Math.min(h, hw[i], after - s[i]); }
  let dx = new Float64Array(n), dy = new Float64Array(n);
  for (let i = 0; i < n; i++) { dx[i] = xy[2 * i] - real[2 * i]; dy[i] = xy[2 * i + 1] - real[2 * i + 1]; }
  for (let p = 0; p < passes; p++) { dx = boxAverage(s, dx, hw); dy = boxAverage(s, dy, hw); }
  for (let i = 0; i < n; i++) { out[2 * i] = real[2 * i] + dx[i]; out[2 * i + 1] = real[2 * i + 1] + dy[i]; }
  return out;
}

// The mean of the piecewise-linear v (values at arc lengths s) over [s_i − hw_i, s_i + hw_i].
function boxAverage(s, v, hw) {
  const n = s.length, I = new Float64Array(n);
  for (let k = 1; k < n; k++) I[k] = I[k - 1] + 0.5 * (v[k] + v[k - 1]) * (s[k] - s[k - 1]);
  const upTo = (x) => {                          // ∫_0^x v, by binary search for x's segment
    let lo = 0, hi = n - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (s[mid] <= x) lo = mid; else hi = mid; }
    const span = s[lo + 1] - s[lo], t = Math.min(Math.max(x - s[lo], 0), span);
    return span > 0 ? I[lo] + t * v[lo] + 0.5 * t * t * (v[lo + 1] - v[lo]) / span : I[lo];
  };
  const out = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const w = hw[i];
    out[i] = w > 0 ? (upTo(s[i] + w) - upTo(s[i] - w)) / (2 * w) : v[i];
  }
  return out;
}

// A line pass's pieces joined into whole lines. land.py cuts every line where it crosses a mesh
// edge, into pieces (runs) that meet end to end at a point stored by that edge, the same way in
// both, so the joins are exact. Where exactly two pieces meet they continue one another; at a
// junction or a dead end a line stops (and a closed loop, a borough's outline, is opened at one
// point). Each line is a list of vertices: where to read each one (`read`: its offset in the
// six-per-segment layout of c and targets), every slot it is stored in (`slot`, with `of` its
// vertex), and whether it sits at a mesh node (`pinned`).
export function lineChains(v) {
  const { c, w, runs } = v;
  const R = runs.length, first = new Int32Array(R + 1);
  for (let r = 0; r < R; r++) first[r + 1] = first[r] + runs[r];
  const keyAt = (seg, end) => {
    const q = 6 * seg + 3 * end, x = 4 * seg + 2 * end;
    return `${c[q]},${c[q + 1]},${c[q + 2]},${w[x]},${w[x + 1]}`;
  };
  const ends = [], meet = new Map();
  for (let r = 0; r < R; r++) {
    ends.push([keyAt(first[r], 0), keyAt(first[r + 1] - 1, 1)]);
    for (const e of [0, 1]) {
      const k = ends[r][e];
      if (!meet.has(k)) meet.set(k, []);
      meet.get(k).push([r, e]);
    }
  }
  const used = new Uint8Array(R);
  const walk = (r, e) => {                       // enter run r at its end e
    const seq = [];
    while (!used[r]) {
      used[r] = 1;
      seq.push([r, e]);
      const out = 1 - e, there = meet.get(ends[r][out]);
      if (there.length !== 2) break;
      const next = there[0][0] === r && there[0][1] === out ? there[1] : there[0];
      [r, e] = next;
    }
    return seq;
  };
  const seqs = [];
  for (let r = 0; r < R; r++) for (const e of [0, 1]) if (!used[r] && meet.get(ends[r][e]).length !== 2) seqs.push(walk(r, e));
  for (let r = 0; r < R; r++) if (!used[r]) seqs.push(walk(r, 0));
  return seqs.map((seq) => {
    const read = [], slot = [], of = [];
    for (const [r, e] of seq) {
      const segs = [];
      for (let g = first[r]; g < first[r + 1]; g++) segs.push(g);
      if (e === 1) segs.reverse();
      segs.forEach((g, i) => {
        const a = 6 * g + (e === 0 ? 0 : 3), b = 6 * g + (e === 0 ? 3 : 0);
        if (read.length === 0) { read.push(a); slot.push(a); of.push(0); }
        else if (i === 0) { slot.push(a); of.push(read.length - 1); }   // the join: the same point again
        else { slot.push(a); of.push(read.length - 1); }
        read.push(b); slot.push(b); of.push(read.length - 1);
      });
    }
    const pinned = Uint8Array.from(read, (q) => (c[q] === c[q + 1] && c[q + 1] === c[q + 2] ? 1 : 0));
    return { read: Int32Array.from(read), slot: Int32Array.from(slot), of: Int32Array.from(of), pinned };
  });
}

// Smooth every line of a pass in place. targets: six floats per segment (the renderer's layout);
// realSeg: four per segment, the real map.
function smoothPass(targets, realSeg, chains, h) {
  for (const ch of chains) {
    const n = ch.read.length, real = new Float64Array(2 * n), xy = new Float64Array(2 * n);
    for (let i = 0; i < n; i++) {
      const q = ch.read[i], g = Math.floor(q / 6), e = q % 6 === 0 ? 0 : 1;
      real[2 * i] = realSeg[4 * g + 2 * e]; real[2 * i + 1] = realSeg[4 * g + 2 * e + 1];
      xy[2 * i] = targets[q]; xy[2 * i + 1] = targets[q + 1];
    }
    const sm = smoothLine(real, xy, ch.pinned, h);
    for (let k = 0; k < ch.slot.length; k++) {
      const q = ch.slot[k], i = ch.of[k];
      targets[q] = sm[2 * i]; targets[q + 1] = sm[2 * i + 1];
    }
  }
}

// ── keyframes on the GPU ────────────────────────────────────────────
// The land placed on keyframes, in the renderer's target layout (three floats per vertex), with
// each line segment's width factor; the last `keep` kept. Placing all of it on one layout takes
// several milliseconds, so the page places the next keyframe ahead, between frames, and a move
// across a keyframe only copies. `smooth` smooths the lines (setSmooth changes it and forgets
// what was placed).
export function createPlacer(scene, keys, { keep = 5, triangles = null, smooth = false, h = SMOOTH_M } = {}) {
  const cache = new Map();
  const real = keys[0].nodes;
  const lines = {};
  for (const [name, p] of Object.entries(scene.passes)) {
    if (p.kind !== "line") continue;
    const v = scene.verts[name];
    // Float32, as the renderer holds the real map, so smoothing leaves it exactly as it is
    lines[name] = { realSeg: place(v.c, v.w, v.Q, real, new Float32Array(4 * p.count)),
      segTri: triangles ? segmentTriangles(v.c, triangles) : null, chains: null };
  }
  function placed(k) {
    let got = cache.get(k);
    if (got) {
      cache.delete(k);
      cache.set(k, got);
      return got;
    }
    got = { wf: {} };
    const maps = triangles ? triangleMaps(triangles, real, keys[k].nodes) : null;
    for (const [name, p] of Object.entries(scene.passes)) {
      const v = scene.verts[name];
      got[name] = place(v.c, v.w, v.Q, keys[k].nodes, new Float32Array(p.targets.length), 3);
      if (p.kind !== "line") continue;
      if (smooth) smoothPass(got[name], lines[name].realSeg, lines[name].chains ||= lineChains(v), h);
      got.wf[name] = maps ? widthFactors(lines[name].realSeg, lines[name].segTri, maps) : null;
    }
    cache.set(k, got);
    while (cache.size > keep) cache.delete(cache.keys().next().value);
    return got;
  }
  return {
    placed,
    has: (k) => cache.has(k),
    setSmooth(on) { if (on !== smooth) { smooth = on; cache.clear(); } },
    get smooth() { return smooth; },
  };
}

// Load the blend from one placed keyframe to the next into the scene: positions from A (copied
// out of its target layout, the same Float32 values), targets B's own arrays, and each line
// segment's width factor at A and at B. Drawn with the renderer's morph u = s, the shader mixes
// them, so moving inside one blend needs no new upload.
export function loadSegment(scene, placedA, placedB) {
  for (const [name, p] of Object.entries(scene.passes)) {
    const a = placedA[name], out = p.kind === "fill" ? p.pos : p.seg;
    for (let i = 0, j = 0; j < out.length; i += 3, j += 2) {
      out[j] = a[i];
      out[j + 1] = a[i + 1];
    }
    p.targets = placedB[name];
    const wa = placedA.wf && placedA.wf[name], wb = placedB.wf && placedB.wf[name];
    if (p.kind === "line" && wa && wb) {
      if (!p.wf || p.wf.length !== 2 * p.count) p.wf = new Float32Array(2 * p.count);
      for (let i = 0; i < p.count; i++) { p.wf[2 * i] = wa[i]; p.wf[2 * i + 1] = wb[i]; }
    }
  }
  return scene;
}

// ── travel modes (#23, L-03) ────────────────────────────────────────
// The build solves each combination of modes over the same slider: train and tube alone (the time
// map itself, timemap.json), car alone and car with train and tube (modes/combo-*.json). With no
// mode on the map is the real map, which is also every combination's slider position 0.

// The combination a set of modes on means: "train", "car", "car-train" or "none".
export function comboOf(on) {
  const ids = ["car", "train"].filter((m) => on.has(m));
  return ids.length ? ids.join("-") : "none";
}

// How the page moves from combination `from` to `to` (they differ by one mode) with the map at
// solved position i: "same"; "down" to the real map (no mode left on); "up" from it (the first mode
// switched on); "swap" at the real map, where every combination is the same; "straight" across, when
// the build checked that blend at i (cross: modes.json's flags, keyed "a|b" in either order); or
// "via-real": back down the old combination to the real map and up the new one, both checked blends.
export function switchPlan(cross, from, to, i) {
  if (from === to) return "same";
  if (to === "none") return "down";
  if (from === "none") return "up";
  if (i === 0) return "swap";
  const flags = (cross && (cross[`${from}|${to}`] || cross[`${to}|${from}`])) || null;
  const f = flags && flags[i];
  return f && f.ok ? "straight" : "via-real";
}

const MODE_NAMES = { train: "Train and tube", car: "Car" };

// One mode's line in the readout, from its pulls at a solved position (modes.json, combo frames):
// how many of its springs pull and where. p: {n, of, share, top}; i: the position (0 is the real map).
export function pullWords(mode, p, i, overheadMin = 0) {
  const name = MODE_NAMES[mode] || mode;
  const springs = mode === "car" ? "road springs" : "springs";
  if (!p) return `${name}: on.`;
  if (i === 0) return `${name}: on; on the real map no spring pulls yet.`;
  if (!p.n) {
    return `${name} moves nothing at this strength: none of its ${p.of} ${springs} is stretched past its time.`;
  }
  const pct = Math.max(1, Math.round(100 * p.share));
  const top = p.top && p.top.length ? (mode === "car" ? `, mostly ${p.top.join(", ")}` : `, hardest ${p.top[0]}`) : "";
  return `${name}: ${p.n} of ${p.of} ${springs} pulling (${pct}%)${top}.` +
    (mode === "car" && overheadMin ? ` Car times include ${overheadMin} minutes once a trip for parking and getting to the car.` : "");
}

// The steps a switch takes, from the plan (switchPlan): glide along the current combination to a
// slider value, enter a combination at a value (where both draw the same layout: the real map, or
// the end of a straight blend), or blend straight across at solved position i. `v` is where the
// map is; `remembered` where the slider was when the last mode went off. Every blend these steps
// draw is one the build checked: a combination's own keyframes, or a straight switch it flagged.
export function switchSteps(plan, from, to, i, v, remembered = 0) {
  if (plan === "same") return [];
  if (plan === "down") return [{ glide: 0 }, { enter: "none", at: 0 }];
  if (plan === "up") return [{ enter: to, at: 0 }, { glide: remembered }];
  if (plan === "swap") return [{ glide: 0 }, { enter: to, at: 0 }];
  if (plan === "straight") return [{ glide: i }, { cross: { from, to, at: i } }, { enter: to, at: i }];
  return [{ glide: 0 }, { enter: to, at: 0 }, { glide: i }];
}

// Run steps one after another: ops.glide(x, done), ops.cross({from, to, at}, done) and
// ops.enter(id, x) (at once); then ops.done(). A glide or blend that ends at once (already there,
// reduced motion) calls done synchronously, which is fine.
export function runSteps(steps, ops) {
  let k = 0;
  const next = () => {
    while (k < steps.length) {
      const s = steps[k++];
      if ("enter" in s) { ops.enter(s.enter, s.at); continue; }
      if ("glide" in s) { ops.glide(s.glide, next); return; }
      if ("cross" in s) { ops.cross(s.cross, next); return; }
    }
    ops.done();
  };
  next();
}

// Which mode buttons can be pressed: Train and tube always; Car only where the build has the car
// (modes.json says so); Walking and Bus never yet. `index` is modes.json or null.
export function modeAvailability(index) {
  const listed = Object.fromEntries(((index && index.modes) || []).map((m) => [m.id, m]));
  return {
    walk: { on: false, why: (listed.walk && listed.walk.why) || "Coming next." },
    bus: { on: false, why: (listed.bus && listed.bus.why) || "Coming next." },
    train: { on: true, why: "" },
    car: { on: Boolean(listed.car && listed.car.available), why: listed.car ? "" : "This map has no car." },
  };
}

// Solved positions where some triangle gets very thin: its larger stretch over its smaller past
// THIN_MARK (the build's metrics.stretch_max). The page marks them under the slider. On build
// 2026-11-12 that is car with train and tube at its last two positions, a few triangles at the
// mesh's south edge; train alone stays under 16:1 and car alone under 21:1.
export const THIN_MARK = 50;

export function thinPositions(frames, limit = THIN_MARK) {
  const out = [];
  frames.forEach((f, i) => {
    const r = f.metrics && f.metrics.stretch_max;
    if (i > 0 && r > limit) out.push({ i, ratio: r });
  });
  return out;
}

// The sha256 of a file's bytes, as hex (Web Crypto, in the page and in node).
export async function sha256Hex(buffer) {
  const d = await globalThis.crypto.subtle.digest("SHA-256", buffer);
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Whether a travel-mode file (modes.json or a combination) was made with this very map: the same
// build and table, and the timemap.json and land.bin whose sha256 it names. A copy kept offline
// from an earlier build of the same map fails, so the page never draws car layouts on land that
// was never checked with them.
export function madeWith(modeDoc, doc, mapHash, landHash) {
  return Boolean(modeDoc) && modeDoc.build === doc.build && modeDoc.table_sha256 === doc.table_sha256 &&
    modeDoc.timemap_sha256 === mapHash && modeDoc.land_sha256 === landHash;
}
