// What one tool hands the next (#28): the pin, the way of travelling and the time of day, so
// From here's pin can be Meet up's first person and the other way round.
//
// It lives in sessionStorage: this tab only, gone when the tab closes, never sent anywhere (a
// URL would put people's start points in server and edge logs; README, "Privacy rules"). The pin
// is rounded to 4 decimals (about 11 m). Anything that doesn't read back as a valid record is
// ignored, and storage that throws (a private window, blocked site data) means start fresh.
import { WINDOWS } from "./transit.js";

export const HANDOFF_KEY = "minutes-away-handoff";
export const MODES = ["walking", "cycling", "driving", "transit"];

const round4 = (x) => Math.round(x * 1e4) / 1e4;

const isCoord = (x, max) => typeof x === "number" && Number.isFinite(x) && Math.abs(x) <= max;

function cleanPin(p) {
  if (!p || typeof p !== "object") return null;
  const { lon, lat } = p;
  if (!isCoord(lon, 180) || !isCoord(lat, 90)) return null;
  const label = typeof p.label === "string" ? p.label.slice(0, 80) : "";
  return { lon: round4(lon), lat: round4(lat), label };
}

// The parts of a record that are valid: {pin?, mode?, when?}.
export function cleanHandoff(raw) {
  const out = {};
  if (!raw || typeof raw !== "object" || raw.v !== 1) return out;
  const pin = cleanPin(raw.pin);
  if (pin) out.pin = pin;
  if (MODES.includes(raw.mode)) out.mode = raw.mode;
  if (WINDOWS.includes(raw.when)) out.when = raw.when;
  return out;
}

function storage() {
  try { return window.sessionStorage; } catch { return null; }
}

export function readHandoff(store = storage()) {
  try { return cleanHandoff(JSON.parse(store.getItem(HANDOFF_KEY))); } catch { return {}; }
}

// Merge `patch` into the record: {pin: null} forgets the pin, and the way of travelling with it
// (a mode only means something for a pin); an invalid value is dropped.
export function writeHandoff(patch, store = storage()) {
  const next = { ...readHandoff(store), ...patch };
  if (patch.pin) next.pin = cleanPin(patch.pin);
  if (patch.pin === null) delete next.mode;
  const record = { v: 1, ...cleanHandoff({ v: 1, ...next }) };
  try { store.setItem(HANDOFF_KEY, JSON.stringify(record)); } catch { /* storage blocked: tools start fresh */ }
  return cleanHandoff(record);
}

// Meet up's group, kept in the same tab so a trip to another tool and back through the switcher
// finds it again (the page itself holds it in memory only). Same rules as the hand-off: this tab
// only, never sent anywhere, anything invalid dropped.
export const GROUP_KEY = "minutes-away-group";
const MAX_PEOPLE = 8;

export function cleanGroup(raw) {
  if (!raw || typeof raw !== "object" || raw.v !== 1 || !Array.isArray(raw.people)) return null;
  const people = [];
  for (const p of raw.people.slice(0, MAX_PEOPLE)) {
    if (!p || typeof p !== "object" || !isCoord(p.lon, 180) || !isCoord(p.lat, 90) || !MODES.includes(p.mode)) continue;
    people.push({
      lon: p.lon, lat: p.lat, mode: p.mode,
      name: typeof p.name === "string" ? p.name.slice(0, 40) : "",
      named: p.named === true,
      example: p.example === true,        // the saved example's people are never handed on
      label: typeof p.label === "string" ? p.label.slice(0, 80) : "",
    });
  }
  if (!people.length) return null;
  return {
    people,
    window: WINDOWS.includes(raw.window) ? raw.window : undefined,
    venue: typeof raw.venue === "string" ? raw.venue.slice(0, 20) : undefined,
  };
}

export function readGroup(store = storage()) {
  try { return cleanGroup(JSON.parse(store.getItem(GROUP_KEY))); } catch { return null; }
}

// Save the group, or forget it (null or no people).
export function writeGroup(group, store = storage()) {
  try {
    const clean = group && cleanGroup({ v: 1, ...group });
    if (clean) store.setItem(GROUP_KEY, JSON.stringify({ v: 1, ...clean }));
    else store.removeItem(GROUP_KEY);
  } catch { /* storage blocked: the group lives in the page only */ }
}
