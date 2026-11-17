// "How far can I get from here?" (#5, W-02)
//
// The server answers, once per (pin, mode, window), with a travel-time field: minutes on a
// grid around the pin (POST /api/travel-time-field). Two views draw it:
//   ordinary  the normal map (MapLibre) with travel-time bands, islands and holes included.
//             The baseline: it always works, even without WebGL2.
//   bent      the mesh renderer (static/shared/mesh/): each place keeps its direction from
//             the pin (by reach or true compass). Walking, cycling and driving use the fold:
//             distance proportional to minutes, so the rings are exact circles. Public
//             transport uses squash (layouts.js), whose rings bend; past the outer rings that
//             passed W-03's checks (views.js CHECKED_OUTERS) a footnote says it bends more.
//             Experimental.
// "Time map" dissolves the real map into the unbent mesh, then morphs it. Pan, zoom
// and resize never fetch; a new pin, mode or window fetches one field, and the old picture
// stays up until the new one is ready.
//
// The page is From here (#28): the pin, the way of travelling and the time of day are handed on
// to Meet up and Whole city (/shared/handoff.js), and picked up from them.
import { readHandoff, writeHandoff } from "/shared/handoff.js";
import { createLatest } from "/shared/latest.js";
import { locate as findMe, offerLocation } from "/shared/locate.js";
import { updateSwitcher, watchCity } from "/shared/nav.js";
import { windowOptions } from "/shared/transit.js";
import { friendly, geocode, postJSON, prefersReducedMotion } from "/shared/api.js";
import { bandPolygons } from "/shared/mesh/contours.js";
import { tileCover } from "/shared/mesh/cover.js";
import { decodeField, geometryField, knownMinutes, nodeAt } from "/shared/mesh/field.js";
import { fitFor, reachBox } from "/shared/mesh/fit.js";
import { cameraFromMap, localFrame } from "/shared/mesh/frame.js";
import { createField } from "/shared/mesh/field.js";
import { buildTapIndex, LABEL_CAP, placeLabels, sortCandidates } from "/shared/mesh/labels.js";
import { DEFAULT_BUDGET, DEFAULT_SPACING_DIV } from "/shared/mesh/pipeline.js";
import { createMeshRenderer } from "/shared/mesh/renderer.js";
import { bandColor, bandRgb, ringPlan } from "/shared/mesh/rings.js";
import { angularSpread, createWarp, dotScale } from "/shared/mesh/warp.js";
import * as V from "./views.js";
import * as T from "./words.js";

// ── State ────────────────────────────────────────────────────────────
const Q = new URLSearchParams(location.search);
const FALLBACK_ORIGIN = { lon: -0.1233, lat: 51.5308, label: "King's Cross", example: true };
const FIELD_CACHE = 8;
const TAP_PX = 12;
const HANDOFF = readHandoff();         // what the last tool in this tab handed on

const S = {
  origin: { ...FALLBACK_ORIGIN },
  mode: "walking",
  window: V.defaultWindow(),
  outer: V.MODES.walking.defaultOuter,
  directions: V.DEFAULT_DIRECTIONS,
  layoutPick: null,      // a chip picked while ?layouts=1 shows them: {mode, layout}, for that mode only
  active: null,          // the field drawn now: {key, json, field, frame}
  bent: false,           // what the person asked for
  u: 0, fade: 0,         // morph (0 ordinary … 1 bent) and the mesh's opacity
  anim: null,
  mesh: null,            // the bent view drawn now: {key, gen, spec, warp, frame, scene, labels, kRef, zRef}
  pendingMesh: null,
  gen: 0,
  dragging: false, dragPos: null,
  transit: { available: false },
  renderer: null, worker: null, // worker: the map's scene; fieldWorker: bands, band layer, taps
  fieldWorker: null,
  frameWhenReady: false,
  notice: "",            // why the page is showing something else (London only, expired): stays until the next choice
  tapSeq: 0, bandsSeq: 0,
  placed: [], tap: null, tapMark: null,
  requests: 0, timings: [], drawn: 0,
};
const fields = new Map();                // key → {json, field, frame}, newest last
const latest = createLatest();           // field requests
const searches = createLatest();         // place searches
let locateSeq = 0;
let requestTimer = null;
let map;
let box = { w: 1, top: 0, bottom: 1, h: 1 };
let lastKey = "";
let dirty = true;
let sheetsSeq = 0;
const sheetWaiters = new Map();
const frameHooks = [];

// ── DOM ──────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const meshCanvas = $("mesh-canvas");
const overlay = $("overlay-canvas");
const ctx = overlay.getContext("2d");
const pinEl = $("origin-pin");
const statusEl = $("status");
const toggleBtn = $("view-toggle");
const backBtn = $("back-to-pin");
const topEl = $("top");
const legendEl = $("legend");
const mapContainer = $("map-container");

function setStatus(text, kind = "") {
  statusEl.textContent = text;
  statusEl.className = "status" + (kind ? " " + kind : "");
}
function setText(el, text) { if (el.textContent !== text) el.textContent = text; }
function show(el, on) { if (el.hidden === on) el.hidden = !on; }

// The part of the map the controls don't cover: views are framed to fit it.
function measureBox() {
  const w = mapContainer.clientWidth, h = mapContainer.clientHeight;
  const top = Math.min(topEl.getBoundingClientRect().bottom, h / 2);
  // The cards under the header (location, back to the pin, the tap list) sit just below it.
  document.documentElement.style.setProperty("--top-h", `${Math.round(topEl.getBoundingClientRect().bottom)}px`);
  const narrow = w <= 640;
  const bottom = narrow ? Math.max(legendEl.getBoundingClientRect().top, top + 120) : h;
  box = { w, top, bottom, h: bottom - top };
}

// ── The field ────────────────────────────────────────────────────────
const currentKey = () => V.fieldKey(S.origin.lon, S.origin.lat, S.mode, S.window);

function remember(key, entry) {
  fields.delete(key);
  fields.set(key, entry);
  while (fields.size > FIELD_CACHE) fields.delete(fields.keys().next().value);
}

function entryFrom(json) {
  const field = decodeField(json);
  return { json, field, frame: localFrame(json.origin) };
}

// The pin, mode or window changed: fetch that field (or take it from the cache). The old
// picture stays up meanwhile; only the newest request may land.
function requestField({ frame = false, delay = 0 } = {}) {
  clearTimeout(requestTimer);
  latest.cancel();
  if (frame) S.frameWhenReady = true;
  const key = currentKey();
  let cached = fields.get(key);
  if (cached && cached.json.mode === "transit" && V.expired(cached.json.valid_until)) {
    fields.delete(key);                    // a tab left open past the tables' last day
    cached = null;
  }
  if (cached) { remember(key, cached); activate(key, cached); return; }
  setStatus(T.UPDATING, "busy");
  const snap = { lon: S.origin.lon, lat: S.origin.lat, mode: S.mode, window: S.window, key };
  requestTimer = setTimeout(async () => {
    const t0 = performance.now();
    let res;
    try {
      S.requests++;
      res = await latest.run(snap, (signal) => postJSON("/api/travel-time-field", {
        longitude: snap.lon, latitude: snap.lat, mode: snap.mode,
        ...(snap.mode === "transit" ? { window: snap.window } : {}),
      }, signal));
    } catch (e) {
      if (e && e.code === "outside_transit_area") { toWalking(T.LONDON_ONLY); return; }
      if (e && e.code === "transit_unavailable") { toWalking(T.NO_TRANSIT); return; }
      if (e && e.code === "no_transit_access") { S.notice = friendly(e); dirty = true; }
      setStatus(friendly(e), "error");
      if (e && e.name !== "AbortError") clearField();
      return;
    }
    if (res.stale) return;
    let entry;
    try { entry = entryFrom(res.value); } catch {
      setStatus(friendly(new Error("bad field")), "error");
      clearField();
      return;
    }
    // A transit field built without its walking near field (a Mapbox hiccup) is shown but not
    // kept: the next visit asks again, as the server's cache does.
    if (!(entry.json.notes || []).includes("near_field_missing")) remember(snap.key, entry);
    S.timings.push({ kind: "field", ms: performance.now() - t0 });
    if (snap.key === currentKey()) activate(snap.key, entry);
  }, delay);
}

function toWalking(message) {
  S.mode = "walking";
  checkRadio("mode", "walking");
  S.outer = V.MODES.walking.defaultOuter;
  buildOuterChips();
  S.notice = message;                      // shown with the limits until the next choice
  requestField({ frame: true });
  dirty = true;
}

// No field for the pin shown: nothing is drawn around it (rather than the old pin's bands).
function clearField() {
  S.active = null;
  const src = map && map.getSource("bands");
  if (src) src.setData({ type: "FeatureCollection", features: [] });
  if (S.bent) setBent(false);
  dirty = true;
}

// A field becomes the one drawn: bands on the normal map at once; the bent view when its scene
// is ready (until then the old bent view, if any, stays).
function activate(key, entry) {
  S.active = { key, ...entry };
  // The server rounds the pin to about 11 m; the pin sits where the field was measured from.
  S.origin = { ...S.origin, lon: entry.json.origin.lon, lat: entry.json.origin.lat };
  setStatus("");
  updateBands();
  updateCredits();
  if (S.frameWhenReady) { S.frameWhenReady = false; frameView(); }
  if (S.bent) prepareMesh();
  dirty = true;
}

// ── The ordinary view: bands on the normal map ───────────────────────
function levels() { return ringPlan(S.outer).map((r) => r.minutes); }

// The bands are contoured in the worker (a transit field is about 300k nodes): the page never
// stalls on them. Without a worker (no WebGL2), here.
function updateBands() {
  if (!map || !map.getSource("bands") || !S.active) return;
  const lv = levels();
  const key = `${S.active.key}|${lv.join(",")}`;
  buildBandLegend(lv);
  if (!S.fieldWorker) { showBands(key, bandPolygons(S.active.field, S.active.frame, lv, { skipFilled: true })); return; }
  S.fieldWorker.postMessage({ type: "bands", id: ++S.bandsSeq, key, levels: lv, field: workerField(S.active.field) });
}

function showBands(key, fc) {
  if (!S.active || key !== `${S.active.key}|${levels().join(",")}`) return;   // overtaken
  const n = levels().length;
  for (const f of fc.features) f.properties.color = bandColor(f.properties.band, n);
  map.getSource("bands").setData(fc);
}

// The colour bar: each band as long as the ground it adds (its area on the normal map), so the
// bar shows how much more you reach in each step, not just the minutes.
function buildBandLegend(lv) {
  const el = $("band-legend");
  const f = S.active && S.active.field;
  const ground = new Float64Array(lv.length);
  if (f) {
    for (let k = 0; k < f.minutes.length; k++) {
      const t = f.minutes[k];
      if (!(t >= 0 && t <= lv[lv.length - 1]) || (f.filled && f.filled[k]) || (f.uncertain && f.uncertain[k])) continue;
      let b = 0;
      while (lv[b] < t) b++;
      ground[b]++;
    }
  }
  const total = ground.reduce((a, b) => a + b, 0);
  let lastLabel = -Infinity, at = 0;
  el.replaceChildren(...lv.map((m, i) => {
    const s = document.createElement("span");
    s.style.background = bandColor(i, lv.length);
    const share = total ? ground[i] / total : 1 / lv.length;
    s.style.flex = `${Math.max(share, 0.004)} 1 0`;
    at += share;
    // Majors only, and none crowding the one before (the first bands can be thin).
    if ((m % 15 === 0 || i === lv.length - 1) && at - lastLabel > 0.08) { s.dataset.label = `${m}`; lastLabel = at; }
    return s;
  }));
}

function frameView() {
  if (!S.active || !map) return;
  measureBox();
  const spec = { kind: "polar", field: geometryField(S.active.field), maxMinutes: S.outer };
  const fit = fitFor(spec, S.active.frame, box.w, box.h, S.outer);
  map.easeTo({
    center: [S.origin.lon, S.origin.lat], zoom: Math.min(16, fit.zoom),
    offset: [0, (box.top + box.bottom) / 2 - mapContainer.clientHeight / 2],
    duration: prefersReducedMotion() ? 0 : 700,
  });
}

// ── The bent view: the mesh ──────────────────────────────────────────
const needsCpuTargets = () => !(S.renderer && S.renderer.mode === "gpu");

// The fold's warp for the active field (the plain time-polar map), bent by its geometry field:
// uncertain patches at their drawn minutes (field.js). Squash's comes back from the field
// worker with the band layer (layouts.js squashLayout).
function foldSpec(entry) {
  const spec = { kind: "polar", field: geometryField(entry.field), maxMinutes: S.outer };
  const spread = V.DIRECTIONS[S.directions];
  if (spread > 0) spec.angles = angularSpread(entry.field, S.outer, { spread });
  return spec;
}
const squashOpts = () => ({ minSlope: Number(Q.get("squashslope")) > 0 ? Number(Q.get("squashslope")) / 1000 : 0,
                             scale: Q.get("ringscale") === "minutes" ? "minutes" : "ground",
                             spread: V.DIRECTIONS[S.directions] });

// The layout wanted now (views.js layoutFor): a chip picked in this mode, else ?layout=, else
// the mode's own (squash for public transport, the fold otherwise). The mode is the active
// field's, the one the mesh is built from: while a new mode's field loads, the old field keeps
// its own layout.
function layoutNow() {
  const mode = S.active ? S.active.json.mode : S.mode;
  const chip = S.layoutPick && S.layoutPick.mode === mode ? S.layoutPick.layout : null;
  return V.layoutFor(mode, chip || Q.get("layout"));
}

// What the workers need (plain arrays; they rebuild the field, its geometry field and the
// warp). Both get the same: the minutes, the filled flags and the uncertain patches.
function workerField(f) {
  return { origin: f.origin, cell: f.cell, nx: f.nx, ny: f.ny, x0: f.x0, y0: f.y0, minutes: f.minutes,
           filled: f.filled, uncertain: f.uncertain || null, drawn: f.uncertain ? f.drawn : null };
}

// The bent view wanted now: the active field, the outer ring, the directions setting.
const desiredMeshKey = () => (S.active ? `${S.active.key}|${S.outer}|${S.directions}|${layoutNow()}` : "");

// Ask the worker for the bent view of the active field: the map's scene, and the band layer
// with its overlap index. It is swapped in when both have arrived, if still the one wanted.
function prepareMesh({ retry = false } = {}) {
  if (!bendOffered() || !S.active) return;
  const entry = S.active;
  const key = desiredMeshKey();
  if (S.mesh && S.mesh.key === key && !retry) { S.pendingMesh = null; return; }
  if (S.pendingMesh && S.pendingMesh.key === key && !retry) return;
  const spec = foldSpec(entry);
  measureBox();
  const fit = fitFor(spec, entry.frame, box.w, box.h, S.outer);
  const reach = reachBox(spec, S.outer);
  const cover = tileCover(entry.frame, reach);
  const eps = V.refineEps(fit.k);
  const gen = ++S.gen;
  const prev = retry && S.mesh && S.mesh.key === key ? S.mesh : null;
  S.pendingMesh = { key, gen, entry, layout: layoutNow(), spec, outer: S.outer, warp: createWarp(spec), frame: entry.frame,
                    kRef: fit.k, zRef: fit.zoom, requestedAt: performance.now(), retried: retry,
                    layers: prev ? prev.layers : null, scene: null };
  const plain = workerField(entry.field), angles = spec.angles || null;   // the same field to both workers
  // The two workers run side by side: the map's scene in one, the band layer (and squash's
  // own bend, which the scene worker also builds for itself) in the other.
  S.worker.postMessage({
    type: "scene", gen, origin: entry.frame.origin, tiles: cover.tiles,
    spacingDiv: DEFAULT_SPACING_DIV, budget: DEFAULT_BUDGET, source: "tiles",
    layout: layoutNow(), squash: squashOpts(),
    warp: needsCpuTargets() ? { kind: "polar", field: plain, maxMinutes: S.outer, angles } : null,
    refine: { field: plain, maxMinutes: S.outer, eps, angles },
  });
  if (!prev) {
    S.fieldWorker.postMessage({ type: "layers", key, gen, field: workerField(entry.field), maxMinutes: S.outer,
                                angles, eps, box: reach, cpu: needsCpuTargets(),
                                layout: layoutNow(), squash: squashOpts(), levels: levels() });
  }
  if (S.bent && !S.mesh) setStatus(T.PREPARING, "busy");
}

function onWorker(e) {
  const msg = e.data;
  if (msg.type === "sheets-result") {
    const w = sheetWaiters.get(msg.id);
    if (w) { sheetWaiters.delete(msg.id); w(Object.assign(msg.hits || [], { uncertain: !!msg.uncertain })); }
    return;
  }
  if (msg.type === "bands-result") { showBands(msg.key, msg.fc); return; }
  const p = S.pendingMesh;
  if (msg.type === "error" && msg.job === "bands") {
    // The normal map's bands failed: say so, rather than leaving the last pin's bands up.
    if (S.active && msg.key === `${S.active.key}|${levels().join(",")}`) {
      const src = map && map.getSource("bands");
      if (src) src.setData({ type: "FeatureCollection", features: [] });
      setStatus(T.BANDS_FAILED, "error");
    }
    return;
  }
  if (msg.type === "error" && (msg.job === "sheets-query" || msg.job === "sheets-index")) {
    const w = sheetWaiters.get(msg.id);
    if (w) { sheetWaiters.delete(msg.id); w([]); }
    return;
  }
  if (msg.type === "error") {
    if (p && (msg.gen === p.gen || msg.key === p.key)) {
      S.pendingMesh = null;                 // so pressing Bend again asks again
      if (!S.mesh || S.mesh.key !== p.key) {
        if (S.bent) setBent(false);
        setStatus(T.BEND_FAILED, "error");
      }
    }
    return;
  }
  if (msg.type === "layers-result") {
    if (p && msg.key === p.key) { p.layers = msg; tryInstall(p, p.sceneWhole); }
    return;
  }
  if (msg.type !== "scene" || !p || msg.gen !== p.gen) return;   // overtaken: never shown
  if (p.key !== desiredMeshKey()) { S.pendingMesh = null; return; }
  const stats = msg.scene.stats || {};
  if (!msg.partial && stats.cover > 0 && stats.loaded === 0) {
    S.pendingMesh = null;                   // no tile at all: there is no map to bend
    if (!S.mesh || S.mesh.key !== p.key) { if (S.bent) setBent(false); }
    setStatus(T.TILES_FAILED, "error");
    return;
  }
  if (msg.partial && S.mesh) return;        // keep the old view until the scene is whole
  p.scene = msg.scene;
  p.sceneWhole = !msg.partial;
  tryInstall(p, !msg.partial);
}

// Both halves in: install. A scene missing tiles is shown, and asked for once more after 5 s
// (only the missing tiles are fetched again; the rest are cached in the worker).
function tryInstall(p, whole) {
  if (!p.scene || !p.layers || S.pendingMesh !== p) return;
  installMesh(p, whole);
  if (!whole) return;
  S.pendingMesh = null;
  S.timings.push({ kind: "scene", ms: performance.now() - p.requestedAt });
  const missing = (p.scene.stats || {}).missing || 0;
  if (missing > 0 && !p.retried) {
    setStatus(T.TILES_MISSING, "busy");
    setTimeout(() => { if (S.bent && S.mesh && S.mesh.key === p.key && !S.pendingMesh) prepareMesh({ retry: true }); }, 5000);
  } else if (statusEl.textContent === T.TILES_MISSING || statusEl.textContent === T.PREPARING) {
    setStatus(missing > 0 ? T.TILES_FAILED : "");
  }
}

function installMesh(p, whole) {
  const r = S.renderer;
  const L = p.layers;
  if (p.layout === "squash" && L.R && !p.squashed) {
    // Squash's bend, as the field worker built it: the view is framed out to its reach.
    const f = p.entry.field;
    const R = createField({ origin: f.origin, cell: f.cell, nx: f.nx, ny: f.ny, x0: f.x0, y0: f.y0, minutes: L.R });
    p.spec = { ...p.spec, field: R, maxMinutes: L.reach, angles: L.angles || null };
    p.warp = createWarp(p.spec);
    p.kRef *= p.outer / L.frame;
    p.squashed = true;
  }
  r.setWarp(p.spec);
  const bands = { pos: L.pos, minutes: L.minutes, index: L.index, targets: L.targets };
  r.setBandMesh(bands);
  if (p.entry.json.mode === "transit") r.setFoldMesh({ pos: bands.pos, index: bands.index, targets: bands.targets || null });
  r.setScene(p.scene);
  S.mesh = { ...p };
  S.mesh.labels = labelsFor(S.mesh);
  if (S.bent || S.u > 0) capZoom(S.mesh);   // a new view while bent: its own zoom limit
  if (whole && statusEl.textContent === T.PREPARING) setStatus("");
  dirty = true;
  if (whole) maybeStartBend();            // bend only once every tile is in
}

// Label candidates: the timetable's stations (with their minutes) for public transport, the
// tiles' stations otherwise, and the tiles' places.
function labelsFor(m) {
  const minutesOf = (x, y) => knownMinutes(m.entry.field, x, y);
  let labels = m.scene.labels;
  if (m.entry.json.mode === "transit" && m.entry.json.stations) {
    const stations = m.entry.json.stations.map((s) => {
      const [x, y] = m.frame.toLocal(s.lon, s.lat);
      return { type: "station", text: s.name, rank: 0, x, y };
    });
    labels = [...stations, ...labels.filter((l) => l.type !== "station")];
  }
  return sortCandidates(labels, minutesOf);
}

// ── Bending ──────────────────────────────────────────────────────────
// The bent map needs both workers: the scene, and the band layer with its tap index.
function bendOffered() { return S.renderer && S.renderer.supported && S.fieldWorker && V.bendOffered(S.mode, location.search); }

function setBent(on) {
  S.bent = !!(on && bendOffered());
  if (S.bent) {
    // Pressing Bend again on a view still missing tiles asks for them again.
    const holes = S.mesh && S.mesh.key === desiredMeshKey() && ((S.mesh.scene.stats || {}).missing || 0) > 0;
    prepareMesh({ retry: holes });
    maybeStartBend();
  } else { S.pendingMesh = null; animateTo(0); }
  updateLegend();
}

// While the bent map covers it, the normal map's layers are hidden, so MapLibre fetches no
// tiles for a map nobody sees; the bent view may zoom in two levels past its framing, no more
// (views.BENT_ZOOM_IN; the mesh is refined for that, views.refineEps).
let basemapShown = true;
function showBasemap(on) {
  if (!map || basemapShown === on) return;
  basemapShown = on;
  for (const layer of map.getStyle().layers) {
    if (layer.id !== "bg") map.setLayoutProperty(layer.id, "visibility", on ? "visible" : "none");
  }
}
function flatNow() {
  S.u = 0; S.fade = 0; S.anim = null;
  showBasemap(true);
  if (map) map.setMaxZoom(null);
}

// The bent view's zoom limit, for the view drawn now. MapLibre jumps to a new maximum below the
// current zoom, so the map eases down to it first.
function capZoom(m) {
  if (!map || !m) return;
  const cap = m.zRef + V.BENT_ZOOM_IN;
  if (map.getZoom() > cap + 1e-6) {
    map.once("moveend", () => { if (S.mesh === m && (S.bent || S.u > 0)) map.setMaxZoom(cap); });
    map.easeTo({ zoom: cap, duration: prefersReducedMotion() ? 0 : V.FADE_MS });
  } else {
    map.setMaxZoom(cap);
  }
}

// Bend once the scene for the field drawn now is ready.
function maybeStartBend() {
  if (!S.bent || S.dragging || !S.mesh || !S.active || S.mesh.entry.key !== S.active.key) return;
  if (S.anim && S.anim.to === 1) return;
  if (S.u >= 1 && S.fade >= 1) return;
  animateTo(1);
}

// Two steps each way: the normal map dissolves into the unbent mesh, then the mesh moves.
function animateTo(target) {
  S.anim = { to: target, start: performance.now(), u0: S.u, f0: S.fade, reduce: prefersReducedMotion() };
  if (target === 0) showBasemap(true);
  else capZoom(S.mesh);
}

const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);

function stepAnim(now) {
  const a = S.anim;
  if (!a) return;
  const settle = () => {
    if (a.to === 1) showBasemap(false);
    else if (map) map.setMaxZoom(null);
  };
  if (a.reduce) { S.u = S.fade = a.to; S.anim = null; settle(); dirty = true; return; }
  const t = now - a.start;
  if (a.to === 1) {
    const fadeDur = V.FADE_MS * (1 - a.f0), morphDur = V.MORPH_MS * (1 - a.u0);
    S.fade = fadeDur > 0 ? Math.min(1, a.f0 + (1 - a.f0) * Math.min(1, t / fadeDur)) : 1;
    const mt = t - fadeDur;
    S.u = morphDur > 0 ? (mt <= 0 ? a.u0 : a.u0 + (1 - a.u0) * ease(Math.min(1, mt / morphDur))) : 1;
    if (S.fade >= 1 && S.u >= 1) { S.anim = null; settle(); }
  } else {
    const morphDur = V.MORPH_MS * a.u0, fadeDur = V.FADE_MS * a.f0;
    S.u = morphDur > 0 ? a.u0 * (1 - ease(Math.min(1, t / morphDur))) : 0;
    const ft = t - morphDur;
    S.fade = fadeDur > 0 ? (ft <= 0 ? a.f0 : a.f0 * (1 - Math.min(1, ft / fadeDur))) : 0;
    if (S.fade <= 0 && S.u <= 0) { S.anim = null; settle(); }
  }
  dirty = true;
}

// ── Camera ───────────────────────────────────────────────────────────
function camera(m) {
  const cam = cameraFromMap(map, m.frame, 1);
  cam.k = m.kRef * Math.pow(2, cam.zoom - m.zRef);
  return cam;
}

// ── Drawing ──────────────────────────────────────────────────────────
const FONTS = { station: "600 12px system-ui, sans-serif", place: "12px system-ui, sans-serif" };
function measure(text, type) { ctx.font = FONTS[type] || FONTS.place; return ctx.measureText(text).width; }

function drawFrame(now) {
  const w = mapContainer.clientWidth, h = mapContainer.clientHeight;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  if (overlay.width !== Math.round(w * dpr) || overlay.height !== Math.round(h * dpr)) {
    overlay.width = Math.round(w * dpr); overlay.height = Math.round(h * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  meshCanvas.style.opacity = String(S.fade);
  mapContainer.style.opacity = S.fade >= 1 ? "0" : "1";
  let pin;
  const m = S.mesh;
  if (S.fade > 0 && m && S.renderer && !S.renderer.lost) {
    const cam = camera(m);
    const lv = ringPlan(m.outer).map((r) => r.minutes);   // the installed view's rings, not the next one's
    const stats = S.renderer.draw(cam, S.u, {
      showFolds: m.entry.json.mode === "transit", adaptive: true,
      bands: { levels: lv, colors: lv.map((_, i) => bandRgb(i, lv.length)), opacity: V.BAND_OPACITY },
    });
    if (!stats.skipped) S.drawn = now;
    pin = { x: cam.pin[0], y: cam.pin[1] };
    if (m.layers && m.layers.lines) drawTimeLines(cam, m.layers.lines, m.outer);
    else drawRings(cam, m.outer);
    drawLabels(m, cam);
  } else {
    const p = map.project([S.origin.lon, S.origin.lat]);
    pin = { x: p.x, y: p.y };
  }
  if (S.dragging && S.dragPos) pin = S.dragPos;
  if (S.tapMark) {
    ctx.beginPath();
    ctx.arc(S.tapMark.x, S.tapMark.y, TAP_PX, 0, 2 * Math.PI);
    ctx.strokeStyle = "rgba(28, 28, 40, 0.8)";
    ctx.lineWidth = 2;
    ctx.stroke();
  }
  pinEl.style.transform = `translate(${pin.x}px, ${pin.y}px)`;
  const pinGone = pin.x < 0 || pin.x > box.w || pin.y < box.top || pin.y > h;
  show(backBtn, pinGone);
  show(pinEl, !pinGone);
}

function drawRings(cam, outer) {
  const a = Math.max(0, Math.min(1, (S.u - V.RING_FROM_U) / (1 - V.RING_FROM_U)));
  if (a <= 0) return;
  ctx.save();
  ctx.globalAlpha = a;
  for (const r of ringPlan(outer)) {
    ctx.beginPath();
    ctx.arc(cam.pin[0], cam.pin[1], cam.k * r.minutes, 0, 2 * Math.PI);
    ctx.strokeStyle = r.major ? "rgba(31, 78, 156, 0.85)" : "rgba(31, 78, 156, 0.35)";
    ctx.lineWidth = r.major ? 2 : 1;
    ctx.stroke();
    if (r.major) {
      ctx.font = "bold 12px system-ui, sans-serif";
      ctx.fillStyle = "#1f4e9c";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillText(`${r.minutes} min`, cam.pin[0] + cam.k * r.minutes * Math.SQRT1_2 + 4, cam.pin[1] - cam.k * r.minutes * Math.SQRT1_2 - 4);
    }
  }
  ctx.restore();
}

// Squash: bent rings (layouts.js bentRings), where the fold draws circles. Each major ring is
// labelled at its highest point on screen.
function drawTimeLines(cam, lines, outer) {
  const a = Math.max(0, Math.min(1, (S.u - V.RING_FROM_U) / (1 - V.RING_FROM_U)));
  if (a <= 0) return;
  const major = new Set(ringPlan(outer).filter((r) => r.major).map((r) => r.minutes));
  ctx.save();
  ctx.globalAlpha = a;
  ctx.lineJoin = "round";
  for (const { minutes, xy } of lines) {
    const big = major.has(minutes);
    let top = null, pen = false;
    ctx.beginPath();
    for (let i = 0; i < xy.length; i += 2) {
      if (Number.isNaN(xy[i])) { pen = false; continue; }
      const x = cam.pin[0] + cam.k * xy[i], y = cam.pin[1] + cam.k * xy[i + 1];
      if (pen) ctx.lineTo(x, y); else { ctx.moveTo(x, y); pen = true; }
      if (big && x > 0 && x < cam.width && y > 20 && (!top || y < top[1])) top = [x, y];
    }
    ctx.strokeStyle = big ? "rgba(31, 78, 156, 0.85)" : "rgba(31, 78, 156, 0.35)";
    ctx.lineWidth = big ? 2 : 1;
    ctx.stroke();
    if (big && top) {
      ctx.font = "bold 12px system-ui, sans-serif";
      ctx.fillStyle = "#1f4e9c";
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      ctx.fillText(`${minutes} min`, top[0], top[1] - 3);
    }
  }
  ctx.restore();
}

function drawLabels(m, cam) {
  const u = S.u;
  const screenOf = (x, y) => m.warp.screenOfLocal(x, y, cam, u);
  const phone = Math.min(cam.width, cam.height) < 600;
  S.placed = placeLabels(m.labels, {
    screenOf, measure,
    jacobianOf: (x, y) => m.warp.screenJacobian(x, y, cam, u),
    minutesOf: (x, y) => knownMinutes(m.entry.field, x, y),
    width: cam.width, height: cam.height, cap: phone ? LABEL_CAP.phone : LABEL_CAP.desktop,
    obstacles: [[cam.pin[0] - 12, cam.pin[1] - 12, cam.pin[0] + 12, cam.pin[1] + 12]],
  });
  S.tap = { index: buildTapIndex(m.labels, screenOf), cam };
  const labelled = new Set(S.placed.filter((l) => l.type === "station").map((l) => `${l.x},${l.y}`));
  for (const l of m.labels) {
    if (l.type !== "station") continue;
    const p = screenOf(l.x, l.y);
    if (p.alpha < 0.5 || p.x < 0 || p.y < 0 || p.x > cam.width || p.y > cam.height) continue;
    const r = (labelled.has(`${l.x},${l.y}`) ? 3.5 : 2.5) * dotScale(m.warp.targetJacobian(l.x, l.y), cam, u);
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
}

function loop(now) {
  stepAnim(now);
  if (map) {
    const c = map.getCenter();
    const key = [c.lng, c.lat, map.getZoom(), S.u, S.fade, mapContainer.clientWidth, mapContainer.clientHeight,
      S.dragging, S.dragPos && S.dragPos.x, S.dragPos && S.dragPos.y, S.mesh && S.mesh.key].join("|");
    if (dirty || key !== lastKey) {
      lastKey = key;
      dirty = false;
      drawFrame(now);
      updateLegend();
      for (const fn of frameHooks.splice(0)) fn(now);
    }
  }
  requestAnimationFrame(loop);
}

// ── Legend ───────────────────────────────────────────────────────────
function updateLegend() {
  const bentNow = S.u > 0.5;
  const outer = bentNow && S.mesh ? S.mesh.outer : S.outer;
  const layout = bentNow && S.mesh ? S.mesh.layout : layoutNow();    // the view drawn, not the next
  setText($("from"), T.fromLine(S.origin));
  setText($("sentence"), S.dragging ? "Drop the pin where you want to start."
    : T.sentence(S.mode, outer, ringPlan(outer), bentNow, layout));
  const limits = S.origin.example ? T.EXAMPLE_NOTE : T.limits(S.active && S.active.json);
  setText($("limits"), S.notice ? `${S.notice} ${limits}` : limits);
  setText($("beyond"), T.beyond(outer, layout));
  show($("beyond"), S.bent || bentNow);    // on the normal map, further places are shown, uncoloured
  const offered = !!bendOffered();
  show(toggleBtn, offered);
  // One label, "Time map"; the state is aria-pressed alone, so a screen reader says it once.
  if (toggleBtn.getAttribute("aria-pressed") !== String(S.bent)) toggleBtn.setAttribute("aria-pressed", String(S.bent));
  show($("time-map-note"), offered && S.bent);

  show($("direction-chips"), offered && S.bent);
  const layouts = Q.get("layouts") === "1" || V.LAYOUTS.includes(Q.get("layout"));
  show($("layout-chips"), offered && S.bent && layouts);
  show($("layout-note"), offered && S.bent && layouts);
  setText($("layout-note"), T.LAYOUT_NOTES[layoutNow()]);
  if (layouts) checkRadio("layout", layoutNow());
  show($("direction-note"), offered && S.bent);
  setText($("direction-note"), T.directionNote(S.directions, layout));
  // The overlap note is for the transit fold drawn (or about to be), never for squash.
  const drawnMode = bentNow && S.mesh ? S.mesh.entry.json.mode : (S.active ? S.active.json.mode : S.mode);
  show($("overlap-note"), S.bent && drawnMode === "transit" && layout !== "squash");
  // What squash gives up, for the view drawn (its own counts: a pending pin never relabels it).
  const givesUp = bentNow && S.mesh && S.mesh.layout === "squash" && S.mesh.entry.json.mode === "transit";
  show($("gives-up"), !!givesUp);
  if (givesUp) {
    const rc = S.mesh.layers && S.mesh.layers.ringCounts;
    setText($("gives-up"), T.givesUp(S.mesh.outer, rc ? rc.pastRing : null, rc ? rc.slowInside : null, rc ? rc.unjudged : 0));
  }
  // Past the rings W-03 checked, a footnote: it bends more there.
  const checked = givesUp ? V.CHECKED_OUTERS[S.mesh.entry.json.mode] : null;
  const rough = !!(checked && S.mesh.outer > Math.max(...checked));
  show($("rough-note"), rough);
  if (rough) setText($("rough-note"), T.roughNote(Math.max(...checked)));
  setText($("overlap-note"), T.OVERLAP_NOTE);
  show($("when-field"), S.mode === "transit" && S.transit.available);
}

function updateCredits() {
  const el = $("transit-credits");
  const json = S.active && S.active.json;
  const credits = json && json.mode === "transit" ? json.credits || [] : [];
  setText(el, credits.join(" · "));
  show(el, credits.length > 0);
  const dev = !!(json && json.development_only);
  setText($("dev-note"), dev ? T.DEV_ONLY : "");
  show($("dev-note"), dev);
}

// ── Taps: what is here? ──────────────────────────────────────────────
function showTapList(heading, items, note = "") {
  setText($("tap-heading"), heading);
  $("tap-items").replaceChildren(...items.map((text) => { const li = document.createElement("li"); li.textContent = text; return li; }));
  setText($("tap-note"), note);
  show($("tap-note"), !!note);
  show($("tap-list"), true);
}
function hideTapList() { show($("tap-list"), false); S.tapMark = null; S.tapSeq++; dirty = true; }

// Always answers: [] after SHEETS_WAIT_MS, or when the worker fails.
const SHEETS_WAIT_MS = 3000;
function querySheets(key, lx, ly, r) {
  if (!S.fieldWorker) return Promise.resolve([]);
  const id = ++sheetsSeq;
  return new Promise((resolve) => {
    sheetWaiters.set(id, resolve);
    setTimeout(() => { if (sheetWaiters.delete(id)) resolve([]); }, SHEETS_WAIT_MS);
    S.fieldWorker.postMessage({ type: "sheets-query", id, key, lx, ly, r });
  });
}

// A worker that fails to load or dies says so as an event, not a message: nothing it was asked
// will come back, so the page stops waiting. A field worker that fails is let go: the normal
// map's bands are contoured here from then on, and the bent map, which needs it, is no longer
// offered.
function onWorkerFailure(e) {
  if (e && e.type === "error" && S.fieldWorker && e.target === S.fieldWorker) {
    try { S.fieldWorker.terminate(); } catch { /* already gone */ }
    S.fieldWorker = null;
    if (S.bent) setBent(false);
    updateBands();
    updateLegend();
  }
  for (const [id, w] of sheetWaiters) { sheetWaiters.delete(id); w([]); }
  const p = S.pendingMesh;
  if (p) {
    S.pendingMesh = null;
    if (!S.mesh || S.mesh.key !== p.key) { if (S.bent) setBent(false); setStatus(T.BEND_FAILED, "error"); }
  }
}

// Name a place under the finger: a station or place label within 400 m, else where it is.
function nameOf(m, x, y) {
  let best = null, bd = 400;
  for (const l of m.labels) {
    const d = Math.hypot(l.x - x, l.y - y);
    if (d < bd) { bd = d; best = l; }
  }
  return best ? best.text : T.unnamedPlace(x, y);
}

async function tapAt(px, py) {
  if (S.anim || (S.u > 0.01 && S.u < 0.99)) return [];    // mid-morph: nothing to point at yet
  const seq = ++S.tapSeq;
  S.tapMark = { x: px, y: py };
  dirty = true;
  if (S.u > 0.99 && S.mesh) {
    const m = S.mesh;
    const cam = camera(m);
    const tap = S.tap;
    const lx = (px - cam.pin[0]) / cam.k, ly = (py - cam.pin[1]) / cam.k;
    const sheets = await querySheets(m.key, lx, ly, TAP_PX / cam.k);
    // Dismissed, tapped again or replaced meanwhile: this answer is for nothing on screen.
    if (seq !== S.tapSeq || S.mesh !== m || S.u <= 0.99) return [];
    const seen = new Set();
    const items = T.placeLines(sheets, (x, y) => nameOf(m, x, y), seen);
    // Every label candidate under the finger too, placed or not.
    if (tap) {
      for (const l of tap.index.query(px, py, TAP_PX)) {
        if (seen.has(l.text)) continue;
        const t = knownMinutes(m.entry.field, l.x, l.y);
        if (!(t >= 0) || t > m.outer) continue;
        seen.add(l.text);
        items.push(`${l.text}: ${T.minutesText(t)}`);
      }
    }
    const note = items.length > 1 ? (m.layout === "squash" ? T.SQUASH_TAP_NOTE : T.OVERLAP_NOTE) : "";
    // Over an uncertain patch: drawn, but it has no reliable time to list.
    if (sheets.uncertain && !items.length) { showTapList(T.UNCERTAIN_TAP, []); return [T.UNCERTAIN_TAP]; }
    showTapList(T.tapHeading(items), items, sheets.uncertain ? [note, T.UNCERTAIN_TAP].filter(Boolean).join(" ") : note);
    return items;
  }
  if (!S.active) return [];
  const ll = map.unproject([px, py]);
  const [x, y] = S.active.frame.toLocal(ll.lng, ll.lat);
  const text = T.tapGeographic(nodeAt(S.active.field, x, y), S.origin.label);
  showTapList(text, []);
  return [text];
}

// ── Origin, pin, search, location ────────────────────────────────────
function setOrigin(lon, lat, label) {
  S.origin = { lon, lat, label, example: false };
  S.notice = "";
  pinEl.setAttribute("aria-label", `Starting point: ${label}. Drag it, or use the arrow keys, to move it.`);
  hideTapList();
  checkTransitCoverage();
  handOn();
}

// What goes to the other tools: the pin (never the example), how you travel there, and for
// public transport the time of day.
function handOn() {
  if (S.origin.example) return;
  const { lon, lat, label } = S.origin;
  writeHandoff({ pin: { lon, lat, label }, mode: S.mode, ...(S.mode === "transit" ? { when: S.window } : {}) });
}

function supersedeLookups() { searches.cancel(); locateSeq++; }

const transitInput = () => document.querySelector('input[name="mode"][value="transit"]');

function checkTransitCoverage() {
  const chip = $("transit-chip");
  const input = transitInput();
  const inside = S.transit.available && V.insideArea(S.transit.coverage, S.origin.lon, S.origin.lat);
  input.disabled = S.transit.available && !inside;
  chip.title = input.disabled ? T.LONDON_ONLY : "";
  if (S.mode === "transit" && S.transit.available && !inside) {
    toWalking(T.LONDON_ONLY);
    handOn();          // outside London: walking is what this pin gets, in the other tools too
  }
}

function initPin() {
  let drag = null;
  pinEl.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    pinEl.setPointerCapture(e.pointerId);
    const r = pinEl.getBoundingClientRect();
    const p = { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    drag = { id: e.pointerId, x: e.clientX, y: e.clientY, dx: p.x - e.clientX, dy: p.y - e.clientY, moved: false };
  });
  pinEl.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.moved && Math.hypot(e.clientX - drag.x, e.clientY - drag.y) > 4) {
      drag.moved = true;
      // Straight to the normal map: the drop is read there, and the old picture must not
      // be drawn around the moving pin.
      S.dragging = true;
      flatNow();
      pinEl.classList.add("dragging");
      supersedeLookups();
      clearTimeout(requestTimer);
      latest.cancel();
    }
    if (drag.moved) { S.dragPos = { x: e.clientX + drag.dx, y: e.clientY + drag.dy }; dirty = true; }
  });
  const finish = (dropped) => {
    const d = drag;
    drag = null;
    if (!d || !d.moved) return;
    pinEl.classList.remove("dragging");
    const at = S.dragPos;
    S.dragging = false;
    S.dragPos = null;
    dirty = true;
    if (!dropped || !at) { maybeStartBend(); return; }   // abandoned: the pin goes back
    const ll = map.unproject([at.x, at.y]);
    setOrigin(ll.lng, ll.lat, "the dropped pin");
    requestField();
  };
  pinEl.addEventListener("wheel", (e) => { e.preventDefault(); map.getCanvas().dispatchEvent(new WheelEvent("wheel", e)); }, { passive: false });
  pinEl.addEventListener("pointerup", (e) => { if (drag && e.pointerId === drag.id) finish(true); });
  pinEl.addEventListener("pointercancel", (e) => { if (drag && e.pointerId === drag.id) finish(false); });
  pinEl.addEventListener("lostpointercapture", (e) => { if (drag && e.pointerId === drag.id) finish(false); });

  const KEYS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
  pinEl.addEventListener("keydown", (e) => {
    const d = KEYS[e.key];
    if (!d) return;
    e.preventDefault();
    const step = e.shiftKey ? 50 : 10;
    const p = map.project([S.origin.lon, S.origin.lat]);
    const x = p.x + d[0] * step, y = p.y + d[1] * step;
    const ll = map.unproject([x, y]);
    supersedeLookups();
    if (S.u > 0 || S.fade > 0) flatNow();
    setOrigin(ll.lng, ll.lat, "the dropped pin");
    const margin = 40;
    if (x < margin || x > box.w - margin || y < box.top + margin || y > box.bottom - margin) {
      map.easeTo({ center: [ll.lng, ll.lat], duration: prefersReducedMotion() ? 0 : 300 });
    }
    requestField({ delay: 700 });
  });
  backBtn.addEventListener("click", () => {
    map.easeTo({ center: [S.origin.lon, S.origin.lat], duration: prefersReducedMotion() ? 0 : 500 });
    pinEl.focus();
  });
}

function moveTo(lon, lat, label) {
  setOrigin(lon, lat, label);
  map.jumpTo({ center: [lon, lat] });
  requestField({ frame: true });
}

function initSearch() {
  const input = $("search");
  $("search-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) { setStatus("Type a place to search for.", "error"); input.focus(); return; }
    locateSeq++;
    setStatus("Searching…", "busy");
    let res;
    try { res = await searches.run(q, (signal) => geocode(q, signal)); } catch (err) { setStatus(friendly(err), "error"); return; }
    if (res.stale) return;
    if (!res.value) { setStatus("We couldn't find that place. Try a nearby street or station, or drag the pin.", "error"); return; }
    input.blur();
    moveTo(res.value.lon, res.value.lat, q);
  });
}

function locate() {
  searches.cancel();
  const mine = ++locateSeq;
  setStatus("Finding your location…", "busy");
  findMe({
    onPlace: (lon, lat) => { if (mine === locateSeq) moveTo(lon, lat, "your location"); },
    onError: (words) => { if (mine === locateSeq) setStatus(`${words} Search for a place or drag the pin instead.`, "error"); },
  });
}

function initLocation() {
  $("locate-btn").addEventListener("click", locate);
  // A pin handed on from another tool is where to start: no need to ask.
  if (HANDOFF.pin) return;
  offerLocation($("location-card"), { onYes: locate, onNo: () => toggleBtn.focus() });
}

// ── Controls ─────────────────────────────────────────────────────────
function checkRadio(name, value) {
  const r = document.querySelector(`input[name="${name}"][value="${value}"]`);
  if (r) r.checked = true;
}

function buildOuterChips() {
  const fs = $("outer-chips");
  const legend = fs.querySelector("legend");
  fs.replaceChildren(legend, ...V.MODES[S.mode].outer.map((m) => {
    const label = document.createElement("label");
    label.className = "chip";
    label.innerHTML = `<input type="radio" name="outer" value="${m}" /><span>${m} min</span>`;
    const radio = label.firstElementChild;
    radio.checked = m === S.outer;
    radio.addEventListener("change", () => { if (radio.checked) setOuter(m); });
    return label;
  }));
}

function setOuter(m) {
  S.outer = m;
  updateBands();
  frameView();
  if (S.bent) prepareMesh();
  dirty = true;
}

function initControls() {
  for (const radio of document.querySelectorAll('input[name="mode"]')) {
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      S.mode = radio.value;
      S.outer = V.MODES[S.mode].defaultOuter;
      S.notice = "";
      handOn();
      buildOuterChips();
      hideTapList();
      if (!bendOffered() && S.bent) { S.bent = false; S.pendingMesh = null; flatNow(); }
      requestField({ frame: true });
    });
  }
  for (const radio of document.querySelectorAll('input[name="layout"]')) {
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      S.layoutPick = { mode: S.active ? S.active.json.mode : S.mode, layout: radio.value };
      hideTapList();
      updateLegend();
      prepareMesh();
    });
  }
  for (const radio of document.querySelectorAll('input[name="directions"]')) {
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      S.directions = radio.value;
      prepareMesh();
    });
  }
  $("window-select").addEventListener("change", (e) => {
    S.window = e.target.value;
    S.notice = "";
    writeHandoff({ when: S.window });       // a time picked here goes with you, pin or not
    handOn();
    requestField();
  });
  // A tab left open: check the tables again when it comes back (they may have expired).
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loadTransitStatus(); });
  // And while it stays open and in view: the tables can expire at midnight in London.
  setInterval(() => {
    const j = S.active && S.active.json;
    if (!document.hidden && j && j.mode === "transit" && V.expired(j.valid_until)) loadTransitStatus();
  }, 10 * 60_000);
  toggleBtn.addEventListener("click", () => setBent(!S.bent));
  $("tap-close").addEventListener("click", hideTapList);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("tap-list").hidden) hideTapList(); });
  $("centre-list").addEventListener("click", () => {
    const c = map.project(map.getCenter());
    tapAt(c.x, c.y);
  });
  map.on("click", (e) => tapAt(e.point.x, e.point.y));
  buildOuterChips();
}

let statusRetryMs = 5000;
async function loadTransitStatus() {
  const later = () => { setTimeout(loadTransitStatus, statusRetryMs); statusRetryMs = Math.min(5 * 60_000, statusRetryMs * 3); };
  try {
    const resp = await fetch("/api/transit/status");
    if (!resp.ok) { later(); return; }
    S.transit = await resp.json();
  } catch { updateSwitcher(null); later(); return; }
  statusRetryMs = 5000;
  updateSwitcher(S.transit);
  if (S.transit.loading) { setTimeout(loadTransitStatus, 3000); return; }
  if (!S.statusSeen) watchCity(S.transit);    // Whole city may still be loading: the switcher asks alone
  // Public transport asked for by the link (?mode=transit), or handed on with a pin by another
  // tool (a mode alone, without its pin, would turn the example into a public transport request).
  const wantTransit = Q.get("mode") ? Q.get("mode") === "transit" : Boolean(HANDOFF.pin) && HANDOFF.mode === "transit";
  if (!S.transit.available || V.expired(S.transit.valid_until)) {
    show($("transit-chip"), false);
    for (const k of [...fields.keys()]) if (k.includes(",transit,")) fields.delete(k);
    const why = S.transit.reason === "expired" ? T.EXPIRED : T.NO_TRANSIT;
    if (S.mode === "transit") toWalking(why);
    else if (!S.statusSeen && wantTransit) { S.notice = why; updateLegend(); }
    S.statusSeen = true;
    return;
  }
  show($("transit-chip"), true);
  windowOptions($("window-select"), S.transit.windows, S.window);
  if (!S.statusSeen && V.WINDOWS.includes(Q.get("window"))) S.window = Q.get("window");
  $("window-select").value = S.window;
  checkTransitCoverage();
  const first = !S.statusSeen;
  S.statusSeen = true;
  if (first && wantTransit && S.mode !== "transit") {
    if (transitInput().disabled) { toWalking(T.LONDON_ONLY); handOn(); return; }
    S.mode = "transit";
    checkRadio("mode", "transit");
    S.outer = V.MODES.transit.defaultOuter;
    buildOuterChips();
    requestField({ frame: true });
  }
}

// ── Map ──────────────────────────────────────────────────────────────
function buildStyle() {
  const origin = window.location.origin;
  const text = { "text-color": "#222", "text-halo-color": "rgba(255,255,255,0.9)", "text-halo-width": 2 };
  return {
    version: 8,
    glyphs: `${origin}/glyphs/{fontstack}/{range}.pbf`,
    sources: {
      streets: { type: "vector", tiles: [`${origin}/tiles/{z}/{x}/{y}`], minzoom: 0, maxzoom: 16, attribution: "© Mapbox © OpenStreetMap" },
      bands: { type: "geojson", data: { type: "FeatureCollection", features: [] } },
    },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": "#f0ede9" } },
      { id: "water", type: "fill", source: "streets", "source-layer": "water", paint: { "fill-color": "#aad3df" } },
      { id: "landuse-green", type: "fill", source: "streets", "source-layer": "landuse",
        filter: ["match", ["get", "class"], ["park", "garden", "cemetery", "grass", "scrub", "forest", "wood"], true, false],
        paint: { "fill-color": "#c8facc" } },
      { id: "building", type: "fill", source: "streets", "source-layer": "building", minzoom: 14,
        paint: { "fill-color": "#dfdbd7", "fill-outline-color": "#c0bbb5" } },
      { id: "road-major-case", type: "line", source: "streets", "source-layer": "road",
        filter: ["match", ["get", "class"], ["motorway", "trunk", "primary"], true, false],
        layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": "#c89b7a", "line-width": ["interpolate", ["linear"], ["zoom"], 10, 2, 16, 8] } },
      { id: "road-major", type: "line", source: "streets", "source-layer": "road",
        filter: ["match", ["get", "class"], ["motorway", "trunk", "primary"], true, false],
        layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": "#fcd6a4", "line-width": ["interpolate", ["linear"], ["zoom"], 10, 1.2, 16, 6] } },
      { id: "road-minor", type: "line", source: "streets", "source-layer": "road",
        filter: ["match", ["get", "class"], ["secondary", "tertiary", "street", "street_limited"], true, false],
        layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": "#ffffff", "line-width": ["interpolate", ["linear"], ["zoom"], 12, 0.5, 16, 4] } },
      { id: "rail", type: "line", source: "streets", "source-layer": "road",
        filter: ["match", ["get", "class"], ["major_rail", "minor_rail"], true, false],
        paint: { "line-color": "#8c8c8c", "line-width": ["interpolate", ["linear"], ["zoom"], 10, 1, 16, 2.5] } },
      // The travel-time bands, over the base map and under every label.
      { id: "bands-fill", type: "fill", source: "bands", paint: { "fill-color": ["get", "color"], "fill-opacity": V.BAND_OPACITY } },
      { id: "bands-line", type: "line", source: "bands",
        paint: { "line-color": ["get", "color"], "line-width": 1, "line-opacity": 0.9 } },
      { id: "road-label", type: "symbol", source: "streets", "source-layer": "road", minzoom: 14,
        filter: ["all", ["has", "name"], ["match", ["get", "class"], ["primary", "secondary", "tertiary", "street", "trunk"], true, false]],
        layout: { "text-field": ["get", "name"], "text-font": ["Open Sans Regular", "Arial Unicode MS Regular"], "text-size": 10, "symbol-placement": "line" },
        paint: { ...text, "text-color": "#555", "text-halo-width": 1.5 } },
      { id: "station-label", type: "symbol", source: "streets", "source-layer": "transit_stop_label", minzoom: 12,
        filter: ["==", ["get", "stop_type"], "station"],
        layout: { "text-field": ["get", "name"], "text-font": ["Open Sans Bold", "Arial Unicode MS Bold"], "text-size": 11, "text-anchor": "top", "text-offset": [0, 0.4] },
        paint: { ...text, "text-color": "#1c1c28" } },
      { id: "place-label", type: "symbol", source: "streets", "source-layer": "place_label",
        layout: { "text-field": ["get", "name"], "text-font": ["Open Sans Bold", "Arial Unicode MS Bold"],
          "text-size": ["interpolate", ["linear"], ["zoom"], 10, 10, 16, 14], "text-max-width": 10 },
        paint: text },
    ],
  };
}

function initMap(center, zoom) {
  map = new maplibregl.Map({
    container: "map-container", style: buildStyle(), center, zoom,
    // The page shows the Mapbox logo and credits itself (#attribution), above every canvas.
    attributionControl: false, dragRotate: false, touchPitch: false, pitchWithRotate: false,
  });
  map.touchZoomRotate.disableRotation();
  map.on("render", () => { if (S.fade > 0) dirty = true; });
  return new Promise((resolve) => map.once("load", resolve));
}

function resize() { if (map) map.resize(); measureBox(); dirty = true; }
window.addEventListener("resize", resize);
if (typeof ResizeObserver === "function") {
  const ro = new ResizeObserver(() => { measureBox(); dirty = true; });
  ro.observe(topEl);
  ro.observe(legendEl);
}

// ── Renderer ─────────────────────────────────────────────────────────
function initRenderer() {
  const r = createMeshRenderer(meshCanvas, {
    backend: Q.get("backend") === "c" ? "cpu" : "gpu", force: Q.get("caps") || "",
    onLost: () => { flatNow(); S.bent = false; S.pendingMesh = null; setStatus(T.NO_BEND, "error"); dirty = true; },
    onRestored: () => { S.mesh = null; S.pendingMesh = null; setStatus(""); dirty = true; },
  });
  S.renderer = r;
  // The field worker contours the normal map's bands, which every device draws.
  S.fieldWorker = new Worker("/shared/mesh/worker.js", { type: "module" });
  S.fieldWorker.onmessage = onWorker;
  S.fieldWorker.onerror = S.fieldWorker.onmessageerror = onWorkerFailure;
  if (!r.supported) {
    document.body.classList.add("no-bend");
    setStatus(T.NO_BEND, "error");
    return;
  }
  S.worker = new Worker("/shared/mesh/worker.js", { type: "module" });
  S.worker.onmessage = onWorker;
  S.worker.onerror = S.worker.onmessageerror = onWorkerFailure;
}

// ── Boot ─────────────────────────────────────────────────────────────
async function loadExample() {
  const resp = await fetch("example-field.json");
  if (!resp.ok) throw new Error("example missing");
  const ex = await resp.json();
  if (!ex.origin || !ex.minutes_b64 || !V.MODES[ex.mode]) throw new Error("example invalid");
  return ex;
}

async function boot() {
  if (typeof maplibregl === "undefined") {
    setStatus("The map couldn't load. Check your connection and reload the page.", "error");
    toggleBtn.disabled = true;
    return;
  }
  measureBox();
  initRenderer();
  let ex = null;
  if (HANDOFF.pin) {
    // Handed on by Meet up or Whole city: start there, with their way of travelling (public
    // transport waits for the tables' status) and their time of day. The link's own ?mode and
    // ?window still win.
    const p = HANDOFF.pin;
    S.origin = { lon: p.lon, lat: p.lat, label: p.label || "your pin", example: false };
    const mode = Q.get("mode") || HANDOFF.mode;
    if (mode && mode !== "transit" && V.MODES[mode]) { S.mode = mode; S.outer = V.MODES[mode].defaultOuter; }
  } else {
    try { ex = await loadExample(); } catch { setStatus("The example didn't load. Search for a place or drag the pin.", "error"); }
  }
  if (ex) {
    S.origin = { lon: ex.origin.lon, lat: ex.origin.lat, label: ex.origin.label || FALLBACK_ORIGIN.label, example: true };
    S.mode = ex.mode;
    S.outer = V.MODES[ex.mode].defaultOuter;
  }
  if (HANDOFF.when) S.window = HANDOFF.when;
  checkRadio("mode", S.mode);
  checkRadio("layout", layoutNow());
  pinEl.setAttribute("aria-label", `Starting point: ${S.origin.label}. Drag it, or use the arrow keys, to move it.`);
  await initMap([S.origin.lon, S.origin.lat], 14);
  if (ex) {
    const key = currentKey();
    const entry = entryFrom(ex);
    remember(key, entry);
    S.frameWhenReady = true;
    activate(key, entry);
  } else if (HANDOFF.pin) {
    requestField({ frame: true });
  }
  initPin();
  initSearch();
  initLocation();
  initControls();
  loadTransitStatus();
  requestAnimationFrame(loop);
  testHook.readyAt = performance.now();
}

// ── For the headless checks (scripts/lab_headless.mjs) ───────────────
const testHook = {
  S, get map() { return map; },
  // Bend (or unbend) and resolve when the morph is done.
  bend(on = true) {
    setBent(on);
    return new Promise((resolve) => {
      const tick = () => (!S.anim && (on ? S.u >= 1 || !S.bent : S.u <= 0) ? resolve(S.u) : requestAnimationFrame(tick));
      tick();
    });
  },
  // Pan the camera and resolve at the next frame drawn with it: {ms, requests}.
  panBy(dx, dy) {
    const t0 = performance.now();
    return new Promise((resolve) => {
      const done = () => resolve({ ms: performance.now() - t0, requests: S.requests });
      if (S.fade > 0) frameHooks.push(done); else map.once("render", done);
      map.panBy([dx, dy], { duration: 0 });
    });
  },
  setPin(lon, lat, label = "a test pin") { setOrigin(lon, lat, label); requestField(); },
  setMode(mode) {
    const r = document.querySelector(`input[name="mode"][value="${mode}"]`);
    r.checked = true;
    r.dispatchEvent(new Event("change"));
  },
  tapAt,
  get requests() { return S.requests; },
};
// Only on request (?test=1): the headless checks drive the page through it.
if (Q.get("test") === "1") window.__warp = testHook;
testHook.ready = boot();
