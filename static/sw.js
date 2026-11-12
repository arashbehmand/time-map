// When it serves this file, the server fills in the build id below with a hash of the
// shipped files (server/app.py), so every deploy that changes them gets a fresh cache and
// the old one is deleted.
const CACHE_NAME = "time-map-__BUILD__";

// Same-origin app files only. Nothing from another site is ever cached here, and
// /api/, /tiles/ and /glyphs/ are never touched (they always go to the network).
const APP_SHELL = [
  "/",
  "/manifest.json",
  "/shared/ui.css",
  "/shared/api.js",
  "/shared/latest.js",
  "/meetup/",
  "/meetup/style.css",
  "/meetup/app.js",
  "/meetup/example.json",
  "/warp/",
  "/warp/style.css",
  "/warp/app.js",
  "/warp/warpmath.js",
  "/warp/example.json",
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

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  if (url.origin !== self.location.origin) return;   // other sites: not ours to cache
  if (!SHELL.has(url.pathname)) return;               // API, tiles, glyphs, anything else
  e.respondWith(networkFirst(e.request));
});
