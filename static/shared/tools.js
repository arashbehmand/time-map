// The app and its three tools (#28): one name for each, the question it answers, and the words
// every tool shares. Pure, so the node tests read the same table.

export const APP_NAME = "Minutes Away";

export const TOOLS = {
  "from-here": {
    name: "From here", path: "/from-here/",
    question: "How far can I get from here?",
  },
  "meet-up": {
    name: "Meet up", path: "/meet-up/",
    question: "Where should we meet?",
  },
  "whole-city": {
    name: "Whole city", path: "/whole-city/",
    question: "What does London look like when distance means travel time?",
  },
};

// One label for each way of travelling, in every tool.
export const MODE_LABEL = { walking: "Walk", cycling: "Cycle", driving: "Drive", transit: "Public transport" };

// The two maps, in every tool.
export const MAP_WORDS = { real: "Real map", time: "Time map" };

// A measured number of minutes: "12 min", or "under a minute".
export function minutesText(min) {
  return min < 1 ? "under a minute" : `${Math.round(min)} min`;
}

const UNPUBLISHED = ["licences_not_cleared", "override_refused_here", "development_only"];
const ABSENT = ["not_configured", "missing", "missing_manifest"];

// Why Whole city can't be opened, in plain words (the reasons are the server's,
// server/transit/whole_city.py and server/transit/provider.py).
export function cityUnavailable(reason) {
  if (reason === "loading") return "Checking for Whole city…";
  if (reason === "offline") return "You're offline, and Whole city hasn't been opened here before. Open it once online to keep it.";
  if (UNPUBLISHED.includes(reason)) return "Whole city isn't published yet.";
  if (ABSENT.includes(reason)) return "Whole city isn't on this server yet.";
  return "Whole city isn't available right now.";
}

// The same, short enough for the switcher on a phone.
export function cityShort(reason) {
  if (UNPUBLISHED.includes(reason)) return "not published yet";
  if (ABSENT.includes(reason)) return "not here yet";
  return "not available";
}
