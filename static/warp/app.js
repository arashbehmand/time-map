// "How far can I get from here?" (#5, W-02)
//
// The server answers, once per (pin, mode, window), with a travel-time field: minutes on a
// grid around the pin (POST /api/travel-time-field). Two views draw it:
//   ordinary  the normal map (MapLibre) with travel-time bands, islands and holes included.
//             The baseline: it always works, even without WebGL2.
//   bent      the mesh renderer (static/shared/mesh/): each place keeps its direction from
//             the pin (by reach or true compass) and sits at a distance proportional to its
//             minutes, so the rings are exact circles. Experimental; for public transport
//             it stays hidden until Direction 4's checks pass (views.js).
// "Bend the map" dissolves the normal map into the unbent mesh, then morphs it. Pan, zoom
// and resize never fetch; a new pin, mode or window fetches one field, and the old picture
// stays up until the new one is ready.
import { createLatest } from "/shared/latest.js";
import { friendly, geocode, loadPref, postJSON, prefersReducedMotion, savePref } from "/shared/api.js";
import { bandMesh, bandPolygons } from "/shared/mesh/contours.js";
import { tileCover } from "/shared/mesh/cover.js";
import { decodeField, nodeAt, sampleField } from "/shared/mesh/field.js";
import { fitFor, reachBox } from "/shared/mesh/fit.js";
import { cameraFromMap, localFrame } from "/shared/mesh/frame.js";
import { buildTapIndex, LABEL_CAP, placeLabels, sortCandidates } from "/shared/mesh/labels.js";
import { DEFAULT_BUDGET, DEFAULT_SPACING_DIV } from "/shared/mesh/pipeline.js";
import { createMeshRenderer } from "/shared/mesh/renderer.js";
import { bandColor, bandRgb, ringPlan } from "/shared/mesh/rings.js";
import { angularSpread, createWarp, dotScale, targetsFor } from "/shared/mesh/warp.js";
import * as V from "./views.js";
import * as T from "./words.js";

// ── State ────────────────────────────────────────────────────────────
const Q = new URLSearchParams(location.search);
const FALLBACK_ORIGIN = { lon: -0.1233, lat: 51.5308, label: "King's Cross", example: true };
const FIELD_CACHE = 8;
const LOCATION_PREF = "timemap-location";
const TAP_PX = 12;

const S = {
  origin: { ...FALLBACK_ORIGIN },
  mode: "walking",
  window: V.defaultWindow(),
  outer: V.MODES.walking.defaultOuter,
  directions: V.DEFAULT_DIRECTIONS,
  active: null,          // the field drawn now: {key, json, field, frame}
  bent: false,           // what the person asked for
  bentOnce: false,       // after the first bend, scenes are prepared for every new field
  u: 0, fade: 0,         // morph (0 ordinary … 1 bent) and the mesh's opacity
  anim: null,
  mesh: null,            // the bent view drawn now: {key, gen, spec, warp, frame, scene, labels, kRef, zRef}
  pendingMesh: null,
  gen: 0,
  dragging: false, dragPos: null,
  transit: { available: false },
  renderer: null, worker: null,
  frameWhenReady: false,
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
  const cached = fields.get(key);
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
      setStatus(friendly(e), "error");
      return;
    }
    if (res.stale) return;
    const entry = entryFrom(res.value);
    remember(snap.key, entry);
    S.timings.push({ kind: "field", ms: performance.now() - t0 });
    if (snap.key === currentKey()) activate(snap.key, entry);
  }, delay);
}

function toWalking(message) {
  S.mode = "walking";
  checkRadio("mode", "walking");
  S.outer = V.MODES.walking.defaultOuter;
  buildOuterChips();
  requestField({ frame: true });
  setStatus(message, "error");
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
  if (S.bent || S.bentOnce) prepareMesh();
  dirty = true;
}

// ── The ordinary view: bands on the normal map ───────────────────────
function levels() { return ringPlan(S.outer).map((r) => r.minutes); }

function updateBands() {
  const src = map && map.getSource("bands");
  if (!src || !S.active) return;
  const lv = levels();
  const fc = bandPolygons(S.active.field, S.active.frame, lv, { skipFilled: true });
  for (const f of fc.features) f.properties.color = bandColor(f.properties.band, lv.length);
  src.setData(fc);
  buildBandLegend(lv);
}

function buildBandLegend(lv) {
  const el = $("band-legend");
  el.replaceChildren(...lv.map((m, i) => {
    const s = document.createElement("span");
    s.style.background = bandColor(i, lv.length);
    if (m % 15 === 0 || i === lv.length - 1) s.dataset.label = `${m}`;
    return s;
  }));
}

function frameView() {
  if (!S.active || !map) return;
  measureBox();
  const spec = { kind: "polar", field: S.active.field, maxMinutes: S.outer };
  const fit = fitFor(spec, S.active.frame, box.w, box.h, S.outer);
  map.easeTo({
    center: [S.origin.lon, S.origin.lat], zoom: Math.min(16, fit.zoom),
    offset: [0, (box.top + box.bottom) / 2 - mapContainer.clientHeight / 2],
    duration: prefersReducedMotion() ? 0 : 700,
  });
}

// ── The bent view: the mesh ──────────────────────────────────────────
const needsCpuTargets = () => !(S.renderer && S.renderer.mode === "gpu");

function meshSpec(entry) {
  const spec = { kind: "polar", field: entry.field, maxMinutes: S.outer };
  const spread = V.DIRECTIONS[S.directions];
  if (spread > 0) spec.angles = angularSpread(entry.field, S.outer, { spread });
  return spec;
}

// What the worker needs (plain arrays; it rebuilds the field and the warp).
function plainField(f) {
  return { origin: f.origin, cell: f.cell, nx: f.nx, ny: f.ny, x0: f.x0, y0: f.y0, minutes: f.minutes };
}

// Ask the worker for the scene of the active field; it is swapped in when it arrives.
function prepareMesh() {
  if (!S.renderer || !S.renderer.supported || !S.active) return;
  const entry = S.active;
  const spec = meshSpec(entry);
  const key = `${entry.key}|${S.outer}|${S.directions}`;
  if (S.mesh && S.mesh.key === key) return;
  if (S.pendingMesh && S.pendingMesh.key === key) return;
  measureBox();
  const fit = fitFor(spec, entry.frame, box.w, box.h, S.outer);
  const cover = tileCover(entry.frame, reachBox(spec, S.outer));
  const gen = ++S.gen;
  S.pendingMesh = { key, gen, entry, spec, warp: createWarp(spec), frame: entry.frame, kRef: fit.k, zRef: fit.zoom, requestedAt: performance.now() };
  S.worker.postMessage({
    type: "scene", gen, origin: entry.frame.origin, tiles: cover.tiles,
    spacingDiv: DEFAULT_SPACING_DIV, budget: DEFAULT_BUDGET, source: "tiles",
    warp: needsCpuTargets() ? { kind: "polar", field: plainField(entry.field), maxMinutes: S.outer, angles: spec.angles || null } : null,
    refine: { field: plainField(entry.field), maxMinutes: S.outer, eps: V.refineEps(S.outer), angles: spec.angles || null },
  });
  if (S.bent && !S.mesh) setStatus(T.PREPARING, "busy");
}

function onWorker(e) {
  const msg = e.data;
  if (msg.type === "sheets-result") {
    const w = sheetWaiters.get(msg.id);
    if (w) { sheetWaiters.delete(msg.id); w(msg.hits || []); }
    return;
  }
  if (msg.type === "error") {
    if (S.pendingMesh && msg.gen === S.pendingMesh.gen) setStatus(friendly(new Error(msg.message)), "error");
    return;
  }
  if (msg.type !== "scene") return;
  const p = S.pendingMesh;
  const m = S.mesh;
  if (p && msg.gen === p.gen) {
    if (msg.partial && m) return;                   // keep the old view until the scene is whole
    installMesh(p, msg.scene, !msg.partial);
    if (!msg.partial) { S.pendingMesh = null; S.timings.push({ kind: "scene", ms: performance.now() - p.requestedAt }); }
  } else if (m && msg.gen === m.gen) {
    m.scene = msg.scene;                            // the rest of a scene already shown
    S.renderer.setScene(msg.scene);
    m.labels = labelsFor(m);
    dirty = true;
  }
}

function installMesh(p, scene, whole) {
  const r = S.renderer;
  const field = p.entry.field;
  r.setWarp(p.spec);
  const bands = bandMesh(field, reachBox(p.spec, S.outer));
  if (needsCpuTargets()) bands.targets = targetsFor(p.warp, bands.pos);
  r.setBandMesh(bands);
  if (p.entry.json.mode === "transit") r.setFoldMesh({ pos: bands.pos, index: bands.index, targets: bands.targets || null });
  r.setScene(scene);
  S.mesh = { ...p, scene };
  S.mesh.labels = labelsFor(S.mesh);
  S.worker.postMessage({ type: "sheets", key: p.key, spec: { field: { ...plainField(field), filled: field.filled }, maxMinutes: S.outer, angles: p.spec.angles || null } });
  if (whole && statusEl.textContent === T.PREPARING) setStatus("");
  dirty = true;
  if (whole) maybeStartBend();            // bend only once every tile is in
}

// Label candidates: the timetable's stations (with their minutes) for public transport, the
// tiles' stations otherwise, and the tiles' places.
function labelsFor(m) {
  const minutesOf = (x, y) => sampleField(m.entry.field, x, y);
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
function bendOffered() { return S.renderer && S.renderer.supported && V.bendOffered(S.mode, location.search); }

function setBent(on) {
  S.bent = !!(on && bendOffered());
  if (S.bent) { S.bentOnce = true; prepareMesh(); maybeStartBend(); }
  else animateTo(0);
  updateLegend();
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
}

const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);

function stepAnim(now) {
  const a = S.anim;
  if (!a) return;
  if (a.reduce) { S.u = S.fade = a.to; S.anim = null; dirty = true; return; }
  const t = now - a.start;
  if (a.to === 1) {
    const fadeDur = V.FADE_MS * (1 - a.f0), morphDur = V.MORPH_MS * (1 - a.u0);
    S.fade = fadeDur > 0 ? Math.min(1, a.f0 + (1 - a.f0) * Math.min(1, t / fadeDur)) : 1;
    const mt = t - fadeDur;
    S.u = morphDur > 0 ? (mt <= 0 ? a.u0 : a.u0 + (1 - a.u0) * ease(Math.min(1, mt / morphDur))) : 1;
    if (S.fade >= 1 && S.u >= 1) S.anim = null;
  } else {
    const morphDur = V.MORPH_MS * a.u0, fadeDur = V.FADE_MS * a.f0;
    S.u = morphDur > 0 ? a.u0 * (1 - ease(Math.min(1, t / morphDur))) : 0;
    const ft = t - morphDur;
    S.fade = fadeDur > 0 ? (ft <= 0 ? a.f0 : a.f0 * (1 - Math.min(1, ft / fadeDur))) : 0;
    if (S.fade <= 0 && S.u <= 0) S.anim = null;
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
    const lv = levels();
    const stats = S.renderer.draw(cam, S.u, {
      showFolds: m.entry.json.mode === "transit", adaptive: true,
      bands: { levels: lv, colors: lv.map((_, i) => bandRgb(i, lv.length)), opacity: V.BAND_OPACITY },
    });
    if (!stats.skipped) S.drawn = now;
    pin = { x: cam.pin[0], y: cam.pin[1] };
    drawRings(cam);
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

function drawRings(cam) {
  const a = Math.max(0, Math.min(1, (S.u - V.RING_FROM_U) / (1 - V.RING_FROM_U)));
  if (a <= 0) return;
  ctx.save();
  ctx.globalAlpha = a;
  for (const r of ringPlan(S.outer)) {
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

function drawLabels(m, cam) {
  const u = S.u;
  const screenOf = (x, y) => m.warp.screenOfLocal(x, y, cam, u);
  const phone = Math.min(cam.width, cam.height) < 600;
  S.placed = placeLabels(m.labels, {
    screenOf, measure,
    jacobianOf: (x, y) => m.warp.screenJacobian(x, y, cam, u),
    minutesOf: (x, y) => sampleField(m.entry.field, x, y),
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
  const plan = ringPlan(S.outer);
  setText($("from"), T.fromLine(S.origin));
  setText($("sentence"), S.dragging ? "Drop the pin where you want to start."
    : T.sentence(S.mode, S.outer, plan.length > 1 ? plan[1].minutes - plan[0].minutes : S.outer, bentNow));
  setText($("limits"), S.origin.example ? T.EXAMPLE_NOTE : T.limits(S.active && S.active.json));
  setText($("beyond"), T.beyond(S.outer));
  const offered = !!bendOffered();
  show(toggleBtn, offered);
  setText(toggleBtn, S.bent ? T.BEND_LABEL.on : T.BEND_LABEL.off);
  if (toggleBtn.getAttribute("aria-pressed") !== String(S.bent)) toggleBtn.setAttribute("aria-pressed", String(S.bent));
  show($("direction-chips"), offered && S.bent);
  show($("direction-note"), offered && S.bent);
  setText($("direction-note"), T.DIRECTION_NOTES[S.directions]);
  show($("overlap-note"), S.bent && S.mode === "transit");
  setText($("overlap-note"), T.OVERLAP_NOTE);
  show($("more-options"), S.mode === "transit" && S.transit.available);
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
function hideTapList() { show($("tap-list"), false); S.tapMark = null; dirty = true; }

function querySheets(key, lx, ly, r) {
  const id = ++sheetsSeq;
  return new Promise((resolve) => {
    sheetWaiters.set(id, resolve);
    S.worker.postMessage({ type: "sheets-query", id, key, lx, ly, r });
  });
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
  S.tapMark = { x: px, y: py };
  dirty = true;
  if (S.u > 0.99 && S.mesh) {
    const m = S.mesh;
    const cam = camera(m);
    const lx = (px - cam.pin[0]) / cam.k, ly = (py - cam.pin[1]) / cam.k;
    const sheets = await querySheets(m.key, lx, ly, TAP_PX / cam.k);
    const seen = new Set();
    const items = [];
    for (const s of sheets) {
      const name = nameOf(m, s.x, s.y);
      if (seen.has(name)) continue;
      seen.add(name);
      items.push(`${name}: ${T.minutesText(s.minutes)}`);
    }
    // Every label candidate under the finger too, placed or not.
    if (S.tap) {
      for (const l of S.tap.index.query(px, py, TAP_PX)) {
        if (seen.has(l.text)) continue;
        const t = sampleField(m.entry.field, l.x, l.y);
        if (!(t >= 0) || t > S.outer) continue;
        seen.add(l.text);
        items.push(`${l.text}: ${T.minutesText(t)}`);
      }
    }
    showTapList(T.tapHeading(items), items, items.length > 1 ? T.OVERLAP_NOTE : "");
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
  pinEl.setAttribute("aria-label", `Starting point: ${label}. Drag it, or use the arrow keys, to move it.`);
  hideTapList();
  checkTransitCoverage();
}

function supersedeLookups() { searches.cancel(); locateSeq++; }

const transitInput = () => document.querySelector('input[name="mode"][value="transit"]');

function checkTransitCoverage() {
  const chip = $("transit-chip");
  const input = transitInput();
  const inside = S.transit.available && V.insideArea(S.transit.coverage, S.origin.lon, S.origin.lat);
  input.disabled = S.transit.available && !inside;
  chip.title = input.disabled ? T.LONDON_ONLY : "";
  if (S.mode === "transit" && S.transit.available && !inside) toWalking(T.LONDON_ONLY);
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
      S.u = 0; S.fade = 0; S.anim = null;
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
    if (S.u > 0 || S.fade > 0) { S.u = 0; S.fade = 0; S.anim = null; }
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
  if (!navigator.geolocation) { setStatus("This browser can't share your location. Search for a place or drag the pin.", "error"); return; }
  searches.cancel();
  const mine = ++locateSeq;
  setStatus("Finding your location…", "busy");
  navigator.geolocation.getCurrentPosition(
    (p) => { if (mine !== locateSeq) return; savePref(LOCATION_PREF, "yes"); moveTo(p.coords.longitude, p.coords.latitude, "your location"); },
    (err) => {
      if (mine !== locateSeq) return;
      setStatus(err.code === 1 ? "Location is turned off for this site. Search for a place or drag the pin instead."
        : "We couldn't get your location. Search for a place or drag the pin instead.", "error");
    },
    { enableHighAccuracy: false, timeout: 15000, maximumAge: 60000 },
  );
}

function initLocation() {
  $("locate-btn").addEventListener("click", locate);
  const card = $("location-card");
  if (!navigator.geolocation || loadPref(LOCATION_PREF) !== null) return;
  card.hidden = false;
  $("location-yes").addEventListener("click", () => { card.hidden = true; savePref(LOCATION_PREF, "yes"); locate(); });
  $("location-no").addEventListener("click", () => { card.hidden = true; savePref(LOCATION_PREF, "no"); toggleBtn.focus(); });
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
  if (S.bent || S.bentOnce) prepareMesh();
  dirty = true;
}

function initControls() {
  for (const radio of document.querySelectorAll('input[name="mode"]')) {
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      S.mode = radio.value;
      S.outer = V.MODES[S.mode].defaultOuter;
      buildOuterChips();
      hideTapList();
      if (!bendOffered() && S.bent) { S.bent = false; S.u = 0; S.fade = 0; S.anim = null; }
      requestField({ frame: true });
    });
  }
  for (const radio of document.querySelectorAll('input[name="directions"]')) {
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      S.directions = radio.value;
      prepareMesh();
    });
  }
  $("window-select").addEventListener("change", (e) => { S.window = e.target.value; requestField(); });
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

async function loadTransitStatus() {
  try {
    const resp = await fetch("/api/transit/status");
    if (!resp.ok) return;
    S.transit = await resp.json();
  } catch { return; }
  if (S.transit.loading) { setTimeout(loadTransitStatus, 3000); return; }
  if (!S.transit.available) return;
  show($("transit-chip"), true);
  const sel = $("window-select");
  sel.replaceChildren(...S.transit.windows.map((w) => new Option(w.label[0].toUpperCase() + w.label.slice(1), w.id)));
  if (V.WINDOWS.includes(Q.get("window"))) S.window = Q.get("window");
  sel.value = S.window;
  checkTransitCoverage();
  if (Q.get("mode") === "transit" && !transitInput().disabled) {
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
    onLost: () => { S.u = 0; S.fade = 0; S.anim = null; S.bent = false; setStatus(T.NO_BEND, "error"); dirty = true; },
    onRestored: () => { S.mesh = null; S.pendingMesh = null; setStatus(""); dirty = true; },
  });
  S.renderer = r;
  if (!r.supported) {
    document.body.classList.add("no-bend");
    setStatus(T.NO_BEND, "error");
    return;
  }
  S.worker = new Worker("/shared/mesh/worker.js", { type: "module" });
  S.worker.onmessage = onWorker;
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
  try { ex = await loadExample(); } catch { setStatus("The example didn't load. Search for a place or drag the pin.", "error"); }
  if (ex) {
    S.origin = { lon: ex.origin.lon, lat: ex.origin.lat, label: ex.origin.label || FALLBACK_ORIGIN.label, example: true };
    S.mode = ex.mode;
    S.outer = V.MODES[ex.mode].defaultOuter;
  }
  checkRadio("mode", S.mode);
  pinEl.setAttribute("aria-label", `Starting point: ${S.origin.label}. Drag it, or use the arrow keys, to move it.`);
  await initMap([S.origin.lon, S.origin.lat], 14);
  if (ex) {
    const key = currentKey();
    const entry = entryFrom(ex);
    remember(key, entry);
    S.frameWhenReady = true;
    activate(key, entry);
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
window.__warp = testHook;
testHook.ready = boot();
