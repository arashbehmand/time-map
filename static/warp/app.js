(() => {
  "use strict";

  const LUT_SIZE = 2048;

  // ── State ────────────────────────────────────────────────────────────
  let userLon = -0.1950, userLat = 51.5340;  // default: London
  let gpsLon  = null,   gpsLat  = null;      // set once GPS resolves
  let warpParams = null;
  let currentZoom = 14;
  let currentProfile = "walking";
  let currentTimes = [5, 10, 15, 20];
  let fetching = false, fetchTimer = null;
  let mapDirty = true;
  let cachedLabels = [];
  let labelUpdateTimer = null;
  let warpParamsZoom = null;
  let warpCenterLon  = null, warpCenterLat = null;
  let autoZoomPending = false;
  let warpModeEnabled = false;

  // ── DOM ──────────────────────────────────────────────────────────────
  const warpCanvas   = document.getElementById("warp-canvas");
  const overlayCanvas = document.getElementById("overlay-canvas");
  const statusEl     = document.getElementById("status");
  const loadingEl    = document.getElementById("loading");
  const loadingMsg   = document.getElementById("loading-msg");
  const profileSel   = document.getElementById("profile");
  const timesInput   = document.getElementById("times");
  const zoomSlider   = document.getElementById("zoom");
  const zoomVal      = document.getElementById("zoom-val");
  const centerBtn    = document.getElementById("center-btn");

  // ── GLSL Shaders ─────────────────────────────────────────────────────
  const VERT_SRC = /* glsl */`#version 300 es
    in vec2 a_pos;
    out vec2 v_uv;
    void main() {
      gl_Position = vec4(a_pos, 0.0, 1.0);
      v_uv = (a_pos + 1.0) * 0.5;
    }`;

  const FRAG_SRC = /* glsl */`#version 300 es
    precision highp float;
    in vec2 v_uv;
    out vec4 fragColor;

    uniform sampler2D u_map;     // MapLibre rendered frame
    uniform sampler2D u_lut;     // RGBA32F 2048x1: source radii [k0,k1,k2,k3] per angle
    uniform sampler2D u_support; // R32F 2048x1: support radius per angle
    uniform vec2 u_center;       // canvas centre in pixels (y-down)
    uniform vec2 u_resolution;   // canvas size in pixels
    // target radii always padded to 4 (repeat last value if K < 4)
    uniform vec4 u_target;
    uniform float u_warp;    // 1.0 = apply warp, 0.0 = passthrough

    const float PI  = 3.14159265359;
    const float EPS = 1e-6;

    void main() {
      // Screen UV → canvas coordinates (y-down, origin top-left)
      vec2 canvas = vec2(v_uv.x * u_resolution.x,
                         (1.0 - v_uv.y) * u_resolution.y);
      vec2 delta = canvas - u_center;
      float rt = length(delta);

      // At the exact centre: pass through
      if (rt < EPS) {
        fragColor = texture(u_map, v_uv);
        return;
      }

      // Sample warp LUT by angle (y-down canvas convention matches Python)
      float theta = atan(delta.y, delta.x);
      float t = (theta + PI) / (2.0 * PI);
      vec4  lut = texture(u_lut,     vec2(t, 0.5));
      float sup = texture(u_support, vec2(t, 0.5)).r;

      // Inverse warp: 4 bands always (LUT and target padded to 4 if K < 4)
      float orig_r = rt;  // default: identity (used when u_warp = 0)

      if (rt <= u_target.x) {
        orig_r = lut.r * rt / max(u_target.x, EPS);

      } else if (rt <= u_target.y) {
        float a = (rt - u_target.x) / max(u_target.y - u_target.x, EPS);
        orig_r = mix(lut.r, lut.g, a);

      } else if (rt <= u_target.z) {
        float a = (rt - u_target.y) / max(u_target.z - u_target.y, EPS);
        orig_r = mix(lut.g, lut.b, a);

      } else if (rt <= u_target.w) {
        float a = (rt - u_target.z) / max(u_target.w - u_target.z, EPS);
        orig_r = mix(lut.b, lut.a, a);

      } else if (rt < sup) {
        // Blend to identity between outermost isochrone and support boundary
        float off = u_target.w - lut.a;
        float den = sup - lut.a;
        float ac  = 1.0 - off / max(den, EPS);
        float bc  = off * sup / max(den, EPS);
        orig_r = (rt - bc) / max(ac, EPS);

      } else {
        orig_r = rt;  // identity beyond support
      }

      // Blend between warped and identity based on u_warp
      orig_r = mix(rt, orig_r, u_warp);

      // Reconstruct original canvas position and convert to UV
      vec2 orig = u_center + normalize(delta) * orig_r;
      // UNPACK_FLIP_Y_WEBGL=true: UV y=0 = canvas bottom → uv_y = 1 - canvas_y/h
      vec2 uv = vec2(orig.x / u_resolution.x,
                     1.0 - orig.y / u_resolution.y);

      if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) {
        // Source position is outside the MapLibre canvas — show the unwarped
        // pixel at this screen position instead of a fill colour.
        // This makes the warp gracefully degrade: inner circles (whose source
        // geometry fits in the canvas) stay warped; outer ones show normal map.
        fragColor = texture(u_map, v_uv);
        return;
      }
      fragColor = texture(u_map, uv);
    }`;

  // ── WebGL2 setup ─────────────────────────────────────────────────────
  let gl, warpProgram, locs = {}, lutTex, supportTex, mapTex, quadVAO;

  function compileShader(gl, type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
      throw new Error("Shader error: " + gl.getShaderInfoLog(s));
    return s;
  }

  function initWebGL() {
    gl = warpCanvas.getContext("webgl2");
    if (!gl) { alert("WebGL2 not supported"); return; }
    gl.getExtension("OES_texture_float_linear");

    const vert = compileShader(gl, gl.VERTEX_SHADER,   VERT_SRC);
    const frag = compileShader(gl, gl.FRAGMENT_SHADER, FRAG_SRC);
    warpProgram = gl.createProgram();
    gl.attachShader(warpProgram, vert);
    gl.attachShader(warpProgram, frag);
    gl.linkProgram(warpProgram);
    if (!gl.getProgramParameter(warpProgram, gl.LINK_STATUS))
      throw new Error("Link error: " + gl.getProgramInfoLog(warpProgram));

    locs = {
      a_pos:       gl.getAttribLocation(warpProgram,  "a_pos"),
      u_map:       gl.getUniformLocation(warpProgram, "u_map"),
      u_lut:       gl.getUniformLocation(warpProgram, "u_lut"),
      u_support:   gl.getUniformLocation(warpProgram, "u_support"),
      u_center:    gl.getUniformLocation(warpProgram, "u_center"),
      u_resolution:gl.getUniformLocation(warpProgram, "u_resolution"),
      u_target:    gl.getUniformLocation(warpProgram, "u_target"),
      u_warp:      gl.getUniformLocation(warpProgram, "u_warp"),
    };

    // Fullscreen quad (two triangles)
    quadVAO = gl.createVertexArray();
    gl.bindVertexArray(quadVAO);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
      -1,-1,  1,-1,  -1, 1,
      -1, 1,  1,-1,   1, 1,
    ]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(locs.a_pos);
    gl.vertexAttribPointer(locs.a_pos, 2, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);

    // LUT texture: RGBA32F, 2048×1
    lutTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, lutTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, LUT_SIZE, 1, 0,
                  gl.RGBA, gl.FLOAT, new Float32Array(LUT_SIZE * 4));

    // Support texture: R32F, 2048×1
    supportTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, supportTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, LUT_SIZE, 1, 0,
                  gl.RED, gl.FLOAT, new Float32Array(LUT_SIZE));

    // Map texture: RGBA, updated each frame from MapLibre canvas
    mapTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, mapTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  }

  // ── Upload warp LUT to GPU ───────────────────────────────────────────
  function uploadLUT(params) {
    const src = params.source_radii;  // [K][2048]
    const k   = params.k;

    // Pack RGBA: channel i = source radii for isochrone i (pad last if K < 4)
    const lutData = new Float32Array(LUT_SIZE * 4);
    const pick = (i) => src[Math.min(i, k - 1)];
    for (let i = 0; i < LUT_SIZE; i++) {
      lutData[i*4+0] = pick(0)[i];
      lutData[i*4+1] = pick(1)[i];
      lutData[i*4+2] = pick(2)[i];
      lutData[i*4+3] = pick(3)[i];
    }
    gl.bindTexture(gl.TEXTURE_2D, lutTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, LUT_SIZE, 1, 0,
                  gl.RGBA, gl.FLOAT, lutData);

    // Support radii: R32F
    const supData = new Float32Array(params.support_radii);
    gl.bindTexture(gl.TEXTURE_2D, supportTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, LUT_SIZE, 1, 0,
                  gl.RED, gl.FLOAT, supData);
  }

  // ── Forward warp: canvas coords → warped canvas coords ───────────────
  function forwardWarpPoint(x, y) {
    if (!warpParams) return [x, y];
    const w = warpCanvas.width, h = warpCanvas.height;
    const s = Math.min(w / warpParams.canvas.width, h / warpParams.canvas.height);
    const cx = w / 2, cy = h / 2;
    const dx = x - cx, dy = y - cy;
    const r = Math.sqrt(dx * dx + dy * dy);
    if (r < 1e-6) return [x, y];

    const theta = Math.atan2(dy, dx);
    const tNorm = (theta + Math.PI) / (2 * Math.PI);
    const ai = Math.round(tNorm * (LUT_SIZE - 1)) % LUT_SIZE;

    const k  = warpParams.k;
    const tr = adaptiveTargetRadii(warpParams, warpCanvas.width, warpCanvas.height);
    const sr = warpParams.source_radii.map(row => row[ai] * s);
    const sup = warpParams.support_radii[ai] * s;

    let rt = r;
    let warped = false;
    if (r <= sr[0]) {
      rt = tr[0] * r / Math.max(sr[0], 1e-6);
      warped = true;
    } else {
      for (let i = 0; i < k - 1; i++) {
        if (r <= sr[i + 1]) {
          const a = (r - sr[i]) / Math.max(sr[i + 1] - sr[i], 1e-6);
          rt = (1 - a) * tr[i] + a * tr[i + 1];
          warped = true;
          break;
        }
      }
      if (!warped && r > sr[k - 1] && r < sup) {
        const blend = (sup - r) / Math.max(sup - sr[k - 1], 1e-6);
        rt = r + blend * (tr[k - 1] - sr[k - 1]);
      }
    }
    return [cx + (dx / r) * rt, cy + (dy / r) * rt];
  }

  // ── Adaptive target radii ─────────────────────────────────────────────
  // Instead of fixed 42% of canvas, scale circles to the actual isochrone
  // pixel size. Circles expand to at most 2.5× the median source radius,
  // capped at 42% of canvas. This means at zoom-10/walking the circles are
  // small (matching the tiny walking isochrone) rather than filling the screen.
  const MAX_TARGET_FRAC = 0.42;
  const STRETCH = 2.5;

  function adaptiveTargetRadii(params, w, h) {
    const s = Math.min(w / params.canvas.width, h / params.canvas.height);
    const k = params.k;
    const canvasHalf = Math.min(w, h) / 2;
    const maxR = MAX_TARGET_FRAC * Math.min(w, h);

    // Median source radius for each isochrone in current canvas pixels
    const medians = params.source_radii.map(row => {
      const sorted = Float64Array.from(row).sort();
      return sorted[Math.floor(sorted.length / 2)] * s;
    });

    // Outermost isochrone whose source fits within the canvas half.
    // At high zoom the inner circles are large in pixel space — use the
    // outermost one that still fits so it fills the display.  This means
    // zoom-16 shows only the 5-min circle (properly warped, filling the
    // screen) rather than four circles all capped at the same 42% radius.
    let fitIdx = 0;
    for (let i = 0; i < k; i++) {
      if (medians[i] <= canvasHalf) fitIdx = i;
    }

    // That circle fills up to maxR; scale all others proportionally
    const outerTarget = Math.min(maxR, medians[fitIdx] * STRETCH);
    const origFit = params.target_radii[fitIdx] * s;
    const scale = outerTarget / Math.max(origFit, 1e-6);
    return params.target_radii.map(r => r * s * scale);
  }

  // ── MapLibre setup ───────────────────────────────────────────────────
  let map;

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

  function initMap(lon, lat) {
    map = new maplibregl.Map({
      container: "map-container",
      style: buildStyle(),
      center: [lon, lat],
      zoom: currentZoom,
      preserveDrawingBuffer: true,
      interactive: true,
      dragPan: true,
      scrollZoom: true,        // mouse wheel zoom
      touchZoomRotate: true,   // pinch-to-zoom on mobile
      dragRotate: false,       // disable compass rotation
      touchPitch: false,       // disable 3D tilt
      doubleClickZoom: true,
    });
    map.on("render", () => {
      mapDirty = true;
      clearTimeout(labelUpdateTimer);
      labelUpdateTimer = setTimeout(updateLabelCache, 150);
    });
    map.on("moveend", () => {
      if (autoZoomPending) return;
      const c = map.getCenter();
      userLon = c.lng; userLat = c.lat;
      statusEl.textContent = `${userLat.toFixed(5)}, ${userLon.toFixed(5)}`;
      if (warpModeEnabled) {
        // Show subtle loading indicator while warp updates
        loadingMsg.textContent = "Updating warp…";
        loadingEl.hidden = false;
        schedule();
      }
    });
    map.on("zoomend", () => {
      currentZoom = Math.round(map.getZoom());
      zoomSlider.value = currentZoom;
      zoomVal.textContent = currentZoom;
      if (!autoZoomPending) schedule();
    });
  }

  // ── Label cache ──────────────────────────────────────────────────────
  function updateLabelCache() {
    if (!map || !warpParams || !map.loaded()) { cachedLabels = []; return; }
    const labels = [];
    const seenPlace = new Set();

    // Place + POI labels (Point geometry)
    try {
      const feats = map.queryRenderedFeatures({ layers: ['place-label', 'poi-label'] });
      for (const feat of feats) {
        const name = feat.properties && feat.properties.name;
        if (!name) continue;
        if (feat.geometry.type !== 'Point') continue;
        if (seenPlace.has(name)) continue;
        seenPlace.add(name);
        const [lon, lat] = feat.geometry.coordinates;
        const pt = map.project([lon, lat]);
        const cls = (feat.properties.class || feat.properties.type || '');
        labels.push({ type: 'place', name, x: pt.x, y: pt.y, cls });
      }
    } catch (_) {}

    // Road labels (LineString geometry)
    try {
      const roadFeats = map.queryRenderedFeatures({ layers: ['road-label'] });
      const roadSeen = {};
      for (const feat of roadFeats) {
        const name = feat.properties && feat.properties.name;
        if (!name) continue;
        const coords = feat.geometry && feat.geometry.coordinates;
        if (!coords || coords.length < 2) continue;
        const midIdx = Math.floor(coords.length / 2);
        const pt = map.project(coords[midIdx]);
        // Deduplicate by name + 60px grid cell
        const key = `${name}|${Math.round(pt.x / 60)}|${Math.round(pt.y / 60)}`;
        if (roadSeen[key]) continue;
        roadSeen[key] = true;
        const p1 = map.project(coords[Math.max(0, midIdx - 1)]);
        const p2 = map.project(coords[Math.min(coords.length - 1, midIdx + 1)]);
        labels.push({ type: 'road', name, x: pt.x, y: pt.y,
                      tanX: p2.x - p1.x, tanY: p2.y - p1.y });
      }
    } catch (_) {}

    cachedLabels = labels;
  }

  // ── Render loop ──────────────────────────────────────────────────────
  const overlay2d = overlayCanvas.getContext("2d");

  function applyWarp() {
    const w = warpCanvas.width, h = warpCanvas.height;
    gl.viewport(0, 0, w, h);
    gl.useProgram(warpProgram);

    // Upload MapLibre's current canvas to mapTex
    if (mapDirty && map) {
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, mapTex);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
      try {
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA,
                      gl.UNSIGNED_BYTE, map.getCanvas());
      } catch (_) {}
      mapDirty = false;
    }

    // Bind textures
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, mapTex);
    gl.uniform1i(locs.u_map, 0);

    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, lutTex);
    gl.uniform1i(locs.u_lut, 1);

    gl.activeTexture(gl.TEXTURE2);
    gl.bindTexture(gl.TEXTURE_2D, supportTex);
    gl.uniform1i(locs.u_support, 2);

    gl.uniform2f(locs.u_center, w / 2, h / 2);
    gl.uniform2f(locs.u_resolution, w, h);

    const mapZoom = map ? Math.round(map.getZoom()) : currentZoom;
    // Check how far the map has panned from the position warp was computed for.
    // Allow significant drift (up to 500px) so users can navigate while warped —
    // the warp will show approximately correct geometry nearby and refetch when
    // movement stops. Fall back to passthrough only for extreme drift.
    let drift = 0;
    if (map && warpCenterLon !== null) {
      const pt = map.project([warpCenterLon, warpCenterLat]);
      drift = Math.hypot(pt.x - w / 2, pt.y - h / 2);
    }
    const warpReady = warpModeEnabled && warpParams && warpParams.k > 0 &&
                      (warpParamsZoom === null || mapZoom === warpParamsZoom) &&
                      drift < 500;

    if (warpReady) {
      const k  = warpParams.k;
      const tr = adaptiveTargetRadii(warpParams, w, h);
      const p  = (i) => tr[Math.min(i, k - 1)];
      gl.uniform4f(locs.u_target, p(0), p(1), p(2), p(3));
      gl.uniform1f(locs.u_warp, 1.0);
    } else {
      gl.uniform4f(locs.u_target, 1, 1, 1, 1);
      gl.uniform1f(locs.u_warp, 0.0);  // passthrough: show raw map
    }

    gl.bindVertexArray(quadVAO);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
    gl.bindVertexArray(null);
  }

  function drawOverlay() {
    const w = overlayCanvas.width, h = overlayCanvas.height;
    overlay2d.clearRect(0, 0, w, h);
    const mapZoom = map ? Math.round(map.getZoom()) : currentZoom;
    if (!warpModeEnabled || !warpParams || (warpParamsZoom !== null && mapZoom !== warpParamsZoom)) return;

    const cx = w / 2, cy = h / 2;
    const tr = adaptiveTargetRadii(warpParams, w, h);

    const maxRing = MAX_TARGET_FRAC * Math.min(w, h) * 1.05; // only draw circles that fit
    overlay2d.setLineDash([6, 4]);
    overlay2d.lineWidth = 1.5;
    overlay2d.font = "bold 11px system-ui";
    for (let i = 0; i < warpParams.rings.length; i++) {
      const ring = warpParams.rings[i];
      const r = tr[Math.min(i, tr.length - 1)];
      if (r > maxRing) continue;  // off-canvas source: skip
      overlay2d.strokeStyle = "rgba(220,50,50,.6)";
      overlay2d.beginPath();
      overlay2d.arc(cx, cy, r, 0, Math.PI * 2);
      overlay2d.stroke();
      overlay2d.fillStyle = "rgba(220,50,50,.8)";
      overlay2d.fillText(ring.minutes + " min", cx + r + 4, cy - 4);
    }
    overlay2d.setLineDash([]);

    // Location dot
    overlay2d.fillStyle = "#4361ee";
    overlay2d.strokeStyle = "#fff";
    overlay2d.lineWidth = 2.5;
    overlay2d.beginPath();
    overlay2d.arc(cx, cy, 7, 0, Math.PI * 2);
    overlay2d.fill();
    overlay2d.stroke();

    // Warped labels
    for (const lbl of cachedLabels) {
      const [wx, wy] = forwardWarpPoint(lbl.x, lbl.y);
      if (wx < -20 || wx > w + 20 || wy < -20 || wy > h + 20) continue;

      if (lbl.type === 'place') {
        const isMajor = /city|town|village/.test(lbl.cls);
        overlay2d.save();
        overlay2d.textAlign = 'center';
        overlay2d.textBaseline = 'middle';
        overlay2d.font = isMajor ? 'bold 13px system-ui' : '11px system-ui';
        overlay2d.strokeStyle = 'rgba(255,255,255,0.9)';
        overlay2d.lineWidth = 3;
        overlay2d.strokeText(lbl.name, wx, wy);
        overlay2d.fillStyle = isMajor ? '#111' : '#444';
        overlay2d.fillText(lbl.name, wx, wy);
        overlay2d.restore();

      } else if (lbl.type === 'road') {
        const L = Math.sqrt(lbl.tanX ** 2 + lbl.tanY ** 2);
        if (L < 1) continue;
        const eps = 10;
        const nx = lbl.tanX / L * eps, ny = lbl.tanY / L * eps;
        const [ax, ay] = forwardWarpPoint(lbl.x - nx, lbl.y - ny);
        const [bx, by] = forwardWarpPoint(lbl.x + nx, lbl.y + ny);
        let angle = Math.atan2(by - ay, bx - ax);
        if (angle >  Math.PI / 2) angle -= Math.PI;
        if (angle < -Math.PI / 2) angle += Math.PI;
        overlay2d.save();
        overlay2d.translate(wx, wy);
        overlay2d.rotate(angle);
        overlay2d.textAlign = 'center';
        overlay2d.textBaseline = 'middle';
        overlay2d.font = '10px system-ui';
        overlay2d.strokeStyle = 'rgba(255,255,255,0.85)';
        overlay2d.lineWidth = 2.5;
        overlay2d.strokeText(lbl.name, 0, 0);
        overlay2d.fillStyle = '#555';
        overlay2d.fillText(lbl.name, 0, 0);
        overlay2d.restore();
      }
    }
  }

  function renderLoop() {
    applyWarp();
    drawOverlay();
    requestAnimationFrame(renderLoop);
  }

  // ── Resize ───────────────────────────────────────────────────────────
  function resize() {
    const w = window.innerWidth, h = window.innerHeight;
    warpCanvas.width    = w; warpCanvas.height    = h;
    overlayCanvas.width = w; overlayCanvas.height = h;
    if (map) map.resize();
    mapDirty = true;
  }
  window.addEventListener("resize", () => { resize(); schedule(); });

  // ── Warp params fetch ────────────────────────────────────────────────
  function schedule() {
    clearTimeout(fetchTimer);
    // Use shorter debounce in warp mode for responsive navigation
    const delay = warpModeEnabled ? 300 : 600;
    fetchTimer = setTimeout(fetchWarpParams, delay);
  }

  async function fetchWarpParams() {
    if (fetching) return;
    fetching = true;
    loadingMsg.textContent = "Computing warp…";
    loadingEl.hidden = false;
    const t0 = Date.now();

    const w = Math.min(warpCanvas.width || window.innerWidth, 2048);
    const h = Math.min(warpCanvas.height || window.innerHeight, 2048);
    try {
      const resp = await fetch("/api/warp-params", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          longitude: userLon, latitude: userLat,
          zoom: currentZoom, width: w, height: h,
          profile: currentProfile,
          travel_times: currentTimes,
        }),
      });
      if (!resp.ok) throw new Error(await resp.text());
      warpParams = await resp.json();
      warpParamsZoom = currentZoom;
      warpCenterLon  = userLon;
      warpCenterLat  = userLat;
      uploadLUT(warpParams);

      // Auto-zoom OUT: only when even the innermost isochrone exceeds canvas_half.
      // Outer circles that overflow are handled gracefully by the passthrough OOB
      // fix in the shader (shows unwarped pixel instead of crumpled garbage).
      // We only force a zoom-out when source_radii[0] (innermost band) is itself
      // off-canvas, meaning there is no useful warp to show at all.
      {
        const innerSrc = warpParams.source_radii[0];
        const sorted = Float64Array.from(innerSrc).sort();
        const medSrc = sorted[Math.floor(sorted.length / 2)];
        const cw = Math.min(warpCanvas.width  || window.innerWidth,  2048);
        const ch = Math.min(warpCanvas.height || window.innerHeight, 2048);
        const s  = Math.min(cw / warpParams.canvas.width, ch / warpParams.canvas.height);
        const medSrcPx = medSrc * s;
        const canvasHalf = Math.min(cw, ch) / 2;  // max valid sample radius
        const ratio = medSrcPx / canvasHalf;
        console.log(`[warp] zoom=${currentZoom} innerSrc=${medSrcPx.toFixed(0)}px canvasHalf=${canvasHalf.toFixed(0)}px ratio=${ratio.toFixed(2)}`);
        if (ratio > 1.0) {
          // ceil guarantees newZoom puts medSrc below canvasHalf in one step
          const deltaZoom = Math.ceil(Math.log2(ratio));
          const newZoom = Math.max(8, Math.min(16, currentZoom - deltaZoom));
          if (newZoom !== currentZoom) {
            console.log(`[warp] auto-zoom ${currentZoom} → ${newZoom} (ratio=${ratio.toFixed(2)})`);
            autoZoomPending = true;
            currentZoom = newZoom;
            zoomSlider.value = currentZoom;
            zoomVal.textContent = currentZoom;
            if (map) map.setZoom(currentZoom);
            setTimeout(() => { autoZoomPending = false; fetchWarpParams(); }, 700);
          }
        }
      }

      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
      statusEl.textContent =
        `${userLat.toFixed(5)}, ${userLon.toFixed(5)}  (${elapsed}s)`;
    } catch (e) {
      statusEl.textContent = "Error: " + e.message.slice(0, 80);
    }
    loadingEl.hidden = true;
    fetching = false;
  }

  // ── Geolocation ──────────────────────────────────────────────────────
  function startLocation() {
    // Start immediately at the default location — no GPS wait
    onFirstLocation();

    // Try to get GPS in the background; lights up the GPS button when ready
    if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(
        (p) => {
          gpsLon = p.coords.longitude;
          gpsLat = p.coords.latitude;
          centerBtn.title = `GPS: ${gpsLat.toFixed(5)}, ${gpsLon.toFixed(5)}`;
          centerBtn.textContent = "📍 My Location";
        },
        () => { /* GPS unavailable — that's fine */ }
      );
    }
  }

  function onFirstLocation() {
    initMap(userLon, userLat);
    schedule();
  }

  // ── Geocoding ────────────────────────────────────────────────────────
  async function geocode(query) {
    // Accept raw "lat,lon" without a network round-trip
    const m = query.match(/^(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)$/);
    if (m) return { lat: +m[1], lon: +m[2] };
    const url = "https://nominatim.openstreetmap.org/search?format=json&limit=1&q="
                + encodeURIComponent(query);
    const data = await (await fetch(url)).json();
    if (!data.length) return null;
    return { lat: +data[0].lat, lon: +data[0].lon };
  }

  // ── Controls ─────────────────────────────────────────────────────────
  function initControls() {
    const warpToggle = document.getElementById("warp-toggle");
    warpToggle.addEventListener("click", () => {
      warpModeEnabled = !warpModeEnabled;
      warpToggle.textContent = warpModeEnabled ? "⟳ Warp ON" : "⟳ Warp";
      warpToggle.classList.toggle("active", warpModeEnabled);
      if (warpModeEnabled) schedule();
    });

    const searchForm  = document.getElementById("search-form");
    const searchInput = document.getElementById("search");
    searchForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const q = searchInput.value.trim();
      if (!q) return;
      statusEl.textContent = "Searching…";
      try {
        const result = await geocode(q);
        if (!result) { statusEl.textContent = "Place not found"; return; }
        userLon = result.lon; userLat = result.lat;
        if (map) map.setCenter([userLon, userLat]);
        schedule();
        searchInput.blur();
      } catch (err) {
        statusEl.textContent = "Search error: " + err.message.slice(0, 60);
      }
    });

    profileSel.addEventListener("change", () => {
      currentProfile = profileSel.value; schedule();
    });
    timesInput.addEventListener("change", () => {
      currentTimes = timesInput.value.split(",").map(Number).filter(n => n > 0);
      schedule();
    });
    zoomSlider.addEventListener("input", () => {
      currentZoom = +zoomSlider.value;
      zoomVal.textContent = currentZoom;
      if (map) map.setZoom(currentZoom);
      schedule();
    });
    centerBtn.addEventListener("click", () => {
      if (gpsLon) {
        // Snap map to GPS fix
        userLon = gpsLon; userLat = gpsLat;
        if (map) map.setCenter([userLon, userLat]);
        schedule();
      } else if (navigator.geolocation) {
        statusEl.textContent = "Requesting GPS…";
        navigator.geolocation.getCurrentPosition(
          (p) => {
            gpsLon = p.coords.longitude; gpsLat = p.coords.latitude;
            userLon = gpsLon; userLat = gpsLat;
            centerBtn.title = `GPS: ${gpsLat.toFixed(5)}, ${gpsLon.toFixed(5)}`;
            centerBtn.textContent = "📍 My Location";
            if (map) map.setCenter([userLon, userLat]);
            schedule();
          },
          (e) => { statusEl.textContent = "GPS error: " + e.message; }
        );
      }
    });
  }

  // ── Boot ─────────────────────────────────────────────────────────────
  resize();
  initWebGL();
  initControls();
  renderLoop();
  startLocation();
})();
