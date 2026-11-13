// The lab's benchmark (gates G6–G9) and tile census. Seeded and scripted, so a run on a
// phone and a run on a laptop do the same things. Results go to the lab's results store.
//   G6 frames         morph and pan, on a device: p50 ≤ 17 ms and p95 ≤ 33 ms on phones. The
//                     frame interval is capped by vsync, so the CPU time of each draw() and the
//                     GPU time of a frame are recorded beside it
//   G7 origin change  warm tiles, field ready, on the bent map → first frame of the new origin
//                     with the GPU finished; it includes uploading B's field texture
//   G8 new origin     cold tiles → first frame, p95 ≤ 1.5 s; only measured with ?geom=tiles,
//                     because synthetic geometry makes no request
//   G9 budget         ≤ the budget (5M vertices shaded) and ≤ 48 tiles per origin, or overBudget reported
import { tileCover } from "../../shared/mesh/cover.js";
import { localFrame } from "../../shared/mesh/frame.js";
import { foldDiagnostics, makeWarpSpec } from "./fields.js";
import { chordError, referenceCamera, REFERENCE_VIEWS, rng } from "./selftest.js";

const nextFrame = () => new Promise((r) => requestAnimationFrame(r));
const pct = (a, p) => (a.length ? [...a].sort((x, y) => x - y)[Math.min(a.length - 1, Math.floor(p * a.length))] : NaN);
const r1 = (v) => (Number.isFinite(v) ? Number(v.toFixed(1)) : v);
const r2 = (v) => (Number.isFinite(v) ? Number(v.toFixed(2)) : v);

// rAF intervals and draw() CPU times while `fn` runs.
async function recordFrames(lab, fn) {
  const times = [], draws = [];
  let last = 0;
  const off = lab.onFrame((now) => { if (last) times.push(now - last); last = now; });
  const offDraw = lab.onDraw((ms) => draws.push(ms));
  await fn();
  off();
  offDraw();
  return { times, draws };
}

// Time from asking for an origin to its first frame drawn with the GPU finished. Rejections
// (a worker or draw failure) come back as { failed }.
async function originChange(lab, id, { clear = false } = {}) {
  const t0 = performance.now();
  const gen = lab.setOrigin(id, { clear });
  const fieldMs = lab.S.requestedAt - t0;          // building the synthetic field (not timed)
  try {
    const { info, drawnAt } = await lab.waitScene(gen);
    return { ms: drawnAt - lab.S.requestedAt, fieldMs, info };
  } catch (e) {
    return { failed: String(e.message || e), fieldMs, info: lab.S.sceneInfo || {} };
  }
}

// GPU time of one bent frame, where the browser offers a timer (bench only; never required).
async function gpuFrameMs(lab, samples = 8) {
  const r = lab.S.renderer, gl = r.gl;
  const ext = gl && gl.getExtension("EXT_disjoint_timer_query_webgl2");
  if (!ext) return null;
  const out = [];
  for (let i = 0; i < samples; i++) {
    const q = gl.createQuery();
    gl.beginQuery(ext.TIME_ELAPSED_EXT, q);
    r.draw(lab.camera(), 1, {});
    gl.endQuery(ext.TIME_ELAPSED_EXT);
    for (let k = 0; k < 60 && !gl.getQueryParameter(q, gl.QUERY_RESULT_AVAILABLE); k++) await nextFrame();
    if (!gl.getParameter(ext.GPU_DISJOINT_EXT) && gl.getQueryParameter(q, gl.QUERY_RESULT_AVAILABLE)) {
      out.push(gl.getQueryParameter(q, gl.QUERY_RESULT) / 1e6);
    }
    gl.deleteQuery(q);
  }
  lab.S.dirty = true;
  return out.length ? r2(pct(out, 0.5)) : null;
}

function rendererInfo(lab) {
  const r = lab.S.renderer, gl = r && r.gl;
  let unmasked = null;
  if (gl) {
    const dbg = gl.getExtension("WEBGL_debug_renderer_info");
    if (dbg) unmasked = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL);
  }
  return { mode: r ? r.mode : "none", fellBack: !!(r && r.fellBack), renderer: r && r.caps ? r.caps.renderer : "deck.gl", unmasked, caps: r && r.caps };
}

const coldRow = (id, c, S) => ({
  origin: id, ms: c.failed ? null : r1(c.ms), failed: c.failed || null, fieldMs: r1(c.fieldMs),
  tiles: c.info.cover, fetched: c.info.requests, missing: c.info.missing, shaded: c.info.shaded, bytes: c.info.bytes,
  overBudget: c.info.overBudget, dropped: c.info.dropped, workerMs: r1(c.info.totalMs), tessMs: r1(c.info.tessMs),
  packMs: r1(c.info.packMs), uploadMs: r1(c.info.uploadMs), peakResident: c.info.peakResident,
  rings: S.cover.rings.map((r) => r.within), coarsened: S.cover.coarsened,
});

export async function runBench(lab, { origins = null, fields = ["transit", "walk", "cliff"], motionFields = ["transit"], spacings = [64, 128, 256] } = {}) {
  const { S, OPT } = lab;
  const ids = origins || S.network.origins.map((o) => o.id);
  const startField = OPT.field, startOrigin = OPT.origin, startSpacing = OPT.spacing;
  const res = {
    kind: "bench", at: new Date().toISOString(), ua: navigator.userAgent, backend: OPT.backend, geom: OPT.geom,
    spacing: OPT.spacing, screen: { w: innerWidth, h: innerHeight, dpr: devicePixelRatio }, gpu: rendererInfo(lab), runs: [],
  };
  S.benchFinish = true;
  try {
    for (const field of fields) {
      OPT.field = field;
      const run = { field, cold: [], warm: [], motion: [], chord: [] };
      await lab.morphTo(1, 0);                     // G7 and G8 time the bent map
      for (const id of ids) {
        const c = await originChange(lab, id, { clear: true });
        run.cold.push(coldRow(id, c, S));
        // Renderer diagnostics on synthetic fields, not Direction 4's verdict; and the chord
        // error (G4's measure) for this field and origin, phone view.
        const ch = c.failed ? null : chordError(lab, S.scene, referenceCamera(lab, REFERENCE_VIEWS[0]), rng(11), 3000);
        run.chord.push({ origin: id, folds: S.spec.kind === "polar" ? foldDiagnostics(S.spec) : null,
          within1px: ch && ch.within1px, p99Px: ch && ch.p99Px, nearFold: ch && ch.nearFold, crossing: ch && ch.crossingOuterMinute });
      }
      for (let i = 0; i < 20; i++) {
        const id = ids[i % ids.length];
        const w = await originChange(lab, id);
        run.warm.push({ origin: id, ms: w.failed ? null : r1(w.ms), failed: w.failed || null, workerMs: r1(w.info.totalMs), uploadMs: r1(w.info.uploadMs), cacheHits: w.info.cacheHits });
      }
      if (motionFields.includes(field)) {
        for (const id of ids) {
          await originChange(lab, id);
          await lab.morphTo(0, 0);
          const morph = await recordFrames(lab, async () => {
            for (let k = 0; k < 3; k++) { await lab.morphTo(1); await lab.morphTo(0); }
          });
          await lab.morphTo(1, 0);
          const pan = await recordFrames(lab, async () => {
            const t0 = performance.now();
            while (performance.now() - t0 < 3000) { S.map.panBy([3, 1.5], { animate: false }); await nextFrame(); }
          });
          const zoom = await recordFrames(lab, async () => {
            const z = S.map.getZoom();
            for (const dz of [1, -1]) { S.map.zoomTo(z + dz, { duration: 600 }); await new Promise((r) => S.map.once("moveend", r)); }
          });
          const gpuMs = await gpuFrameMs(lab);
          const frames = [...morph.times, ...pan.times], draws = [...morph.draws, ...pan.draws];
          run.motion.push({ origin: id, morphP50: r1(pct(morph.times, 0.5)), morphP95: r1(pct(morph.times, 0.95)),
            panP50: r1(pct(pan.times, 0.5)), panP95: r1(pct(pan.times, 0.95)), zoomP95: r1(pct(zoom.times, 0.95)),
            p50: r1(pct(frames, 0.5)), p95: r1(pct(frames, 0.95)), max: r1(Math.max(...frames)), frames: frames.length,
            drawP50Ms: r2(pct(draws, 0.5)), drawP95Ms: r2(pct(draws, 0.95)), gpuFrameMs: gpuMs });
        }
      }
      res.runs.push(run);
    }
    // Spacing sweep, transit field, Baker Street: vertices against chord error.
    OPT.field = "transit";
    res.spacingSweep = [];
    for (const div of spacings) {
      OPT.spacing = div;
      const c = await originChange(lab, ids[0]);
      const ch = chordError(lab, S.scene, referenceCamera(lab, REFERENCE_VIEWS[0]), rng(7), 6000);
      res.spacingSweep.push({ spacingDiv: div, shaded: c.info.shaded, fillVerts: c.info.fillVerts, segments: c.info.segments,
        bytes: c.info.bytes, tessMs: r1(c.info.tessMs), chord: ch });
    }
  } finally {
    S.benchFinish = false;
    OPT.spacing = startSpacing; OPT.field = startField; OPT.origin = startOrigin;
  }
  await originChange(lab, startOrigin);
  res.summary = summarise(res);
  return res;
}

function summarise(res) {
  const motion = res.runs.flatMap((r) => r.motion);
  const colds = res.runs.flatMap((r) => r.cold);
  const cold = colds.filter((c) => c.ms !== null).map((c) => c.ms);
  const warm = res.runs.flatMap((r) => r.warm.filter((w) => w.ms !== null).map((w) => w.ms));
  const failed = colds.filter((c) => c.failed).length + res.runs.reduce((s, r) => s + r.warm.filter((w) => w.failed).length, 0);
  const missing = colds.reduce((s, c) => s + (c.missing || 0), 0);
  return {
    backend: res.backend, mode: res.gpu.mode, fellBack: res.gpu.fellBack, renderer: res.gpu.unmasked || res.gpu.renderer, geom: res.geom,
    G6_frames: {
      worstP50: Math.max(...motion.map((m) => m.p50)), worstP95: Math.max(...motion.map((m) => m.p95)),
      drawP95Ms: Math.max(...motion.map((m) => m.drawP95Ms)), gpuFrameMs: Math.max(...motion.map((m) => m.gpuFrameMs ?? 0)),
      note: "frame intervals are capped by vsync; the gate is judged on the phones",
    },
    G7_originChange: { p50: r1(pct(warm, 0.5)), p95: r1(pct(warm, 0.95)), pass: failed === 0 && pct(warm, 0.95) <= 200 },
    G8_newOrigin: res.geom === "tiles"
      ? { p50: r1(pct(cold, 0.5)), p95: r1(pct(cold, 0.95)), missingTiles: missing, pass: failed === 0 && missing === 0 && pct(cold, 0.95) <= 1500 }
      : { p95NoNetwork: r1(pct(cold, 0.95)), pass: null, note: "not measured: synthetic geometry makes no request" },
    G9_budget: {
      maxShaded: Math.max(...colds.map((c) => c.shaded || 0)), maxTiles: Math.max(...colds.map((c) => c.tiles || 0)),
      overBudget: colds.some((c) => c.overBudget),
    },
    failures: failed,
    spacing: res.spacingSweep.map((s) => `spacing ${s.spacingDiv}: ${s.shaded} shaded, ${(s.chord.within1px * 100).toFixed(2)}% ≤ 1 px, p99 ${s.chord.p99Px} px`),
  };
}

// What real tiles hold around the five origins (needs a tiles token on the server).
export async function runCensus(lab) {
  const { S } = lab;
  const perOrigin = [];
  const union = new Map();
  for (const o of S.network.origins) {
    const spec = makeWarpSpec("transit", o, S.network, { maxMinutes: lab.OPT.max });
    const cover = tileCover(localFrame(o), lab.reachBox(spec, spec.maxMinutes));
    perOrigin.push({ origin: o.id, tiles: cover.tiles.map((t) => `${t.z}/${t.x}/${t.y}`), wanted: cover.wanted, coarsened: cover.coarsened });
    for (const t of cover.tiles) union.set(`${t.z}/${t.x}/${t.y}`, t);
  }
  const gen = Date.now();
  const tiles = await new Promise((resolve) => {
    S.onCensus = (msg) => {
      if (msg.gen !== gen) return;
      if (msg.type === "census-progress") { document.getElementById("out").textContent = `Census: ${msg.done}/${msg.of} tiles`; return; }
      S.onCensus = null;
      resolve(msg.tiles);
    };
    S.worker.postMessage({ type: "census", gen, tiles: [...union.values()], spacings: [64, 128, 256] });
  });
  const byKey = new Map(tiles.map((t) => [`${t.z}/${t.x}/${t.y}`, t]));
  const sum = (list, f) => list.reduce((s, t) => s + (f(t) || 0), 0);
  const layers = {}, roadClasses = {}, stationKeys = {}, stationModes = {};
  for (const t of tiles) {
    for (const [k, v] of Object.entries(t.layers || {})) layers[k] = (layers[k] || 0) + v;
    for (const [k, v] of Object.entries(t.roadClasses || {})) roadClasses[k] = (roadClasses[k] || 0) + v;
    for (const [k, v] of Object.entries(t.stationKeys || {})) stationKeys[k] = (stationKeys[k] || 0) + v;
    for (const [k, v] of Object.entries(t.stationModes || {})) stationModes[k] = (stationModes[k] || 0) + v;
  }
  const summary = {
    uniqueTiles: tiles.length, missing: tiles.filter((t) => t.missing).length,
    hasRoadLabel: tiles.some((t) => t.hasRoadLabel),
    perOrigin: perOrigin.map((p) => {
      const ts = p.tiles.map((k) => byKey.get(k)).filter(Boolean);
      return {
        origin: p.origin, tiles: p.tiles.length, wanted: p.wanted, coarsened: p.coarsened,
        kB: Math.round(sum(ts, (t) => t.bytes) / 1024), decodeMs: r1(sum(ts, (t) => t.decodeMs)),
        shaded: Object.fromEntries([64, 128, 256].map((d) => [d, sum(ts, (t) => t.vertices && t.vertices[d] && t.vertices[d].shaded)])),
      };
    }),
    rail: Object.fromEntries(Object.entries(roadClasses).filter(([k]) => /rail/.test(k))),
    stationModes,
  };
  return { kind: "census", at: new Date().toISOString(), summary, layers, roadClasses, stationKeys, tiles };
}
