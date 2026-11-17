// Whole city offline (#28). The service worker keeps the map's files after a view (sw.js,
// cityData). Beside them the Whole city page keeps the status it was shown with, so an installed
// app opens Whole city without the network: whatever its date (nothing expires, #30; the page
// says services may have changed past the build's valid-until day), and only the times of day
// whose files were kept. Online, the server decides every time, and a refusal drops all of it
// (here, and in the service worker).

export const CITY_CACHE = "whole-city-data";      // the service worker's name too
const KEPT_STATUS = "/api/whole-city/offline-status";

// The kept status, as /api/transit/status's whole_city part, with only the kept windows and
// `offline: true`; null when there is none, it is unreadable (then it is dropped), or there is no
// Cache API (a page that isn't secure, or storage blocked).
export async function keptCity() {
  return (await keptCityWhy()).city;
}

// The same, with why there is none: "offline" (nothing kept).
export async function keptCityWhy() {
  const none = { city: null, reason: "offline" };
  try {
    if (!(await caches.has(CITY_CACHE))) return none;
    const cache = await caches.open(CITY_CACHE);
    const kept = await cache.match(KEPT_STATUS);
    if (!kept) return none;
    const c = await kept.json();
    if (!c || !c.available || !Array.isArray(c.windows)) {
      await caches.delete(CITY_CACHE);
      return none;
    }
    // Only the windows viewed under this build: a window kept from an older build is never
    // offered with this build's dates.
    const viewed = Array.isArray(c.viewed) ? c.viewed : [];
    const windows = [];
    for (const w of c.windows) {
      if (viewed.includes(w.id) && await cache.match(`/api/whole-city/${w.id}/timemap.json`)) windows.push(w);
    }
    return windows.length ? { city: { ...c, windows, offline: true }, reason: "" } : none;
  } catch { return none; }
}

const FILES = ["timemap.json", "land.bin", "report.md", "shepard.svg",
  // the travel modes (#23), fetched when first switched on
  "modes/modes.json", "modes/combo-car.json", "modes/combo-car-train.json", "modes/roads.bin", "modes/report.md"];

// Keep the status a window was shown with (`win`, its files just fetched through the service
// worker). Windows viewed earlier under the same build stay offered; when the build has
// changed, the other windows' files are from the old build, so they are dropped.
export async function keepCity(c, win) {
  try {
    const cache = await caches.open(CITY_CACHE);
    const old = await cache.match(KEPT_STATUS);
    const prev = old ? await old.json() : null;
    const same = prev && prev.build === c.build && Array.isArray(prev.viewed);
    if (prev && !same) {
      for (const w of prev.viewed || c.windows.map((x) => x.id)) {
        if (w === win) continue;
        for (const f of FILES) await cache.delete(`/api/whole-city/${w}/${f}`);
      }
    }
    const viewed = [...new Set([...(same ? prev.viewed : []), win])];
    await cache.put(KEPT_STATUS, new Response(JSON.stringify({ ...c, viewed }),
                                              { headers: { "Content-Type": "application/json" } }));
  } catch { /* nothing kept: offline, the page says it can't be shown */ }
}

// Drop one window's kept travel-mode files (#23): made with another build of its map.
export async function forgetModes(win) {
  try {
    const cache = await caches.open(CITY_CACHE);
    for (const f of FILES) if (f.startsWith("modes/")) await cache.delete(`/api/whole-city/${win}/${f}`);
  } catch { /* nothing kept */ }
}

export async function forgetCity() {
  try { await caches.delete(CITY_CACHE); } catch { /* nothing to forget */ }
}

// The status a page should go by: the server's, or, with no answer at all, the kept one
// ({whole_city} only). Online, a Whole city the server refuses (not still loading) is forgotten.
export async function cityOrKept(status) {
  if (!status) {
    const kept = await keptCity();
    return kept ? { whole_city: kept } : null;
  }
  const c = status.whole_city;
  if (c && !c.available && c.reason !== "loading") await forgetCity();
  return status;
}
