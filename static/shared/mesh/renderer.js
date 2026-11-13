// The mesh renderer: draws the worker's packed passes through the warp with WebGL2.
//
//   B "gpu"  the vertex shader evaluates the warp from a float texture (field or grid)
//   C "cpu"  the worker evaluated the warp per vertex; the shader only mixes by u
// Both share every buffer, program and draw call except that one #define. Lines are one
// instanced quad per segment, extruded in screen pixels after both ends are mapped, and
// drawn as a capsule with analytic anti-aliasing, so their width never stretches. Fills
// are drawn with no face culling, so folded (flipped) triangles still show.
//
// A scene is packed in metres around one origin, and a warp belongs to one origin; draw()
// refuses a camera, scene or warp from a different origin rather than draw a misplaced map.
import { createEvaluator, floatTexture, link, probeCaps } from "./caps.js";
import { BACKGROUND, hexToRgba, PASSES, STYLES, widthAt } from "./schema-streets-v8.js";
import { ANGLE_VEC4, STYLE, WARP_GLSL, warpDefines } from "./warp.js";

export const MAX_DPR = 2;
const NSTYLE = STYLES.length;
const FOLD_TINT = [0.86, 0.12, 0.42];

const COMMON = (defines) => `#version 300 es
precision highp float;
precision highp int;
${defines}
#define NSTYLE ${NSTYLE}
${WARP_GLSL}
uniform vec4  u_color[NSTYLE];
uniform float u_width[NSTYLE];
uniform bool  u_adaptive;       // the scale-aware style on or off
uniform float u_bent[NSTYLE];   // each class's strength on the bent map (scale-aware style)
uniform float u_dpr;
vec4 clipOf(vec2 css) {
  vec2 c = css / u_view * 2.0 - 1.0;
  return vec4(c.x, -c.y, 0.0, 1.0);
}
`;

const FILL_VS = (d) => `${COMMON(d)}
in vec2 a_pos;
in float a_style;
#ifdef CPU_TARGETS
in vec3 a_t;
#endif
out vec4 v_color;
void main() {
  vec3 s = warpScreen(a_pos, TARGET(a_pos, a_t));
  v_color = u_color[int(a_style)] * s.z;        // premultiplied
  gl_Position = clipOf(s.xy);
}`;

const FILL_FS = `#version 300 es
precision highp float;
in vec4 v_color;
uniform bool u_showFolds;
uniform bool u_foldOnly;    // the fold layer: draw only flipped triangles, as a tint
uniform bool u_adaptive;    // scale-aware style: folded parks and water are dimmed
out vec4 o;
void main() {
  // Unfolded triangles face front (frontFace CW, see draw()); a fold flips them.
  if (u_foldOnly) {
    if (gl_FrontFacing) discard;
    o = vec4(vec3(${FOLD_TINT.join(", ")}) * 0.3, 0.3) * v_color.a;
    return;
  }
  vec4 c = v_color;
  if (u_showFolds && !gl_FrontFacing) c.rgb = mix(c.rgb, vec3(${FOLD_TINT.join(", ")}) * c.a, 0.65);
  else if (u_adaptive && !gl_FrontFacing) c *= ${STYLE.FILL_FOLD_FADE.toFixed(2)};
  o = c;
}`;

const LINE_VS = (d) => `${COMMON(d)}
in vec2 a_corner;      // x: 0 at a, 1 at b; y: -1 or 1 across
in vec2 a_a;
in vec2 a_b;
in float a_style;
#ifdef CPU_TARGETS
in vec3 a_ta;
in vec3 a_tb;
in vec4 a_jac;         // the warp's local scale at the midpoint, from the worker
#endif
out vec2 v_seg;        // along, across: device px from a, in the segment's own frame
out float v_len;
out float v_hw;
out vec4 v_color;
out vec2 v_alpha;      // alpha at a and at b, interpolated along the segment
void main() {
  vec3 A = warpScreen(a_a, TARGET(a_a, a_ta));
  vec3 B = warpScreen(a_b, TARGET(a_b, a_tb));
  vec2 pa = A.xy * u_dpr, pb = B.xy * u_dpr;
#if defined(WARP_POLAR) || defined(WARP_GRID)
  vec4 jt = fieldJacobian(0.5 * (a_a + a_b));
#elif defined(CPU_TARGETS)
  vec4 jt = a_jac;
#else
  vec4 jt = vec4(0.0);
#endif
  vec2 emph = u_adaptive ? lineEmphasis(jt, a_b - a_a) : vec2(1.0);
  float hw = 0.5 * u_width[int(a_style)] * u_dpr * emph.x;
  float m = hw + 1.0;                           // room for the anti-aliased edge
  vec2 d = pb - pa;
  float len = length(d);
  vec2 dir = len > 1e-4 ? d / len : vec2(1.0, 0.0);
  vec2 nrm = vec2(-dir.y, dir.x);
  float along = a_corner.x * len + (a_corner.x * 2.0 - 1.0) * m;
  float across = a_corner.y * m;
  vec2 p = pa + dir * along + nrm * across;
  v_seg = vec2(along, across);
  v_len = len;
  v_hw = hw;
  if (u_adaptive) emph.y *= mix(1.0, u_bent[int(a_style)], u_u);
  v_color = u_color[int(a_style)] * emph.y;
  v_alpha = vec2(A.z, B.z);
  gl_Position = clipOf(p / u_dpr);
}`;

const LINE_FS = `#version 300 es
precision highp float;
in vec2 v_seg;
in float v_len;
in float v_hw;
in vec4 v_color;
in vec2 v_alpha;
out vec4 o;
void main() {
  float t = clamp(v_seg.x, 0.0, v_len);
  float d = length(vec2(v_seg.x - t, v_seg.y));   // distance to the segment: a capsule
  float cov = clamp(v_hw + 0.5 - d, 0.0, 1.0);
  if (cov <= 0.0) discard;
  float alpha = mix(v_alpha.x, v_alpha.y, v_len > 1e-4 ? t / v_len : 0.5);
  o = v_color * cov * alpha;
}`;

const FILL_ATTRIBS = ["a_pos", "a_style", "a_t"];
const LINE_ATTRIBS = ["a_corner", "a_a", "a_b", "a_style", "a_ta", "a_tb", "a_jac"];
const BENT = new Float32Array(STYLES.map((s) => s.bent ?? 1));

// A directions-by-reach table (warp.js angularSpread) as the shader's vec4 array.
function packAngles({ a, tMax }) {
  const v = new Float32Array(ANGLE_VEC4 * 4);
  v.set(a);
  return { v, tMax };
}
function setAngleUniforms(gl, L, angles) {
  gl.uniform1i(L.u_angleOn, angles ? 1 : 0);
  if (angles) { gl.uniform4fv(L.u_angles, angles.v); gl.uniform1f(L.u_angleTMax, angles.tMax); }
}
const UNIFORMS = ["u_cam", "u_ppm", "u_view", "u_pin", "u_k", "u_u", "u_dpr", "u_color", "u_width",
  "u_showFolds", "u_foldOnly", "u_warpTex", "u_warpGrid", "u_warpSize", "u_adaptive", "u_bent", "u_angleOn", "u_angles", "u_angleTMax"];
const KINDS = ["none", "polar", "grid"];

export function sameOrigin(a, b) {
  return !!a && !!b && Math.abs(a.lon - b.lon) < 1e-12 && Math.abs(a.lat - b.lat) < 1e-12;
}

// opts: { backend: "gpu" | "cpu", force: "" | "no-vtf" | "no-r32f" | "no-webgl2", onLost, onRestored }
export function createMeshRenderer(canvas, opts = {}) {
  let gl = opts.force === "no-webgl2" ? null
    : canvas.getContext("webgl2", { antialias: true, alpha: false, premultipliedAlpha: true, preserveDrawingBuffer: false });
  const caps = probeCaps(gl, opts.force || "");
  if (!gl || !caps.webgl2) return { supported: false, caps, mode: "none" };

  let mode = opts.backend === "gpu" && caps.gpu ? "gpu" : "cpu";
  let lost = false;
  let programs = new Map();
  let passes = {};          // name → {vao, count, kind, buffers}
  let fold = null;          // the fold layer: {vao, count, buffers}
  let warpTex = null;       // {texture, kind, x0, y0, cell, nx, ny, maxMinutes, angles}
  let warpKind = "none";
  let warpOrigin = null, sceneOrigin = null;
  let quad = null;
  let evaluators = new Map();
  let styleCache = { zoom: NaN, sets: null };
  let lastStats = { drawCalls: 0, instances: 0, fillIndices: 0 };

  function buildProgram(kind, wk, cpu) {
    const d = warpDefines(wk, cpu);
    const p = kind === "fill"
      ? link(gl, FILL_VS(d), FILL_FS, { attribs: FILL_ATTRIBS })
      : link(gl, LINE_VS(d), LINE_FS, { attribs: LINE_ATTRIBS });
    return { p, L: Object.fromEntries(UNIFORMS.map((n) => [n, gl.getUniformLocation(p, n)])) };
  }

  // Compile and link every program this mode can need, now: a driver that can't build them
  // drops a rung here (gpu → cpu → unsupported), not halfway through a frame.
  function compileAll() {
    const cpu = mode === "cpu";
    const made = new Map();
    for (const kind of ["fill", "line"]) {
      for (const wk of cpu ? ["none", "cpu"] : KINDS) {
        made.set(`${kind}|${wk}`, buildProgram(kind, wk === "cpu" ? "polar" : wk, cpu && wk === "cpu"));
      }
    }
    return made;
  }

  function init() {
    programs = compileAll();
    evaluators = new Map();
    passes = {};
    fold = null;
    warpTex = null;
    warpOrigin = sceneOrigin = null;
    quad = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, quad);
    // Two triangles: (along, across) corners.
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, -1, 1, -1, 1, 1, 0, -1, 1, 1, 0, 1]), gl.STATIC_DRAW);
    // a_jac when a line buffer carries none: zero means "no local scale", not WebGL's (0,0,0,1).
    gl.vertexAttrib4f(LINE_ATTRIBS.indexOf("a_jac"), 0, 0, 0, 0);
  }
  try {
    init();
  } catch (e) {
    if (mode !== "gpu") return { supported: false, caps: { ...caps, programs: String(e.message || e) }, mode: "none" };
    mode = "cpu";
    caps.programs = String(e.message || e);
    try { init(); } catch (e2) { return { supported: false, caps: { ...caps, programs: String(e2.message || e2) }, mode: "none" }; }
  }
  const fellBack = opts.backend === "gpu" && mode !== "gpu";

  const onLost = (e) => {
    e.preventDefault();
    lost = true;
    if (opts.onLost) opts.onLost();          // the page shows the ordinary map meanwhile
  };
  const onRestored = () => {
    try { init(); } catch { return; }        // still broken: stay lost
    lost = false;
    if (opts.onRestored) opts.onRestored();  // the page sends the warp and the scene again
  };
  canvas.addEventListener("webglcontextlost", onLost);
  canvas.addEventListener("webglcontextrestored", onRestored);

  function program(kind) {
    return programs.get(`${kind}|${mode === "cpu" ? (warpKind === "none" ? "none" : "cpu") : warpKind}`);
  }

  function attrib(loc, buf, size, stride, offset, divisor) {
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, size, gl.FLOAT, false, stride, offset);
    gl.vertexAttribDivisor(loc, divisor);
  }

  function upload(data) {
    const b = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, b);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
    return b;
  }

  function freePasses() {
    for (const p of Object.values(passes)) {
      gl.deleteVertexArray(p.vao);
      for (const b of p.buffers) gl.deleteBuffer(b);
    }
    passes = {};
  }

  // The fold layer belongs to the warp, not to a scene: it survives scene uploads (partial
  // scenes, retries) and goes when the warp changes.
  function freeFold() {
    if (!fold) return;
    gl.deleteVertexArray(fold.vao);
    for (const b of fold.buffers) gl.deleteBuffer(b);
    fold = null;
  }

  function fillVao(s) {
    const vao = gl.createVertexArray();
    gl.bindVertexArray(vao);
    const pb = upload(s.pos), sb = upload(s.style || new Float32Array(s.pos.length / 2));
    const buffers = [pb, sb];
    attrib(0, pb, 2, 0, 0, 0);
    attrib(1, sb, 1, 0, 0, 0);
    if (mode === "cpu" && s.targets) { const tb = upload(s.targets); buffers.push(tb); attrib(2, tb, 3, 0, 0, 0); }
    const ib = gl.createBuffer();
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, s.index, gl.STATIC_DRAW);
    buffers.push(ib);
    gl.bindVertexArray(null);
    return { vao, buffers, count: s.index.length, kind: "fill" };
  }

  // scene: packScene() output (from the worker), with the origin it was packed around.
  function setScene(scene) {
    if (lost) return 0;
    const t0 = performance.now();
    freePasses();
    sceneOrigin = scene.origin || null;
    for (const [name, s] of Object.entries(scene.passes)) {
      if (!s.count) continue;
      if (s.kind === "fill") { passes[name] = fillVao(s); continue; }
      const vao = gl.createVertexArray();
      gl.bindVertexArray(vao);
      const buffers = [];
      {
        attrib(0, quad, 2, 0, 0, 0);
        const segb = upload(s.seg), sb = upload(s.style);
        buffers.push(segb, sb);
        attrib(1, segb, 2, 16, 0, 1);
        attrib(2, segb, 2, 16, 8, 1);
        attrib(3, sb, 1, 0, 0, 1);
        if (mode === "cpu" && s.targets) {
          const tb = upload(s.targets); buffers.push(tb);
          attrib(4, tb, 3, 24, 0, 1);
          attrib(5, tb, 3, 24, 12, 1);
          if (s.jac) { const jb = upload(s.jac); buffers.push(jb); attrib(6, jb, 4, 0, 0, 1); }
        }
      }
      gl.bindVertexArray(null);
      passes[name] = { vao, count: s.count, kind: s.kind, buffers };
    }
    return performance.now() - t0;
  }

  // The fold layer (lab): a mesh of the field's own cells, drawn only where it flips, so every
  // fold shows, not only folded parks and water. {pos, index, targets?} like a fill pass.
  function setFoldMesh(mesh) {
    if (lost) return;
    freeFold();
    if (mesh && mesh.index.length) fold = fillVao(mesh);
  }

  // warp: {kind: "polar", field, maxMinutes} | {kind: "grid", grid} | null. In the gpu
  // back-end the field (or grid) becomes a float texture; in cpu the worker has it.
  function setWarp(warp) {
    if (lost) return 0;
    const t0 = performance.now();
    if (warpTex) { gl.deleteTexture(warpTex.texture); warpTex = null; }
    freeFold();
    warpKind = warp ? warp.kind : "none";
    const src = warp ? (warp.kind === "polar" ? warp.field : warp.grid) : null;
    warpOrigin = src ? src.origin : null;
    if (mode === "gpu" && warp) {
      const src = warp.kind === "polar" ? warp.field : warp.grid;
      const data = warp.kind === "polar" ? src.minutes : src.xy;
      warpTex = {
        texture: floatTexture(gl, warp.kind, src.nx, src.ny, data), kind: warp.kind,
        x0: src.x0, y0: src.y0, cell: src.cell, nx: src.nx, ny: src.ny,
        maxMinutes: warp.kind === "polar" ? (warp.maxMinutes ?? 1e9) : 1e9,
        angles: warp.kind === "polar" && warp.angles ? packAngles(warp.angles) : null,
      };
    }
    return performance.now() - t0;
  }

  function sizeCanvas(cam) {
    const dpr = Math.min(MAX_DPR, (typeof devicePixelRatio === "number" && devicePixelRatio) || 1);
    const w = Math.max(1, Math.round(cam.width * dpr)), h = Math.max(1, Math.round(cam.height * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    return dpr;
  }

  function setUniforms(L, cam, u, dpr, styleSet, showFolds, foldOnly = false, adaptive = true) {
    gl.uniform2f(L.u_cam, cam.center[0], cam.center[1]);
    gl.uniform1f(L.u_ppm, cam.ppm);
    gl.uniform2f(L.u_view, cam.width, cam.height);
    gl.uniform2f(L.u_pin, cam.pin[0], cam.pin[1]);
    gl.uniform1f(L.u_k, cam.k);
    gl.uniform1f(L.u_u, u);
    gl.uniform1f(L.u_dpr, dpr);
    gl.uniform4fv(L.u_color, styleSet.colors);
    gl.uniform1fv(L.u_width, styleSet.widths);
    gl.uniform1i(L.u_showFolds, showFolds ? 1 : 0);
    gl.uniform1i(L.u_foldOnly, foldOnly ? 1 : 0);
    gl.uniform1i(L.u_adaptive, adaptive ? 1 : 0);
    gl.uniform1fv(L.u_bent, BENT);
    if (warpTex) {
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, warpTex.texture);
      gl.uniform1i(L.u_warpTex, 0);
      gl.uniform4f(L.u_warpGrid, warpTex.x0, warpTex.y0, warpTex.cell, warpTex.maxMinutes);
      gl.uniform2i(L.u_warpSize, warpTex.nx, warpTex.ny);
      setAngleUniforms(gl, L, warpTex.angles);
    }
  }

  function styleSets(zoom) {
    const z = Math.round(zoom * 100) / 100;
    if (styleCache.zoom === z) return styleCache.sets;
    const fill = { colors: new Float32Array(NSTYLE * 4), widths: new Float32Array(NSTYLE) };
    const casing = { colors: new Float32Array(NSTYLE * 4), widths: new Float32Array(NSTYLE) };
    STYLES.forEach((s, i) => {
      fill.colors.set(hexToRgba(s.color), i * 4);
      fill.widths[i] = widthAt(s.width, z);
      casing.colors.set(hexToRgba(s.casing || s.color), i * 4);
      casing.widths[i] = widthAt(s.casingWidth || s.width, z);
    });
    styleCache = { zoom: z, sets: { fill, casing } };
    return styleCache.sets;
  }

  // Why this camera can't be drawn with the current scene and warp, or null.
  function mismatch(cam) {
    if (sceneOrigin && cam.origin && !sameOrigin(sceneOrigin, cam.origin)) return "scene and camera have different origins";
    if (mode === "gpu" && warpOrigin && sceneOrigin && !sameOrigin(warpOrigin, sceneOrigin)) return "scene and warp have different origins";
    return null;
  }

  // Draw one frame. cam: frame.js camera; u: morph. Returns stats for this frame, or
  // {skipped} when the frame would be wrong (lost context, or origins that don't match).
  // adaptive: the scale-aware style (line widths and fades follow the bent map's local scale).
  function draw(cam, u, { showFolds = false, target = null, adaptive = true } = {}) {
    if (lost) return { skipped: "context lost" };
    const why = mismatch(cam);
    if (why) return { skipped: why };
    const dpr = sizeCanvas(cam);
    gl.bindFramebuffer(gl.FRAMEBUFFER, target);
    gl.viewport(0, 0, canvas.width, canvas.height);
    const bg = hexToRgba(BACKGROUND);
    gl.clearColor(bg[0], bg[1], bg[2], 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.CULL_FACE);
    // Triangles are wound positive in y-down local coordinates; clip space is y-up, so an
    // unfolded triangle is clockwise there.
    gl.frontFace(gl.CW);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    const sets = styleSets(cam.zoom);
    let drawCalls = 0, instances = 0, fillIndices = 0;
    let foldDrawn = false;
    for (const pass of PASSES) {
      if (pass.kind === "line" && !foldDrawn && fold && showFolds) {
        // The fold layer goes over the fills and under the lines.
        foldDrawn = true;
        const prog = program("fill");
        gl.useProgram(prog.p);
        setUniforms(prog.L, cam, u, dpr, sets.fill, false, true, adaptive);
        gl.bindVertexArray(fold.vao);
        gl.drawElements(gl.TRIANGLES, fold.count, gl.UNSIGNED_INT, 0);
        drawCalls++;
      }
      const p = passes[pass.buffer || pass.name];
      if (!p) continue;
      const prog = program(p.kind);
      gl.useProgram(prog.p);
      setUniforms(prog.L, cam, u, dpr, pass.casing ? sets.casing : sets.fill, showFolds, false, adaptive);
      gl.bindVertexArray(p.vao);
      if (p.kind === "fill") {
        gl.drawElements(gl.TRIANGLES, p.count, gl.UNSIGNED_INT, 0);
        fillIndices += p.count;
      } else {
        gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, p.count);
        instances += p.count;
      }
      drawCalls++;
    }
    gl.bindVertexArray(null);
    lastStats = { drawCalls, instances, fillIndices, dpr };
    return lastStats;
  }

  // Run warpScreen() on the GPU for points (local metres) through transform feedback, with
  // the same program code the draw uses. targets: three floats per point (cpu back-end).
  function evaluate(points, cam, u, targets = null) {
    const key = `${warpKind}|${mode}`;
    if (!evaluators.has(key)) evaluators.set(key, createEvaluator(gl, warpKind, mode === "cpu"));
    return evaluators.get(key).run(points, targets,
      { cam: cam.center, ppm: cam.ppm, view: [cam.width, cam.height], pin: cam.pin, k: cam.k, u },
      mode === "gpu" ? warpTex : null);
  }

  // Draw line segments alone, in white, into an offscreen target and read back a rectangle
  // of it (device px, y down): the checks measure widths and positions from these pixels.
  // lines: {seg: Float32Array (ax, ay, bx, by per segment, local metres), style: Float32Array,
  // targets?, jac?: Float32Array (cpu back-end)}. rect: [x, y, w, h] in device px.
  function renderLines(cam, u, lines, rect, { adaptive = true } = {}) {
    const dpr = sizeCanvas(cam);
    const fb = gl.createFramebuffer(), rb = gl.createRenderbuffer();
    gl.bindRenderbuffer(gl.RENDERBUFFER, rb);
    gl.renderbufferStorage(gl.RENDERBUFFER, gl.RGBA8, canvas.width, canvas.height);
    gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.RENDERBUFFER, rb);
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    const n = lines.seg.length / 4;
    const vao = gl.createVertexArray();
    gl.bindVertexArray(vao);
    attrib(0, quad, 2, 0, 0, 0);
    const bufs = [upload(lines.seg), upload(lines.style)];
    attrib(1, bufs[0], 2, 16, 0, 1);
    attrib(2, bufs[0], 2, 16, 8, 1);
    attrib(3, bufs[1], 1, 0, 0, 1);
    if (mode === "cpu") {
      bufs.push(upload(lines.targets));
      attrib(4, bufs[2], 3, 24, 0, 1);
      attrib(5, bufs[2], 3, 24, 12, 1);
      if (lines.jac) { bufs.push(upload(lines.jac)); attrib(6, bufs[3], 4, 0, 0, 1); }
    }
    const prog = program("line");
    gl.useProgram(prog.p);
    const sets = styleSets(cam.zoom);
    const colors = new Float32Array(NSTYLE * 4).fill(1);
    setUniforms(prog.L, cam, u, dpr, { colors, widths: sets.fill.widths }, false, false, adaptive);
    gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, n);
    const [x, y, w, h] = rect;
    const raw = new Uint8Array(w * h * 4);
    gl.readPixels(x, canvas.height - y - h, w, h, gl.RGBA, gl.UNSIGNED_BYTE, raw);
    // Flip to y down, keep the red channel as coverage 0..1.
    const cov = new Float32Array(w * h);
    for (let j = 0; j < h; j++) for (let i = 0; i < w; i++) cov[j * w + i] = raw[4 * ((h - 1 - j) * w + i)] / 255;
    gl.bindVertexArray(null);
    gl.deleteVertexArray(vao);
    for (const b of bufs) gl.deleteBuffer(b);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.deleteFramebuffer(fb);
    gl.deleteRenderbuffer(rb);
    return { cov, rect, dpr, widths: sets.fill.widths };
  }

  function stats() {
    return { ...lastStats, mode };
  }

  // Free everything this renderer made, and stop listening to the canvas. Safe to call twice.
  let destroyed = false;
  function destroy() {
    if (destroyed) return;
    destroyed = true;
    canvas.removeEventListener("webglcontextlost", onLost);
    canvas.removeEventListener("webglcontextrestored", onRestored);
    if (lost) return;
    freePasses();
    freeFold();
    for (const p of programs.values()) gl.deleteProgram(p.p);
    for (const e of evaluators.values()) e.dispose();
    if (warpTex) gl.deleteTexture(warpTex.texture);
    gl.deleteBuffer(quad);
    programs = new Map(); evaluators = new Map(); warpTex = null; quad = null;
  }

  return {
    supported: true, caps, mode, fellBack, gl,
    setScene, setWarp, setFoldMesh, draw, evaluate, renderLines, stats, destroy,
    get lost() { return lost; },
    get origins() { return { scene: sceneOrigin, warp: warpOrigin }; },
    get hasFoldLayer() { return !!fold; },
  };
}
