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
  if (bent && layout === "squash") return `Each bent ring runs through the first place that many minutes away in each direction, a ring ${spacingWords(plan)}. The space between two rings grows with the places they add, so a wide band means many more places.`;
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
  squash: "Squash: going outward, places almost never come back closer, so the map hardly overlaps. The middle is drawn as it is; further out, distance isn't minutes, and the rings bend to follow them. Colour is always each place's own time: many quicker places near stations are drawn beyond their ring, a few much slower patches that are hard to get into are drawn inside one, and patches the route data leaves out (behind tracks, say) are drawn small and left uncoloured. Zoom out to see the outer rings.",
};

export const SQUASH_TAP_NOTE = "These places are drawn close together; each has its own time.";
export const BANDS_FAILED = "The travel-time bands couldn't be drawn. Move the pin to try again.";
export const OVERLAP_NOTE = "Where the map overlaps itself, different places are the same number of minutes away in the same direction.";

export function minutesText(m) {
  return m < 1 ? "under a minute" : `about ${Math.round(m)} min`;
}

// An uncertain patch (field.js): the route data leaves it out, so it has no time to show; it is
// left uncoloured, and drawn small on the bent map.
export const UNCERTAIN_TAP = "No reliable travel time here: the route data leaves this patch out.";

// What a tap on the ordinary map says.
export function tapGeographic(node, label) {
  if (node && node.uncertain) return UNCERTAIN_TAP;
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
