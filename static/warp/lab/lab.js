// The renderer lab (issue #5, W-01): one page that draws the same tiles through back-end
// B (GPU warp), C (CPU warp in the worker) or A (deck.gl probe), and measures them.
// Query switches: origin, field, backend=b|c|a, geom=synthetic|tiles (tiles spends the tile
// budget), spacing, max, caps=no-vtf|no-r32f|no-webgl2, folds=1, census=1, selftest=1, bench=1,
// spread=0..1 (directions by reach, default 0.5), style=plain, network=0, budget.
import { tileCover } from "../../shared/mesh/cover.js";
import { cameraFromMap, WORLD_PX } from "../../shared/mesh/frame.js";
import { sampleField } from "../../shared/mesh/field.js";
import { buildTapIndex, LABEL_CAP, placeLabels, sortCandidates } from "../../shared/mesh/labels.js";
import { DEFAULT_BUDGET, DEFAULT_SPACING_DIV } from "../../shared/mesh/pipeline.js";
import { createMeshRenderer } from "../../shared/mesh/renderer.js";
import { maplibreStyle } from "../../shared/mesh/schema-streets-v8.js";
import { angularSpread, createWarp, dotScale, targetsFor } from "../../shared/mesh/warp.js";
import { FIELD_HALF, GRID_FIELDS, makeWarpSpec, POLAR_FIELDS } from "./fields.js";

const Q = new URLSearchParams(location.search);
const OPT = {
  origin: Q.get("origin") || "baker-street",
  field: Q.get("field") || "transit",
  backend: Q.get("backend") || "b",
  // Synthetic unless asked: real tiles go through the billable /tiles proxy.
  geom: Q.get("geom") === "tiles" ? "tiles" : "synthetic",
  spacing: Number(Q.get("spacing")) || DEFAULT_SPACING_DIV,
  max: Number(Q.get("max")) || 45,
  caps: Q.get("caps") || "",
  folds: Q.get("folds") === "1",
  // The scale-aware style (line widths, fades and dots follow the bent map's local scale);
  // ?style=plain draws every line at its nominal width.
  adaptive: Q.get("style") !== "plain",
  // The toy network drawn schematically on the transit field (?network=0 hides it).
  network: Q.get("network") !== "0",
  // Directions by reach (warp.js angularSpread): 0 keeps every bearing, 1 gives each
  // direction the share of the circle it holds of the area reachable on each ring; the
  // default, 0.5, goes halfway.
  spread: Q.has("spread") ? Math.min(1, Math.max(0, Number(Q.get("spread")) || 0)) : 0.5,
  budget: Number(Q.get("budget")) || DEFAULT_BUDGET,
};
const MORPH_MS = 1800;
const RING_FROM_U = 0.9;          // the time rings fade in over the last tenth of the morph
const REACH_MARGIN_M = 1000;      // tiles beyond the outer minute, for the fade-out
const MISSING_RETRY_MS = 5000;
const RESULTS_KEY = "timemap-mesh-lab-results";
const $ = (id) => document.getElementById(id);
const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);
const reducedMotion = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// ── State ────────────────────────────────────────────────────────────
// The active view (origin, spec, warp, frame, scene) changes only all at once, when the
// scene for a new origin arrives: until then the old one stays on screen, consistent.
const S = {
  opt: OPT, network: null, example: null,
  origin: null, spec: null, warp: null, frame: null, cover: null, zRef: 12, kRef: 1,
  pending: null,
  u: 0, anim: null,
  scene: null, sceneInfo: null, gen: 0, labels: [], placed: [], tap: null,
  frameTimes: [], drawTimes: [], lastFrame: 0, dirty: true, lastKey: "",
  renderer: null, deck: null, map: null, worker: null, waiters: [], drawHooks: [],
  lostContext: false, foldFor: null, missingRetry: null,
};

function storeResult(kind, value) {
  let all = {};
  try { all = JSON.parse(localStorage.getItem(RESULTS_KEY) || "{}"); } catch { /* storage blocked */ }
  (all[kind] = all[kind] || []).push(value);
  try { localStorage.setItem(RESULTS_KEY, JSON.stringify(all)); } catch { /* storage blocked */ }
  S.lastResults = all;
  return all;
}
function allResults() {
  try { return JSON.parse(localStorage.getItem(RESULTS_KEY) || "{}"); } catch { return S.lastResults || {}; }
}
function out(text) { $("out").textContent = text; }

// ── Camera and fit ───────────────────────────────────────────────────
const isGrid = () => S.spec && S.spec.kind === "grid";

// k: CSS px per layout unit. Polar: the outer ring at 0.42 of the short side at the fit zoom.
// Grid: layout units are metres, so k is the map's own px per metre.
function camera() {
  const cam = cameraFromMap(S.map, S.frame, 1);
  cam.k = isGrid() ? cam.ppm : S.kRef * Math.pow(2, cam.zoom - S.zRef);
  return cam;
}

function reachable(spec, maxMinutes, fn) {
  const f = spec.field;
  for (let j = 0; j < f.ny; j += 2) for (let i = 0; i < f.nx; i += 2) {
    const t = f.minutes[j * f.nx + i];
    if (t >= 0 && t <= maxMinutes) fn(f.x0 + i * f.cell, f.y0 + j * f.cell);
  }
}

// The zoom that puts most of the reachable area (90th percentile distance) on screen.
export function fitFor(spec, frame, width, height, maxMinutes) {
  let R = 12000;
  if (spec.kind === "polar") {
    const d = [];
    reachable(spec, maxMinutes, (x, y) => d.push(Math.hypot(x, y)));
    d.sort((a, b) => a - b);
    if (d.length) R = Math.max(500, d[Math.floor(d.length * 0.9)]);
  }
  const short = Math.min(width, height);
  const ppm = (0.45 * short) / R;
  const zoom = Math.log2((ppm * frame.m) / WORLD_PX);
  return { zoom, k: (0.42 * short) / maxMinutes };
}

// The tiles only need to cover what is reachable (plus a margin for the fade-out), not the
// whole field: a 45-minute walk needs a few kilometres, not ±25 km.
export function reachBox(spec, maxMinutes) {
  if (spec.kind !== "polar") return [-FIELD_HALF, -FIELD_HALF, FIELD_HALF, FIELD_HALF];
  let x0 = 0, y0 = 0, x1 = 0, y1 = 0;
  reachable(spec, maxMinutes, (x, y) => { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); });
  const m = REACH_MARGIN_M;
  return [Math.max(-FIELD_HALF, x0 - m), Math.max(-FIELD_HALF, y0 - m), Math.min(FIELD_HALF, x1 + m), Math.min(FIELD_HALF, y1 + m)];
}

// ── Scene requests ───────────────────────────────────────────────────
function needsCpuTargets() {
  // C and A need per-vertex placements from the worker; B evaluates them on the GPU.
  return !(OPT.backend === "b" && S.renderer && S.renderer.mode === "gpu");
}

function requestScene(view = S.pending || S, { clear = false } = {}) {
  S.gen++;
  view.gen = S.gen;
  if (clear) S.worker.postMessage({ type: "clear", gen: S.gen });
  S.requestedAt = performance.now();
  S.worker.postMessage({
    type: "scene", gen: S.gen, origin: view.frame.origin, tiles: view.cover.tiles,
    spacingDiv: OPT.spacing, budget: OPT.budget, source: OPT.geom,
    syntheticUrl: new URL("./synthetic-tiles.js", import.meta.url).href, network: S.network,
    warp: needsCpuTargets() ? view.spec : null,
  });
  return S.gen;
}

// Resolves after the first successful draw of the complete scene for generation `gen` (or a
// newer one); rejects if the worker or the draw fails first.
function waitScene(gen = S.gen) {
  return new Promise((resolve, reject) => S.waiters.push({ gen, resolve, reject }));
}
function failWaiters(why) {
  const w = S.waiters;
  S.waiters = [];
  S.drawHooks = [];
  for (const x of w) x.reject(new Error(why));
}

// Make a staged origin the active view, together with its first scene.
function activate(view) {
  S.origin = view.origin; S.spec = view.spec; S.warp = view.warp; S.frame = view.frame;
  S.cover = view.cover; S.zRef = view.zRef; S.kRef = view.kRef;
  S.pending = null;
  S.uploadMs = S.renderer && S.renderer.setWarp ? S.renderer.setWarp(S.spec) : 0;
  S.foldFor = null;
  if (view.reframe) S.map.jumpTo({ center: [view.origin.lon, view.origin.lat], zoom: view.zRef });
  clearTimeout(S.missingRetry);
  S.missingRetry = null;
}

function onWorker(e) {
  const msg = e.data;
  if (msg.type === "error") { out(`Worker error: ${msg.message}`); failWaiters(`worker: ${msg.message}`); return; }
  if (msg.type === "census" || msg.type === "census-progress") { if (S.onCensus) S.onCensus(msg); return; }
  if (msg.type !== "scene" || msg.gen !== S.gen) return;           // stale
  const t0 = performance.now();
  let warpMs = 0;
  if (S.pending && S.pending.gen === msg.gen) { activate(S.pending); warpMs = S.uploadMs; }
  const upload = S.deck ? S.deck.setScene(msg.scene) : S.renderer.setScene ? S.renderer.setScene(msg.scene) : 0;
  S.scene = msg.scene;
  S.sceneInfo = { ...msg.scene.stats, uploadMs: upload + warpMs, roundTripMs: t0 - S.requestedAt };
  S.labels = sortCandidates(msg.scene.labels, S.spec.kind === "polar" ? (x, y) => sampleField(S.spec.field, x, y) : null);
  S.dirty = true;
  if (OPT.folds) buildFoldLayer();
  if (!msg.partial) {
    const ready = S.waiters.filter((w) => w.gen <= msg.gen);
    S.waiters = S.waiters.filter((w) => w.gen > msg.gen);
    S.drawHooks.push((info) => ready.forEach((w) => w.resolve({ scene: msg.scene, info: S.sceneInfo, drawnAt: info.at })));
    // Tiles that failed: ask again once, later (they are not cached, so only they are fetched).
    if (msg.scene.stats.missing > 0 && !S.missingRetry) {
      S.missingRetry = setTimeout(() => { if (!S.pending) requestScene(S); }, MISSING_RETRY_MS);
    }
  }
}

// ── Origin and field ─────────────────────────────────────────────────
function originById(id) {
  if (OPT.field === "contours") {
    const o = S.example.origin;
    return { id: "kings-cross", label: o.label, lon: o.lon, lat: o.lat };
  }
  return S.network.origins.find((o) => o.id === id) || S.network.origins[0];
}

// Stage an origin: build its field, warp and tile cover, and ask for its scene. The view
// switches when that scene arrives (activate), so nothing is drawn with mismatched origins.
function setOrigin(id, { frame: reframe = true, clear = false } = {}) {
  const origin = originById(id);
  const spec = makeWarpSpec(OPT.field, origin, S.network, { example: S.example, maxMinutes: OPT.max });
  if (spec.kind === "polar" && OPT.spread > 0) spec.angles = angularSpread(spec.field, spec.maxMinutes ?? OPT.max, { spread: OPT.spread });
  const warp = createWarp(spec);
  const el = S.map.getContainer();
  const max = spec.maxMinutes ?? OPT.max;
  const fit = fitFor(spec, warp.frame, el.clientWidth, el.clientHeight, max);
  S.pending = {
    origin, spec, warp, frame: warp.frame, zRef: fit.zoom, kRef: fit.k, reframe,
    cover: tileCover(warp.frame, reachBox(spec, max)),
  };
  return requestScene(S.pending, { clear });
}

// ── Fold layer (lab): the field's own cells, tinted where they flip ──
function buildFoldLayer() {
  if (!S.renderer || !S.renderer.setFoldMesh || S.spec.kind !== "polar") return;
  if (S.foldFor === S.spec) return;
  S.foldFor = S.spec;
  const f = S.spec.field, step = 2, n = Math.floor((f.nx - 1) / step) + 1;
  const pos = new Float32Array(n * n * 2), index = [];
  for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) {
    pos[2 * (j * n + i)] = f.x0 + i * step * f.cell;
    pos[2 * (j * n + i) + 1] = f.y0 + j * step * f.cell;
  }
  for (let j = 0; j < n - 1; j++) for (let i = 0; i < n - 1; i++) {
    const v = j * n + i;
    index.push(v, v + 1, v + n + 1, v, v + n + 1, v + n);
  }
  S.renderer.setFoldMesh({ pos, index: Uint32Array.from(index), targets: needsCpuTargets() ? targetsFor(S.warp, pos) : null });
}

// ── Morph ────────────────────────────────────────────────────────────
function morphTo(to, dur = reducedMotion() ? 0 : MORPH_MS) {
  S.anim = { from: S.u, to, start: performance.now(), dur };
  return new Promise((resolve) => { S.anim.resolve = resolve; });
}

function stepMorph(now) {
  const a = S.anim;
  if (!a) return;
  const t = a.dur > 0 ? Math.min(1, (now - a.start) / a.dur) : 1;
  S.u = a.from + (a.to - a.from) * ease(t);
  $("u").value = String(S.u);
  if (t >= 1) {
    S.anim = null;
    $("morph").textContent = S.u > 0.5 ? "Unbend" : "Bend";
    if (a.resolve) a.resolve();
  }
}

// ── Labels ───────────────────────────────────────────────────────────
const overlay = $("overlay"), ctx = overlay.getContext("2d");
const FONTS = { station: "600 12px system-ui, sans-serif", place: "12px system-ui, sans-serif" };
const PIN_R = 10;
// The toy network's line colours (close to TfL's, darkened where needed to read on the map).
const LINE_COLORS = {
  Metropolitan: "#9b0056", Jubilee: "#56616a", Elizabeth: "#6950a1", Piccadilly: "#003688",
  DLR: "#00a4a7", "Southern via Clapham": "#3f8f2f", "Thameslink via Norwood": "#d6007f",
};
const NETWORK_FROM_U = 0.3;       // the schematic network fades in over the rest of the morph

function measure(text, type) { ctx.font = FONTS[type] || FONTS.place; return ctx.measureText(text).width; }

// Placed for what is on screen now (current camera and u), every drawn frame, so labels never
// collide mid-morph and a tap queries the positions being drawn.
function placeLabelsNow(cam) {
  if (!S.scene) { S.placed = []; S.tap = null; return; }
  const u = S.u;
  const screenOf = (x, y) => S.warp.screenOfLocal(x, y, cam, u);
  const phone = Math.min(cam.width, cam.height) < 600;
  S.placed = placeLabels(S.labels, {
    screenOf, measure,
    jacobianOf: (x, y) => S.warp.screenJacobian(x, y, cam, u),
    minutesOf: S.spec.kind === "polar" ? (x, y) => sampleField(S.spec.field, x, y) : null,
    width: cam.width, height: cam.height, cap: phone ? LABEL_CAP.phone : LABEL_CAP.desktop,
    obstacles: [[cam.pin[0] - PIN_R, cam.pin[1] - PIN_R, cam.pin[0] + PIN_R, cam.pin[1] + PIN_R]],
  });
  S.tap = buildTapIndex(S.labels, screenOf);
}

// ── Drawing ──────────────────────────────────────────────────────────
function drawOverlay(cam) {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const w = Math.round(cam.width * dpr), h = Math.round(cam.height * dpr);
  if (overlay.width !== w || overlay.height !== h) { overlay.width = w; overlay.height = h; }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cam.width, cam.height);
  const u = S.u;
  // The rings are exact only on the bent map, so they appear only at the end of the morph.
  const ringAlpha = Math.max(0, Math.min(1, (u - RING_FROM_U) / (1 - RING_FROM_U)));
  if (!isGrid() && ringAlpha > 0) {
    const max = S.spec.maxMinutes ?? OPT.max;
    ctx.save();
    ctx.globalAlpha = ringAlpha;
    for (let m = 5; m <= max + 1e-9; m += 5) {
      const major = m % 15 === 0;
      ctx.beginPath();
      ctx.arc(cam.pin[0], cam.pin[1], cam.k * m, 0, 2 * Math.PI);
      ctx.strokeStyle = major ? "rgba(42, 95, 191, 0.85)" : "rgba(42, 95, 191, 0.35)";
      ctx.lineWidth = major ? 2 : 1;
      ctx.stroke();
      if (major) {
        ctx.font = "bold 12px system-ui, sans-serif";
        ctx.fillStyle = "#2a5fbf";
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillText(`${m} min`, cam.pin[0] + cam.k * m * Math.SQRT1_2 + 4, cam.pin[1] - cam.k * m * Math.SQRT1_2 - 4);
      }
    }
    ctx.restore();
  }
  drawNetwork(cam, u);
  placeLabelsNow(cam);
  // Every station gets a dot, labelled or not, so one that loses a collision still shows
  // where it is (a tap lists it). In the scale-aware style a dot shrinks where the bent map
  // is crowded and grows where it is open, and a labelled station's dot is a little bigger.
  const labelled = new Set(S.placed.filter((l) => l.type === "station").map((l) => `${l.x},${l.y}`));
  for (const l of S.labels) {
    if (l.type !== "station") continue;
    const p = S.warp.screenOfLocal(l.x, l.y, cam, u);
    if (p.alpha < 0.5 || p.x < 0 || p.y < 0 || p.x > cam.width || p.y > cam.height) continue;
    const r = OPT.adaptive ? (labelled.has(`${l.x},${l.y}`) ? 3.5 : 2.5) * dotScale(S.warp.targetJacobian(l.x, l.y), cam, u) : 3;
    ctx.globalAlpha = p.alpha;
    ctx.beginPath();
    ctx.arc(p.x, p.y, r, 0, 2 * Math.PI);
    ctx.fillStyle = "#1c1c28";
    ctx.fill();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = "#fff";
    ctx.stroke();
  }
  ctx.textBaseline = "middle";
  for (const l of S.placed) {
    ctx.globalAlpha = l.alpha;
    ctx.font = FONTS[l.type] || FONTS.place;
    ctx.textAlign = "left";
    ctx.lineWidth = 3;
    ctx.strokeStyle = "rgba(255,255,255,0.9)";
    ctx.strokeText(l.label, l.box[0], (l.box[1] + l.box[3]) / 2);
    ctx.fillStyle = l.type === "station" ? "#1c1c28" : "#444";
    ctx.fillText(l.label, l.box[0], (l.box[1] + l.box[3]) / 2);
  }
  ctx.globalAlpha = 1;
  ctx.beginPath();
  ctx.arc(cam.pin[0], cam.pin[1], 7, 0, 2 * Math.PI);
  ctx.fillStyle = "#c2185b";
  ctx.fill();
  ctx.lineWidth = 3;
  ctx.strokeStyle = "#fff";
  ctx.stroke();
}

// The network that shaped the bent map, drawn as straight lines from stop to stop: on a time
// map the ride between two stations has no geography of its own (the tiles' rail between
// them crosses the tears between walking areas), so a schematic line says more. Only stops
// inside the outer minute are joined.
function drawNetwork(cam, u) {
  if (!OPT.network || OPT.field !== "transit" || S.spec.kind !== "polar" || !S.network) return;
  const a = Math.max(0, Math.min(1, (u - NETWORK_FROM_U) / (1 - NETWORK_FROM_U)));
  if (a <= 0) return;
  const max = S.spec.maxMinutes ?? OPT.max;
  ctx.save();
  ctx.globalAlpha = a;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  for (const line of S.network.lines) {
    const runs = [];
    let run = [];
    for (const [, lon, lat] of line.stops) {
      const [x, y] = S.frame.toLocal(lon, lat);
      const t = sampleField(S.spec.field, x, y);
      if (t < 0 || t > max) { if (run.length > 1) runs.push(run); run = []; continue; }
      const p = S.warp.screenOfLocal(x, y, cam, u);
      run.push([p.x, p.y]);
    }
    if (run.length > 1) runs.push(run);
    for (const [width, color] of [[6, "rgba(255,255,255,0.9)"], [3.5, LINE_COLORS[line.name] || "#333"]]) {
      ctx.lineWidth = width;
      ctx.strokeStyle = color;
      for (const r of runs) {
        ctx.beginPath();
        r.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.stroke();
      }
    }
  }
  ctx.restore();
}

function percentile(arr, p) {
  if (!arr.length) return NaN;
  const s = [...arr].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.floor(p * s.length))];
}

let hudAt = 0;
function updateHud(now, drawStats) {
  if (now - hudAt < 500) return;
  hudAt = now;
  const ft = S.frameTimes.slice(-120), dt = S.drawTimes.slice(-120);
  const i = S.sceneInfo || {};
  const r = S.renderer;
  const lines = [
    `back-end ${OPT.backend.toUpperCase()} → ${S.deck ? "deck.gl" : r ? r.mode : "none"}${r && r.fellBack ? " (fell back)" : ""}   field ${OPT.field}   geom ${OPT.geom}`,
    `frame p50 ${percentile(ft, 0.5).toFixed(1)} ms  p95 ${percentile(ft, 0.95).toFixed(1)} ms   draw p95 ${percentile(dt, 0.95).toFixed(2)} ms   u ${S.u.toFixed(2)}`,
    `tiles ${i.loaded ?? 0}/${i.cover ?? 0} (fetched ${i.requests ?? 0}, cache ${i.cacheHits ?? 0}, missing ${i.missing ?? 0})${S.cover && S.cover.coarsened ? `  rings shrunk ×${S.cover.coarsened}` : ""}`,
    `shaded ${i.shaded ?? 0} (fill verts ${i.fillVerts ?? 0}, segments ${i.segments ?? 0})  ${((i.bytes ?? 0) / 1e6).toFixed(1)} MB${i.overBudget ? "  OVER BUDGET" : ""}`,
    `dropped ${JSON.stringify(i.dropped || {})}   draw calls ${drawStats && !drawStats.skipped ? drawStats.drawCalls : "-"}`,
    `worker ${(i.totalMs ?? 0).toFixed(0)} ms  upload ${(i.uploadMs ?? 0).toFixed(1)} ms  labels ${S.placed.length}/${S.labels.length}`,
  ];
  if (r && r.caps) lines.push(`gpu ok ${r.caps.gpu}  r32f ${r.caps.r32f && r.caps.r32f.ok}  float-linear ${r.caps.floatLinear}  ${r.caps.renderer}`);
  if (S.lostContext) lines.push("WebGL context lost: showing the ordinary map");
  $("hud").textContent = lines.join("\n");
  const loading = S.pending || (S.sceneInfo && S.sceneInfo.partial);
  $("badge").textContent = [S.origin ? S.origin.label : "", loading ? "loading…" : ""].filter(Boolean).join(" · ");
}

function loop(now) {
  if (S.lastFrame) S.frameTimes.push(now - S.lastFrame);
  if (S.frameTimes.length > 600) S.frameTimes.splice(0, S.frameTimes.length - 600);
  S.lastFrame = now;
  stepMorph(now);
  let drawStats = null;
  if (S.map && S.frame && !S.lostContext) {
    const cam = camera();
    const key = [cam.center[0], cam.center[1], cam.zoom, cam.width, cam.height, S.u, OPT.folds].join("|");
    if (S.dirty || key !== S.lastKey) {
      S.lastKey = key;
      S.dirty = false;
      let ok = true;
      const t0 = performance.now();
      try {
        if (S.deck) drawStats = S.deck.draw(cam, S.u);
        else if (S.renderer && S.renderer.supported) drawStats = S.renderer.draw(cam, S.u, { showFolds: OPT.folds, adaptive: OPT.adaptive });
        if (drawStats && drawStats.skipped) ok = false;
      } catch (e) {
        ok = false;
        S.drawError = String(e.message || e);
        out(`Draw failed: ${S.drawError}`);
        console.error(e);
        failWaiters(`draw: ${S.drawError}`);
      }
      const drawMs = performance.now() - t0;
      S.drawTimes.push(drawMs);
      if (S.drawTimes.length > 600) S.drawTimes.splice(0, S.drawTimes.length - 600);
      for (const fn of S.drawListeners || []) fn(drawMs);
      drawOverlay(cam);
      if (ok && S.drawHooks.length) {
        if (S.benchFinish && S.renderer.gl) S.renderer.gl.finish();   // bench: until the GPU is done
        const hooks = S.drawHooks; S.drawHooks = [];
        for (const h of hooks) h({ at: performance.now() });
      }
    }
    for (const fn of S.frameListeners || []) fn(now);
  }
  updateHud(now, drawStats);
  requestAnimationFrame(loop);
}

// ── Map credits ──────────────────────────────────────────────────────
// Mapbox's logo and credits show whenever Mapbox data is drawn (real tiles, the base map, or
// the ordinary-map fallback); over synthetic geometry the page says it is synthetic instead.
function updateCredits() {
  const mapbox = OPT.geom === "tiles" || $("basemap").checked || document.body.classList.contains("no-mesh");
  $("attribution").hidden = !mapbox;
  $("mapbox-logo").hidden = !mapbox;
  $("synthetic-note").hidden = mapbox;
}

function showOrdinaryMap(on) {
  document.body.classList.toggle("no-mesh", on);
  S.map.setStyle(maplibreStyle(location.origin, { withData: on || $("basemap").checked }));
  updateCredits();
}

// ── Controls ─────────────────────────────────────────────────────────
function reloadWith(changes) {
  const q = new URLSearchParams(location.search);
  for (const [k, v] of Object.entries(changes)) q.set(k, v);
  location.search = q.toString();
}

function initControls() {
  const originSel = $("origin");
  for (const o of S.network.origins) originSel.add(new Option(o.label, o.id));
  originSel.value = OPT.origin;
  originSel.disabled = OPT.field === "contours";
  originSel.addEventListener("change", () => { OPT.origin = originSel.value; setOrigin(OPT.origin); });
  const fieldSel = $("field");
  for (const f of [...POLAR_FIELDS, ...GRID_FIELDS]) fieldSel.add(new Option(f, f));
  fieldSel.value = OPT.field;
  fieldSel.addEventListener("change", () => reloadWith({ field: fieldSel.value }));
  for (const [id, key] of [["backend", "backend"], ["geom", "geom"], ["spacing", "spacing"], ["spread", "spread"]]) {
    $(id).value = String(OPT[key]);
    $(id).addEventListener("change", () => reloadWith({ [key]: $(id).value }));
  }
  $("morph").addEventListener("click", () => morphTo(S.u > 0.5 || (S.anim && S.anim.to === 1) ? 0 : 1));
  $("u").addEventListener("input", () => { S.anim = null; S.u = Number($("u").value); S.dirty = true; });
  $("folds").checked = OPT.folds;
  $("adaptive").checked = OPT.adaptive;
  $("network").checked = OPT.network;
  $("network").addEventListener("change", () => { OPT.network = $("network").checked; S.dirty = true; });
  $("adaptive").addEventListener("change", () => { OPT.adaptive = $("adaptive").checked; S.dirty = true; });
  $("folds").addEventListener("change", () => { OPT.folds = $("folds").checked; if (OPT.folds) buildFoldLayer(); S.dirty = true; });
  $("basemap").addEventListener("change", () => {
    document.body.classList.toggle("basemap", $("basemap").checked);
    showOrdinaryMap(document.body.classList.contains("no-mesh"));
  });
  $("selftest").addEventListener("click", () => runSelfTestNow());
  $("bench").addEventListener("click", () => runBenchNow());
  $("census").addEventListener("click", () => runCensusNow());
  $("copy").addEventListener("click", async () => {
    const text = JSON.stringify(allResults(), null, 1);
    try { await navigator.clipboard.writeText(text); out("Copied. Paste it into the report."); }
    catch { out(text); }
  });
  $("clear").addEventListener("click", () => {
    try { localStorage.removeItem(RESULTS_KEY); } catch { /* storage blocked */ }
    S.lastResults = {};
    out("Results cleared.");
  });
  S.map.on("click", (e) => {
    if (!S.tap) return;
    const hits = S.tap.query(e.point.x, e.point.y);
    const box = $("tap");
    box.hidden = false;
    box.textContent = hits.length
      ? "Under your finger:\n" + hits.map((h) => {
        const m = S.spec.kind === "polar" ? sampleField(S.spec.field, h.x, h.y) : -1;
        return `  ${h.text}${m >= 0 ? ` · ${Math.round(m)} min` : ""}`;
      }).join("\n")
      : "Nothing named here.";
  });
  S.map.on("move", () => { S.dirty = true; });
}

// The checks need the mesh: without it (no WebGL2, or a lost context) they say so and stop.
function meshMissing(kind) {
  if (S.renderer && S.renderer.supported && !S.lostContext && S.frame) return null;
  const r = { kind, skipped: "no mesh here (no WebGL2, or the context is lost): showing the ordinary map" };
  out(r.skipped);
  return r;
}

async function runSelfTestNow() {
  const missing = meshMissing("selftest");
  if (missing) return missing;
  out("Running the self-test…");
  const { runSelfTest } = await import("./selftest.js");
  const r = await runSelfTest(labApi);
  storeResult("selftest", r);
  out(JSON.stringify(r, null, 1));
  return r;
}
async function runBenchNow(opts) {
  const missing = meshMissing("bench");
  if (missing) return missing;
  out("Running the benchmark… (don't touch the map)");
  const { runBench } = await import("./bench.js");
  const r = await runBench(labApi, opts);
  storeResult("bench", r);
  out(JSON.stringify(r.summary, null, 1));
  return r;
}
async function runCensusNow() {
  if (OPT.geom !== "tiles") { out("The census reads real tiles: open the lab with ?geom=tiles (it spends tile budget)."); return null; }
  out("Running the tile census (needs a tiles token on the server)…");
  const { runCensus } = await import("./bench.js");
  const r = await runCensus(labApi);
  storeResult("census", r);
  out(JSON.stringify(r.summary, null, 1));
  return r;
}

// What selftest.js and bench.js (and scripts/lab_headless.mjs) use.
const labApi = {
  S, OPT, camera, setOrigin, requestScene, waitScene, morphTo, fitFor, reachBox,
  storeResult, allResults, percentile,
  runSelfTest: runSelfTestNow, runBench: runBenchNow, runCensus: runCensusNow,
  onFrame(fn) { (S.frameListeners = S.frameListeners || []).push(fn); return () => { S.frameListeners = S.frameListeners.filter((f) => f !== fn); }; },
  onDraw(fn) { (S.drawListeners = S.drawListeners || []).push(fn); return () => { S.drawListeners = S.drawListeners.filter((f) => f !== fn); }; },
};

// ── Boot ─────────────────────────────────────────────────────────────
async function boot() {
  if (typeof maplibregl === "undefined") { out("MapLibre didn't load."); return; }
  const [network, example] = await Promise.all([
    fetch("stations.json").then((r) => r.json()),
    fetch("../example.json").then((r) => r.json()),
  ]);
  S.network = network; S.example = example;
  const first = originById(OPT.origin);
  S.worker = new Worker(new URL("../../shared/mesh/worker.js", import.meta.url), { type: "module" });
  S.worker.onmessage = onWorker;

  if (OPT.backend === "a") {
    const { createDeckBackend } = await import("./backend-deck.js");
    $("deck-canvas").hidden = false;
    $("mesh").hidden = true;
    S.deck = await createDeckBackend($("deck-canvas"));
    S.renderer = { supported: true, mode: "deck", caps: null, setWarp() { return 0; }, setScene() { return 0; } };
  } else {
    S.renderer = createMeshRenderer($("mesh"), {
      backend: OPT.backend === "b" ? "gpu" : "cpu", force: OPT.caps,
      // A lost context shows the ordinary map until the mesh is rebuilt and drawn again.
      onLost: () => { S.lostContext = true; failWaiters("WebGL context lost"); showOrdinaryMap(true); },
      onRestored: () => {
        S.renderer.setWarp(S.spec);
        S.foldFor = null;
        const gen = requestScene(S);
        waitScene(gen).then(() => { S.lostContext = false; showOrdinaryMap(false); }, () => {});
        S.lostContext = false;
      },
    });
  }
  // The last rung: no WebGL2, so the ordinary map (MapLibre with its own data) is the view.
  const meshOk = S.renderer.supported;
  if (!meshOk) {
    document.body.classList.add("no-mesh");
    out("No WebGL2 here: showing the ordinary map (the geographic view), as the product would.");
  }
  S.map = new maplibregl.Map({
    container: "map", style: maplibreStyle(location.origin, { withData: !meshOk }),
    center: [first.lon, first.lat], zoom: 11,
    attributionControl: false, dragRotate: false, pitchWithRotate: false, touchPitch: false,
    renderWorldCopies: false,
  });
  const loaded = new Promise((r) => S.map.once("load", r));
  S.map.touchZoomRotate.disableRotation();
  initControls();
  updateCredits();
  await loaded;
  if (!meshOk) {
    for (const id of ["selftest", "bench", "morph", "u", "folds"]) $(id).disabled = true;
    S.origin = first;
    S.map.jumpTo({ center: [first.lon, first.lat], zoom: 12 });
    labApi.readyAt = performance.now();
    return;
  }
  const gen = setOrigin(OPT.origin);
  requestAnimationFrame(loop);
  await waitScene(gen);
  labApi.readyAt = performance.now();
  if (Q.get("selftest") === "1") await runSelfTestNow();
  if (Q.get("bench") === "1") await runBenchNow();
  if (Q.get("census") === "1") await runCensusNow();
}

window.__lab = labApi;
labApi.ready = boot().catch((e) => { out(`Boot failed: ${e.message}`); throw e; });
