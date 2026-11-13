// What the warp page offers (#5, W-02): the ways of travelling, how far each can show, which
// windows of the timetable, and which may bend. Pure, so the tests read the same table.

export const MODES = {
  walking: { label: "Walk", outer: [15, 30, 45, 60], defaultOuter: 30 },
  cycling: { label: "Cycle", outer: [15, 30, 45, 60], defaultOuter: 30 },
  driving: { label: "Drive", outer: [15, 30, 45, 60], defaultOuter: 30 },
  transit: { label: "Public transport", outer: [30, 45, 60, 90, 120], defaultOuter: 45 },
};

// The bent (time) view is experimental everywhere. For public transport it stays hidden
// until Direction 4's checks pass at all five test origins (docs/warp-time-polar.md);
// ?polar=1 shows it anyway, for the comprehension test.
export const TIME_POLAR = { walking: "experimental", cycling: "experimental", driving: "experimental", transit: "hidden" };

export function bendOffered(mode, query = "") {
  return TIME_POLAR[mode] !== "hidden" || new URLSearchParams(query).get("polar") === "1";
}

// Directions on the bent map: by reach (the default, the owner's choice) or true compass.
export const DIRECTIONS = { reach: 0.5, compass: 0 };
export const DEFAULT_DIRECTIONS = "reach";

export const WINDOWS = ["wd08", "wd13", "wd19", "sa13"];

// The timetable window nearest the time in London now (the server's default too).
export function defaultWindow(date = new Date()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/London", weekday: "short", hour: "numeric", hourCycle: "h23",
  }).formatToParts(date).map((p) => [p.type, p.value]));
  if (parts.weekday === "Sat" || parts.weekday === "Sun") return "sa13";
  const h = Number(parts.hour);
  return h < 11 ? "wd08" : h < 16 ? "wd13" : "wd19";
}

export const MORPH_MS = 1800;     // ordinary → bent
export const FADE_MS = 300;       // the ordinary map dissolves into the mesh before it moves
export const RING_FROM_U = 0.9;   // the exact rings appear over the last tenth of the morph
export const BAND_OPACITY = 0.38;

// The mesh is refined to half a pixel at the largest px-per-minute it is drawn at: the outer
// ring at 0.42 of a 1440 × 900 view's short side.
export function refineEps(outer) {
  const kMax = (0.42 * 900) / outer;
  return 0.5 / kMax;
}

// Is (lon, lat) inside a GeoJSON Polygon or MultiPolygon (lon/lat)?
export function insideArea(geom, lon, lat) {
  if (!geom) return false;
  const polys = geom.type === "Polygon" ? [geom.coordinates] : geom.type === "MultiPolygon" ? geom.coordinates : [];
  const inRing = (ring) => {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i], [xj, yj] = ring[j];
      if ((yi > lat) !== (yj > lat) && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  };
  return polys.some((p) => inRing(p[0]) && !p.slice(1).some(inRing));
}

export const fieldKey = (lon, lat, mode, window) =>
  `${lon.toFixed(4)},${lat.toFixed(4)},${mode},${mode === "transit" ? window : ""}`;
