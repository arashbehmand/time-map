// Decode → classify → tessellate one tile, and pack a set of tiles into per-pass buffers.
// Pure (no fetch, no DOM), so node tests run the same steps the worker does.
import { PbfReader } from "../vendor/pbf/index.js";
import { VectorTile, classifyRings } from "../vendor/vector-tile/index.js";
import { tilePlacement } from "./frame.js";
import { classify, NEAR_FIELD_M, PASSES } from "./schema-streets-v8.js";
import { enforceBudget, gridFill, lineSegments } from "./tessellate.js";
import { targetsFor } from "./warp.js";

export const DEFAULT_SPACING_DIV = 128;  // cells per z12 tile edge: about 48 m in London (the size-weighted chord check fails at 64)
export const DEFAULT_BUDGET = 300_000;   // vertices shaded: fill vertices + 6 per line segment (D19)

const now = () => (typeof performance !== "undefined" ? performance.now() : Date.now());

// Feature source for tessellateTile: yields {layer, props, type, extent, geometry()}.
export function* vectorTileFeatures(bytes) {
  const vt = new VectorTile(new PbfReader(bytes));
  for (const name of Object.keys(vt.layers)) {
    const layer = vt.layers[name];
    for (let i = 0; i < layer.length; i++) {
      const f = layer.feature(i);
      yield { layer: name, props: f.properties, type: f.type, extent: layer.extent, geometry: () => f.loadGeometry() };
    }
  }
}

// features: an iterable from vectorTileFeatures() or the lab's synthetic source.
export function tessellateTile(features, { z, x, y, spacingDiv = DEFAULT_SPACING_DIV }) {
  const t0 = now();
  const fills = [], lines = [], labels = [];
  let extent = 4096, seen = 0, fillVerts = 0, segments = 0;
  for (const f of features) {
    seen++;
    const c = classify(f.layer, f.props, f.type, z);
    if (!c) continue;
    extent = f.extent || extent;
    const cell = extent / spacingDiv;
    const geom = f.geometry();
    if (c.kind === "fill") {
      for (const poly of classifyRings(geom)) {
        const verts = [], index = [];
        const area = gridFill(poly, extent, cell, verts, index);
        if (!index.length) continue;
        fills.push({ style: c.style, cat: c.cat, area, verts: Float32Array.from(verts), index: Uint32Array.from(index) });
        fillVerts += verts.length / 2;
      }
    } else if (c.kind === "line") {
      const segs = [];
      for (const line of geom) lineSegments(line, extent, cell, segs);
      if (!segs.length) continue;
      lines.push({ style: c.style, cat: c.cat, pass: c.pass, segs: Float32Array.from(segs) });
      segments += segs.length / 4;
    } else if (c.kind === "label") {
      for (const pts of geom) {
        for (const p of pts) {
          // Half-open, so a label in the buffer zone is kept by one tile only.
          if (p.x >= 0 && p.x < extent && p.y >= 0 && p.y < extent) {
            labels.push({ type: c.type, text: c.text, rank: c.rank, ex: p.x, ey: p.y });
          }
        }
      }
    }
  }
  return {
    z, x, y, extent, spacingDiv, fills, lines, labels,
    stats: { features: seen, fills: fills.length, lines: lines.length, labels: labels.length, fillVerts, segments, ms: now() - t0 },
  };
}

// Pack tile meshes into one buffer per pass, in local metres around `frame`'s origin; the
// scene says which origin that is, so it can never be drawn with another origin's warp.
// With `warp` (back-end C), also writes per-vertex placements. Applies the budget, counted in
// vertices shaded: one per fill vertex, six per line segment (one instanced quad).
export const LINE_VERTS = 6;

export function packScene(meshes, frame, { budget = DEFAULT_BUDGET, warp = null } = {}) {
  const t0 = now();
  const items = [];
  const bboxDist = (xs, ys) => Math.hypot((xs[0] + xs[1]) / 2, (ys[0] + ys[1]) / 2);
  for (const m of meshes) {
    const p = tilePlacement(frame, m.z, m.x, m.y, m.extent);
    const a2 = p.scale * p.scale;
    for (const f of m.fills) {
      let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      for (let j = 0; j < f.verts.length; j += 2) {
        x0 = Math.min(x0, f.verts[j]); x1 = Math.max(x1, f.verts[j]);
        y0 = Math.min(y0, f.verts[j + 1]); y1 = Math.max(y1, f.verts[j + 1]);
      }
      const dist = bboxDist([p.ox + x0 * p.scale, p.ox + x1 * p.scale], [p.oy + y0 * p.scale, p.oy + y1 * p.scale]);
      items.push({ m, p, f, kind: "fill", cat: f.cat, count: f.verts.length / 2, area: f.area * a2, dist });
    }
    for (const f of m.lines) {
      let segs = f.segs;
      if (f.cat === "secondary") {
        // Secondary roads only in the near field, decided by distance, not by tile zoom.
        const keep = [];
        for (let j = 0; j < segs.length; j += 4) {
          const mx = p.ox + ((segs[j] + segs[j + 2]) / 2) * p.scale, my = p.oy + ((segs[j + 1] + segs[j + 3]) / 2) * p.scale;
          if (Math.hypot(mx, my) <= NEAR_FIELD_M) keep.push(segs[j], segs[j + 1], segs[j + 2], segs[j + 3]);
        }
        if (!keep.length) continue;
        segs = Float32Array.from(keep);
      }
      let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      for (let j = 0; j < segs.length; j += 2) {
        x0 = Math.min(x0, segs[j]); x1 = Math.max(x1, segs[j]);
        y0 = Math.min(y0, segs[j + 1]); y1 = Math.max(y1, segs[j + 1]);
      }
      const dist = bboxDist([p.ox + x0 * p.scale, p.ox + x1 * p.scale], [p.oy + y0 * p.scale, p.oy + y1 * p.scale]);
      items.push({ m, p, f, segs, kind: "line", cat: f.cat, count: LINE_VERTS * (segs.length / 4), area: 0, dist });
    }
  }
  const b = enforceBudget(items, budget);

  const fillPasses = { water: [], park: [] };
  const linePasses = { rail: [], road: [] };
  for (let i = 0; i < items.length; i++) {
    if (!b.keep[i]) continue;
    const it = items[i];
    if (it.kind === "fill") fillPasses[it.cat === "water" ? "water" : "park"].push(it);
    else linePasses[it.f.pass].push(it);
  }

  const passes = {};
  for (const [name, list] of Object.entries(fillPasses)) {
    let nv = 0, ni = 0;
    for (const it of list) { nv += it.f.verts.length / 2; ni += it.f.index.length; }
    const pos = new Float32Array(nv * 2), style = new Float32Array(nv), index = new Uint32Array(ni);
    const zoom = new Uint8Array(nv);
    let v = 0, k = 0;
    for (const it of list) {
      const { verts, index: idx } = it.f, { ox, oy, scale } = it.p;
      for (let j = 0; j < idx.length; j++) index[k++] = idx[j] + v;
      for (let j = 0; j < verts.length / 2; j++, v++) {
        pos[2 * v] = ox + verts[2 * j] * scale;
        pos[2 * v + 1] = oy + verts[2 * j + 1] * scale;
        style[v] = it.f.style;
        zoom[v] = it.m.z;
      }
    }
    passes[name] = { kind: "fill", pos, style, index, zoom, count: ni };
    if (warp) passes[name].targets = targetsFor(warp, pos);
  }
  for (const [name, list] of Object.entries(linePasses)) {
    let ns = 0;
    for (const it of list) ns += it.segs.length / 4;
    const seg = new Float32Array(ns * 4), style = new Float32Array(ns), zoom = new Uint8Array(ns);
    let s = 0;
    for (const it of list) {
      const { segs } = it, { ox, oy, scale } = it.p;
      for (let j = 0; j < segs.length; j += 4, s++) {
        seg[4 * s] = ox + segs[j] * scale;
        seg[4 * s + 1] = oy + segs[j + 1] * scale;
        seg[4 * s + 2] = ox + segs[j + 2] * scale;
        seg[4 * s + 3] = oy + segs[j + 3] * scale;
        style[s] = it.f.style;
        zoom[s] = it.m.z;
      }
    }
    passes[name] = { kind: "line", seg, style, zoom, count: ns };
    if (warp) {
      // Two placements per segment, a then b, three floats each: the same layout as `seg`
      // read as endpoint pairs, so targetsFor can fill it in one pass.
      passes[name].targets = targetsFor(warp, seg, new Float32Array(ns * 6));
    }
  }

  // Labels: local metres; one per name within 300 m (a station is often in several layers).
  const labels = [];
  for (const m of meshes) {
    const p = tilePlacement(frame, m.z, m.x, m.y, m.extent);
    for (const l of m.labels) {
      const x = p.ox + l.ex * p.scale, y = p.oy + l.ey * p.scale;
      if (labels.some((o) => o.text === l.text && Math.hypot(o.x - x, o.y - y) < 300)) continue;
      labels.push({ type: l.type, text: l.text, rank: l.rank, x, y });
    }
  }

  let fillVerts = 0, segments = 0, bytes = 0;
  for (const p of Object.values(passes)) {
    if (p.kind === "fill") fillVerts += p.pos.length / 2;
    else segments += p.count;
    for (const a of [p.pos, p.style, p.index, p.seg, p.targets, p.zoom]) if (a) bytes += a.byteLength;
  }
  return {
    origin: { lon: frame.origin.lon, lat: frame.origin.lat },
    passes, labels,
    stats: {
      tiles: meshes.length, fillVerts, segments, shaded: fillVerts + LINE_VERTS * segments, bytes,
      dropped: b.dropped, overBudget: b.overBudget, packMs: now() - t0,
    },
  };
}

// Cells per tile edge at zoom z, for one ground cell at every zoom: `base` cells per z12 tile
// edge (128 is about 48 m in London). The cell lattices of neighbouring zooms then coincide,
// so a z10 tile and a z14 tile put the same vertices on their shared edge.
export function spacingForZoom(z, base = DEFAULT_SPACING_DIV) {
  return Math.max(1, Math.round(base * Math.pow(2, 12 - z)));
}

// The pass list the renderer draws, with buffers named.
export const DRAW_ORDER = PASSES;

// What one real tile holds, for the census (lab ?census=1).
export function censusTile(bytes, z) {
  const t0 = now();
  const vt = new VectorTile(new PbfReader(bytes));
  const decodeMs = now() - t0;
  const layers = {}, roadClasses = {}, stationKeys = {}, stationModes = {};
  for (const name of Object.keys(vt.layers)) {
    const layer = vt.layers[name];
    layers[name] = layer.length;
    for (let i = 0; i < layer.length; i++) {
      const f = layer.feature(i);
      if (name === "road") {
        const k = `${f.properties.class}|${f.properties.structure ?? "none"}`;
        roadClasses[k] = (roadClasses[k] || 0) + 1;
      }
      if (name === "transit_stop_label") {
        for (const key of Object.keys(f.properties)) stationKeys[key] = (stationKeys[key] || 0) + 1;
        const k = `${f.properties.mode}|${f.properties.stop_type}`;
        stationModes[k] = (stationModes[k] || 0) + 1;
      }
    }
  }
  return { bytes: bytes.byteLength, decodeMs, layers, roadClasses, stationKeys, stationModes, hasRoadLabel: "road_label" in vt.layers, z };
}
