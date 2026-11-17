// What the warp page offers (#5, W-02): the ways of travelling, how far each can show, which
// windows of the timetable, and which may bend. Pure, so the tests read the same table.

export const MODES = {
  walking: { label: "Walk", outer: [15, 30, 45, 60], defaultOuter: 30 },
  cycling: { label: "Cycle", outer: [15, 30, 45, 60], defaultOuter: 30 },
  driving: { label: "Drive", outer: [15, 30, 45, 60], defaultOuter: 30 },
  transit: { label: "Public transport", outer: [30, 45, 60, 90, 120], defaultOuter: 45 },
};

// The time map (the bent view) is experimental everywhere. Public transport draws it with squash
// (layoutFor, below), which passed W-03's checks at all five test origins, four windows and both
// directions settings (#27, docs/warp-time-polar.md); its exact fold still fails Direction 4.
// A mode set to "hidden" is offered only with ?polar=1.
export const TIME_POLAR = { walking: "experimental", cycling: "experimental", driving: "experimental", transit: "experimental" };

export function bendOffered(mode, query = "") {
  return TIME_POLAR[mode] !== "hidden" || new URLSearchParams(query).get("polar") === "1";
}

// Directions on the bent map: by reach (the default, the owner's choice) or true compass.
export const DIRECTIONS = { reach: 0.5, compass: 0 };
export const DEFAULT_DIRECTIONS = "reach";

// How the bent map handles places that a fast line makes as quick as nearer ones (layouts.js).
// Public transport draws squash (W-03, #27): the fold overlaps itself along every fast line.
// Walking, cycling and driving keep the fold, whose rings are exact. A pick (?layout=…, or the
// trial chips that ?layouts=1 shows) wins.
export const LAYOUTS = ["fold", "squash"];
export function layoutFor(mode, pick = null) {
  if (LAYOUTS.includes(pick)) return pick;
  return mode === "transit" ? "squash" : "fold";
}

// The windows, expiry and coverage helpers every page shares live in /shared/transit.js (#28).
export { WINDOWS, defaultWindow, expired, insideArea } from "../shared/transit.js";

export const MORPH_MS = 1800;     // ordinary → bent
export const FADE_MS = 300;       // the ordinary map dissolves into the mesh before it moves
export const RING_FROM_U = 0.9;   // the exact rings appear over the last tenth of the morph
export const BAND_OPACITY = 0.38;

// The bent view is framed at k (px per minute) and may zoom in two levels more (k × 4), no
// further. The mesh is refined to half a pixel one level in (k × 2): a pixel at the deepest
// zoom. Refining for two levels quadrupled the work on central London (and filled the budget).
export const BENT_ZOOM_IN = 2;
export const REFINE_ZOOM_IN = 1;
export function refineEps(kFit) {
  return 0.5 / (kFit * 2 ** REFINE_ZOOM_IN);
}

export const fieldKey = (lon, lat, mode, window) =>
  `${lon.toFixed(4)},${lat.toFixed(4)},${mode},${mode === "transit" ? window : ""}`;
