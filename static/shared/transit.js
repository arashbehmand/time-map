// What every page needs to know about London's public transport tables (#28): the timetable
// windows, whether a build has expired, whether a pin is inside the area they cover, and the
// server's status. Pure apart from pollStatus, so the node tests read it as the pages do.

export const WINDOWS = ["wd08", "wd13", "wd19", "sa13"];

// The timetable window nearest the time in London now (the server's default too).
export function defaultWindow(date = new Date()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/London", weekday: "short", hour: "2-digit", hourCycle: "h23",
  }).formatToParts(date).map((p) => [p.type, p.value]));
  if (parts.weekday === "Sat" || parts.weekday === "Sun") return "sa13";
  const h = Number(parts.hour) % 24;                // some engines say 24 for midnight
  return h < 11 ? "wd08" : h < 16 ? "wd13" : "wd19";
}

// The day after `validUntil` (YYYY-MM-DD), in London, the tables are expired.
export function expired(validUntil, date = new Date()) {
  if (!validUntil) return false;
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/London" }).format(date);
  return today > validUntil;
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

// "a weekday, 08:00–09:00" → "A weekday, 08:00–09:00", for a <select> or a line of text.
export const windowText = (label) => label[0].toUpperCase() + label.slice(1);

// Fill a "When" <select> with the server's windows ({id, label}) and pick `value`.
export function windowOptions(select, windows, value) {
  select.replaceChildren(...windows.map((w) => new Option(windowText(w.label), w.id)));
  if (windows.some((w) => w.id === value)) select.value = value;
  return select.value;
}

// Is the server still loading the tables or the Whole city map?
export const statusLoading = (s) => Boolean(s && (s.loading || (s.whole_city && s.whole_city.reason === "loading")));

// GET the transit status, asking again while the server is still loading (right after a
// deploy) and after a failed request (a blip, or a 429 from the per-IP limit), waiting a little
// longer each time. Resolves the last good answer (still loading if it never finished), or null
// when the server never answered.
// A request that never reached the server (no network, or the server down) is tried once more
// after a moment, then given up on, so an offline page doesn't sit on "Checking…" (Whole city
// then falls back to its kept copy, /shared/offline.js).
export async function pollStatus({ url = "/api/transit/status", tries = 10, waitMs = 3000, maxWaitMs = 30000,
                                   netTries = 2, netWaitMs = 1500 } = {}) {
  let s = null, wait = waitMs, netFailures = 0;
  for (let i = 0; i < tries; i++) {
    let failed = false;
    try {
      const resp = await fetch(url);
      if (resp.ok) s = await resp.json(); else failed = true;
    } catch {
      netFailures++;
      if (netFailures >= netTries || (typeof navigator !== "undefined" && navigator.onLine === false)) return s;
      await new Promise((r) => setTimeout(r, netWaitMs));
      continue;
    }
    if (!failed && !statusLoading(s)) return s;
    if (i + 1 < tries) await new Promise((r) => setTimeout(r, wait));
    if (failed) wait = Math.min(maxWaitMs, wait * 2);
  }
  return s;
}
