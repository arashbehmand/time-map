// Label policy for the mesh view (D17) and the tap index (D18). Pure: the page passes a
// text-measuring function and draws the result on its 2D overlay.
//
//   candidates  stations (the quicker first within a rank), then places by symbolrank (lower
//               is more important), from the decoded tiles, so places that start off screen
//               still get labels
//   placement   greedy, by priority, for what is on screen now (call it every drawn frame);
//               a station gets a dot and its label beside it (right, else left), a place is
//               centred on its point; every box must fit inside the view and clear the
//               obstacles (the pin) and the labels already placed, with a 4 px gap
//   hidden      a place whose anchor is folded on screen (det ≤ 0) or stretched beyond 3:1.
//               Stations are never hidden for that: a fast line folds the map right at its
//               stations, so hiding them would hide what transit is about.
//   tap index   every candidate, placed or not, so a tap can list all places under a finger

export const LABEL_CAP = { phone: 40, desktop: 80 };
export const MAX_STRETCH = 3;
export const GAP = 4;
export const DOT_OFFSET = 7;      // from a station's dot to its label
export const TAP_RADIUS = 12;
const TYPE_ORDER = { station: 0, place: 1 };

// Within a rank, a station reached sooner goes first (minutesOf, when given), then by name.
export function sortCandidates(labels, minutesOf = null) {
  const mins = new Map();
  const m = (l) => {
    if (!minutesOf || l.type !== "station") return 0;
    if (!mins.has(l)) { const t = minutesOf(l.x, l.y); mins.set(l, t >= 0 ? t : Infinity); }
    return mins.get(l);
  };
  return [...labels].sort((a, b) =>
    (TYPE_ORDER[a.type] ?? 9) - (TYPE_ORDER[b.type] ?? 9) || a.rank - b.rank || m(a) - m(b) ||
    (a.text < b.text ? -1 : a.text > b.text ? 1 : 0) || a.x - b.x || a.y - b.y);
}

const overlaps = (a, b, gap) => a[0] - gap < b[2] && a[2] + gap > b[0] && a[1] - gap < b[3] && a[3] + gap > b[1];

// labels: [{type, text, rank, x, y}] in local metres (from packScene), already sorted.
// screenOf(x, y) → {x, y, alpha}; jacobianOf(x, y) → {det, stretch} | null, for the map as
// it is on screen now. measure(text, type) → width px. obstacles: boxes no label may cover.
// Returns placed labels with their anchor (sx, sy), text box and whether to draw a dot.
export function placeLabels(labels, { screenOf, jacobianOf, measure, minutesOf, width, height, cap, obstacles = [] }) {
  const placed = [];
  const boxes = [];
  for (const l of labels) {
    if (placed.length >= cap) break;
    const s = screenOf(l.x, l.y);
    if (s.alpha < 0.5) continue;
    if (s.x < 0 || s.y < 0 || s.x > width || s.y > height) continue;
    const station = l.type === "station";
    if (!station) {
      const j = jacobianOf(l.x, l.y);
      if (j && (j.det <= 0 || j.stretch > MAX_STRETCH)) continue;
    }
    const minutes = station && minutesOf ? minutesOf(l.x, l.y) : null;
    const text = minutes !== null && minutes >= 0 ? `${l.text} · ${Math.round(minutes)} min` : l.text;
    const w = measure(text, l.type), h = station ? 14 : 13;
    // Right of the dot, else left; then further out on each side (clears the pin when the
    // station is the origin itself).
    const far = DOT_OFFSET + 8;
    const tries = station
      ? [[s.x + DOT_OFFSET, s.y - h / 2], [s.x - DOT_OFFSET - w, s.y - h / 2], [s.x + far, s.y - h / 2], [s.x - far - w, s.y - h / 2]]
      : [[s.x - w / 2, s.y - h / 2]];
    for (const [bx, by] of tries) {
      const box = [bx, by, bx + w, by + h];
      if (box[0] < 0 || box[1] < 0 || box[2] > width || box[3] > height) continue;
      if (obstacles.some((o) => overlaps(box, o, 0))) continue;
      if (boxes.some((b) => overlaps(box, b, GAP))) continue;
      boxes.push(box);
      placed.push({ ...l, label: text, sx: s.x, sy: s.y, alpha: s.alpha, box, dot: station });
      break;
    }
  }
  return placed;
}

// Screen-space grid of every candidate, for "what is under my finger".
export function buildTapIndex(labels, screenOf, { cell = 24 } = {}) {
  const grid = new Map();
  const all = [];
  for (const l of labels) {
    const s = screenOf(l.x, l.y);
    if (s.alpha < 0.5) continue;
    const e = { l, sx: s.x, sy: s.y };
    all.push(e);
    const key = `${Math.floor(s.x / cell)},${Math.floor(s.y / cell)}`;
    if (!grid.has(key)) grid.set(key, []);
    grid.get(key).push(e);
  }
  return {
    size: all.length,
    // Everything within `radius` px of (x, y), nearest first.
    query(x, y, radius = TAP_RADIUS) {
      const out = [];
      const c0 = Math.floor((x - radius) / cell), c1 = Math.floor((x + radius) / cell);
      const r0 = Math.floor((y - radius) / cell), r1 = Math.floor((y + radius) / cell);
      for (let cx = c0; cx <= c1; cx++) {
        for (let cy = r0; cy <= r1; cy++) {
          for (const e of grid.get(`${cx},${cy}`) || []) {
            const d = Math.hypot(e.sx - x, e.sy - y);
            if (d <= radius) out.push({ ...e.l, distance: d });
          }
        }
      }
      return out.sort((a, b) => a.distance - b.distance);
    },
  };
}
