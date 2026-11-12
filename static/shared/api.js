// Calls to our own server, and the plain sentences shown when they fail.
// The server answers errors with {code, detail}; detail is a fixed, safe sentence.

export const NETWORK_MESSAGE = "Can't reach the server. Check your connection and try again.";

const REWRITES = {
  invalid_request: "Something about that request wasn't valid. Please try again.",
  no_isochrone: "We can't work out travel times from this spot. Move the pin onto a street.",
};

export class ApiError extends Error {
  constructor(status, code, detail) {
    super(detail || code || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

export async function readError(resp) {
  let code = "", detail = "";
  try {
    const body = await resp.json();
    if (body && typeof body.code === "string") code = body.code;
    if (body && typeof body.detail === "string") detail = body.detail;
  } catch { /* not JSON */ }
  return new ApiError(resp.status, code, detail);
}

// One sentence for a person to read. Empty for an abort, which is never shown.
export function friendly(err) {
  if (!err || err.name === "AbortError") return "";
  if (err instanceof ApiError) {
    if (REWRITES[err.code]) return REWRITES[err.code];
    if (err.detail) return err.detail;
    return `Something went wrong (error ${err.status}). Please try again.`;
  }
  // fetch() rejects with a TypeError when the network is down.
  if (err instanceof TypeError) return NETWORK_MESSAGE;
  return "Something went wrong. Please try again.";
}

export async function postJSON(url, body, signal) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!resp.ok) throw await readError(resp);
  return resp.json();
}

// Place search through /api/geocode, on submit only (Nominatim forbids search-as-you-type).
// "lat, lon" is read directly. Returns {lat, lon}, or null when nothing matched.
export async function geocode(query, signal) {
  const m = query.match(/^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/);
  if (m) {
    const lat = +m[1], lon = +m[2];
    if (Math.abs(lat) > 85 || Math.abs(lon) > 180) return null;
    return { lat, lon };
  }
  const resp = await fetch("/api/geocode", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ q: query.slice(0, 200) }),
    signal,
  });
  if (resp.status === 404) return null;
  if (!resp.ok) throw await readError(resp);
  return resp.json();
}

export function prefersReducedMotion() {
  return typeof matchMedia === "function" &&
    matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// localStorage can throw (private windows, blocked storage); treat that as "no value".
export function loadPref(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
export function savePref(key, value) {
  try { localStorage.setItem(key, value); } catch { /* ignore */ }
}

export function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
