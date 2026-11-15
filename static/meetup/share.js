// A meetup group as a link (#3, U-02). The group goes in the URL fragment, which the browser
// never sends to a server: #g=1&p=lat,lon,mode[,name]&p=…[&w=window&v=venue].
// Start points are rounded to 3 decimals (about 100 m) unless that person opted in to an
// exact point (5 decimals). A name goes in only if someone typed it. Search labels, answers
// and ids never go in. Pure, so tests/js/meetup-share.test.js reads the same rules.
import { WINDOWS } from "../warp/views.js";

export const VERSION = "1";
export const MAX_PEOPLE = 8;         // server cap (server/meetup/models.py)
export const MAX_NAME = 40;
// Characters after the #. Every group the page allows fits: 8 people at 5 decimals, each
// with a 40-character name of 4-byte characters (12 characters each once percent-encoded),
// is under 4,300 (tests/js/meetup-share.test.js checks it).
export const MAX_LINK = 4500;
export const MODE_CODE = { walking: "w", cycling: "c", driving: "d", transit: "t" };
const CODE_MODE = Object.fromEntries(Object.entries(MODE_CODE).map(([m, c]) => [c, m]));
export const VENUES = ["station", "pub", "bar", "cafe", "restaurant", "park"];
const KEYS = new Set(["g", "p", "w", "v"]);

export function roundCoord(x, exact = false) {
  return Number(Number(x).toFixed(exact ? 5 : 3));
}

// Control characters out, and the invisible ones that reorder or hide text (zero-width
// spaces and joiners, bidi marks, embeddings, overrides and isolates, line and paragraph
// separators, the byte-order mark). Spaces trimmed, at most MAX_NAME characters.
// eslint-disable-next-line no-control-regex
const HIDDEN = /[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]/g;
export function cleanName(name) {
  if (typeof name !== "string") return "";
  return [...name.replace(HIDDEN, "").trim()].slice(0, MAX_NAME).join("").trim();
}

// people: [{lat, lon, mode, name, named, exact}]. Returns the fragment without "#".
export function encodeGroup({ people, window = null, venue = null }) {
  const q = new URLSearchParams();
  q.set("g", VERSION);
  for (const p of people) {
    const parts = [roundCoord(p.lat, p.exact), roundCoord(p.lon, p.exact), MODE_CODE[p.mode]];
    const name = p.named ? cleanName(p.name) : "";
    if (name) parts.push(name);
    q.append("p", parts.join(","));
  }
  if (people.some((p) => p.mode === "transit")) {
    if (window) q.set("w", window);
    if (venue) q.set("v", venue);
  }
  return q.toString();
}

const strip = (hash) => (typeof hash === "string" ? hash.replace(/^#/, "") : "");

// Does this fragment claim to be a group? (An unrelated fragment is left alone.)
export function isGroupHash(hash) {
  const s = strip(hash);
  if (!s) return false;
  try { return new URLSearchParams(s).has("g"); } catch { return false; }
}

function number(s, limit) {
  if (typeof s !== "string" || !/^-?\d{1,3}(\.\d{1,7})?$/.test(s)) return null;
  const x = Number(s);
  return Number.isFinite(x) && Math.abs(x) <= limit ? x : null;
}

// {people: [{lat, lon, mode, name|null}], window|null, venue|null}, or null for anything
// that isn't a whole, valid group. All or nothing.
export function decodeGroup(hash) {
  const s = strip(hash);
  if (!s || s.length > MAX_LINK) return null;
  let q;
  try { q = new URLSearchParams(s); } catch { return null; }
  for (const k of q.keys()) if (!KEYS.has(k)) return null;
  if (q.getAll("g").length !== 1 || q.get("g") !== VERSION) return null;
  if (q.getAll("w").length > 1 || q.getAll("v").length > 1) return null;
  const rows = q.getAll("p");
  if (rows.length < 2 || rows.length > MAX_PEOPLE) return null;
  const people = [];
  for (const row of rows) {
    const [lat, lon, code, ...rest] = row.split(",");
    // Own keys only: "constructor" or "__proto__" is not a mode.
    const mode = Object.hasOwn(CODE_MODE, code) ? CODE_MODE[code] : null;
    const p = { lat: number(lat, 90), lon: number(lon, 180), mode };
    if (p.lat === null || p.lon === null || !p.mode) return null;
    const name = cleanName(rest.join(","));
    p.name = name || null;
    people.push(p);
  }
  const window = q.has("w") ? q.get("w") : null;
  const venue = q.has("v") ? q.get("v") : null;
  if (window !== null && !WINDOWS.includes(window)) return null;
  if (venue !== null && !VENUES.includes(venue)) return null;
  return { people, window, venue };
}

// What this page can give a shared group (pure, so the rules are tested on their own):
// `transitOk(person)` says whether that start may use public transport here; `windows` and
// `venues` are the time-of-day ids and kinds of place on offer. People who can't have public
// transport walk; the window and venue are kept only while someone is still on it.
// Returns {people, window, venue, walkers (indexes), windowGone, venueGone}.
export function reconcileGroup(group, { transitOk, windows = [], venues = [] }) {
  const people = group.people.map((q) => ({ ...q, mode: q.mode === "transit" && !transitOk(q) ? "walking" : q.mode }));
  const walkers = group.people.map((q, i) => (q.mode === "transit" && people[i].mode !== "transit" ? i : -1))
    .filter((i) => i >= 0);
  const onTransit = people.some((p) => p.mode === "transit");
  const windowGone = onTransit && group.window !== null && !windows.includes(group.window);
  const venueGone = onTransit && group.venue !== null && group.venue !== "station" && !venues.includes(group.venue);
  return {
    people, walkers, windowGone, venueGone,
    window: onTransit && group.window !== null && !windowGone ? group.window : null,
    venue: onTransit && group.venue !== null && !venueGone ? group.venue : null,
  };
}

// Which link the page opens at load. A link in the address bar is opened and solved. Without
// one, a link opened earlier in this tab (kept in history.state, which survives a reload and
// a phone reloading a tab it discarded) is restored without solving again. `state` is the
// page's history.state; anything that isn't a group link there is ignored.
export const STATE_KEY = "meetupGroup";
export function linkAtLoad(hash, state) {
  if (isGroupHash(hash)) return { hash, solve: true };
  const kept = state && typeof state === "object" ? state[STATE_KEY] : null;
  if (typeof kept === "string" && isGroupHash(kept)) return { hash: kept, solve: false };
  return null;
}
