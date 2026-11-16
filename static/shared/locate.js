// Asking for the visitor's location, the same way in every tool (#28): the card on open, the
// "Use my location" button, the saved answer and the words. The answer is saved under one key,
// so answering the card in one tool answers it in the other.
import { loadPref, savePref } from "./api.js";

export const LOCATION_PREF = "timemap-location";

export const LOCATION_WORDS = {
  busy: "Finding your location…",
  unsupported: "This browser can't share your location.",
  denied: "Location is turned off for this site.",
  failed: "We couldn't get your location.",
};

// One location fix. onError gets one of LOCATION_WORDS; the tool adds what to do instead.
export function locate({ onPlace, onError }) {
  if (!navigator.geolocation) { onError(LOCATION_WORDS.unsupported); return; }
  navigator.geolocation.getCurrentPosition(
    (p) => { savePref(LOCATION_PREF, "yes"); onPlace(p.coords.longitude, p.coords.latitude); },
    (err) => onError(err.code === 1 ? LOCATION_WORDS.denied : LOCATION_WORDS.failed),
    { enableHighAccuracy: false, timeout: 15000, maximumAge: 60000 },
  );
}

// Show the card (a .card with a [data-yes] and a [data-no] button) once: never again after
// either answer, in any tool. Returns whether it was shown.
export function offerLocation(card, { onYes, onNo }) {
  if (!navigator.geolocation || loadPref(LOCATION_PREF) !== null) return false;
  card.hidden = false;
  card.querySelector("[data-yes]").addEventListener("click", () => {
    card.hidden = true; savePref(LOCATION_PREF, "yes"); onYes();
  });
  card.querySelector("[data-no]").addEventListener("click", () => {
    card.hidden = true; savePref(LOCATION_PREF, "no"); onNo();
  });
  return true;
}
