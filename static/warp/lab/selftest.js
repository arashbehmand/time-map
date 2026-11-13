// The lab's self-test: gates G1–G5 of the W-01 plan (docs/warp-renderer-spike.md).
//   G1 parity        GPU positions (transform feedback) vs warp.js, ≤ 0.1 px; for back-end C
//                    with the placements the worker actually wrote
//   G2 registration  u = 0 vs MapLibre's map.project, ≤ 0.5 px, at three zooms
//   G3 radius        drawn: random field points rendered as dots, read back from pixels;
//                    |dot centre − pin| vs k·T ≤ 1 px at u = 1, and the dot on its ordinary
//                    position ≤ 0.5 px at u = 0 (this covers the clip-space and DPR mapping)
//   G4 chord         points on drawn segments and triangles vs the exact warp, sampled by
//                    their size on screen; ≥ 99% within 1 px away from folds. Near folds,
//                    near no-data, and primitives that cross the outer minute are reported
//                    on their own
//   G5 width         line widths read back from pixels: a segment where the map stretches
//                    ≥ 4:1, a chain of short segments with a kink, and segments at twelve
//                    angles; every one within ±0.25 px of nominal
import { makeCamera } from "../../shared/mesh/frame.js";
import { NO_DATA, sampleField } from "../../shared/mesh/field.js";
import { STYLE_ID } from "../../shared/mesh/schema-streets-v8.js";
import { targetsFor } from "../../shared/mesh/warp.js";

export const REFERENCE_VIEWS = [{ name: "phone", width: 390, height: 844 }, { name: "desktop", width: 1440, height: 900 }];
const N = 1000;

export function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// A reference camera centred on the origin, sized like a phone or a laptop.
export function referenceCamera(lab, view) {
  const { S } = lab;
  const max = S.spec.maxMinutes ?? lab.OPT.max;
  const fit = lab.fitFor(S.spec, S.frame, view.width, view.height, max);
  const cam = makeCamera(S.frame, {
    centerLon: S.origin.lon, centerLat: S.origin.lat, zoom: fit.zoom,
    width: view.width, height: view.height, k: fit.k,
  });
  if (S.spec.kind === "grid") cam.k = cam.ppm;
  return cam;
}

const round = (v, d = 3) => (Number.isFinite(v) ? Number(v.toFixed(d)) : v);
const pct = (a, p) => (a.length ? [...a].sort((x, y) => x - y)[Math.min(a.length - 1, Math.floor(p * a.length))] : 0);
const share = (a) => (a.length ? a.filter((e) => e <= 1).length / a.length : 1);

// Random vertices of the scene, with the worker's placements for them when it wrote some.
function sampleVertices(scene, n, rand) {
  const pools = [];
  for (const p of Object.values(scene.passes)) {
    if (!p.count) continue;
    pools.push(p.kind === "fill" ? { a: p.pos, t: p.targets, n: p.pos.length / 2 } : { a: p.seg, t: p.targets, n: p.seg.length / 2 });
  }
  const total = pools.reduce((s, p) => s + p.n, 0);
  const pts = new Float32Array(2 * n), targets = new Float32Array(3 * n);
  let haveTargets = true;
  for (let i = 0; i < n; i++) {
    let k = Math.floor(rand() * total), p = 0;
    while (k >= pools[p].n) { k -= pools[p].n; p++; }
    pts[2 * i] = pools[p].a[2 * k];
    pts[2 * i + 1] = pools[p].a[2 * k + 1];
    if (pools[p].t) targets.set(pools[p].t.subarray(3 * k, 3 * k + 3), 3 * i);
    else haveTargets = false;
  }
  return { pts, targets: haveTargets ? targets : null };
}

function g1Parity(lab, rand) {
  const { S } = lab, r = S.renderer;
  if (!r.evaluate) return { skipped: "no GPU evaluator for this back-end" };
  const { pts, targets } = sampleVertices(S.scene, N, rand);
  if (r.mode === "cpu" && !targets) return { skipped: "scene has no worker placements" };
  let max = 0, alphaMismatch = 0;
  for (const view of REFERENCE_VIEWS) {
    const cam = referenceCamera(lab, view);
    for (const u of [0, 0.5, 1]) {
      const gpu = r.evaluate(pts, cam, u, r.mode === "cpu" ? targets : null);
      for (let i = 0; i < N; i++) {
        const js = S.warp.screenOfLocal(pts[2 * i], pts[2 * i + 1], cam, u);
        max = Math.max(max, Math.hypot(gpu[3 * i] - js.x, gpu[3 * i + 1] - js.y));
        if (Math.abs(gpu[3 * i + 2] - js.alpha) > 1e-3) alphaMismatch++;
      }
    }
  }
  return { maxPx: round(max, 4), alphaMismatch, samples: N * 6, source: r.mode === "cpu" ? "worker placements" : "field texture", pass: max <= 0.1 && alphaMismatch === 0 };
}

function g2Registration(lab, rand) {
  const { S } = lab, map = S.map;
  const z0 = map.getZoom(), c0 = map.getCenter();
  let max = 0;
  for (const dz of [0, 1, 2]) {
    map.jumpTo({ center: [S.origin.lon, S.origin.lat], zoom: z0 + dz });
    const cam = lab.camera();
    const b = map.getBounds();
    for (let i = 0; i < Math.ceil(N / 3); i++) {
      const lon = b.getWest() + rand() * (b.getEast() - b.getWest());
      const lat = b.getSouth() + rand() * (b.getNorth() - b.getSouth());
      const ours = S.warp.toScreen(lon, lat, cam, 0);
      const theirs = map.project([lon, lat]);
      max = Math.max(max, Math.hypot(ours.x - theirs.x, ours.y - theirs.y));
    }
  }
  map.jumpTo({ center: c0, zoom: z0 });
  return { maxPx: round(max, 4), samples: 3 * Math.ceil(N / 3), pass: max <= 0.5 };
}

// Centres of dots drawn through the real line program (zero-length segments), read back.
// pts: local metres; returns CSS px centres (coverage-weighted), or null where nothing drew.
function drawnCentres(r, warp, cam, u, pts, want, style) {
  const out = new Array(pts.length / 2).fill(null);
  const R = 10;                                    // device px around the expected centre
  const order = [...out.keys()];
  while (order.length) {
    // A batch of dots at least 3R apart, so their windows don't overlap.
    const batch = [];
    for (let i = order.length - 1; i >= 0; i--) {
      const k = order[i];
      if (batch.every((b) => Math.hypot(want[b].x - want[k].x, want[b].y - want[k].y) > 3 * R)) { batch.push(k); order.splice(i, 1); }
    }
    const seg = new Float32Array(batch.length * 4), sty = new Float32Array(batch.length).fill(style);
    batch.forEach((k, j) => { seg.set([pts[2 * k], pts[2 * k + 1], pts[2 * k], pts[2 * k + 1]], 4 * j); });
    const targets = r.mode === "cpu" ? targetsFor(warp, seg) : null;
    const probe = r.renderLines(cam, u, { seg, style: sty, targets }, [0, 0, 1, 1]);
    const dpr = probe.dpr, W = Math.round(cam.width * dpr), H = Math.round(cam.height * dpr);
    const all = r.renderLines(cam, u, { seg, style: sty, targets }, [0, 0, W, H]);
    batch.forEach((k) => {
      const cx = want[k].x * dpr, cy = want[k].y * dpr;
      let s = 0, sx = 0, sy = 0;
      for (let y = Math.max(0, Math.floor(cy - R)); y < Math.min(H, Math.ceil(cy + R)); y++) {
        for (let x = Math.max(0, Math.floor(cx - R)); x < Math.min(W, Math.ceil(cx + R)); x++) {
          const c = all.cov[y * W + x];
          s += c; sx += c * (x + 0.5); sy += c * (y + 0.5);
        }
      }
      if (s > 0.5) out[k] = { x: sx / s / dpr, y: sy / s / dpr };
    });
  }
  return out;
}

function g3Radius(lab, rand) {
  const { S } = lab, r = S.renderer;
  if (S.spec.kind !== "polar") return { skipped: "grid layouts have no travel time" };
  if (!r.renderLines) return { skipped: "no pixel read-back for this back-end" };
  const f = S.spec.field, maxMin = S.spec.maxMinutes ?? lab.OPT.max;
  const cam = referenceCamera(lab, REFERENCE_VIEWS[0]);
  const pts = [];
  for (let tries = 0; pts.length < 2 * 300 && tries < 100000; tries++) {
    const x = f.x0 + rand() * (f.nx - 1) * f.cell, y = f.y0 + rand() * (f.ny - 1) * f.cell;
    const t = sampleField(f, x, y);
    if (t === NO_DATA || t > maxMin || Math.hypot(x, y) < 50) continue;
    const s = S.warp.screenOfLocal(x, y, cam, 1), g = S.warp.screenOfLocal(x, y, cam, 0);
    const inView = (p) => p.x > 12 && p.y > 12 && p.x < cam.width - 12 && p.y < cam.height - 12;
    if (!inView(s) || !inView(g)) continue;
    pts.push(x, y);
  }
  const P = Float32Array.from(pts), n = P.length / 2;
  const at = (u) => Array.from({ length: n }, (_, i) => S.warp.screenOfLocal(P[2 * i], P[2 * i + 1], cam, u));
  const bent = drawnCentres(r, S.warp, cam, 1, P, at(1), STYLE_ID.motorway);
  const flat = drawnCentres(r, S.warp, cam, 0, P, at(0), STYLE_ID.motorway);
  const radiusErr = [], placeErr = [];
  const ordinary = at(0);
  for (let i = 0; i < n; i++) {
    const t = sampleField(f, P[2 * i], P[2 * i + 1]);
    if (bent[i]) radiusErr.push(Math.abs(Math.hypot(bent[i].x - cam.pin[0], bent[i].y - cam.pin[1]) - cam.k * t));
    if (flat[i]) placeErr.push(Math.hypot(flat[i].x - ordinary[i].x, flat[i].y - ordinary[i].y));
  }
  const maxR = Math.max(0, ...radiusErr), maxP = Math.max(0, ...placeErr);
  return {
    method: "dots drawn through the line program, centres read back from pixels (phone view)",
    samples: n, drawn: radiusErr.length, radiusMaxPx: round(maxR), radiusP99Px: round(pct(radiusErr, 0.99)),
    ordinaryMaxPx: round(maxP), pass: radiusErr.length === n && maxR <= 1 && maxP <= 0.5,
  };
}

// Chord error at a reference view: what the GPU draws between mapped vertices vs the exact
// warp of the same point, for primitives picked by their size on screen at u = 1 (so the
// long far-field ones count as much as they show). Exported for the benchmark.
export function chordError(lab, scene, cam, rand, n = 4000) {
  const { S } = lab, warp = S.warp;
  const cell = S.spec.kind === "polar" ? S.spec.field.cell : S.spec.grid.cell;
  const prims = [];                                 // [weight, kind, pass, k]
  let total = 0;
  const mixedEdges = [];
  for (const p of Object.values(scene.passes)) {
    if (!p.count) continue;
    if (p.kind === "line") {
      for (let k = 0; k < p.count; k++) {
        const g = p.seg;
        const a = warp.screenOfLocal(g[4 * k], g[4 * k + 1], cam, 1), b = warp.screenOfLocal(g[4 * k + 2], g[4 * k + 3], cam, 1);
        if (a.alpha < 1 !== b.alpha < 1) {
          const a5 = warp.screenOfLocal(g[4 * k], g[4 * k + 1], cam, 0.5), b5 = warp.screenOfLocal(g[4 * k + 2], g[4 * k + 3], cam, 0.5);
          mixedEdges.push(Math.hypot(a5.x - b5.x, a5.y - b5.y));
        }
        const w = Math.hypot(a.x - b.x, a.y - b.y);
        if (w > 0) { total += w; prims.push([total, "line", p, k]); }
      }
    } else {
      for (let k = 0; k < p.count / 3; k++) {
        const v = [0, 1, 2].map((c) => p.index[3 * k + c]);
        const s = v.map((i) => warp.screenOfLocal(p.pos[2 * i], p.pos[2 * i + 1], cam, 1));
        const known = s.filter((q) => q.alpha === 1).length;
        if (known > 0 && known < 3) {
          const s5 = v.map((i) => warp.screenOfLocal(p.pos[2 * i], p.pos[2 * i + 1], cam, 0.5));
          mixedEdges.push(Math.max(...[0, 1, 2].map((e) => Math.hypot(s5[e].x - s5[(e + 1) % 3].x, s5[e].y - s5[(e + 1) % 3].y))));
        }
        const w = Math.abs((s[1].x - s[0].x) * (s[2].y - s[0].y) - (s[1].y - s[0].y) * (s[2].x - s[0].x)) / 2;
        if (w > 0) { total += w; prims.push([total, "fill", p, k]); }
      }
    }
  }
  const pick = () => {
    const r = rand() * total;
    let lo = 0, hi = prims.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (prims[mid][0] < r) lo = mid + 1; else hi = mid; }
    return prims[lo];
  };
  const near = (x, y) => {
    let noData = false;
    for (const [dx, dy] of [[0, 0], [cell, 0], [-cell, 0], [0, cell], [0, -cell]]) {
      const j = warp.jacobian(x + dx, y + dy);
      if (!j) noData = true;
      else if (j.det <= 0) return "fold";
    }
    return noData ? "noData" : null;
  };
  const errs = [], fold = [], noData = [], byZoom = {};
  let unknown = 0;
  for (let s = 0; s < n && prims.length; s++) {
    const [, kind, p, k] = pick();
    let pts, w, z;
    if (kind === "line") {
      const g = p.seg, t = rand();
      pts = [[g[4 * k], g[4 * k + 1]], [g[4 * k + 2], g[4 * k + 3]]];
      w = [1 - t, t];
      z = p.zoom ? p.zoom[k] : 0;
    } else {
      const idx = p.index, pos = p.pos;
      pts = [0, 1, 2].map((c) => [pos[2 * idx[3 * k + c]], pos[2 * idx[3 * k + c] + 1]]);
      let a = rand(), b = rand();
      if (a + b > 1) { a = 1 - a; b = 1 - b; }
      w = [1 - a - b, a, b];
      z = p.zoom ? p.zoom[idx[3 * k]] : 0;
    }
    const sx = pts.map(([x, y]) => warp.screenOfLocal(x, y, cam, 1));
    const px = pts.reduce((acc, q, i) => acc + q[0] * w[i], 0), py = pts.reduce((acc, q, i) => acc + q[1] * w[i], 0);
    const exact = warp.screenOfLocal(px, py, cam, 1);
    // Unknown places are invisible at u = 1 (alpha 0): their error doesn't show.
    if (exact.alpha < 1 && sx.every((q) => q.alpha < 1)) { unknown++; continue; }
    const dx = sx.reduce((acc, q, i) => acc + q.x * w[i], 0), dy = sx.reduce((acc, q, i) => acc + q.y * w[i], 0);
    const e = Math.hypot(dx - exact.x, dy - exact.y);
    const where = near(px, py);
    (where === "fold" ? fold : where === "noData" ? noData : errs).push(e);
    if (!where) (byZoom[z] = byZoom[z] || []).push(e);
  }
  return {
    samples: errs.length, within1px: round(share(errs), 4), p99Px: round(pct(errs, 0.99)), maxPx: round(pct(errs, 1)),
    byZoom: Object.fromEntries(Object.entries(byZoom).map(([z, a]) => [z, { samples: a.length, within1px: round(share(a), 4), p99Px: round(pct(a, 0.99)) }])),
    nearFold: { samples: fold.length, within1px: round(share(fold), 4), p99Px: round(pct(fold, 0.99)) },
    nearNoData: { samples: noData.length, within1px: round(share(noData), 4), p99Px: round(pct(noData, 0.99)) },
    invisible: unknown,
    crossingOuterMinute: { primitives: mixedEdges.length, longestEdgeAtHalfPx: round(Math.max(0, ...mixedEdges), 1), p99EdgeAtHalfPx: round(pct(mixedEdges, 0.99), 1) },
  };
}

function g4Chord(lab, rand) {
  const per = {};
  let pass = true;
  for (const view of REFERENCE_VIEWS) {
    per[view.name] = chordError(lab, lab.S.scene, referenceCamera(lab, view), rand);
    if (per[view.name].within1px < 0.99) pass = false;
  }
  return { ...per, pass };
}

// deck.gl's position hook runs after LineLayer has extruded the width from the ordinary
// (geographic) direction; this probe moves each corner by its end's displacement, which
// keeps that offset, so the drawn width across the bent segment is nominal × |cos(turn)|.
// Computed from that geometry, not read from pixels.
function g5WidthDeck(lab, rand) {
  const { S } = lab, warp = S.warp;
  const cam = referenceCamera(lab, REFERENCE_VIEWS[0]);
  const lines = Object.values(S.scene.passes).filter((p) => p.kind === "line" && p.count);
  const ratios = [];
  for (let s = 0; s < 4000 && lines.length; s++) {
    const p = lines[Math.floor(rand() * lines.length)], k = Math.floor(rand() * p.count), g = p.seg;
    const a0 = warp.screenOfLocal(g[4 * k], g[4 * k + 1], cam, 0), b0 = warp.screenOfLocal(g[4 * k + 2], g[4 * k + 3], cam, 0);
    const a1 = warp.screenOfLocal(g[4 * k], g[4 * k + 1], cam, 1), b1 = warp.screenOfLocal(g[4 * k + 2], g[4 * k + 3], cam, 1);
    if (a1.alpha < 1 || b1.alpha < 1) continue;
    const t0 = Math.atan2(b0.y - a0.y, b0.x - a0.x), t1 = Math.atan2(b1.y - a1.y, b1.x - a1.x);
    if (Math.hypot(b1.x - a1.x, b1.y - a1.y) < 0.5) continue;
    ratios.push(Math.abs(Math.cos(t1 - t0)));
  }
  ratios.sort((x, y) => x - y);
  const sh = (f) => ratios.filter(f).length / Math.max(1, ratios.length);
  return {
    method: "analytic: the probe's translation-only adapter (stock hook)", segments: ratios.length,
    medianWidthRatio: round(ratios[Math.floor(ratios.length / 2)] ?? NaN),
    under75pct: round(sh((v) => v < 0.75)), under25pct: round(sh((v) => v < 0.25)),
    pass: sh((v) => v < 0.9) === 0,
  };
}

// Width of a drawn path from pixels: coverage inside a rectangle ÷ path length inside it.
function widthIn(cov, rect, path, dpr) {
  const [rx, ry, rw, rh] = rect;
  let sum = 0;
  for (let i = 0; i < rw * rh; i++) sum += cov[i];
  let inside = 0;
  for (let i = 1; i < path.length; i++) {
    const [ax, ay] = path[i - 1], [bx, by] = path[i];
    const steps = Math.ceil(Math.hypot(bx - ax, by - ay) * 20);
    for (let s = 0; s < steps; s++) {
      const t = (s + 0.5) / steps, x = ax + (bx - ax) * t, y = ay + (by - ay) * t;
      if (x >= rx && x < rx + rw && y >= ry && y < ry + rh) inside += Math.hypot(bx - ax, by - ay) / steps;
    }
  }
  return inside > 0 ? sum / inside / dpr : NaN;
}

// A path given in CSS px at u = 0 on the reference view → local metres (the camera is
// centred on the origin, so screen offsets from the pin are metres × ppm).
function pathToLocal(cam, pts) {
  return pts.map(([x, y]) => [(x - cam.pin[0]) / cam.ppm, (y - cam.pin[1]) / cam.ppm]);
}

function drawPath(r, warp, cam, u, local, style) {
  const seg = new Float32Array((local.length - 1) * 4);
  for (let i = 1; i < local.length; i++) seg.set([...local[i - 1], ...local[i]], 4 * (i - 1));
  const sty = new Float32Array(local.length - 1).fill(style);
  return { seg, style: sty, targets: r.mode === "cpu" ? targetsFor(warp, seg) : null };
}

function g5Width(lab, rand) {
  const { S } = lab, r = S.renderer, warp = S.warp;
  if (r.mode === "deck") return g5WidthDeck(lab, rand);
  if (!r.renderLines) return { skipped: "no pixel read-back for this back-end" };
  const style = STYLE_ID.primary;
  const results = {};
  const fails = [];

  // 1. A chain of 4-px segments, straight then turning 30°, on the ordinary map (u = 0),
  //    where every joint overlaps two capsules.
  const cam = referenceCamera(lab, REFERENCE_VIEWS[0]);
  const cx = cam.width / 2, cy = cam.height / 2 + 120;
  const pts = [];
  for (let i = 0; i <= 30; i++) pts.push([cx - 120 + 4 * i, cy]);
  const turn = (30 * Math.PI) / 180;
  for (let i = 1; i <= 30; i++) pts.push([cx + 4 * i * Math.cos(turn), cy + 4 * i * Math.sin(turn)]);
  const chain = drawPath(r, warp, cam, 0, pathToLocal(cam, pts), style);
  const probe = r.renderLines(cam, 0, chain, [0, 0, 1, 1]);
  const dpr = probe.dpr, nominal = probe.widths[style];
  const devPts = pts.map(([x, y]) => [x * dpr, y * dpr]);
  const straightRect = [Math.round((cx - 100) * dpr), Math.round((cy - 12) * dpr), Math.round(80 * dpr), Math.round(24 * dpr)];
  const kinkRect = [Math.round((cx - 20) * dpr), Math.round((cy - 20) * dpr), Math.round(40 * dpr), Math.round(40 * dpr)];
  const s1 = r.renderLines(cam, 0, chain, straightRect), s2 = r.renderLines(cam, 0, chain, kinkRect);
  results.chainStraightPx = round(widthIn(s1.cov, straightRect, devPts, dpr));
  results.chainKinkPx = round(widthIn(s2.cov, kinkRect, devPts, dpr));

  // 2. Single 120-px segments at twelve angles.
  const angles = [];
  for (let k = 0; k < 12; k++) {
    const a = (k * Math.PI) / 12, half = 60;
    const p = [[cx - half * Math.cos(a), cam.height / 2 - 150 - half * Math.sin(a)], [cx + half * Math.cos(a), cam.height / 2 - 150 + half * Math.sin(a)]];
    const one = drawPath(r, warp, cam, 0, pathToLocal(cam, p), style);
    const mid = [(p[0][0] + p[1][0]) / 2, (p[0][1] + p[1][1]) / 2];
    const rect = [Math.round((mid[0] - 20) * dpr), Math.round((mid[1] - 20) * dpr), Math.round(40 * dpr), Math.round(40 * dpr)];
    const got = r.renderLines(cam, 0, one, rect);
    angles.push(widthIn(got.cov, rect, p.map(([x, y]) => [x * dpr, y * dpr]), dpr));
  }
  results.anglesMinPx = round(Math.min(...angles));
  results.anglesMaxPx = round(Math.max(...angles));

  // 3. One segment where the bent map stretches most (≥ 4:1), at u = 1 on the live view.
  const live = lab.camera();
  const lines = Object.values(S.scene.passes).filter((p) => p.kind === "line" && p.count);
  let best = null;
  for (let s = 0; s < 4000 && lines.length; s++) {
    const p = lines[Math.floor(rand() * lines.length)], k = Math.floor(rand() * p.count), g = p.seg;
    const mx = (g[4 * k] + g[4 * k + 2]) / 2, my = (g[4 * k + 1] + g[4 * k + 3]) / 2;
    const j = warp.jacobian(mx, my);
    if (!j || j.det <= 0 || !Number.isFinite(j.stretch)) continue;
    const m = warp.screenOfLocal(mx, my, live, 1);
    if (m.alpha < 1 || m.x < 60 || m.y < 60 || m.x > live.width - 60 || m.y > live.height - 60) continue;
    if (!best || j.stretch > best.stretch) best = { stretch: j.stretch, g, k, mx, my };
  }
  if (best && best.stretch >= 4) {
    const { g, k } = best;
    const dx = g[4 * k + 2] - g[4 * k], dy = g[4 * k + 3] - g[4 * k + 1];
    let s = 0.5;
    for (let i = 0; i < 30; i++) {
      const sa = warp.screenOfLocal(best.mx - dx * s, best.my - dy * s, live, 1), sb = warp.screenOfLocal(best.mx + dx * s, best.my + dy * s, live, 1);
      if (sa.alpha < 1 || sb.alpha < 1) { s /= 1.5; break; }
      if (Math.hypot(sb.x - sa.x, sb.y - sa.y) >= 100) break;
      s *= 1.5;
    }
    const local = [[best.mx - dx * s, best.my - dy * s], [best.mx + dx * s, best.my + dy * s]];
    const scr = local.map(([x, y]) => warp.screenOfLocal(x, y, live, 1));
    if (Math.hypot(scr[1].x - scr[0].x, scr[1].y - scr[0].y) >= 60) {
      const one = drawPath(r, warp, live, 1, local, style);
      const p0 = r.renderLines(live, 1, one, [0, 0, 1, 1]);
      const mid = [(scr[0].x + scr[1].x) / 2, (scr[0].y + scr[1].y) / 2];
      const rect = [Math.round((mid[0] - 20) * p0.dpr), Math.round((mid[1] - 20) * p0.dpr), Math.round(40 * p0.dpr), Math.round(40 * p0.dpr)];
      const got = r.renderLines(live, 1, one, rect);
      results.stretched = { stretch: round(best.stretch, 1), widthPx: round(widthIn(got.cov, rect, scr.map((q) => [q.x * p0.dpr, q.y * p0.dpr]), p0.dpr)), nominalPx: round(p0.widths[style]) };
      if (Math.abs(results.stretched.widthPx - results.stretched.nominalPx) > 0.25) fails.push("stretched");
    }
  } else {
    results.stretched = { skipped: `no on-screen segment stretched 4:1 (best ${best ? round(best.stretch, 1) : "none"})` };
  }
  results.nominalPx = round(nominal);
  if (Math.abs(results.chainStraightPx - nominal) > 0.25) fails.push("chain");
  if (Math.abs(results.chainKinkPx - nominal) > 0.25) fails.push("kink");
  if (angles.some((w) => Math.abs(w - nominal) > 0.25)) fails.push("angles");
  return { ...results, dpr, failed: fails, pass: fails.length === 0 };
}

export async function runSelfTest(lab) {
  const { S, OPT } = lab;
  const rand = rng(20261113);
  const result = {
    kind: "selftest", at: new Date().toISOString(), ua: navigator.userAgent,
    backend: OPT.backend, mode: S.renderer.mode, fellBack: !!S.renderer.fellBack,
    programsError: S.renderer.caps ? S.renderer.caps.programs || null : null,
    field: OPT.field, origin: S.origin.id, geom: OPT.geom,
    spacing: OPT.spacing, renderer: S.renderer.caps ? S.renderer.caps.renderer : "deck.gl",
    caps: S.renderer.caps ? { gpu: S.renderer.caps.gpu, floatLinear: S.renderer.caps.floatLinear, colorBufferFloat: S.renderer.caps.colorBufferFloat, maxVertexTextureUnits: S.renderer.caps.maxVertexTextureUnits } : null,
    gates: {},
  };
  const run = (name, fn) => {
    try { result.gates[name] = fn(); }
    catch (e) { result.gates[name] = { error: String(e.message || e), pass: false }; }
  };
  run("G1_parity", () => g1Parity(lab, rand));
  run("G2_registration", () => g2Registration(lab, rand));
  run("G3_radius", () => g3Radius(lab, rand));
  run("G4_chord", () => g4Chord(lab, rand));
  run("G5_width", () => g5Width(lab, rand));
  S.dirty = true;
  return result;
}
