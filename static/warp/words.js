// Every sentence the warp page shows (#5, W-02). Pure. No jargon: tests/test_static.py
// checks these strings, and tests/js/warp-words.test.js checks what they say.
import { spacing } from "../shared/mesh/rings.js";

const VERBS = { walking: "walk", cycling: "cycle", driving: "drive", transit: "get" };

export function fromLine(origin) {
  return `From: ${origin.label}${origin.example ? " (example)" : ""}`;
}

// "every 5 minutes up to 30, then every 10 up to 60, then every 15", from a ring plan.
export function spacingWords(plan) {
  const sp = spacing(plan);
  return sp.map((s, i) => `${i ? "then every " + s.step : "every " + s.step + " minutes"}${sp[i + 1] ? " up to " + sp[i + 1].from : ""}`).join(", ");
}

export function sentence(mode, outer, plan, bent, layout = "fold") {
  if (bent && layout === "squash") return `Each bent ring runs through the nearest places that many minutes away in every direction: a ring ${spacingWords(plan)}. The map is bent so that nothing overlaps.`;
  if (bent) return `Distance from the pin now means travel time: a ring ${spacingWords(plan)}.`;
  return `The coloured bands show where you can ${VERBS[mode]} to within ${outer} minutes, a band ${spacingWords(plan)}.`;
}

export function limits(field) {
  if (!field) return "";
  if (field.mode === "transit") {
    const parts = [`Rough estimates from the timetable for ${field.window_label}.`,
                   "Walking near the pin, then trains, trams and buses."];
    if ((field.notes || []).includes("near_field_missing")) parts.push("Walking near the pin is estimated from the timetable this time.");
    return parts.join(" ");
  }
  if (field.mode === "walking") return "Walking at 4 km/h on streets and paths, up to 60 minutes. Times between the lines are estimated.";
  const how = field.mode === "cycling" ? "Cycling" : "Driving";
  return `${how} times are estimates, up to 60 minutes. Times between the lines are estimated.`;
}

export const EXAMPLE_NOTE = "This is a saved example. Drag the pin or search to see your own.";
export const NO_TRANSIT = "Public transport times aren't available here yet.";
export const LONDON_ONLY = "Public transport times cover London only, so this shows walking.";
export const DEV_ONLY = "Development only: these public transport times are not licensed for publication.";
export const BEND_LABEL = { off: "Bend the map (experimental)", on: "Back to the normal map" };
export const NO_BEND = "This browser can't bend the map. The bands still show on the normal map.";
export const PREPARING = "Preparing the bent map…";
export const TILES_MISSING = "Some map tiles didn't load. Trying again…";
export const TILES_FAILED = "The map tiles didn't load, so the map can't bend right now.";
export const BEND_FAILED = "The bent map couldn't be prepared. Press the button to try again.";
export const EXPIRED = "These public transport times have expired, so this shows walking.";
export const UPDATING = "Updating…";

export function beyond(outer, layout = "fold") {
  if (layout === "squash") return `Places more than ${outer} minutes away are left uncoloured.`;
  return `Places more than ${outer} minutes away aren't shown.`;
}

export const DIRECTION_NOTES = {
  reach: "By reach: directions with more places in reach get more room, so compass directions bend. Minutes stay exact.",
  compass: "True compass: every place keeps its direction from the pin. Fast lines squeeze into thin wedges.",
};

export const LAYOUT_NOTES = {
  fold: "Fold: every place sits at its own minutes. Where a fast line makes far places as quick as nearer ones, the map overlaps itself.",
  squash: "Squash: going outward, places never come back closer, so nothing overlaps. Distance from the pin isn't minutes any more, so the rings bend to follow them. Quick spots near stations, beyond a ring, show by their colour.",
  islands: "Islands: the ground around each station you reach by train or bus is cut out and drawn as a small map on the ring of its time. The gaps between islands are slow ground.",
};

export const OVERLAP_NOTE = "Where the map overlaps itself, different places are the same number of minutes away in the same direction.";

export function minutesText(m) {
  return m < 1 ? "under a minute" : `about ${Math.round(m)} min`;
}

// What a tap on the ordinary map says.
export function tapGeographic(node, label) {
  if (!node || node.minutes === -1) return "No travel time here.";
  if (node.filled) return "No street here (water or parkland).";
  if (node.minutes >= 1e4) return "More than 3 hours, or no route in the timetable.";
  return `${minutesText(node.minutes)[0].toUpperCase()}${minutesText(node.minutes).slice(1)} from ${label}.`;
}

export function tapHeading(sheets) {
  if (!sheets.length) return "Nothing drawn here.";
  if (sheets.length === 1) return "This spot is";
  return `These ${sheets.length} places are all drawn here:`;
}

const COMPASS = ["east", "south-east", "south", "south-west", "west", "north-west", "north", "north-east"];

// A place with no name: its distance and direction from the pin (local metres, y south).
export function unnamedPlace(x, y) {
  const d = Math.hypot(x, y);
  if (d < 100) return "next to the pin";
  const k = Math.round(Math.atan2(y, x) / (Math.PI / 4));
  const dir = COMPASS[(k + 8) % 8];
  return d < 1000 ? `${Math.round(d / 50) * 50} m ${dir} of the pin` : `${(d / 1000).toFixed(1)} km ${dir} of the pin`;
}
