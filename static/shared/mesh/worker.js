// Module worker: fetch tiles through /tiles, decode, tessellate (cached per tile), and pack
// the current tile set into per-pass buffers. With back-end C it also evaluates the warp
// for every vertex. A newer request makes older work stale: its results are dropped, and
// fetches it no longer needs are aborted.
import { bandMesh, bandPolygons } from "./contours.js";
import { localFrame } from "./frame.js";
import { createField, createTargetGrid, sampleField } from "./field.js";
import { censusTile, LINE_VERTS, packScene, spacingForZoom, tessellateTile, vectorTileFeatures } from "./pipeline.js";
import { TILE_URL } from "./schema-streets-v8.js";
import { meshSheetIndex } from "./hit.js";
import { timeLines } from "./layouts.js";
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

function warpFrom(spec) {
  if (!spec) return null;
  if (spec.kind === "polar") return createWarp({ kind: "polar", field: createField(spec.field), maxMinutes: spec.maxMinutes, angles: spec.angles || null });
  if (spec.kind === "grid") return createWarp({ kind: "grid", grid: createTargetGrid(spec.grid) });
  return null;
}

// The field the mesh is refined against (refine.js): sent with every scene, back-end B too.
function refineFrom(spec) {
  if (!spec) return null;
  return { field: createField(spec.field), maxMinutes: spec.maxMinutes, eps: spec.eps, maxDepth: spec.maxDepth,
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
    const scene = packScene(meshes, frame, { budget, warp, refine, cut: msg.cut || null });
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
//                 from the same triangles, so a tap reads what the bands are drawn with
//   sheets-query  what is under a finger, from the last layers built
let sheets = null;

function fieldOf(spec) {
  const f = createField(spec);
  if (spec.filled) f.filled = spec.filled;
  return f;
}

function fieldMessage(msg) {
  if (msg.type === "bands") {
    const f = fieldOf(msg.field);
    const fc = bandPolygons(f, localFrame(f.origin), msg.levels, { skipFilled: true });
    self.postMessage({ type: "bands-result", id: msg.id, key: msg.key, fc });
  } else if (msg.type === "layers") {
    if (sheets && sheets.key === msg.key && sheets.result) {
      self.postMessage(sheets.result.message, sheets.result.copy());
      return;
    }
    const f = fieldOf(msg.field);
    // The layout's own warp when it isn't the plain fold (layouts.js): squash bends by another
    // field, islands by a target grid. The band minutes are always the field's own.
    const warp = msg.warp ? warpFrom(msg.warp)
      : createWarp({ kind: "polar", field: f, maxMinutes: msg.maxMinutes, angles: msg.angles || null });
    const m = bandMesh(f, msg.box, { mask: msg.mask || null });
    const n = m.pos.length / 2;
    const bendBy = msg.warp && msg.warp.kind === "polar" ? createField(msg.warp.field) : f;
    const r = msg.eps
      ? refineFills({ pos: m.pos, index: m.index, style: new Float32Array(n), zoom: new Uint8Array(n) },
        makeEdgeTest(bendBy, msg.maxMinutes, msg.eps, msg.angles || null), { maxDepth: MAX_DEPTH })
      : { pos: m.pos, index: m.index, added: 0, skipped: 0 };
    const minutes = new Float32Array(r.pos.length / 2);
    minutes.set(m.minutes);
    for (let v = n; v < minutes.length; v++) minutes[v] = sampleField(f, r.pos[2 * v], r.pos[2 * v + 1]);
    const mesh = { pos: r.pos, minutes, index: r.index };
    const targets = msg.cpu ? targetsFor(warp, mesh.pos) : null;
    const message = { type: "layers-result", key: msg.key, refined: r.added, skipped: r.skipped,
                      lines: msg.lines ? timeLines(f, warp, msg.lines) : null };
    // The index keeps pos and minutes, so the page gets copies.
    const copy = () => {
      const out = { pos: mesh.pos.slice(), minutes: mesh.minutes.slice(), index: mesh.index.slice(), targets: targets ? targets.slice() : null };
      Object.assign(message, out);
      return [out.pos.buffer, out.minutes.buffer, out.index.buffer, ...(out.targets ? [out.targets.buffer] : [])];
    };
    sheets = { key: msg.key, index: meshSheetIndex(mesh, warp), result: { message, copy } };
    self.postMessage(message, copy());
  } else {
    const hits = sheets && sheets.key === msg.key ? sheets.index.query(msg.lx, msg.ly, msg.r) : null;
    self.postMessage({ type: "sheets-result", id: msg.id, key: msg.key, hits });
  }
}

self.onmessage = (e) => {
  const msg = e.data;
  if (msg.type === "bands" || msg.type === "layers" || msg.type === "sheets-query") {
    try { fieldMessage(msg); } catch (err) { self.postMessage({ type: "error", gen: msg.gen ?? -1, key: msg.key, message: String(err && err.message || err) }); }
    return;
  }
  if (msg.type === "scene") buildScene(msg).catch((err) => self.postMessage({ type: "error", gen: msg.gen, message: String(err && err.message || err) }));
  else if (msg.type === "clear") { cache.clear(); resident = 0; peakResident = 0; self.postMessage({ type: "cleared", gen: msg.gen }); }
  else if (msg.type === "census") census(msg).catch((err) => self.postMessage({ type: "error", gen: msg.gen, message: String(err && err.message || err) }));
};
