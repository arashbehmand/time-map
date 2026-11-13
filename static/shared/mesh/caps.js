// What this browser's WebGL2 can do for the mesh renderer, found by trying it.
//
// Back-end B (gpu) needs a float texture read in the vertex shader. That is core WebGL2
// (R32F/RG32F with NEAREST filtering, texelFetch, ≥ 16 vertex texture units), so no
// extension is asked for. Drivers still get it wrong, so the probe uploads a 2×2 texture,
// runs the real warp code over it through transform feedback, and checks the numbers.
// Float filtering (OES_texture_float_linear) and float render targets are only reported.
//
// Ladder: gpu (B) → cpu (C, no textures at all) → no mesh (the page shows the ordinary map).
import { WARP_GLSL, warpDefines } from "./warp.js";

const TF_VS = (defines) => `#version 300 es
precision highp float;
precision highp int;
${defines}
${WARP_GLSL}
in vec2 a_p;
#ifdef CPU_TARGETS
in vec3 a_t;
#endif
out vec3 v_out;
void main() {
  v_out = warpScreen(a_p, TARGET(a_p, a_t));
  gl_Position = vec4(0.0, 0.0, 0.0, 1.0);
}`;
const TF_FS = `#version 300 es
precision highp float;
out vec4 o;
void main() { o = vec4(0.0); }`;

export function compile(gl, type, src) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(s);
    gl.deleteShader(s);
    throw new Error(`shader: ${log}`);
  }
  return s;
}

export function link(gl, vs, fs, { attribs = [], varyings = null } = {}) {
  const p = gl.createProgram();
  gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, vs));
  gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, fs));
  attribs.forEach((name, i) => gl.bindAttribLocation(p, i, name));
  if (varyings) gl.transformFeedbackVaryings(p, varyings, gl.INTERLEAVED_ATTRIBS);
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(`link: ${gl.getProgramInfoLog(p)}`);
  return p;
}

// A transform-feedback evaluator of warpScreen() for one warp kind / back-end.
// run(points xy, targets|null, uniforms) → Float32Array (x, y, alpha) per point.
export function createEvaluator(gl, kind, cpuTargets) {
  const prog = link(gl, TF_VS(warpDefines(kind, cpuTargets)), TF_FS, { attribs: ["a_p", "a_t"], varyings: ["v_out"] });
  const loc = (n) => gl.getUniformLocation(prog, n);
  const L = Object.fromEntries(["u_cam", "u_ppm", "u_view", "u_pin", "u_k", "u_u", "u_warpTex", "u_warpGrid", "u_warpSize"].map((n) => [n, loc(n)]));
  return {
    prog,
    run(points, targets, U, tex = null) {
      const n = points.length / 2;
      const vao = gl.createVertexArray();
      gl.bindVertexArray(vao);
      const pb = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, pb);
      gl.bufferData(gl.ARRAY_BUFFER, points, gl.STATIC_DRAW);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
      let tb = null;
      if (cpuTargets) {
        tb = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, tb);
        gl.bufferData(gl.ARRAY_BUFFER, targets, gl.STATIC_DRAW);
        gl.enableVertexAttribArray(1);
        gl.vertexAttribPointer(1, 3, gl.FLOAT, false, 0, 0);
      }
      const out = gl.createBuffer();
      gl.bindBuffer(gl.TRANSFORM_FEEDBACK_BUFFER, out);
      gl.bufferData(gl.TRANSFORM_FEEDBACK_BUFFER, n * 12, gl.STREAM_READ);
      gl.useProgram(prog);
      gl.uniform2f(L.u_cam, U.cam[0], U.cam[1]);
      gl.uniform1f(L.u_ppm, U.ppm);
      gl.uniform2f(L.u_view, U.view[0], U.view[1]);
      gl.uniform2f(L.u_pin, U.pin[0], U.pin[1]);
      gl.uniform1f(L.u_k, U.k);
      gl.uniform1f(L.u_u, U.u);
      if (tex) {
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, tex.texture);
        gl.uniform1i(L.u_warpTex, 0);
        gl.uniform4f(L.u_warpGrid, tex.x0, tex.y0, tex.cell, tex.maxMinutes);
        gl.uniform2i(L.u_warpSize, tex.nx, tex.ny);
      }
      const tf = gl.createTransformFeedback();
      gl.bindTransformFeedback(gl.TRANSFORM_FEEDBACK, tf);
      gl.bindBufferBase(gl.TRANSFORM_FEEDBACK_BUFFER, 0, out);
      gl.enable(gl.RASTERIZER_DISCARD);
      gl.beginTransformFeedback(gl.POINTS);
      gl.drawArrays(gl.POINTS, 0, n);
      gl.endTransformFeedback();
      gl.disable(gl.RASTERIZER_DISCARD);
      gl.bindBufferBase(gl.TRANSFORM_FEEDBACK_BUFFER, 0, null);
      gl.bindTransformFeedback(gl.TRANSFORM_FEEDBACK, null);
      const result = new Float32Array(n * 3);
      gl.bindBuffer(gl.TRANSFORM_FEEDBACK_BUFFER, out);
      gl.getBufferSubData(gl.TRANSFORM_FEEDBACK_BUFFER, 0, result);
      gl.bindBuffer(gl.TRANSFORM_FEEDBACK_BUFFER, null);
      gl.bindVertexArray(null);
      gl.deleteVertexArray(vao);
      gl.deleteBuffer(pb);
      if (tb) gl.deleteBuffer(tb);
      gl.deleteBuffer(out);
      gl.deleteTransformFeedback(tf);
      return result;
    },
    dispose() { gl.deleteProgram(prog); },
  };
}

export function floatTexture(gl, kind, nx, ny, data) {
  const t = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, t);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
  if (kind === "polar") gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, nx, ny, 0, gl.RED, gl.FLOAT, data);
  else gl.texImage2D(gl.TEXTURE_2D, 0, gl.RG32F, nx, ny, 0, gl.RG, gl.FLOAT, data);
  return t;
}

function tryKind(gl, kind) {
  // 2×2 grid at (0, 0) with cell 1; sample at (0.5, 0.5): bilinear of the corners.
  const data = kind === "polar" ? new Float32Array([1, 2, 3, 4]) : new Float32Array([0, 0, 2, 0, 0, 4, 2, 4]);
  const want = kind === "polar"
    ? [Math.SQRT1_2 * 2.5, Math.SQRT1_2 * 2.5]
    : [1, 2];
  let texture = null, ev = null;
  try {
    texture = floatTexture(gl, kind, 2, 2, data);
    if (gl.getError() !== gl.NO_ERROR) return { ok: false, why: "upload" };
    ev = createEvaluator(gl, kind, false);
    const r = ev.run(new Float32Array([0.5, 0.5]), null,
      { cam: [0, 0], ppm: 1, view: [0, 0], pin: [0, 0], k: 1, u: 1 },
      { texture, x0: 0, y0: 0, cell: 1, nx: 2, ny: 2, maxMinutes: 1e9 });
    const ok = Math.abs(r[0] - want[0]) < 1e-4 && Math.abs(r[1] - want[1]) < 1e-4 && r[2] === 1;
    return ok ? { ok: true } : { ok: false, why: `read ${r[0].toFixed(4)},${r[1].toFixed(4)}` };
  } catch (e) {
    return { ok: false, why: String(e.message || e).slice(0, 120) };
  } finally {
    if (texture) gl.deleteTexture(texture);
    if (ev) ev.dispose();
  }
}

// force: "" | "no-vtf" | "no-r32f" | "no-webgl2" (the lab's ?caps= switch, to test each rung).
export function probeCaps(gl, force = "") {
  if (!gl || force === "no-webgl2") return { webgl2: false, gpu: false, cpu: false, forced: force || null };
  const exts = gl.getSupportedExtensions() || [];
  const caps = {
    webgl2: true,
    maxTextureSize: gl.getParameter(gl.MAX_TEXTURE_SIZE),
    maxVertexTextureUnits: gl.getParameter(gl.MAX_VERTEX_TEXTURE_IMAGE_UNITS),
    maxVertexAttribs: gl.getParameter(gl.MAX_VERTEX_ATTRIBS),
    maxVertexUniformVectors: gl.getParameter(gl.MAX_VERTEX_UNIFORM_VECTORS),
    floatLinear: exts.includes("OES_texture_float_linear"),        // reported, never required
    colorBufferFloat: exts.includes("EXT_color_buffer_float"),     // reported, never required
    renderer: gl.getParameter(gl.RENDERER),
    version: gl.getParameter(gl.VERSION),
    forced: force || null,
  };
  const r32f = force === "no-r32f" ? { ok: false, why: "forced" } : tryKind(gl, "polar");
  const rg32f = force === "no-r32f" ? { ok: false, why: "forced" } : tryKind(gl, "grid");
  const vtf = force !== "no-vtf" && caps.maxVertexTextureUnits >= 1;
  caps.r32f = r32f; caps.rg32f = rg32f; caps.vtf = vtf;
  caps.gpu = vtf && r32f.ok && rg32f.ok;
  caps.cpu = true;
  return caps;
}
