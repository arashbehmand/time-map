// Whole city (#28): the full London time map of #6 (L-02), in the app. It draws the land with the
// app's mesh renderer (/shared/mesh/, cpu back-end, a target per vertex) and the stations, names
// and the Metropolitan line (carried through the mesh like the land) on a 2-D canvas above it.
//
// Its data is one build of `python -m transit.layout.timemap`, which the server serves only
// under the transit tables' own gate (server/transit/whole_city.py): GET /api/transit/status says
// whether it can be shown here, and for which times of day. When it can't, the page says so and
// fetches nothing else. It makes no routing and no tile requests.
//
// The map moves smoothly. The slider runs continuously from the real map to the last solved
// position; between two neighbouring keyframes the page draws their straight blend, which the
// build checked never folds (timemap.py, motion_keyframes), and the renderer's morph does the
// mixing on the GPU. "Time map" plays the whole move at an even pace, and pressed again plays it
// back; with reduced motion they jump. Faint rings mark where the named stations really are.
//
// Lines (rail, borough outlines, the Metropolitan line) get thinner where the map squeezes the
// ground across them, and smooth lines round off the small bends they take where they cross a
// mesh edge (carry.js says how). ?details=1 shows the build's own numbers as well.
import { readHandoff, writeHandoff } from "/shared/handoff.js";
import { createMeshRenderer } from "/shared/mesh/renderer.js";
import { localFrame, makeCamera } from "/shared/mesh/frame.js";
import { STYLE_ID } from "/shared/mesh/schema-streets-v8.js";
import { cityUnavailable } from "/shared/tools.js";
import { forgetCity, keepCity, keptCityWhy } from "/shared/offline.js";
import { defaultWindow, pollStatus, statusLoading, windowText } from "/shared/transit.js";
import {
  blend, buildLand, createMotion, createPlacer, decodeLand, keyframes, loadSegment, pathLengths, placeLine,
  segmentAt, segmentTriangles, smoothLine, triangleMaps, widthFactors,
} from "./carry.js";

const $ = (id) => document.getElementById(id);
const glCanvas = $("map"), overlay = $("overlay"), tip = $("tip"), status = $("status");
const slider = $("frame"), readout = $("readout"), toggle = $("bend");
const DETAILS = new URLSearchParams(location.search).get("details") === "1";
const TAP_PX = 5;            // a press that moves less than this is a tap, not a drag
const PLAY_MS = 4500;        // the whole move, real map to the end; a part of it takes its share
const STEP_MS = 450;         // the shortest glide with the arrow keys
const HANDOFF = readHandoff();

// Say why there's no map here, and stop: nothing below runs (an unsettled await, not an error,
// so the console stays clean).
async function unavailable(reason) {
  status.textContent = "";
  $("unavailable-text").textContent = cityUnavailable(reason);
  $("unavailable").hidden = false;
  $("controls").hidden = true;
  await new Promise(() => {});
}

async function load(url, kind) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(String(r.status));
  return kind === "json" ? r.json() : r.arrayBuffer();
}

// ── which build, which time of day ──────────────────────────────────
// One quick question first: no network at all means the map kept from the last visit, if there
// is one and its build hasn't expired (/shared/offline.js). Otherwise,
// right after a deploy the server may still be loading the tables or this map: keep asking for a
// couple of minutes, saying so, before giving up.
let state = null, offline = null, noNetwork = "";
try {
  const resp = await fetch("/api/transit/status");
  if (resp.ok) state = await resp.json();
} catch {
  ({ city: offline, reason: noNetwork } = await keptCityWhy());
}
if (!offline && (!state || statusLoading(state))) state = await pollStatus();
for (let round = 0; !offline && statusLoading(state) && round < 6; round++) {
  status.textContent = "Whole city is still getting ready…";
  state = await pollStatus();
}
if (!offline && !state && !noNetwork) ({ city: offline, reason: noNetwork } = await keptCityWhy());   // never answered
const city = offline || (state && state.whole_city);
if (!offline && city && !city.available && city.reason !== "loading") await forgetCity();
if (!city || !city.available) {
  if (city && city.reason === "loading") {
    status.textContent = "";
    $("unavailable-text").textContent = "Whole city is still getting ready. Reload the page in a moment.";
    $("unavailable").hidden = false;
    await new Promise(() => {});
  }
  await unavailable(city ? city.reason : noNetwork);   // "expired", "offline", or "" (no answer, nothing kept)
}
const built = city.windows.map((w) => w.id);
const label = Object.fromEntries(city.windows.map((w) => [w.id, w.label]));
// The time of day: one picked here (?window=, so it works with storage blocked too), else the
// one handed on by From here or Meet up, if it's built. A fallback (the time in London now, else
// the first built) doesn't overwrite what they asked for; a choice made here does.
const asked = new URLSearchParams(location.search).get("window");
const chosenHere = built.includes(asked) ? asked : null;
const wanted = chosenHere || HANDOFF.when;
const now = defaultWindow();
const win = wanted && built.includes(wanted) ? wanted : built.includes(now) ? now : built[0];
if (wanted && wanted !== win) {
  $("window-note").textContent = `That time of day isn't built yet. Showing ${label[win]}.`;
  $("window-note").hidden = false;
}
if (built.length > 1) {
  const sel = $("window-select");
  sel.replaceChildren(...city.windows.map((w) => new Option(windowText(w.label), w.id)));
  sel.value = win;
  sel.hidden = $("when-label").hidden = false;
  sel.addEventListener("change", () => {
    writeHandoff({ when: sel.value });
    const q = new URLSearchParams(location.search);
    q.set("window", sel.value);
    location.search = q.toString();
  });
  if (chosenHere) sel.focus();      // back where the keyboard was before the page reloaded
} else {
  $("when-line").textContent = `When: ${label[win]}.`;
}

let doc = null, landBuf = null;
try {
  doc = await load(`/api/whole-city/${win}/timemap.json`, "json");
  landBuf = doc.land ? await load(`/api/whole-city/${win}/land.bin`, "bin") : null;
} catch {
  doc = null;
}
if (!doc) await unavailable(offline ? "offline" : "load_failed");
if (!offline) keepCity(city);       // for the next time there is no network
status.textContent = "";
$("controls").hidden = false;
for (const el of document.querySelectorAll(".details-only")) el.hidden = !DETAILS;

const frame = localFrame({ lon: doc.frame.origin[0], lat: doc.frame.origin[1] });
const kinds = doc.nodes.kind;
const nStation = doc.nodes.stations;
const tri = Uint32Array.from(doc.triangles);
const keys = keyframes(doc);
const last = doc.frames.length - 1;              // the slider runs 0 … last
const real = keys[0].nodes;
const inner = kinds.map((k, i) => (k !== 2 ? i : -1)).filter((i) => i >= 0);
const cum = pathLengths(keys, inner);
const reduceMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// A public page's line about where this comes from (the build's own notice is for its report).
$("notice").textContent = `Made from London's public transport timetables for ${label[win]}: typical times, ` +
  `not live ones. Valid until ${city.valid_until}.` +
  (city.development_only ? " Shown on this computer only: this build's licences aren't cleared." : "") +
  (offline ? " You're offline: this is the map from your last visit." : "");
const credits = [...new Set((Array.isArray(doc.attribution) ? doc.attribution : []).map((c) => String(c).replace("(c)", "©")))];
$("credits").textContent = credits.length ? "Credits: " + credits.join("; ") + "." : "";
if (city.report && city.report[win]) {
  $("report-link").href = `/api/whole-city/${win}/report.md`;
  $("shepard-link").href = `/api/whole-city/${win}/shepard.svg`;
  $("checked").hidden = false;
}
slider.max = String(last);

// ── renderer ────────────────────────────────────────────────────────
const renderer = createMeshRenderer(glCanvas, {
  backend: "cpu",
  onLost: () => { status.textContent = "The land went missing for a moment. It comes back by itself."; draw(); },
  onRestored: () => { status.textContent = ""; renderer.setWarp({ kind: "mesh", origin: frame.origin }); uploaded = -1; moveTo(motion.v); },
});
let scene = null;
if (!renderer.supported) {
  status.textContent = "This browser can't draw the land, so only the stations show.";
} else {
  renderer.setWarp({ kind: "mesh", origin: frame.origin });
  if (landBuf) scene = buildLand(decodeLand(landBuf, doc.land), doc.land, real, STYLE_ID);
}
const smoothBox = $("smooth");
const placer = scene ? createPlacer(scene, keys, { triangles: tri, smooth: smoothBox.checked }) : null;

// The Metropolitan line, carried like the land: on every keyframe its exact and smoothed
// vertices, and each segment's width factor (thinner where the map squeezes it).
const maps = keys.map((k) => triangleMaps(tri, real, k.nodes));
const routes = doc.lines.map((ln) => {
  const nv = ln.c.length / 3;
  const segC = new Uint16Array(6 * Math.max(0, nv - 1));
  for (let i = 0; i + 1 < nv; i++) segC.set(ln.c.slice(3 * i, 3 * i + 6), 6 * i);
  const pinned = Uint8Array.from({ length: nv }, (_, i) => (ln.c[3 * i] === ln.c[3 * i + 1] && ln.c[3 * i + 1] === ln.c[3 * i + 2] ? 1 : 0));
  const realXY = placeLine(ln, doc.weight_scale, real);
  const realSeg = new Float64Array(4 * Math.max(0, nv - 1));
  for (let i = 0; i + 1 < nv; i++) realSeg.set(realXY.subarray(2 * i, 2 * i + 4), 4 * i);
  const segTri = segmentTriangles(segC, tri);
  const at = keys.map((k) => placeLine(ln, doc.weight_scale, k.nodes));
  return { ...ln, at, smoothed: at.map((xy) => smoothLine(realXY, xy, pinned)), wf: maps.map((mp) => widthFactors(realSeg, segTri, mp)) };
});

// ── view ────────────────────────────────────────────────────────────
const view = { cx: 0, cy: 0, zoom: 10, w: 1, h: 1 };
let v = 0;                   // where the map is drawn: 0 the real map … last the end of the slider
let uploaded = -1;           // the blend whose layouts the renderer holds
let seg = { k: 0, s: 0 };
let dragging = false;        // the slider is being dragged (the readout stays quiet until it is let go)
let picked = -1;             // the station "Show a station" picked: ringed until the map moves
// One move at a time, driven by one animation-frame callback (carry.js createMotion).
const motion = createMotion({
  keys, cum, last, playMs: PLAY_MS, stepMs: STEP_MS, reduceMotion,
  now: () => performance.now(), schedule: (f) => requestAnimationFrame(f), cancel: (h) => cancelAnimationFrame(h),
  onMove: moveTo, onSettle: settled,
});
const nodes = new Float64Array(real.length);
let routeXY = routes.map((r) => r.at[0]), routeWF = routes.map((r) => r.wf[0]);

function bbox() {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const k of keys) for (const i of inner) {
    x0 = Math.min(x0, k.nodes[2 * i]); x1 = Math.max(x1, k.nodes[2 * i]);
    y0 = Math.min(y0, k.nodes[2 * i + 1]); y1 = Math.max(y1, k.nodes[2 * i + 1]);
  }
  return [x0, y0, x1, y1];
}

// How much of the map the controls cover at the bottom: on a phone they are a sheet over it.
function covered() {
  const c = $("controls"), stage = glCanvas.parentElement;
  if (c.hidden || getComputedStyle(c).position !== "fixed") return 0;
  return Math.max(0, stage.getBoundingClientRect().bottom - c.getBoundingClientRect().top);
}

// One fixed view for the whole move, so nothing but the map moves; framed in the part of the
// map that the controls leave in view.
function fit() {
  const [x0, y0, x1, y1] = bbox();
  const cover = Math.min(covered(), view.h * 0.6);
  const ppm = 0.94 * Math.min(view.w / (x1 - x0), (view.h - cover) / (y1 - y0));
  view.cx = (x0 + x1) / 2;
  view.cy = (y0 + y1) / 2 + cover / 2 / ppm;
  view.zoom = Math.log2((ppm * frame.m) / 512);
}

function camera() {
  const [lon, lat] = frame.fromLocal(view.cx, view.cy);
  const cam = makeCamera(frame, { centerLon: lon, centerLat: lat, zoom: view.zoom, width: view.w, height: view.h, k: 1 });
  cam.k = cam.ppm;           // targets are local metres, drawn at the ordinary map's scale
  return cam;
}

function toScreen(cam, x, y) {
  return [(x - cam.center[0]) * cam.ppm + cam.width / 2, (y - cam.center[1]) * cam.ppm + cam.height / 2];
}

function resize() {
  const r = glCanvas.parentElement.getBoundingClientRect();
  const first = view.w === 1;
  view.w = Math.max(1, r.width);
  view.h = Math.max(1, r.height);
  if (first) fit();
  draw();
}

// ── where the map is ────────────────────────────────────────────────
function moveTo(value) {
  const to = Math.max(0, Math.min(last, value));
  const dir = Math.sign(to - v);
  v = to;
  seg = segmentAt(keys, v);
  const b = Math.min(seg.k + 1, keys.length - 1);
  blend(keys[seg.k].nodes, keys[b].nodes, seg.s, nodes);
  const which = smoothBox.checked ? "smoothed" : "at";
  routeXY = routes.map((r) => blend(r[which][seg.k], r[which][b], seg.s));
  routeWF = routes.map((r) => blend(r.wf[seg.k], r.wf[b], seg.s));
  if (scene && renderer.supported && !renderer.lost && uploaded !== seg.k) {
    loadSegment(scene, placer.placed(seg.k), placer.placed(b));
    renderer.setScene({ origin: frame.origin, passes: scene.passes, labels: [] });
    uploaded = seg.k;
  }
  ahead(dir);
  slider.value = String(v);
  const text = where();
  if (slider.getAttribute("aria-valuetext") !== text) slider.setAttribute("aria-valuetext", text);
  const live = motion.moving || dragging;
  const politeness = live ? "off" : "polite";
  if (readout.getAttribute("aria-live") !== politeness) readout.setAttribute("aria-live", politeness);
  if (live) showLive(); else showReadout();
  updateToggle();
  draw();
}

// Place the next two keyframes the map is heading for, one per task between frames, so crossing
// into the next blend only copies and uploads.
let aheadTimer = 0, aheadFor = "";
function ahead(dir) {
  if (!placer || !dir) return;
  const want = dir > 0 ? [seg.k + 2, seg.k + 3] : [seg.k - 1, seg.k - 2];
  if (aheadFor === want.join()) return;           // already on its way: let it finish
  aheadFor = want.join();
  clearTimeout(aheadTimer);
  const next = () => {
    const k = want.shift();
    if (k === undefined) return;
    if (k < 0 || k >= keys.length || placer.has(k)) { next(); return; }
    placer.placed(k);
    aheadTimer = setTimeout(next, 0);
  };
  aheadTimer = setTimeout(next, 0);
}

function onMain() {
  const i = Math.round(v);
  return Math.abs(v - i) < 1e-6 ? i : null;
}

// ρ to three significant figures, so neighbouring keyframes read apart (44.9 and 45.2).
const fmt = (rho) => String(Number(rho.toPrecision(3)));

// Where the map is, in plain words; with ?details=1, in the build's own terms (the train
// springs' strength, and where the layout jumps).
function where() {
  const i = onMain();
  if (i === 0) return "The real map";
  if (!DETAILS) {
    if (i === last) return "The time map";
    return `On the way to the time map, ${Math.round((100 * v) / last)}% of the way`;
  }
  if (i !== null) return `Train springs ${fmt(doc.frames[i].rho)} times the grid's strength`;
  const a = keys[seg.k], b = keys[Math.min(seg.k + 1, keys.length - 1)];
  if (a.settling || b.settling || a.rho === b.rho) {
    return `At ${fmt(b.rho)} times the grid's strength the layout jumps to another arrangement; the map follows ` +
      "the solver's own steps";
  }
  return a.rho === 0 ? `Between the real map and ${fmt(b.rho)} times the grid's strength`
    : `Between ${fmt(a.rho)} and ${fmt(b.rho)} times the grid's strength`;
}

// Each station's leftover stress where the map is: solved positions have their own; between two,
// the colours blend from one to the other (an in-between layout has no stress of its own).
function stressAt(i) {
  const i0 = Math.floor(v + 1e-9), i1 = Math.min(last, i0 + 1), f = v - i0;
  const a = doc.frames[i0].stress[i], b = doc.frames[i1].stress[i];
  return f < 1e-9 ? a : a + (b - a) * f;
}

// The same in words, for the tip: how well the station's distances match its travel times.
function fitWords(x) {
  const t = x / doc.stress_scale[1];
  return t < 1 / 3 ? "its distances match its travel times closely"
    : t < 2 / 3 ? "its distances are somewhat off its travel times" : "its distances are far off its travel times";
}

// ── drawing ─────────────────────────────────────────────────────────
function stressColour(x) {
  const t = Math.max(0, Math.min(1, x / doc.stress_scale[1]));
  const a = [44, 123, 182], b = [255, 255, 191], c = [215, 25, 28];
  const [p, q, s] = t < 0.5 ? [a, b, t * 2] : [b, c, (t - 0.5) * 2];
  return `rgb(${p.map((y, k) => Math.round(y + (q[k] - y) * s)).join(",")})`;
}

// The map itself stays light (the renderer's background and land colours), so what is drawn on
// it keeps the light map's colours too.
const MAP = { met: "#9b0056", dotEdge: "#ffffff", halo: "rgba(255,255,255,0.85)", text: "#1b1b1f", ghost: "#4a4458", pick: "#2f4fd8" };

function draw() {
  tip.hidden = true;           // whatever it pointed at has moved
  const cam = camera();
  if (renderer.supported && !renderer.lost) renderer.draw(cam, seg.s, { adaptive: false });
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const W = Math.round(view.w * dpr), H = Math.round(view.h * dpr);
  if (overlay.width !== W || overlay.height !== H) { overlay.width = W; overlay.height = H; }
  const g = overlay.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, view.w, view.h);
  const at = (i) => toScreen(cam, nodes[2 * i], nodes[2 * i + 1]);
  if ($("mesh").checked) {
    g.strokeStyle = "rgba(90,90,110,0.35)";
    g.lineWidth = 0.6;
    g.beginPath();
    for (let t = 0; t < tri.length / 3; t++) {
      const [a, b, c] = [tri[3 * t], tri[3 * t + 1], tri[3 * t + 2]];
      if (kinds[a] === 2 || kinds[b] === 2 || kinds[c] === 2) continue;
      const pa = at(a), pb = at(b), pc = at(c);
      g.moveTo(...pa); g.lineTo(...pb); g.lineTo(...pc); g.closePath();
    }
    g.stroke();
  }
  if ($("line").checked) {
    // Segment by segment, each as wide as the map leaves it (thinner where it is squeezed).
    g.strokeStyle = MAP.met;
    g.lineCap = "round";
    routeXY.forEach((xy, r) => {
      const wf = routeWF[r];
      for (let k = 0; k + 1 < xy.length / 2; k++) {
        const [x0, y0] = toScreen(cam, xy[2 * k], xy[2 * k + 1]);
        const [x1, y1] = toScreen(cam, xy[2 * k + 2], xy[2 * k + 3]);
        g.lineWidth = 3 * wf[k];
        g.beginPath(); g.moveTo(x0, y0); g.lineTo(x1, y1); g.stroke();
      }
    });
    g.lineCap = "butt";
  }
  // Where the named stations really are: a faint ring there, and a dashed line to where they are now.
  if ($("ghosts").checked && v > 0) {
    g.strokeStyle = MAP.ghost;
    g.lineWidth = 1;
    g.globalAlpha = 0.6;
    for (const i of doc.labels) {
      const [rx, ry] = toScreen(cam, real[2 * i], real[2 * i + 1]);
      const [x, y] = at(i);
      if (Math.hypot(x - rx, y - ry) < 3) continue;
      g.setLineDash([3, 3]);
      g.beginPath(); g.moveTo(rx, ry); g.lineTo(x, y); g.stroke();
      g.setLineDash([]);
      g.beginPath(); g.arc(rx, ry, 4, 0, 2 * Math.PI); g.stroke();
    }
    g.globalAlpha = 1;
  }
  const r = Math.max(2, Math.min(4, view.zoom - 8));
  for (let i = 0; i < nStation; i++) {
    const [x, y] = at(i);
    if (x < -10 || y < -10 || x > view.w + 10 || y > view.h + 10) continue;
    g.beginPath();
    g.arc(x, y, r, 0, 2 * Math.PI);
    g.fillStyle = stressColour(stressAt(i));
    g.fill();
    g.lineWidth = 0.8;
    g.strokeStyle = MAP.dotEdge;
    g.stroke();
  }
  if (picked >= 0) {
    const [x, y] = at(picked);
    g.lineWidth = 3;
    g.strokeStyle = MAP.pick;
    g.beginPath(); g.arc(x, y, r + 6, 0, 2 * Math.PI); g.stroke();
  }
  if ($("labels").checked) {
    g.font = "12px system-ui, sans-serif";
    g.textBaseline = "middle";
    const boxes = [];
    for (const i of picked >= 0 ? [picked, ...doc.labels] : doc.labels) {
      const [x, y] = at(i);
      const text = doc.nodes.label[i];
      const w = g.measureText(text).width;
      const box = [x + 6, y - 8, x + 10 + w, y + 8];
      if (boxes.some((b) => !(box[2] < b[0] || box[0] > b[2] || box[3] < b[1] || box[1] > b[3]))) continue;
      boxes.push(box);
      g.lineWidth = 3;
      g.strokeStyle = MAP.halo;
      g.strokeText(text, x + 7, y);
      g.fillStyle = MAP.text;
      g.fillText(text, x + 7, y);
    }
  }
}

function km(i, j, xy) {
  return Math.hypot(xy[2 * i] - xy[2 * j], xy[2 * i + 1] - xy[2 * j + 1]) / 1000;
}

const pairText = (r, now, was) => `${r.name}: ${now.toFixed(1)} km apart here, ${was.toFixed(1)} km really`;

// The full readout, for a map at rest: where the map is, and the distance between each checked
// pair of stations here and really. ?details=1 adds the build's checks.
function showReadout() {
  const i = onMain();
  const m = i !== null ? doc.frames[i].metrics : null;
  const lines = [];
  if (!DETAILS) lines.push(`${where()}.`);
  else if (i === 0) lines.push("The real map. Every triangle at its real size; 0 flipped.");
  else if (m) {
    lines.push(`${where()}. ${m.flipped} flipped triangles; the most squeezed keeps ` +
      `${(m.area_ratio_min * 100).toFixed(1)}% of its area${m.squeezed_near ? ` (near ${m.squeezed_near})` : ""}.`);
  } else lines.push(`${where()}. No triangle turns over anywhere on the way: every step was checked.`);
  for (const r of doc.ratios) {
    if (r.station === null || r.reference === null) { if (DETAILS) lines.push(`${r.name}: absent`); continue; }
    const now = km(r.station, r.reference, nodes), was = km(r.station, r.reference, real);
    const val = DETAILS && m && m.ratios[r.key];
    lines.push(val && val.ratio != null
      ? `${pairText(r, now, was)}, ${val.ratio.toFixed(2)}× the median (${val.status})` : pairText(r, now, was));
  }
  readout.replaceChildren(...lines.map((t) => Object.assign(document.createElement("div"), { textContent: t })));
  holdReadout();
}

// While the map moves, only the distances change: they update in place, so nothing jumps.
function showLive() {
  const shown = doc.ratios.filter((r) => DETAILS || (r.station !== null && r.reference !== null));
  const divs = readout.children;
  if (divs.length !== shown.length + 1) { showReadout(); return; }
  divs[0].textContent = `${where()}…`;
  shown.forEach((r, k) => {
    if (r.station === null || r.reference === null) return;
    divs[k + 1].textContent = pairText(r, km(r.station, r.reference, nodes), km(r.station, r.reference, real));
  });
  holdReadout();
}

// The readout grows but never shrinks at one width, so the map above it keeps its size while the
// text changes (on a phone the rows wrap differently at rest and on the move).
let readoutFloor = 0, readoutWidth = 0;
function holdReadout() {
  if (readout.clientWidth !== readoutWidth) {
    readoutWidth = readout.clientWidth;
    readoutFloor = 0;
    readout.style.minHeight = "";
  }
  const h = readout.offsetHeight;
  if (h > readoutFloor) {
    readoutFloor = h;
    readout.style.minHeight = `${h}px`;
  }
}

// ── motion ──────────────────────────────────────────────────────────
function settled() {
  if (readout.getAttribute("aria-live") !== "polite") readout.setAttribute("aria-live", "polite");
  showReadout();
  updateToggle();
}

// One button, always called "Time map": pressed while the map is at the time map or heading
// there, so its state is said once (aria-pressed), never in its name. Pressed mid-move it turns
// round.
function updateToggle() {
  const pressed = String(motion.back);
  if (toggle.getAttribute("aria-pressed") !== pressed) toggle.setAttribute("aria-pressed", pressed);
}

toggle.addEventListener("click", () => motion.toggle());

// Dragging the slider moves the map continuously, the readout's live distances quiet until it is
// let go (change), when the full readout comes back once.
slider.addEventListener("input", () => {
  dragging = true;
  motion.drag(Number(slider.value));
});
slider.addEventListener("change", () => {
  dragging = false;
  settled();
});

// Arrow and page keys glide to the previous or next solved position; Home and End play to the ends.
slider.addEventListener("keydown", (e) => {
  if (motion.key(e.key, e.repeat)) e.preventDefault();
});

function zoomBy(dz) {
  view.zoom += dz;
  draw();
}
$("fit").addEventListener("click", () => { picked = -1; fit(); draw(); });
$("zoom-in").addEventListener("click", () => zoomBy(0.5));
$("zoom-out").addEventListener("click", () => zoomBy(-0.5));
for (const id of ["labels", "line", "mesh", "ghosts"]) $(id).addEventListener("change", draw);
smoothBox.addEventListener("change", () => {
  if (placer) placer.setSmooth(smoothBox.checked);
  uploaded = -1;
  moveTo(motion.v);
});

// "Show a station": what a tap on a dot shows, for the keyboard too. It centres the station,
// rings it and says how well it fits.
const stationSel = $("station-select");
const byName = [...Array(nStation).keys()].sort((a, b) => doc.nodes.label[a].localeCompare(doc.nodes.label[b]));
stationSel.append(...byName.map((i) => new Option(doc.nodes.label[i], String(i))));
stationSel.addEventListener("change", () => {
  if (stationSel.value === "") { picked = -1; draw(); return; }
  picked = Number(stationSel.value);
  view.cx = nodes[2 * picked];
  view.cy = nodes[2 * picked + 1] + covered() / 2 / camera().ppm;
  draw();
  showTip(picked);
});

// Marks under the slider (?details=1): the first solved position where Wembley Park passes its
// check against Baker Street, and any place where the layout jumps.
function markSlider() {
  const add = (at, text, title, cls) => {
    const m = document.createElement("span");
    m.className = cls;
    m.style.left = `${(100 * at) / last}%`;
    m.textContent = text;
    m.title = title;
    $("marks").append(m);
  };
  const passes = (f) => Boolean(f.metrics.ratios.wembley_park && f.metrics.ratios.wembley_park.status === "pass");
  const first = doc.frames.findIndex(passes);
  if (first > 0) {
    const holds = doc.frames.slice(first).every(passes);
    add(first, "Wembley Park passes", "The first solved position where Wembley Park passes L-01's check against Baker " +
      `Street (${fmt(doc.frames[first].rho)} times the grid's strength); ` +
      (holds ? "it passes at every solved position after it." : "it fails again at some later solved positions."), "mark");
  }
  for (const j of (doc.motion && doc.motion.jumps) || []) {
    add(j.at_from, "Jumps", `From ${fmt(j.from)} to ${fmt(j.rho)} times the grid's strength the layout jumps to another ` +
      `arrangement (${j.station} moves ${(j.move_m / 1000).toFixed(1)} km); the map follows the solver's own steps there.`,
      "mark jump");
  }
}

// ── pointer ─────────────────────────────────────────────────────────
const pointers = new Map();
let pinch = null, moved = false, downAt = null;
overlay.addEventListener("pointerdown", (e) => {
  overlay.setPointerCapture(e.pointerId);
  pointers.set(e.pointerId, [e.offsetX, e.offsetY]);
  moved = false;
  downAt = [e.offsetX, e.offsetY];
  if (pointers.size === 2) {
    const [a, b] = [...pointers.values()];
    pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), zoom: view.zoom };
  }
});
overlay.addEventListener("pointermove", (e) => {
  const prev = pointers.get(e.pointerId);
  if (!prev) { hover(e.offsetX, e.offsetY); return; }
  const dx = e.offsetX - prev[0], dy = e.offsetY - prev[1];
  pointers.set(e.pointerId, [e.offsetX, e.offsetY]);
  if (downAt && Math.hypot(e.offsetX - downAt[0], e.offsetY - downAt[1]) > TAP_PX) moved = true;
  if (pointers.size === 2 && pinch) {
    const [a, b] = [...pointers.values()];
    view.zoom = pinch.zoom + Math.log2(Math.hypot(a[0] - b[0], a[1] - b[1]) / pinch.d);
  } else if (pointers.size === 1) {
    const ppm = camera().ppm;
    view.cx -= dx / ppm;
    view.cy -= dy / ppm;
  }
  draw();
});
const up = (e) => {
  if (pointers.has(e.pointerId) && !moved) hover(e.offsetX, e.offsetY);
  pointers.delete(e.pointerId);
  if (pointers.size < 2) pinch = null;
};
overlay.addEventListener("pointerup", up);
overlay.addEventListener("pointercancel", up);
overlay.addEventListener("wheel", (e) => {
  e.preventDefault();
  const cam = camera();
  const mx = cam.center[0] + (e.offsetX - view.w / 2) / cam.ppm, my = cam.center[1] + (e.offsetY - view.h / 2) / cam.ppm;
  const z = view.zoom - Math.sign(e.deltaY) * 0.25;
  const s = Math.pow(2, z - view.zoom);
  view.cx = mx - (mx - view.cx) / s;
  view.cy = my - (my - view.cy) / s;
  view.zoom = z;
  draw();
}, { passive: false });

function showTip(i) {
  const [sx, sy] = toScreen(camera(), nodes[2 * i], nodes[2 * i + 1]);
  const k = onMain();
  if (!DETAILS) {
    tip.textContent = `${doc.nodes.label[i]}: ${fitWords(stressAt(i))}.`;
  } else {
    tip.textContent = k !== null
      ? `${doc.nodes.label[i]}: leftover stress ${doc.frames[k].stress[i].toFixed(2)}`
      : `${doc.nodes.label[i]}: leftover stress ${doc.frames[Math.floor(v)].stress[i].toFixed(2)} at ` +
        `${fmt(doc.frames[Math.floor(v)].rho)}, ${doc.frames[Math.ceil(v)].stress[i].toFixed(2)} at ` +
        `${fmt(doc.frames[Math.ceil(v)].rho)} times the grid's strength (the solved positions either side)`;
  }
  tip.hidden = false;
  tip.style.left = `${Math.max(0, Math.min(view.w - 220, sx + 10))}px`;
  tip.style.top = `${Math.max(0, sy - 30)}px`;
}

function hover(x, y) {
  if (motion.moving) return;
  const cam = camera();
  let best = -1, bd = 14;
  for (let i = 0; i < nStation; i++) {
    const [sx, sy] = toScreen(cam, nodes[2 * i], nodes[2 * i + 1]);
    const d = Math.hypot(sx - x, sy - y);
    if (d < bd) { bd = d; best = i; }
  }
  if (best < 0) { tip.hidden = true; return; }
  showTip(best);
}

if (DETAILS) markSlider();
new ResizeObserver(resize).observe(glCanvas.parentElement);
motion.jump(0);

// For the headless checks (?test=1): the motion and the view.
if (new URLSearchParams(location.search).get("test") === "1") {
  window.__city = { motion, view, get v() { return v; }, win, doc };
}
