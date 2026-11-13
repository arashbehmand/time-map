// Which tiles to load for an origin: a quadtree of non-overlapping tiles over what the field
// can reach, finer near the origin. It depends on the field, not the screen, because the bent
// map can draw a place 20 km away next to the pin. Pure.
import { tilePlacement, tileSizeMetres } from "./frame.js";

// Distance rings, nearest first: a tile is split into four while it is coarser than the ring
// its nearest point falls in. So the cover also holds tiles at the zooms in between (a z12
// tile touching the 1 km ring becomes four z13 tiles, some of which split again). The zoom
// only sets how generalised the source geometry is: the mesh cell is the same size at every
// zoom (pipeline.js, spacingForZoom). The census (lab ?geom=tiles&census=1) fixes these
// numbers; the plan's first guess (z14 to 2 km) wanted 51-78 tiles for the five test origins.
export const DEFAULT_RINGS = [
  { within: 1000, z: 14 },
  { within: 10000, z: 12 },
  { within: Infinity, z: 10 },
];
export const DEFAULT_TILE_CAP = 48;

function wantZoom(dist, rings) {
  for (const r of rings) if (dist <= r.within) return r.z;
  return rings[rings.length - 1].z;
}

// Nearest distance (metres) from the origin (local 0, 0) to a tile's square.
function tileDistance(frame, z, x, y) {
  const p = tilePlacement(frame, z, x, y, 1);
  const s = tileSizeMetres(frame, z);
  const dx = Math.max(p.ox, 0, -(p.ox + s));
  const dy = Math.max(p.oy, 0, -(p.oy + s));
  return Math.hypot(dx, dy);
}

// bbox: local metres [minX, minY, maxX, maxY] (usually the field's extent).
// Over the cap, the rings shrink (finer zooms reach less far) until it fits; only if even
// that fails are the farthest tiles cut, and `capped` says so.
export function tileCover(frame, bbox, { rings = DEFAULT_RINGS, cap = DEFAULT_TILE_CAP } = {}) {
  let r = rings, coarsened = 0, out = coverOnce(frame, bbox, r);
  while (out.length > cap && coarsened < 8) {
    r = r.map((ring) => ({ ...ring, within: ring.within * 0.8 }));
    coarsened++;
    out = coverOnce(frame, bbox, r);
  }
  const capped = out.length > cap;
  return { tiles: capped ? out.slice(0, cap) : out, wanted: out.length, capped, coarsened, rings: r };
}

function coverOnce(frame, bbox, rings) {
  const zMin = Math.min(...rings.map((r) => r.z));
  const n = Math.pow(2, zMin);
  const m = frame.m;
  // bbox → tile index range at zMin
  const tx = (x) => Math.floor((frame.mx0 + x / m) * n);
  const ty = (y) => Math.floor((frame.my0 + y / m) * n);
  const x0 = Math.max(0, tx(bbox[0])), x1 = Math.min(n - 1, tx(bbox[2]));
  const y0 = Math.max(0, ty(bbox[1])), y1 = Math.min(n - 1, ty(bbox[3]));

  const overlaps = (z, x, y) => {
    const p = tilePlacement(frame, z, x, y, 1);
    const s = tileSizeMetres(frame, z);
    return p.ox < bbox[2] && p.ox + s > bbox[0] && p.oy < bbox[3] && p.oy + s > bbox[1];
  };

  const out = [];
  const visit = (z, x, y) => {
    if (!overlaps(z, x, y)) return;
    const d = tileDistance(frame, z, x, y);
    if (z < wantZoom(d, rings)) {
      for (let i = 0; i < 4; i++) visit(z + 1, 2 * x + (i & 1), 2 * y + (i >> 1));
    } else {
      out.push({ z, x, y, dist: d });
    }
  };
  for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) visit(zMin, x, y);

  out.sort((a, b) => a.dist - b.dist || a.z - b.z || a.x - b.x || a.y - b.y);
  return out;
}

export const tileKey = (t) => `${t.z}/${t.x}/${t.y}`;
