// When it serves this file, the server fills in the build id below with a hash of the
// shipped files (server/app.py), so every deploy that changes them gets a fresh cache and
// the old one is deleted.
const CACHE_NAME = "time-map-__BUILD__";

// Same-origin app files only. Nothing from another site is ever cached here, and /api/,
// /tiles/ and /glyphs/ go to the network, except Whole city's data, which is kept after a view
// (cityData, below), never precached: a server without a build still installs.
const APP_SHELL = [
  "/",
  "/manifest.json",
  "/shared/ui.css",
  "/shared/api.js",
  "/shared/latest.js",
  "/shared/handoff.js",
  "/shared/locate.js",
  "/shared/nav.js",
  "/shared/offline.js",
  "/shared/tools.js",
  "/shared/transit.js",
  "/meet-up/",
  "/meet-up/style.css",
  "/meet-up/app.js",
  "/meet-up/words.js",
  "/meet-up/share.js",
  "/meet-up/example.json",
  "/from-here/",
  "/from-here/style.css",
  "/from-here/app.js",
  "/from-here/views.js",
  "/from-here/words.js",
  "/from-here/example-field.json",
  "/whole-city/",
  "/whole-city/style.css",
  "/whole-city/app.js",
  "/whole-city/carry.js",
  // The mesh renderer the warp page bends the map with, its worker and their decoders.
  "/shared/mesh/caps.js",
  "/shared/mesh/contours.js",
  "/shared/mesh/cover.js",
  "/shared/mesh/diagnostics.js",
  "/shared/mesh/field.js",
  "/shared/mesh/fit.js",
  "/shared/mesh/frame.js",
  "/shared/mesh/hit.js",
  "/shared/mesh/layouts.js",
  "/shared/mesh/labels.js",
  "/shared/mesh/pipeline.js",
  "/shared/mesh/refine.js",
  "/shared/mesh/renderer.js",
  "/shared/mesh/rings.js",
  "/shared/mesh/schema-streets-v8.js",
  "/shared/mesh/tessellate.js",
  "/shared/mesh/warp.js",
  "/shared/mesh/worker.js",
  "/shared/vendor/earcut/earcut.js",
  "/shared/vendor/pbf/index.js",
  "/shared/vendor/point-geometry/index.js",
  "/shared/vendor/vector-tile/index.js",
  "/icons/mapbox-logo.svg",
];
const SHELL = new Set(APP_SHELL);

self.addEventListener("install", (e) => {
  // cache: "reload" skips the browser's HTTP cache, so a new build never stores old files.
  e.waitUntil(caches.open(CACHE_NAME).then((cache) =>
    cache.addAll(APP_SHELL.map((path) => new Request(path, { cache: "reload" })))));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k.startsWith("time-map-") && k !== CACHE_NAME).map((k) => caches.delete(k)),
      ))
      .then(() => self.clients.claim()),
  );
});

// Network first: a deploy shows up on the next load. The cache answers only when the
// network fails (offline).
async function networkFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    // cache: "no-cache" revalidates with the server (a 304 when unchanged), never a stale copy.
    const response = await fetch(request, { cache: "no-cache" });
    if (response.ok && response.type === "basic") await cache.put(request, response.clone());
    return response;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw err;
  }
}

// Whole city's data (#28), kept for offline use once it has been viewed: the one tool that needs
// nothing else from the network. Network first, so online the server's gate decides every time:
// a refusal (its tables were withdrawn, say) drops everything kept. Offline, the page draws the
// kept build whatever its date, saying how old its timetable is (whole-city/app.js). Its own
// cache name, so a new app build doesn't throw the map away.
const CITY_CACHE = "whole-city-data";
const CITY_DATA = "/api/whole-city/";

async function cityData(request) {
  const cache = await caches.open(CITY_CACHE);
  let response;
  try {
    response = await fetch(request);
  } catch (err) {
    const kept = await cache.match(request);
    if (kept) return kept;
    throw err;
  }
  if (response.ok && response.type === "basic") await cache.put(request, response.clone());
  else if (response.status === 404) await caches.delete(CITY_CACHE);
  return response;
}

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  if (url.origin !== self.location.origin) return;   // other sites: not ours to cache
  if (url.pathname.startsWith(CITY_DATA)) { e.respondWith(cityData(e.request)); return; }
  if (!SHELL.has(url.pathname)) return;               // the rest of the API, tiles, glyphs
  e.respondWith(networkFirst(e.request));
});
