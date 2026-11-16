// Module worker: fetch tiles through /tiles, decode, tessellate (cached per tile), and pack
// the current tile set into per-pass buffers. With back-end C it also evaluates the warp
// for every vertex. A newer request makes older work stale: its results are dropped, and
// fetches it no longer needs are aborted.
import { bandMesh, bandPolygons } from "./contours.js";
import { localFrame } from "./frame.js";
import { createField, createTargetGrid, geometryField, sampleField } from "./field.js";
import { censusTile, LINE_VERTS, packScene, spacingForZoom, tessellateTile, vectorTileFeatures } from "./pipeline.js";
import { TILE_URL } from "./schema-streets-v8.js";
import { meshSheetIndex } from "./hit.js";
import { bentRings, cutMask, squashLayout } from "./layouts.js";
import { ringShares } from "./diagnostics.js";
import { makeEdgeTest, MAX_DEPTH, refineFills } from "./refine.js";
import { createWarp, targetsFor } from "./warp.js";

// The server's tile limit (16 at once, server/settings.py) is per process, shared by every
// visitor; past it a request waits up to 10 s, then gets 503. Keep well under it.
const MAX_IN_FLIGHT = 8;
const RESIDENT_LIMIT = 8_000_000; // cached vertices shaded: a real scene and most of the last one
const PARTIAL_EVERY_MS = 400;     // while tiles arrive, send what has arrived this often
const RETRIES = 2;                // network errors and 5xx other than 503
const MAX_RETRY_AFTER_S = 2;      // 429/503: retry once, only if the server asks for ≤ 2 s

const cache = new Map();          // mesh key → mesh, least recently used first
const fetches = new Map();        // "z/x/y" → { promise, controller }: shared across scenes
let resident = 0, peakResident = 0;
let current = { gen: 0 };
let synthetic = null;
let censusAbort = null;

const now = () => performance.now();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const weight = (m) => m.stats.fillVerts + LINE_VERTS * m.stats.segments;
const tileId = (t) => `${t.z}/${t.x}/${t.y}`;

function remember(key, mesh) {
  if (cache.has(key)) { resident -= weight(cache.get(key)); cache.delete(key); }
  cache.set(key, mesh);
  resident += weight(mesh);
  peakResident = Math.max(peakResident, resident);
}

// Least recently used first; tiles of the current set last. A packed scene no longer needs
// its meshes, so the limit holds even when one scene alone is bigger than it.
function evict(current) {
  for (const pass of [false, true]) {
    for (const [key, m] of cache) {
      if (resident <= RESIDENT_LIMIT) return;
      if (current.has(key) !== pass) continue;
      cache.delete(key);
      resident -= weight(m);
    }
  }
}

// One fetch per tile, however many scenes want it. A newer scene aborts only the fetches it
// doesn't need, so a quick change of origin doesn't pay for the same tile twice.
function fetchShared(t, counters) {
  const id = tileId(t);
  let f = fetches.get(id);
  if (!f) {
    const controller = new AbortController();
    f = { controller, promise: fetchTile(t, controller.signal, counters).finally(() => fetches.delete(id)) };
    fetches.set(id, f);
  } else {
    counters.shared++;
  }
  return f.promise;
}

async function fetchTile(t, signal, counters) {
  counters.requests++;
  for (let attempt = 0; ; attempt++) {
    counters.attempts++;
    let resp;
    try {
      resp = await fetch(TILE_URL(t.z, t.x, t.y), { signal });
    } catch (e) {
      if (signal.aborted) throw e;
      resp = null;
    }
    if (resp && resp.status === 204) return new Uint8Array(0);
    if (resp && resp.ok) return new Uint8Array(await resp.arrayBuffer());
    if (resp && (resp.status === 429 || resp.status === 503)) {
      // Budget spent, busy or rate-limited: each retry costs the visitor's per-IP quota.
      const after = Number(resp.headers.get("Retry-After"));
      if (attempt > 0 || !(after >= 0 && after <= MAX_RETRY_AFTER_S)) return null;
      await sleep(after * 1000);
    } else {
      if ((resp && resp.status < 500) || attempt >= RETRIES) return null;
      await sleep(500 * 2 ** attempt);
    }
    if (signal.aborted) throw new DOMException("aborted", "AbortError");
  }
}

async function loadMesh(t, opts, counters) {
  const spacingDiv = spacingForZoom(t.z, opts.spacingDiv);
  const key = `${opts.source}:${spacingDiv}:${tileId(t)}`;
  const hit = cache.get(key);
  if (hit) {
    cache.delete(key); cache.set(key, hit);      // most recently used
    counters.cacheHits++;
    return { key, mesh: hit };
  }
  let features;
  const t0 = now();
  if (opts.source === "synthetic") {
    features = synthetic.syntheticFeatures(t.z, t.x, t.y);
  } else {
    const bytes = await fetchShared(t, counters);
    if (bytes === null) return { key, mesh: null };
    counters.tileBytes += bytes.byteLength;
    features = bytes.byteLength ? vectorTileFeatures(bytes) : [];
  }
  const mesh = tessellateTile(features, { z: t.z, x: t.x, y: t.y, spacingDiv });
  mesh.stats.loadMs = now() - t0;
  counters.tessMs += mesh.stats.ms;
  remember(key, mesh);
  return { key, mesh };
}

// A polar warp bends by the geometry field (uncertain patches at their drawn minutes).
const fieldFrom = (f) => geometryField(f.kind === "field" ? f : fieldOf(f));
function warpFrom(spec) {
  if (!spec) return null;
  if (spec.kind === "polar") return createWarp({ kind: "polar", field: fieldFrom(spec.field), maxMinutes: spec.maxMinutes, angles: spec.angles || null });
  if (spec.kind === "grid") return createWarp({ kind: "grid", grid: createTargetGrid(spec.grid) });
  return null;
}

// The field the mesh is refined against (refine.js): sent with every scene, back-end B too.
function refineFrom(spec) {
  if (!spec) return null;
  return { field: fieldFrom(spec.field), maxMinutes: spec.maxMinutes, eps: spec.eps, maxDepth: spec.maxDepth, far: spec.far,
           angles: spec.angles || null };
}

function post(gen, scene, extra) {
  const transfer = [];
  for (const p of Object.values(scene.passes)) {
    for (const a of [p.pos, p.style, p.index, p.seg, p.targets, p.zoom, p.jac]) if (a) transfer.push(a.buffer);
  }
  self.postMessage({ type: "scene", gen, ...extra, scene }, transfer);
}

async function buildScene(msg) {
  const { gen, origin, tiles, spacingDiv, budget, source } = msg;
  current = { gen };
  const wanted = new Set(tiles.map(tileId));
  for (const [id, f] of fetches) if (!wanted.has(id)) f.controller.abort();
  if (source === "synthetic" && !synthetic) {
    const mod = await import(msg.syntheticUrl);
    synthetic = mod.createSyntheticSource(msg.network);
  }

  const t0 = now();
  const frame = localFrame(origin);
  // Squash (layouts.js) bends by its own field: built here from the same field the band-layer
  // worker gets (filled flags included), so both build the same layout, and refined to the same
  // half pixel at its wider framing.
  if (msg.layout === "squash" && msg.refine) {
    const sq = squashLayout(fieldOf(msg.refine.field), msg.refine.maxMinutes, msg.squash || {});
    const eps = msg.refine.eps * (sq.frame / msg.refine.maxMinutes), angles = sq.spec.angles;
    msg = { ...msg, refine: { ...msg.refine, field: sq.R, maxMinutes: sq.reach, eps, far: sq.frame, angles },
            warp: msg.warp ? { ...msg.warp, field: sq.R, maxMinutes: sq.reach, angles } : null };
  }
  const warp = warpFrom(msg.warp);
  const refine = refineFrom(msg.refine);
  const opts = { source, spacingDiv };
  const counters = { requests: 0, attempts: 0, shared: 0, cacheHits: 0, tileBytes: 0, tessMs: 0 };
  const loaded = new Array(tiles.length).fill(null);
  const settled = new Array(tiles.length).fill(false);
  const keys = new Set();

  const pack = (partial) => {
    const meshes = loaded.filter(Boolean);
    const missingTiles = tiles.filter((t, i) => settled[i] && !loaded[i]).map(tileId);
    const scene = packScene(meshes, frame, { budget, warp, refine });
    scene.stats = { ...scene.stats, ...counters, cover: tiles.length, loaded: meshes.length,
                    missing: missingTiles.length, missingTiles, partial, totalMs: now() - t0,
                    resident, peakResident, backend: warp ? "cpu" : "gpu" };
    return scene;
  };

  // While tiles arrive, send what has arrived every PARTIAL_EVERY_MS (the old picture stays
  // up until the first one).
  let next = 0, sentCount = 0;
  const partialTimer = setInterval(() => {
    if (current.gen !== gen) { clearInterval(partialTimer); return; }
    const n = loaded.filter(Boolean).length;
    if (n > sentCount) { sentCount = n; post(gen, pack(true), { partial: true }); }
  }, PARTIAL_EVERY_MS);

  const workerLoop = async () => {
    while (next < tiles.length) {
      const i = next++;
      try {
        const r = await loadMesh(tiles[i], opts, counters);
        keys.add(r.key);
        loaded[i] = r.mesh;
      } catch (e) {
        if (current.gen !== gen) return;
      }
      settled[i] = true;
      if (current.gen !== gen) return;
    }
  };
  try {
    await Promise.all(Array.from({ length: Math.min(MAX_IN_FLIGHT, tiles.length) }, workerLoop));
  } finally {
    clearInterval(partialTimer);
  }
  if (current.gen !== gen) return;               // superseded: drop it
  const scene = pack(false);
  evict(keys);
  scene.stats.resident = resident;
  post(gen, scene, { partial: false });
}

async function census(msg) {
  if (censusAbort) censusAbort.abort();
  const abort = censusAbort = new AbortController();
  const out = [];
  for (const t of msg.tiles) {
    if (abort.signal.aborted) return;
    const counters = { requests: 0, attempts: 0 };
    let bytes;
    try { bytes = await fetchTile(t, abort.signal, counters); } catch { return; }
    if (!bytes || !bytes.byteLength) { out.push({ ...t, missing: !bytes }); continue; }
    const c = censusTile(bytes, t.z);
    c.vertices = {};
    for (const base of msg.spacings || [32, 64, 128]) {
      const m = tessellateTile(vectorTileFeatures(bytes), { z: t.z, x: t.x, y: t.y, spacingDiv: spacingForZoom(t.z, base) });
      c.vertices[base] = { fillVerts: m.stats.fillVerts, segments: m.stats.segments, shaded: weight(m), ms: m.stats.ms };
    }
    out.push({ ...t, ...c });
    self.postMessage({ type: "census-progress", gen: msg.gen, done: out.length, of: msg.tiles.length });
  }
  self.postMessage({ type: "census", gen: msg.gen, tiles: out });
}

// The warp page's field work, off its main thread (#5, W-02):
//   bands         the normal map's travel-time bands as GeoJSON (contours.js), for one field
//                 and one set of ring levels
//   layers        the bent view's band mesh, refined like the map (refine.js), with its CPU
//                 targets when the renderer needs them; the overlap index (hit.js) is built
//                 from the same triangles, so a tap reads what the bands are drawn with. A burst
//                 of layers requests is coalesced to the newest, and the last few views' results
//                 and indexes are kept, so a tap on the view still shown finds its index while a
//                 newer one is prepared, and returning to it costs nothing
//   sheets-query  what is under a finger, from the index of the view it names
// Errors come back as {type: "error", job, key, id, gen}.
const SHEETS_KEEP = 4;
const sheetsByKey = new Map();
let queuedLayers = null;

function fieldOf(spec) {
  const f = createField(spec);
  if (spec.filled) f.filled = spec.filled;
  if (spec.uncertain) { f.uncertain = spec.uncertain; f.drawn = spec.drawn || spec.minutes; }
  return f;
}

function fieldMessage(msg) {
  if (msg.type === "bands") {
    const f = fieldOf(msg.field);
    const fc = bandPolygons(f, localFrame(f.origin), msg.levels, { skipFilled: true });
    self.postMessage({ type: "bands-result", id: msg.id, key: msg.key, fc });
  } else if (msg.type === "layers") {
    const kept = sheetsByKey.get(msg.key);
    if (kept && kept.result) {
      sheetsByKey.delete(msg.key); sheetsByKey.set(msg.key, kept);       // most recent last
      self.postMessage(kept.result.message, kept.result.copy());
      return;
    }
    // Coalesce: a newer layers request that arrives while this one waits replaces it.
    const first = !queuedLayers;
    queuedLayers = msg;
    if (first) setTimeout(() => { const m = queuedLayers; queuedLayers = null; runGuarded(m, () => buildLayers(m)); }, 0);
  } else {
    const kept = sheetsByKey.get(msg.key);
    const hits = kept && kept.index ? kept.index.query(msg.lx, msg.ly, msg.r) : null;
    const uncertain = !!(kept && kept.unsure && kept.unsure.query(msg.lx, msg.ly, msg.r).length);
    self.postMessage({ type: "sheets-result", id: msg.id, key: msg.key, hits, uncertain });
  }
}

function buildLayers(msg) {
    const ms = {}, tick = (k, t0) => { ms[k] = Math.round(now() - t0); return now(); };
    let t0 = now();
    const f = fieldOf(msg.field);
    // Squash (layouts.js) bends by its own field, out to its own reach, with its own by-reach
    // table, and draws bent rings; its R, reach, frame and table go back to the page, which bends
    // its labels the same way. The band minutes are always the field's own.
    const sq = msg.layout === "squash" ? squashLayout(f, msg.maxMinutes, msg.squash || {}) : null;
    const angles = sq ? sq.spec.angles : msg.angles || null;
    const bendBy = sq ? sq.R : geometryField(f), reach = sq ? sq.reach : msg.maxMinutes;
    const frame = sq ? sq.frame : msg.maxMinutes;
    const eps = msg.eps * (frame / msg.maxMinutes);
    const warp = createWarp({ kind: "polar", field: bendBy, maxMinutes: reach, angles });
    t0 = tick("squash", t0);
    const m = bandMesh(f, msg.box);
    const n = m.pos.length / 2;
    const r = refineFills({ pos: m.pos, index: m.index, style: new Float32Array(n), zoom: new Uint8Array(n) },
      makeEdgeTest(bendBy, reach, eps, angles, { far: sq ? sq.frame : Infinity }), { maxDepth: MAX_DEPTH });
    t0 = tick("bandMesh", t0);
    const minutes = new Float32Array(r.pos.length / 2);
    minutes.set(m.minutes);
    for (let v = n; v < minutes.length; v++) minutes[v] = sampleField(f, r.pos[2 * v], r.pos[2 * v + 1]);
    const mesh = { pos: r.pos, minutes, index: r.index };
    const targets = msg.cpu ? targetsFor(warp, mesh.pos) : null;
    // The rings through the first place that many minutes away on the true minutes, stepping
    // over the cut (walled-off) patches.
    const lines = sq ? bentRings(sq.ringSource, warp, msg.levels) : null;
    t0 = tick("rings", t0);
    // What the page states squash gives up (W-03): the places within the outer ring's minutes
    // drawn past it, counted as the lab's checks count them (diagnostics.js ringShares).
    const outerLine = lines ? lines.find((l) => l.minutes === msg.maxMinutes) : null;
    const shares = outerLine ? ringShares(warp, f, msg.maxMinutes, outerLine, { cut: cutMask(f, sq) }) : null;
    const ringCounts = shares ? { pastRing: shares.outside, slowInside: shares.slowInsideExCut, judged: shares.counts.judged } : null;
    t0 = tick("shares", t0);
    const message = { type: "layers-result", key: msg.key, refined: r.added, skipped: r.skipped, reach, frame, lines, ms,
                      angles: sq ? sq.spec.angles : null, ringCounts };
    // The index keeps pos and minutes, so the page gets copies.
    const copy = () => {
      const out = { pos: mesh.pos.slice(), minutes: mesh.minutes.slice(), index: mesh.index.slice(), targets: targets ? targets.slice() : null,
                    R: sq ? sq.R.minutes.slice() : null };
      Object.assign(message, out);
      return [out.pos.buffer, out.minutes.buffer, out.index.buffer, ...(out.targets ? [out.targets.buffer] : []), ...(out.R ? [out.R.buffer] : [])];
    };
    // The band layer goes first; the overlap index (only needed by a tap) is built right after,
    // and a tap that comes in meanwhile waits for it in the worker's queue.
    const entry = { index: null, unsure: null, result: { message, copy } };
    sheetsByKey.set(msg.key, entry);
    while (sheetsByKey.size > SHEETS_KEEP) sheetsByKey.delete(sheetsByKey.keys().next().value);
    self.postMessage(message, copy());
    try {
      entry.index = meshSheetIndex(mesh, warp, { maxMinutes: msg.maxMinutes });   // the bands' outer ring
      // The uncertain patches: drawn, uncoloured, and a tap there says so.
      if (f.uncertain) entry.unsure = meshSheetIndex(bandMesh(f, msg.box, { uncertainOnly: true }), warp);
    } catch (err) {
      self.postMessage({ type: "error", job: "sheets-index", key: msg.key, gen: msg.gen ?? -1, message: String(err && err.message || err) });
    }
}

function runGuarded(msg, fn) {
  try { fn(); } catch (err) {
    self.postMessage({ type: "error", job: msg.type, key: msg.key, id: msg.id, gen: msg.gen ?? -1, message: String(err && err.message || err) });
  }
}

self.onmessage = (e) => {
  const msg = e.data;
  if (msg.type === "bands" || msg.type === "layers" || msg.type === "sheets-query") {
    runGuarded(msg, () => fieldMessage(msg));
    return;
  }
  if (msg.type === "scene") buildScene(msg).catch((err) => self.postMessage({ type: "error", job: "scene", gen: msg.gen, message: String(err && err.message || err) }));
  else if (msg.type === "clear") { cache.clear(); resident = 0; peakResident = 0; self.postMessage({ type: "cleared", gen: msg.gen }); }
  else if (msg.type === "census") census(msg).catch((err) => self.postMessage({ type: "error", gen: msg.gen, message: String(err && err.message || err) }));
};
