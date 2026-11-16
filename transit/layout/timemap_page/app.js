// The full London time map (#6, L-02): the page. It draws the land with W-01's mesh renderer
// (cpu back-end, a target per vertex) and the stations, names and the Metropolitan line (carried
// through the mesh like the land) on a 2-D canvas above it.
//
// The map moves smoothly. The slider runs continuously from the real map to the last solved
// position; between two neighbouring keyframes the page draws their straight blend, which the
// build checked never folds (timemap.py, motion_keyframes), and the renderer's morph does the
// mixing on the GPU. "Bend by travel time" plays the whole move at an even pace and "Back to the
// real map" plays it back; with reduced motion they jump. Faint rings mark where the named
// stations really are, so the move can be followed.
//
// Lines (rail, borough outlines, the Metropolitan line) get thinner where the map squeezes the
// ground across them, and "Smooth lines" rounds off the small bends they take where they cross
// a mesh edge (carry.js says how; water, parks and the mesh keep the exact map). It loads only
// its own files and runs no routing.
import { createMeshRenderer } from "./mesh/renderer.js";
import { localFrame, makeCamera } from "./mesh/frame.js";
import { STYLE_ID } from "./mesh/schema-streets-v8.js";
import {
  blend, buildLand, createMotion, createPlacer, decodeLand, keyframes, loadSegment, pathLengths, placeLine,
  segmentAt, segmentTriangles, smoothLine, triangleMaps, widthFactors,
} from "./carry.js";

const $ = (id) => document.getElementById(id);
const glCanvas = $("map"), overlay = $("overlay"), tip = $("tip"), status = $("status");
const slider = $("frame"), readout = $("readout"), toggle = $("bend");
const TAP_PX = 5;            // a press that moves less than this is a tap, not a drag
const PLAY_MS = 4500;        // the whole move, real map to the end; a part of it takes its share
const STEP_MS = 450;         // the shortest glide with the arrow keys

async function load(name, kind) {
  const r = await fetch(name);
  if (!r.ok) throw new Error(`${name}: ${r.status} ${r.statusText}`);
  return kind === "json" ? r.json() : r.arrayBuffer();
}

let doc, landBuf;
try {
  doc = await load("timemap.json", "json");
  landBuf = doc.land ? await load(doc.land.file, "bin") : null;
} catch (e) {
  status.textContent = `This page's data didn't load (${e.message}). Serve this folder over http (python -m http.server) and reload.`;
  throw e;
}

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

$("notice").textContent = doc.notice;
$("credits").textContent = "Credits: " + doc.attribution.join("; ") + ".";
slider.max = String(last);

// ── renderer ────────────────────────────────────────────────────────
const renderer = createMeshRenderer(glCanvas, {
  backend: "cpu",
  onLost: () => { status.textContent = "The graphics context was lost; the land comes back when it is restored."; draw(); },
  onRestored: () => { status.textContent = ""; renderer.setWarp({ kind: "mesh", origin: frame.origin }); uploaded = -1; moveTo(motion.v); },
});
let scene = null;
if (!renderer.supported) {
  status.textContent = "This browser has no WebGL2, so the land isn't drawn; the stations still are.";
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

// One fixed view for the whole move, so nothing but the map moves.
function fit() {
  const [x0, y0, x1, y1] = bbox();
  view.cx = (x0 + x1) / 2;
  view.cy = (y0 + y1) / 2;
  const ppm = 0.94 * Math.min(view.w / (x1 - x0), view.h / (y1 - y0));
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

// Where the map is, from the two keyframes being blended (solved positions, positions solved in
// between, or the solver's own steps after a jump).
function where() {
  const i = onMain();
  if (i === 0) return "The real map";
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

// ── drawing ─────────────────────────────────────────────────────────
function stressColour(x) {
  const t = Math.max(0, Math.min(1, x / doc.stress_scale[1]));
  const a = [44, 123, 182], b = [255, 255, 191], c = [215, 25, 28];
  const [p, q, s] = t < 0.5 ? [a, b, t * 2] : [b, c, (t - 0.5) * 2];
  return `rgb(${p.map((y, k) => Math.round(y + (q[k] - y) * s)).join(",")})`;
}

// The map itself stays light in either page theme (the renderer's background and land colours),
// so what is drawn on it keeps the light map's colours too.
const MAP = { met: "#9b0056", dotEdge: "#ffffff", halo: "rgba(255,255,255,0.85)", text: "#1b1b1f", ghost: "#4a4458" };

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
  if ($("labels").checked) {
    g.font = "12px system-ui, sans-serif";
    g.textBaseline = "middle";
    const boxes = [];
    for (const i of doc.labels) {
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

// The full readout, for a map at rest: what this position is, and each checked pair.
function showReadout() {
  const i = onMain();
  const m = i !== null ? doc.frames[i].metrics : null;
  const lines = [];
  if (i === 0) lines.push("The real map. Every triangle at its real size; 0 flipped.");
  else if (m) {
    lines.push(`${where()}. ${m.flipped} flipped triangles; the most squeezed keeps ` +
      `${(m.area_ratio_min * 100).toFixed(1)}% of its area${m.squeezed_near ? ` (near ${m.squeezed_near})` : ""}.`);
  } else lines.push(`${where()}. No triangle turns over anywhere on the way: every step was checked.`);
  for (const r of doc.ratios) {
    if (r.station === null || r.reference === null) { lines.push(`${r.name}: absent`); continue; }
    const now = km(r.station, r.reference, nodes), was = km(r.station, r.reference, real);
    const val = m && m.ratios[r.key];
    lines.push(val && val.ratio != null
      ? `${r.name}: ${now.toFixed(2)} km (${was.toFixed(2)} km really), ${val.ratio.toFixed(2)}× the median (${val.status})`
      : `${r.name}: ${now.toFixed(2)} km (${was.toFixed(2)} km really)`);
  }
  readout.replaceChildren(...lines.map((t) => Object.assign(document.createElement("div"), { textContent: t })));
  holdReadout();
}

// While the map moves, only the distances change: they update in place, so nothing jumps.
function showLive() {
  const divs = readout.children;
  if (divs.length !== doc.ratios.length + 1) { showReadout(); return; }
  divs[0].textContent = `${where()}…`;
  doc.ratios.forEach((r, k) => {
    if (r.station === null || r.reference === null) return;
    divs[k + 1].textContent = `${r.name}: ${km(r.station, r.reference, nodes).toFixed(2)} km ` +
      `(${km(r.station, r.reference, real).toFixed(2)} km really)`;
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

// The one button names what it does: bend all the way, or go back, from wherever the map is;
// pressed mid-move it turns round.
function updateToggle() {
  const text = motion.back ? "Back to the real map" : "Bend by travel time";
  if (toggle.textContent !== text) toggle.textContent = text;
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

$("fit").addEventListener("click", () => { fit(); draw(); });
for (const id of ["labels", "line", "mesh", "ghosts"]) $(id).addEventListener("change", draw);
smoothBox.addEventListener("change", () => {
  if (placer) placer.setSmooth(smoothBox.checked);
  uploaded = -1;
  moveTo(motion.v);
});

// Marks under the slider: the first solved position where Wembley Park passes its check against
// Baker Street, and any place where the layout jumps.
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
  const [sx, sy] = toScreen(cam, nodes[2 * best], nodes[2 * best + 1]);
  const i = onMain();
  tip.textContent = i !== null
    ? `${doc.nodes.label[best]}: leftover stress ${doc.frames[i].stress[best].toFixed(2)}`
    : `${doc.nodes.label[best]}: leftover stress ${doc.frames[Math.floor(v)].stress[best].toFixed(2)} at ` +
      `${fmt(doc.frames[Math.floor(v)].rho)}, ${doc.frames[Math.ceil(v)].stress[best].toFixed(2)} at ` +
      `${fmt(doc.frames[Math.ceil(v)].rho)} times the grid's strength (the solved positions either side)`;
  tip.hidden = false;
  tip.style.left = `${Math.min(view.w - 200, sx + 10)}px`;
  tip.style.top = `${Math.max(0, sy - 30)}px`;
}

markSlider();
new ResizeObserver(resize).observe(glCanvas.parentElement);
motion.jump(0);
