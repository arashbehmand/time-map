// "How far can I get from here?"
//
// A pin marks the starting point. The server answers, once per (pin, mode), where the
// 5/10/15/20-minute lines are around it (radii per angle at a reference zoom). Pan, zoom
// and resize only rescale that answer here, so they never send a request. The ordinary
// map shows the lines; the time map bends them into circles, so distance from the pin
// means travel time.
import { createLatest } from "/shared/latest.js";
import {
  friendly, geocode, postJSON, prefersReducedMotion, loadPref, savePref,
} from "/shared/api.js";
import * as W from "./warpmath.js";

// ── State ────────────────────────────────────────────────────────────
const FALLBACK_ORIGIN = { lon: -0.1233, lat: 51.5308, label: "King's Cross", example: true };
const CACHE_MAX = 8;
const LOCATION_PREF = "timemap-location";

const state = {
  origin: { ...FALLBACK_ORIGIN },
  mode: "walking",
  bent: false,          // what the person asked for
  u: 0,                 // how bent the picture is right now, 0 … 1
  dragging: false,
  dragPos: null,
  canBend: true,
};
const answers = new Map();             // answerKey → answer, newest last
const latest = createLatest();         // warp answers
const searches = createLatest();       // place searches
let locateSeq = 0;
let requestTimer = null;
let anim = { from: 0, to: 0, start: 0, dur: 0 };
let nextDuration = null;
let uploadedKey = null;
let mapDirty = true;
let cachedLabels = [];
let labelTimer = null;
let map;
let view = { w: 1, h: 1 };              // the map container's size, CSS px
let box = { w: 1, top: 0, bottom: 1, h: 1 };
let lastFrameKey = "";

// ── DOM ──────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const warpCanvas = $("warp-canvas");
const overlayCanvas = $("overlay-canvas");
const pinEl = $("origin-pin");
const statusEl = $("status");
const fromEl = $("from");
const sentenceEl = $("sentence");
const zoomHintEl = $("zoom-hint");
const toggleBtn = $("view-toggle");
const backBtn = $("back-to-pin");
const overlay2d = overlayCanvas.getContext("2d");
const topEl = $("top");
const legendEl = $("legend");

const mapContainer = $("map-container");

// The part of the map the controls don't cover: rings are sized and framed to fit it.
// Measured on resize and when the controls change size, never per frame.
function measureBox() {
  const { w, h } = view;
  const top = Math.min(topEl.getBoundingClientRect().bottom, h / 2);
  const narrow = w <= 640;
  const bottom = narrow ? Math.max(legendEl.getBoundingClientRect().top, top + 120) : h;
  box = { w, top, bottom, h: bottom - top };
}

function setStatus(text, kind = "") {
  statusEl.textContent = text;
  statusEl.className = "status" + (kind ? " " + kind : "");
}

function setText(el, text) {
  if (el.textContent !== text) el.textContent = text;
}

function currentKey() {
  return W.answerKey(state.origin.lon, state.origin.lat, state.mode);
}
function currentAnswer() {
  return answers.get(currentKey()) || null;
}

function store(answer) {
  answers.delete(answer.key);
  answers.set(answer.key, answer);
  while (answers.size > CACHE_MAX) answers.delete(answers.keys().next().value);
}

function answerFrom(params, lon, lat, mode, refZoom) {
  const source = params.source_radii.map((row) => Float32Array.from(row));
  const outer = source[source.length - 1];
  return {
    key: W.answerKey(lon, lat, mode),
    refZoom,
    minutes: params.contour_minutes.map(Number),
    source,
    medianOuter: W.median(outer),
    maxOuter: Math.max(...outer),
  };
}

// ── GLSL ─────────────────────────────────────────────────────────────
const VERT_SRC = /* glsl */`#version 300 es
  in vec2 a_pos;
  out vec2 v_uv;
  void main() {
    gl_Position = vec4(a_pos, 0.0, 1.0);
    v_uv = (a_pos + 1.0) * 0.5;
  }`;

// Mirrors warpmath.js: lutSample, supportRadius and inverseRadius.
const FRAG_SRC = /* glsl */`#version 300 es
  precision highp float;
  precision highp int;
  in vec2 v_uv;
  out vec4 fragColor;

  uniform sampler2D u_map;          // the MapLibre frame
  uniform highp sampler2D u_lut;    // RGBA32F ${W.LUT_SIZE}x1, NEAREST: source radii per angle
  uniform vec2  u_center;           // the pin, canvas pixels (y down)
  uniform vec2  u_resolution;       // canvas size in pixels
  uniform vec4  u_target;           // circle radii, padded to 4
  uniform float u_scale;            // 2^(zoom - refZoom)
  uniform float u_warp;             // 0 ordinary map ... 1 time map
  uniform bool  u_active;           // false: no answer, show the map as it is

  const float PI  = 3.14159265359;
  const float EPS = 1e-6;
  const float PAD = ${W.SUPPORT_PAD.toFixed(1)};
  const int   N   = ${W.LUT_SIZE};
  const vec4  UNAVAILABLE = vec4(0.84, 0.83, 0.81, 1.0);

  // Linear interpolation between neighbouring bins, by hand: 32-bit float textures
  // are not filterable everywhere (OES_texture_float_linear is missing on some phones).
  vec4 lutAt(float theta) {
    float f  = (theta + PI) / (2.0 * PI) * float(N) - 0.5;
    float fl = floor(f);
    float a  = f - fl;
    int i0 = min(int(mod(fl, float(N))), N - 1);
    int i1 = i0 + 1 == N ? 0 : i0 + 1;
    return mix(texelFetch(u_lut, ivec2(i0, 0), 0), texelFetch(u_lut, ivec2(i1, 0), 0), a);
  }

  float supportAt(vec2 dir, float floorR) {
    float tx = dir.x >  1e-6 ? (u_resolution.x + PAD - u_center.x) / dir.x
             : dir.x < -1e-6 ? (-PAD - u_center.x) / dir.x : 1e20;
    float ty = dir.y >  1e-6 ? (u_resolution.y + PAD - u_center.y) / dir.y
             : dir.y < -1e-6 ? (-PAD - u_center.y) / dir.y : 1e20;
    return max(max(0.0, min(tx, ty)), floorR);
  }

  void main() {
    vec2 canvas = vec2(v_uv.x * u_resolution.x, (1.0 - v_uv.y) * u_resolution.y);
    vec2 delta = canvas - u_center;
    float rt = length(delta);
    if (!u_active || rt < EPS) { fragColor = texture(u_map, v_uv); return; }

    float theta = atan(delta.y, delta.x);
    vec4 S = lutAt(theta) * u_scale;
    vec4 E = mix(S, u_target, u_warp);
    vec2 dir = delta / rt;
    float sup = supportAt(dir, max(S.w, u_target.w) + PAD);

    float r;
    if      (rt <= E.x) r = S.x * rt / max(E.x, EPS);
    else if (rt <= E.y) r = mix(S.x, S.y, (rt - E.x) / max(E.y - E.x, EPS));
    else if (rt <= E.z) r = mix(S.y, S.z, (rt - E.y) / max(E.z - E.y, EPS));
    else if (rt <= E.w) r = mix(S.z, S.w, (rt - E.z) / max(E.w - E.z, EPS));
    else if (rt < sup)  r = mix(S.w, sup, (rt - E.w) / max(sup - E.w, EPS));
    else                r = rt;

    vec2 orig = u_center + dir * r;
    // UNPACK_FLIP_Y_WEBGL=true: uv.y = 0 is the canvas bottom.
    vec2 uv = vec2(orig.x / u_resolution.x, 1.0 - orig.y / u_resolution.y);
    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) {
      // The source is off screen, so this part of the time map can't be drawn. Show it
      // grey (fading in with the bend) rather than an ordinary pixel that would pass
      // for travel time. The legend says to zoom out.
      fragColor = mix(texture(u_map, v_uv), UNAVAILABLE, u_warp);
      return;
    }
    fragColor = texture(u_map, uv);
  }`;

// ── WebGL2 ───────────────────────────────────────────────────────────
let gl = null, program, locs = {}, lutTex, mapTex, quadVAO;

function compileShader(type, src) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
  return s;
}

function initWebGL() {
  gl = warpCanvas.getContext("webgl2");
  if (!gl) throw new Error("no WebGL2");
  program = gl.createProgram();
  gl.attachShader(program, compileShader(gl.VERTEX_SHADER, VERT_SRC));
  gl.attachShader(program, compileShader(gl.FRAGMENT_SHADER, FRAG_SRC));
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
  for (const name of ["u_map", "u_lut", "u_center", "u_resolution", "u_target", "u_scale", "u_warp", "u_active"]) {
    locs[name] = gl.getUniformLocation(program, name);
  }
  const aPos = gl.getAttribLocation(program, "a_pos");

  quadVAO = gl.createVertexArray();
  gl.bindVertexArray(quadVAO);
  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
  gl.enableVertexAttribArray(aPos);
  gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);
  gl.bindVertexArray(null);

  lutTex = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, lutTex);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, W.LUT_SIZE, 1, 0, gl.RGBA, gl.FLOAT,
                new Float32Array(W.LUT_SIZE * 4));

  mapTex = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, mapTex);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  if (gl.getError() !== gl.NO_ERROR) throw new Error("texture setup failed");

  warpCanvas.addEventListener("webglcontextlost", (e) => { e.preventDefault(); disableBending(); });
}

function uploadLUT(a) {
  const k = a.source.length;
  const data = new Float32Array(W.LUT_SIZE * 4);
  for (let i = 0; i < W.LUT_SIZE; i++) {
    for (let c = 0; c < 4; c++) data[i * 4 + c] = a.source[Math.min(c, k - 1)][i];
  }
  gl.bindTexture(gl.TEXTURE_2D, lutTex);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, W.LUT_SIZE, 1, 0, gl.RGBA, gl.FLOAT, data);
  uploadedKey = a.key;
}

function disableBending() {
  state.canBend = false;
  state.bent = false;
  gl = null;
  document.body.classList.add("no-bend");
  toggleBtn.disabled = true;
  setStatus("This browser can't bend the map. The lines still show on the normal map.", "error");
}

// ── Map ──────────────────────────────────────────────────────────────
function buildStyle() {
  const origin = window.location.origin;
  return {
    version: 8,
    glyphs: `${origin}/glyphs/{fontstack}/{range}.pbf`,
    sources: {
      streets: {
        type: "vector",
        tiles: [`${origin}/tiles/{z}/{x}/{y}`],
        minzoom: 0, maxzoom: 16,
        attribution: "© Mapbox © OpenStreetMap",
      },
    },
    layers: [
      { id: "bg", type: "background",
        paint: { "background-color": "#f0ede9" } },

      { id: "water", type: "fill", source: "streets", "source-layer": "water",
        paint: { "fill-color": "#aad3df" } },

      { id: "landuse-green", type: "fill", source: "streets", "source-layer": "landuse",
        filter: ["match", ["get", "class"],
          ["park","garden","cemetery","grass","scrub","forest","wood"], true, false],
        paint: { "fill-color": "#c8facc" } },

      { id: "landuse-residential", type: "fill", source: "streets", "source-layer": "landuse",
        filter: ["==", ["get", "class"], "residential"],
        paint: { "fill-color": "#eae6e1" } },

      { id: "building", type: "fill", source: "streets", "source-layer": "building",
        paint: { "fill-color": "#dfdbd7", "fill-outline-color": "#c0bbb5" } },

      // Road casings
      { id: "road-motorway-case", type: "line", source: "streets", "source-layer": "road",
        filter: ["==", ["get", "class"], "motorway"],
        layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": "#e05f4f",
          "line-width": ["interpolate",["linear"],["zoom"],10,4,16,10] } },

      { id: "road-major-case", type: "line", source: "streets", "source-layer": "road",
        filter: ["match", ["get","class"], ["trunk","primary"], true, false],
        layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": "#c89b7a",
          "line-width": ["interpolate",["linear"],["zoom"],10,2,16,7] } },

      { id: "road-secondary-case", type: "line", source: "streets", "source-layer": "road",
        filter: ["==", ["get", "class"], "secondary"],
        layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": "#e0d8c0",
          "line-width": ["interpolate",["linear"],["zoom"],10,1,16,5] } },

      // Road fills
      { id: "road-motorway", type: "line", source: "streets", "source-layer": "road",
        filter: ["==", ["get", "class"], "motorway"],
        layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": "#e892a2",
          "line-width": ["interpolate",["linear"],["zoom"],10,2.5,16,7] } },

      { id: "road-trunk", type: "line", source: "streets", "source-layer": "road",
        filter: ["==", ["get", "class"], "trunk"],
        layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": "#f9b29c",
          "line-width": ["interpolate",["linear"],["zoom"],10,2,16,6] } },

      { id: "road-primary", type: "line", source: "streets", "source-layer": "road",
        filter: ["==", ["get", "class"], "primary"],
        layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": "#fcd6a4",
          "line-width": ["interpolate",["linear"],["zoom"],10,1.5,16,5] } },

      { id: "road-secondary", type: "line", source: "streets", "source-layer": "road",
        filter: ["==", ["get", "class"], "secondary"],
        layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": "#f7fabf",
          "line-width": ["interpolate",["linear"],["zoom"],10,1,16,4] } },

      { id: "road-tertiary", type: "line", source: "streets", "source-layer": "road",
        filter: ["match",["get","class"],
          ["tertiary","street","street_limited"], true, false],
        layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": "#fff",
          "line-width": ["interpolate",["linear"],["zoom"],10,0.5,16,3] } },

      { id: "road-service", type: "line", source: "streets", "source-layer": "road",
        filter: ["match",["get","class"],
          ["service","track","path","pedestrian","footway"], true, false],
        layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": "#e0e0e0", "line-width": 0.8 } },

      // Labels
      { id: "road-label", type: "symbol", source: "streets", "source-layer": "road_label",
        layout: {
          "text-field": ["get","name"],
          "text-font": ["Open Sans Regular","Arial Unicode MS Regular"],
          "text-size": 10, "symbol-placement": "line", "text-max-angle": 30,
        },
        paint: { "text-color": "#666",
          "text-halo-color": "rgba(255,255,255,0.85)", "text-halo-width": 1.5,
          "text-opacity": 0 } },

      { id: "place-label", type: "symbol", source: "streets", "source-layer": "place_label",
        layout: {
          "text-field": ["get","name"],
          "text-font": ["Open Sans Bold","Arial Unicode MS Bold"],
          "text-size": ["interpolate",["linear"],["zoom"],10,10,16,14],
          "text-max-width": 10,
        },
        paint: { "text-color": "#222",
          "text-halo-color": "rgba(255,255,255,0.9)", "text-halo-width": 2,
          "text-opacity": 0 } },

      { id: "poi-label", type: "symbol", source: "streets", "source-layer": "poi_label",
        minzoom: 14,
        layout: {
          "text-field": ["get","name"],
          "text-font": ["Open Sans Regular","Arial Unicode MS Regular"],
          "text-size": 10, "text-max-width": 8,
        },
        paint: { "text-color": "#555",
          "text-halo-color": "rgba(255,255,255,0.9)", "text-halo-width": 1.5,
          "text-opacity": 0 } },
    ],
  };
}

function initMap(center, zoom) {
  map = new maplibregl.Map({
    container: "map-container",
    style: buildStyle(),
    center,
    zoom,
    // This map is hidden under the warp canvas, so its own credit would be invisible;
    // the page shows #attribution and the Mapbox logo instead (index.html).
    attributionControl: false,
    preserveDrawingBuffer: true,
    dragRotate: false,
    touchPitch: false,
    pitchWithRotate: false,
  });
  map.touchZoomRotate.disableRotation();
  map.on("render", () => {
    mapDirty = true;
    clearTimeout(labelTimer);
    labelTimer = setTimeout(updateLabelCache, 150);
  });
}

function updateLabelCache() {
  if (!map || !map.loaded()) return;
  const labels = [];
  const seen = new Set();
  try {
    for (const f of map.queryRenderedFeatures({ layers: ["place-label", "poi-label"] })) {
      const name = f.properties && f.properties.name;
      if (!name || f.geometry.type !== "Point" || seen.has(name)) continue;
      seen.add(name);
      const cls = f.properties.class || f.properties.type || "";
      labels.push({ type: "place", name, at: f.geometry.coordinates, major: /city|town|village/.test(cls) });
    }
  } catch { /* style not ready */ }
  try {
    const roadSeen = new Set();
    for (const f of map.queryRenderedFeatures({ layers: ["road-label"] })) {
      const name = f.properties && f.properties.name;
      const coords = f.geometry && f.geometry.coordinates;
      if (!name || !coords || coords.length < 2 || typeof coords[0][0] !== "number") continue;
      const mid = Math.floor(coords.length / 2);
      const pt = map.project(coords[mid]);
      const key = `${name}|${Math.round(pt.x / 60)}|${Math.round(pt.y / 60)}`;
      if (roadSeen.has(key)) continue;
      roadSeen.add(key);
      labels.push({ type: "road", name, at: coords[mid],
                    a: coords[Math.max(0, mid - 1)], b: coords[Math.min(coords.length - 1, mid + 1)] });
    }
  } catch { /* style not ready */ }
  cachedLabels = labels;
  lastFrameKey = "";
}

// ── Geometry for this frame ──────────────────────────────────────────
function pinScreen() {
  if (state.dragging && state.dragPos) return state.dragPos;
  const p = map.project([state.origin.lon, state.origin.lat]);
  return { x: p.x, y: p.y };
}

// Everything the overlay and shader need for the current frame, or null with no answer.
function frameGeometry(w, h, pin) {
  const a = currentAnswer();
  if (!a || state.dragging) return null;
  const scale = W.scaleFor(a.refZoom, map.getZoom());
  const minutes = a.minutes;
  const T = W.targetRadii(a.medianOuter * scale, minutes, box.w, box.h);
  const k = a.source.length;
  const at = (theta) => {
    const S = a.source.map((row) => W.lutSample(row, theta) * scale);
    const E = W.effectiveTargets(S, T, state.u);
    const sup = W.supportRadius(pin.x, pin.y, theta, w, h, W.supportFloor(S[k - 1], T[k - 1]));
    return { S, E, sup };
  };
  return { a, scale, T, k, at };
}

// ── Drawing ──────────────────────────────────────────────────────────
const RING_COLOURS = ["#3d7fd9", "#2a5fbf", "#3f3fa6", "#5a2a8a"];

function drawRings(g, pin, w, h) {
  const steps = 360;
  const curves = [];
  for (let i = 0; i < g.k; i++) curves.push([]);
  for (let s = 0; s <= steps; s++) {
    const theta = -Math.PI + (2 * Math.PI * s) / steps;
    const { E } = g.at(theta);
    for (let i = 0; i < g.k; i++) {
      curves[i].push([pin.x + Math.cos(theta) * E[i], pin.y + Math.sin(theta) * E[i]]);
    }
  }
  const trace = (pts) => {
    overlay2d.moveTo(pts[0][0], pts[0][1]);
    for (let j = 1; j < pts.length; j++) overlay2d.lineTo(pts[j][0], pts[j][1]);
    overlay2d.closePath();
  };

  // On the time map, dim what lies past the outer ring: there the map is ordinary again.
  if (state.u > 0.01) {
    overlay2d.save();
    overlay2d.beginPath();
    overlay2d.rect(0, 0, w, h);
    trace(curves[g.k - 1]);
    overlay2d.fillStyle = `rgba(28, 28, 40, ${0.22 * state.u})`;
    overlay2d.fill("evenodd");
    overlay2d.restore();
  }

  overlay2d.lineJoin = "round";
  for (let i = 0; i < g.k; i++) {
    const outer = i === g.k - 1;
    overlay2d.beginPath();
    trace(curves[i]);
    overlay2d.strokeStyle = "rgba(255,255,255,0.9)";
    overlay2d.lineWidth = outer ? 6 : 5;
    overlay2d.stroke();
    overlay2d.strokeStyle = RING_COLOURS[Math.min(i, RING_COLOURS.length - 1)];
    overlay2d.lineWidth = outer ? 3 : 2;
    overlay2d.stroke();
  }

  // Minute labels, up and to the right of the pin.
  const theta = -Math.PI / 4;
  const { E } = g.at(theta);
  overlay2d.font = "bold 13px system-ui, sans-serif";
  overlay2d.textAlign = "left";
  overlay2d.textBaseline = "middle";
  for (let i = 0; i < g.k; i++) {
    const x = pin.x + Math.cos(theta) * E[i] + 4, y = pin.y + Math.sin(theta) * E[i] - 4;
    const text = `${g.a.minutes[i]} min`;
    overlay2d.lineWidth = 4;
    overlay2d.strokeStyle = "rgba(255,255,255,0.95)";
    overlay2d.strokeText(text, x, y);
    overlay2d.fillStyle = RING_COLOURS[Math.min(i, RING_COLOURS.length - 1)];
    overlay2d.fillText(text, x, y);
  }

  if (state.u > 0.5) {
    const top = g.at(-Math.PI / 2);
    const y = Math.max(pin.y - top.E[g.k - 1] - 12, box.top + 14);
    const text = "Travel times end at this ring. Past it the map eases back to normal.";
    overlay2d.font = "600 12px system-ui, sans-serif";
    overlay2d.textAlign = "center";
    overlay2d.globalAlpha = Math.min(1, (state.u - 0.5) * 2);
    overlay2d.lineWidth = 4;
    overlay2d.strokeStyle = "rgba(255,255,255,0.95)";
    overlay2d.strokeText(text, pin.x, y);
    overlay2d.fillStyle = "#1c1c28";
    overlay2d.fillText(text, pin.x, y);
    overlay2d.globalAlpha = 1;
  }
}

// The map's own labels are hidden (they would bend into mush); they are drawn here,
// moved to where their place sits on the picture.
function drawLabels(g, pin, w, h) {
  const move = (x, y) => {
    if (!g || state.u < 0.001) return [x, y];
    const dx = x - pin.x, dy = y - pin.y;
    const r = Math.hypot(dx, dy);
    if (r < 1e-6) return [x, y];
    const theta = Math.atan2(dy, dx);
    const { S, E, sup } = g.at(theta);
    const rt = W.forwardRadius(r, S, E, sup);
    return [pin.x + (dx / r) * rt, pin.y + (dy / r) * rt];
  };
  for (const lbl of cachedLabels) {
    const pt = map.project(lbl.at);
    const [x, y] = move(pt.x, pt.y);
    if (x < -20 || x > w + 20 || y < -20 || y > h + 20) continue;
    overlay2d.save();
    overlay2d.textAlign = "center";
    overlay2d.textBaseline = "middle";
    if (lbl.type === "place") {
      overlay2d.font = lbl.major ? "bold 13px system-ui, sans-serif" : "11px system-ui, sans-serif";
      overlay2d.translate(x, y);
    } else {
      const p1 = map.project(lbl.a), p2 = map.project(lbl.b);
      const tanX = p2.x - p1.x, tanY = p2.y - p1.y;
      const L = Math.hypot(tanX, tanY);
      if (L < 1) { overlay2d.restore(); continue; }
      const nx = (tanX / L) * 10, ny = (tanY / L) * 10;
      const [ax, ay] = move(pt.x - nx, pt.y - ny);
      const [bx, by] = move(pt.x + nx, pt.y + ny);
      let angle = Math.atan2(by - ay, bx - ax);
      if (angle > Math.PI / 2) angle -= Math.PI;
      if (angle < -Math.PI / 2) angle += Math.PI;
      overlay2d.font = "10px system-ui, sans-serif";
      overlay2d.translate(x, y);
      overlay2d.rotate(angle);
    }
    overlay2d.lineWidth = 3;
    overlay2d.strokeStyle = "rgba(255,255,255,0.9)";
    overlay2d.strokeText(lbl.name, 0, 0);
    overlay2d.fillStyle = lbl.type === "place" ? (lbl.major ? "#111" : "#333") : "#444";
    overlay2d.fillText(lbl.name, 0, 0);
    overlay2d.restore();
  }
}

function applyWarp(g, pin, w, h) {
  gl.viewport(0, 0, w, h);
  gl.useProgram(program);
  if (mapDirty) {
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, mapTex);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    try {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, map.getCanvas());
    } catch { /* canvas not ready */ }
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    mapDirty = false;
  }
  gl.activeTexture(gl.TEXTURE0);
  gl.bindTexture(gl.TEXTURE_2D, mapTex);
  gl.uniform1i(locs.u_map, 0);
  gl.activeTexture(gl.TEXTURE1);
  gl.bindTexture(gl.TEXTURE_2D, lutTex);
  gl.uniform1i(locs.u_lut, 1);
  gl.uniform2f(locs.u_center, pin.x, pin.y);
  gl.uniform2f(locs.u_resolution, w, h);

  const active = !!g && state.u > 0.0005;
  gl.uniform1i(locs.u_active, active ? 1 : 0);
  if (active) {
    if (uploadedKey !== g.a.key) uploadLUT(g.a);
    const p = (i) => g.T[Math.min(i, g.k - 1)];
    gl.uniform4f(locs.u_target, p(0), p(1), p(2), p(3));
    gl.uniform1f(locs.u_scale, g.scale);
    gl.uniform1f(locs.u_warp, state.u);
  }
  gl.bindVertexArray(quadVAO);
  gl.drawArrays(gl.TRIANGLES, 0, 6);
  gl.bindVertexArray(null);
}

// ── Animation ────────────────────────────────────────────────────────
const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);

function stepAnimation(now) {
  const want = state.canBend && state.bent && !state.dragging && currentAnswer() ? 1 : 0;
  if (want !== anim.to) {
    const dur = prefersReducedMotion() ? 0 : (nextDuration ?? 600);
    anim = { from: state.u, to: want, start: now, dur };
    nextDuration = null;
  }
  const t = anim.dur > 0 ? Math.min(1, (now - anim.start) / anim.dur) : 1;
  state.u = anim.from + (anim.to - anim.from) * ease(t);
}

// ── Legend ───────────────────────────────────────────────────────────
function setAttr(el, name, value) {
  if (el.getAttribute(name) !== value) el.setAttribute(name, value);
}

function updateLegend(g, pin) {
  const w = box.w;
  const o = state.origin;
  const words = W.MODE_WORDS[state.mode];
  setText(fromEl, `From: ${o.label}${o.example ? " (example)" : ""}`);
  if (g) {
    setText(sentenceEl, W.legendSentence(state.mode, g.a.minutes, state.u > 0.5));
  } else if (state.dragging) {
    setText(sentenceEl, "Drop the pin where you want to start.");
  } else if (!state.bent) {
    setText(sentenceEl, `Press "Show time map" to see how far you can ${words.verb} from the pin.`);
  } else {
    setText(sentenceEl, "Working out how far you can get from the pin.");
  }
  const bentNow = state.bent && state.canBend;
  setText(toggleBtn, bentNow ? "Show normal map" : "Show time map");
  setAttr(toggleBtn, "aria-pressed", String(bentNow));

  // Parts of the answer that can't be shown: off the map, or (bent) their source is.
  let hint = "";
  if (g) {
    const sourceR = g.a.maxOuter * g.scale;
    const R = sourceR * (1 - state.u) + g.T[g.k - 1] * state.u;
    const out = (r) => pin.x - r < 0 || pin.x + r > w || pin.y - r < box.top || pin.y + r > box.bottom;
    if (out(R)) hint = "Zoom out to see every ring.";
    else if (state.u > 0.01 && (pin.x - sourceR < 0 || pin.x + sourceR > view.w || pin.y - sourceR < 0 || pin.y + sourceR > view.h)) {
      hint = "Grey areas can't be drawn from this view. Zoom out to fill them in.";
    }
  }
  setText(zoomHintEl, hint);
  if (zoomHintEl.hidden !== !hint) zoomHintEl.hidden = !hint;
  const pinGone = pin.x < 0 || pin.x > w || pin.y < box.top || pin.y > view.h;
  if (backBtn.hidden === pinGone) backBtn.hidden = !pinGone;
  if (pinEl.hidden === !pinGone) pinEl.hidden = pinGone;
}

// ── Render loop ──────────────────────────────────────────────────────
function renderLoop(now) {
  const { w, h } = view;
  if (map) {
    stepAnimation(now);
    const pin = pinScreen();
    const c = map.getCenter();
    // Skip the frame when nothing on screen can have changed.
    const key = [c.lng, c.lat, map.getZoom(), state.u, pin.x, pin.y, w, h, box.top, box.bottom,
                 currentKey(), answers.size, state.bent, state.dragging, mapDirty].join("|");
    if (key !== lastFrameKey) {
      lastFrameKey = key;
      pinEl.style.transform = `translate(${pin.x}px, ${pin.y}px)`;
      const g = frameGeometry(w, h, pin);
      if (gl) applyWarp(g, pin, w, h);

      const dpr = window.devicePixelRatio || 1;
      overlay2d.setTransform(dpr, 0, 0, dpr, 0, 0);
      overlay2d.clearRect(0, 0, w, h);
      drawLabels(g, pin, w, h);
      if (g) drawRings(g, pin, w, h);
      updateLegend(g, pin);
    }
  }
  requestAnimationFrame(renderLoop);
}

// Everything (canvases, shader, pin, rings) is measured in the map container's box,
// so the bend is centred on the pin even while a phone's toolbar resizes the window.
function resize() {
  const w = Math.max(1, mapContainer.clientWidth), h = Math.max(1, mapContainer.clientHeight);
  const dpr = window.devicePixelRatio || 1;
  view = { w, h };
  warpCanvas.width = w; warpCanvas.height = h;
  overlayCanvas.width = Math.round(w * dpr); overlayCanvas.height = Math.round(h * dpr);
  if (map) map.resize();
  measureBox();
  mapDirty = true;
  lastFrameKey = "";
}
window.addEventListener("resize", resize);
if (typeof ResizeObserver === "function") {
  const ro = new ResizeObserver(() => { measureBox(); lastFrameKey = ""; });
  ro.observe(topEl);
  ro.observe(legendEl);
  new ResizeObserver(resize).observe(mapContainer);
}

// ── Answers ──────────────────────────────────────────────────────────
function framing(a) {
  measureBox();
  return {
    zoom: W.fitZoom(a.medianOuter, a.refZoom, box.w, box.h),
    // Centre the pin in the free area, not under the controls.
    offset: [0, (box.top + box.bottom) / 2 - view.h / 2],
  };
}

function frameAnswer(a) {
  const f = framing(a);
  map.easeTo({
    center: [state.origin.lon, state.origin.lat],
    zoom: f.zoom,
    offset: f.offset,
    duration: prefersReducedMotion() ? 0 : 700,
  });
}

// The current (pin, mode) changed, or the time map was switched on. Any request for older
// inputs is abandoned at once, so its reply can never be drawn. Nothing is fetched while
// the normal map is showing: switching the time map on fetches the newest inputs.
// `frame` reframes the map on the answer once it is there.
let frameWhenAnswered = false;

function requestAnswer({ frame = false, delay = 250 } = {}) {
  clearTimeout(requestTimer);
  latest.cancel();
  if (frame) frameWhenAnswered = true;
  const cached = currentAnswer();
  if (cached) {
    store(cached);
    setStatus("");
    if (frameWhenAnswered) { frameWhenAnswered = false; frameAnswer(cached); }
    return;
  }
  if (!state.bent || !state.canBend) {
    setStatus("");
    return;
  }
  setStatus("Working out travel times…", "busy");
  const snap = { lon: state.origin.lon, lat: state.origin.lat, mode: state.mode };
  requestTimer = setTimeout(async () => {
    const times = W.RING_TIMES[snap.mode];
    const refZoom = W.REF_ZOOM[snap.mode];
    let res;
    try {
      res = await latest.run(snap, (signal) => postJSON("/api/warp-params", {
        longitude: snap.lon, latitude: snap.lat, zoom: refZoom,
        width: 1024, height: 1024, profile: snap.mode, travel_times: times,
      }, signal));
    } catch (e) {
      failed(friendly(e));
      return;
    }
    if (res.stale) return;
    const problem = W.validLut(res.value);
    if (problem) { failed(problem); return; }
    const a = answerFrom(res.value, snap.lon, snap.lat, snap.mode, refZoom);
    store(a);
    setStatus("");
    // The lines appear on the normal map first, then bend: familiar first, then the twist.
    nextDuration = 1800;
    if (frameWhenAnswered) { frameWhenAnswered = false; frameAnswer(a); }
  }, delay);
}

// No answer to show: go back to the normal map, so "Show time map" tries again.
function failed(message) {
  setStatus(message, "error");
  state.bent = false;
  lastFrameKey = "";
}

function setOrigin(lon, lat, label) {
  state.origin = { lon, lat, label, example: false };
  pinEl.setAttribute("aria-label",
    `Starting point: ${label}. Drag it, or use the arrow keys, to move it.`);
}

// Anything that sets the origin overrides a search or location fix still on its way.
function supersedeLookups() {
  searches.cancel();
  locateSeq++;
}

// ── Pin: drag and keyboard ───────────────────────────────────────────
function initPin() {
  let drag = null;
  pinEl.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    pinEl.setPointerCapture(e.pointerId);
    const p = pinScreen();
    // Keep the grab offset, so the pin doesn't jump under the pointer.
    drag = { id: e.pointerId, x: e.clientX, y: e.clientY, dx: p.x - e.clientX, dy: p.y - e.clientY, moved: false };
  });
  pinEl.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.moved && Math.hypot(e.clientX - drag.x, e.clientY - drag.y) > 4) {
      drag.moved = true;
      // Straight to the normal map: the drop is read on the normal map, and the old
      // answer must not be drawn around the moving pin.
      state.dragging = true;
      state.u = 0;
      anim = { from: 0, to: 0, start: 0, dur: 0 };
      pinEl.classList.add("dragging");
      supersedeLookups();
    }
    if (drag.moved) state.dragPos = { x: e.clientX + drag.dx, y: e.clientY + drag.dy };
  });
  const finish = (dropped) => {
    const d = drag;
    drag = null;
    if (!d || !d.moved) return;
    pinEl.classList.remove("dragging");
    const at = state.dragPos;
    state.dragging = false;
    state.dragPos = null;
    lastFrameKey = "";
    if (!dropped || !at) return;           // abandoned: the pin goes back where it was
    const ll = map.unproject([at.x, at.y]);
    setOrigin(ll.lng, ll.lat, "the dropped pin");
    requestAnswer();
  };
  // The pin sits above the map: pass wheel zoom through to it.
  pinEl.addEventListener("wheel", (e) => {
    e.preventDefault();
    map.getCanvas().dispatchEvent(new WheelEvent("wheel", e));
  }, { passive: false });
  pinEl.addEventListener("pointerup", (e) => { if (drag && e.pointerId === drag.id) finish(true); });
  pinEl.addEventListener("pointercancel", (e) => { if (drag && e.pointerId === drag.id) finish(false); });
  pinEl.addEventListener("lostpointercapture", (e) => { if (drag && e.pointerId === drag.id) finish(false); });

  const KEYS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
  pinEl.addEventListener("keydown", (e) => {
    const d = KEYS[e.key];
    if (!d) return;
    e.preventDefault();
    const step = e.shiftKey ? 50 : 10;
    const p = map.project([state.origin.lon, state.origin.lat]);
    const x = p.x + d[0] * step, y = p.y + d[1] * step;
    const ll = map.unproject([x, y]);
    supersedeLookups();
    setOrigin(ll.lng, ll.lat, "the dropped pin");
    // Keep a keyboard-moved pin in view, so it never hides while it has focus.
    const margin = 40;
    if (x < margin || x > box.w - margin || y < box.top + margin || y > box.bottom - margin) {
      map.easeTo({ center: [ll.lng, ll.lat], duration: prefersReducedMotion() ? 0 : 300 });
    }
    requestAnswer({ delay: 700 });
  });

  backBtn.addEventListener("click", () => {
    map.easeTo({ center: [state.origin.lon, state.origin.lat], duration: prefersReducedMotion() ? 0 : 500 });
    pinEl.focus();
  });
}

// ── Search and location ──────────────────────────────────────────────
function moveTo(lon, lat, label) {
  setOrigin(lon, lat, label);
  map.jumpTo({ center: [lon, lat] });
  requestAnswer({ frame: true });
}

function initSearch() {
  const form = $("search-form");
  const input = $("search");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) { setStatus("Type a place to search for.", "error"); input.focus(); return; }
    locateSeq++;
    setStatus("Searching…", "busy");
    let res;
    try {
      res = await searches.run(q, (signal) => geocode(q, signal));
    } catch (err) {
      setStatus(friendly(err), "error");
      return;
    }
    if (res.stale) return;
    if (!res.value) {
      setStatus("We couldn't find that place. Try a nearby street or station, or drag the pin.", "error");
      return;
    }
    input.blur();
    moveTo(res.value.lon, res.value.lat, q);
  });
}

function locate() {
  if (!navigator.geolocation) {
    setStatus("This browser can't share your location. Search for a place or drag the pin.", "error");
    return;
  }
  searches.cancel();
  const mine = ++locateSeq;
  setStatus("Finding your location…", "busy");
  navigator.geolocation.getCurrentPosition(
    (p) => {
      if (mine !== locateSeq) return;
      savePref(LOCATION_PREF, "yes");
      moveTo(p.coords.longitude, p.coords.latitude, "your location");
    },
    (err) => {
      if (mine !== locateSeq) return;
      setStatus(err.code === 1
        ? "Location is turned off for this site. Search for a place or drag the pin instead."
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
  $("location-yes").addEventListener("click", () => {
    card.hidden = true;
    savePref(LOCATION_PREF, "yes");
    locate();
  });
  $("location-no").addEventListener("click", () => {
    card.hidden = true;
    savePref(LOCATION_PREF, "no");
    toggleBtn.focus();
  });
}

// ── Controls ─────────────────────────────────────────────────────────
function initControls() {
  for (const radio of document.querySelectorAll('input[name="mode"]')) {
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      state.mode = radio.value;
      requestAnswer({ frame: true });
    });
  }
  toggleBtn.addEventListener("click", () => {
    if (!state.canBend) return;
    state.bent = !state.bent;
    nextDuration = 1800;
    lastFrameKey = "";
    if (state.bent) {
      requestAnswer({ delay: 0 });         // the newest inputs, unless already answered
    } else {
      clearTimeout(requestTimer);
      latest.cancel();
      setStatus("");
    }
  });
}

function checkMode(mode) {
  const radio = document.querySelector(`input[name="mode"][value="${mode}"]`);
  if (radio) radio.checked = true;
}

// ── Boot ─────────────────────────────────────────────────────────────
async function loadExample() {
  const resp = await fetch("example.json");
  if (!resp.ok) throw new Error("example missing");
  const ex = await resp.json();
  if (W.validLut(ex) || !ex.origin || !W.RING_TIMES[ex.profile]) throw new Error("example invalid");
  return ex;
}

async function boot() {
  if (typeof maplibregl === "undefined") {
    setStatus("The map couldn't load. Check your connection and reload the page.", "error");
    toggleBtn.disabled = true;
    return;
  }
  resize();
  try { initWebGL(); } catch { disableBending(); }

  let zoom = 14, firstAnswer = null;
  try {
    const ex = await loadExample();
    state.origin = { lon: ex.origin.lon, lat: ex.origin.lat, label: ex.origin.label, example: true };
    state.mode = ex.profile;
    const a = answerFrom(ex, ex.origin.lon, ex.origin.lat, ex.profile, ex.ref_zoom);
    store(a);
    firstAnswer = a;
    zoom = framing(a).zoom;
  } catch {
    setStatus("The example didn't load. Search for a place or drag the pin, then press Show time map.", "error");
  }
  checkMode(state.mode);
  pinEl.setAttribute("aria-label",
    `Starting point: ${state.origin.label}. Drag it, or use the arrow keys, to move it.`);

  initMap([state.origin.lon, state.origin.lat], zoom);
  if (firstAnswer) map.easeTo({ center: [state.origin.lon, state.origin.lat], offset: framing(firstAnswer).offset, duration: 0 });
  initPin();
  initSearch();
  initLocation();
  initControls();
  requestAnimationFrame(renderLoop);
}

boot();
