const CACHE_NAME = "time-map-v2";

const APP_SHELL = [
  "/",
  "/meetup/",
  "/meetup/style.css",
  "/meetup/app.js",
  "/warp/",
  "/warp/style.css",
  "/warp/app.js",
  "/icons/mapbox-logo.svg",
];

// Pinned to the exact files the pages load with an integrity hash. They are fetched in
// CORS mode: an integrity-checked page request cannot use an opaque cached response.
const CDN_ASSETS = [
  "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css",
  "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll([
      ...APP_SHELL,
      ...CDN_ASSETS.map((url) => new Request(url, { mode: "cors", credentials: "omit" })),
    ]))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);

  // Network-only for API calls and map tiles
  if (
    url.pathname.startsWith("/api/") ||
    url.pathname.startsWith("/tiles/") ||
    url.pathname.startsWith("/glyphs/") ||
    url.hostname === "api.mapbox.com"
  ) {
    return;
  }

  // Cache-first for app shell, network-first for everything else
  e.respondWith(
    caches.match(e.request).then((cached) => {
      const fetchPromise = fetch(e.request)
        .then((response) => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(e.request, clone));
          }
          return response;
        })
        .catch(() => cached);

      return cached || fetchPromise;
    })
  );
});
