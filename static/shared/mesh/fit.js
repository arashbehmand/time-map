// Framing a warp (moved from the lab so the warp page shares it). Pure.
//   fitFor    the zoom that puts most of the reachable area (90th percentile distance) on
//             screen, and k (CSS px per minute) that puts the outer ring at 0.42 of the
//             view's short side
//   reachBox  what the tiles must cover: the reachable area plus a margin for the fade-out
//             (a 45-minute walk needs a few kilometres, not the whole field), in local metres
import { WORLD_PX } from "./frame.js";

export const RING_SHARE = 0.42;
export const REACH_MARGIN_M = 1000;

function reachable(spec, maxMinutes, fn, step = 2) {
  const f = spec.field;
  for (let j = 0; j < f.ny; j += step) for (let i = 0; i < f.nx; i += step) {
    const t = f.minutes[j * f.nx + i];
    if (t >= 0 && t <= maxMinutes) fn(f.x0 + i * f.cell, f.y0 + j * f.cell);
  }
}

function extent(src) {
  return [src.x0, src.y0, src.x0 + (src.nx - 1) * src.cell, src.y0 + (src.ny - 1) * src.cell];
}

export function fitFor(spec, frame, width, height, maxMinutes) {
  let R = 12000;
  if (spec.kind === "polar") {
    const d = [];
    reachable(spec, maxMinutes, (x, y) => d.push(Math.hypot(x, y)));
    d.sort((a, b) => a - b);
    if (d.length) R = Math.max(500, d[Math.floor(d.length * 0.9)]);
  }
  const short = Math.min(width, height);
  const ppm = (0.45 * short) / R;
  const zoom = Math.log2((ppm * frame.m) / WORLD_PX);
  return { zoom, k: (RING_SHARE * short) / maxMinutes };
}

export function reachBox(spec, maxMinutes, margin = REACH_MARGIN_M) {
  const [ex0, ey0, ex1, ey1] = extent(spec.kind === "polar" ? spec.field : spec.grid);
  if (spec.kind !== "polar") return [ex0, ey0, ex1, ey1];
  let x0 = 0, y0 = 0, x1 = 0, y1 = 0;
  reachable(spec, maxMinutes, (x, y) => { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); });
  return [Math.max(ex0, x0 - margin), Math.max(ey0, y0 - margin), Math.min(ex1, x1 + margin), Math.min(ey1, y1 + margin)];
}
