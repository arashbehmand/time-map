// "Where should we meet?"
//
// People are added by search, by "Use my location" or by tapping the map. Find asks the
// server for the place that keeps the longest trip shortest ("Fairest"), with the place whose
// average trip is shortest beside it when that is another place. A group can be shared as a
// link (share.js) that finds a place for it when opened. Only the latest answer counts: a change to the group throws away an answer
// still on its way, and if one was on its way, the newest group is solved instead.
//
// The page is Meet up (#28): the first person's start and way of travelling, and the time of
// day, are handed on to From here and Whole city (/shared/handoff.js); a pin handed on from them
// starts the group.
import { esc, friendly, geocode, postJSON } from "/shared/api.js";
import { readGroup, readHandoff, writeGroup, writeHandoff } from "/shared/handoff.js";
import { createLatest } from "/shared/latest.js";
import { locate, offerLocation } from "/shared/locate.js";
import { updateSwitcher, watchCity } from "/shared/nav.js";
import { MODE_LABEL } from "/shared/tools.js";
import { expired, insideArea, windowOptions } from "/shared/transit.js";
import { decodeGroup, encodeGroup, isGroupHash, linkAtLoad, reconcileGroup, STATE_KEY } from "./share.js";
import {
  ALL_TIES_TEXT, altText, answerNotes, headline, isPlace, isVenue, journeyLink, journeyNote, meaning, mins,
  namedOverBest, noPlaceSentence, OPEN_HOURS_HINT, placeHeadline, quickestText, SHARE_BAD_LINK, SHARE_COPIED,
  SHARE_FAILED,
  SHARE_LABEL, SHARE_RESTORED, SHARE_WAITING, shareChanged, stationAltText, TIE_TEXT, VENUE_WORD,
} from "./words.js";

const COLORS = ["#c62828", "#00796b", "#6a1b9a", "#1565c0", "#ef6c00", "#2e7d32", "#ad1457", "#4e342e"];
// Public transport is offered only while the London tables are available, and only to people
// who start inside the area they cover (GET /api/transit/status).
const MODE_ING = { walking: "walking", cycling: "cycling", driving: "driving", transit: "by public transport" };
const DEFAULT_WINDOW = "wd19";     // weekday evening, as the server
// Where a group with someone on public transport meets: a pub unless they pick otherwise
// (the server's own default is a station). Only kinds the tables' build has are offered.
const DEFAULT_VENUE = "pub";
const VENUE_LABEL = { pub: "A pub", bar: "A bar", cafe: "A café", restaurant: "A restaurant", park: "A park" };
const LONDON_ONLY = "Public transport times cover London only";
const MAX_PARTICIPANTS = 8;        // server cap (server/meetup/models.py)
const MAX_REACH_MINUTES = 60;      // routing limit for "how far each person can get"
// Reach is drawn by Mapbox's travel-time areas, not from the journeys the answer used.
const REACH_LABEL = "Show how far each person gets in their trip time";
const REACH_HINT = "These areas come from a different Mapbox service than the answer, so their edges are approximate.";
const HANDOFF = readHandoff();     // what the last tool in this tab handed on
const RESOLVE_DELAY_MS = 800;      // edits during a solve: wait for the edits to settle
const TAP_DELAY_MS = 300;          // a second click inside this is a double-click zoom
const ATTRIBUTION =
  '<a href="https://www.mapbox.com/about/maps/" target="_blank" rel="noopener">© Mapbox</a> ' +
  '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">© OpenStreetMap</a> ' +
  '<strong><a href="https://apps.mapbox.com/feedback/" target="_blank" rel="noopener">Improve this map</a></strong>';

// ── State ───────────────────────────────────────────────────────
let people = [];               // {id, name, lon, lat, mode, color, label}
let nextId = 1;
let map = null;
const markers = new Map();     // id → maplibre Marker
let resultMarkers = [];
let lastAnswer = null;         // {data, snap, reach} for the answer on screen
let reachShown = false;
const reachLayers = new Set(); // person ids with drawn reach layers
const solves = createLatest();
const reaches = createLatest();
const searches = createLatest();
let addSeq = 0;                // pending search or location fix; bumped to abandon them
let touched = false;           // the visitor has changed something: the example must not land
let resolveTimer = null;
let tapTimer = null;
let undoTimer = null;
let markerDrags = 0;           // a marker is being dragged: its place isn't final yet
let styleReady = false;        // the map style has loaded once (layers can be added from then on)
let transit = { available: false };   // GET /api/transit/status
let windowId = DEFAULT_WINDOW;        // when the group meets (public transport only)
let venueId = DEFAULT_VENUE;          // where the group meets (public transport only)
let venueKinds = [];                  // the venue kinds the tables' build has
let statusRetryMs = 5000;
let statusSeen = false;                // the tables' status has been read once
let openSeq = 0;                      // shared links opened, and group edits: the newest wins
let shareSeq = 0;                     // links made; a newer one, or a changed box, voids an older one
// Settles the first time the tables' status is known (or failed to load): a shared group with
// someone on public transport waits for it before it is added.
let settleTransit;
const transitSettled = new Promise((resolve) => { settleTransit = resolve; });

// ── DOM ─────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const nameInput = $("name-input");
const locInput = $("loc-input");
// The add form's way of travelling: a row of chips, the same control as in From here.
const addMode = () => (document.querySelector('input[name="add-mode"]:checked') || { value: "walking" }).value;
function setAddMode(mode) {
  const r = document.querySelector(`input[name="add-mode"][value="${mode}"]`);
  if (r) r.checked = true;
}
const addBtn = $("add-btn");
const peopleList = $("people");
const findBtn = $("find-btn");
const statusEl = $("status");
const answerEl = $("answer");
const exampleNote = $("example-note");
const addStatusEl = $("add-status");

function setStatus(text, kind = "", el = statusEl) {
  el.textContent = text;
  el.className = "status" + (kind ? " " + kind : "");
}
const setAddStatus = (text, kind = "") => setStatus(text, kind, addStatusEl);

// ── Map ─────────────────────────────────────────────────────────
function buildMapStyle() {
  const o = window.location.origin;
  return {
    version: 8,
    glyphs: `${o}/glyphs/{fontstack}/{range}.pbf`,
    sources: {
      streets: {
        type: "vector",
        tiles: [`${o}/tiles/{z}/{x}/{y}`],
        minzoom: 0, maxzoom: 16,
        attribution: ATTRIBUTION,
      },
    },
    layers: [
      { id: "bg",    type: "background", paint: { "background-color": "#f0ede9" } },
      { id: "water", type: "fill",   source: "streets", "source-layer": "water",
        paint: { "fill-color": "#aad3df" } },
      { id: "green", type: "fill",   source: "streets", "source-layer": "landuse",
        filter: ["match", ["get", "class"], ["park", "garden", "grass", "forest", "wood"], true, false],
        paint: { "fill-color": "#c8facc" } },
      { id: "bldg",  type: "fill",   source: "streets", "source-layer": "building",
        paint: { "fill-color": "#dfdbd7", "fill-outline-color": "#c8c4be" } },
      { id: "road-major", type: "line", source: "streets", "source-layer": "road",
        filter: ["match", ["get", "class"], ["motorway", "trunk", "primary", "secondary"], true, false],
        layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": "#fff",
                 "line-width": ["interpolate", ["linear"], ["zoom"], 8, 1, 16, 5] } },
      { id: "road-minor", type: "line", source: "streets", "source-layer": "road",
        filter: ["match", ["get", "class"], ["tertiary", "street", "pedestrian", "footway"], true, false],
        layout: { "line-join": "round", "line-cap": "round" },
        paint: { "line-color": "#fff", "line-width": 0.8 } },
      { id: "place", type: "symbol", source: "streets", "source-layer": "place_label",
        layout: {
          "text-field": ["get", "name"],
          "text-font": ["Open Sans Regular", "Arial Unicode MS Regular"],
          "text-size": ["interpolate", ["linear"], ["zoom"], 8, 10, 14, 13],
        },
        paint: { "text-color": "#333",
                 "text-halo-color": "rgba(255,255,255,0.85)", "text-halo-width": 1.5 } },
    ],
  };
}

// The Mapbox wordmark, which Mapbox requires on maps that use its data.
class MapboxLogoControl {
  onAdd() {
    const a = document.createElement("a");
    a.className = "maplibregl-ctrl mapbox-logo";
    a.href = "https://www.mapbox.com/";
    a.target = "_blank";
    a.rel = "noopener";
    a.setAttribute("aria-label", "Mapbox");
    a.innerHTML = '<img src="/icons/mapbox-logo.svg" alt="Mapbox" width="80" height="20" />';
    this._el = a;
    return a;
  }
  onRemove() { this._el.remove(); }
}

function initMap() {
  map = new maplibregl.Map({
    container: "map",
    style: buildMapStyle(),
    center: [-0.118, 51.509],
    zoom: 11,
    attributionControl: { compact: false },
    dragRotate: false,
    touchPitch: false,
  });
  map.touchZoomRotate.disableRotation();
  map.addControl(new MapboxLogoControl(), "bottom-left");
  map.once("load", () => { styleReady = true; });

  // Tapping the map adds a person there, except:
  // - a tap on a marker or popup;
  // - a tap while a popup is open (that tap closes the popup);
  // - a double-click or double-tap, which zooms.
  map.on("click", (e) => {
    const target = e.originalEvent && e.originalEvent.target;
    if (target && target.closest && target.closest(".maplibregl-marker, .maplibregl-popup")) return;
    if (document.querySelector("#map .maplibregl-popup")) return;
    if (tapTimer) { clearTimeout(tapTimer); tapTimer = null; return; }
    const { lng, lat } = e.lngLat;
    tapTimer = setTimeout(() => {
      tapTimer = null;
      const typed = nameInput.value.trim();
      const name = typed || defaultName();
      setAddStatus("");   // before adding: addPerson may say the person is walking instead
      if (addPerson({ name, named: !!typed, lon: lng, lat, mode: addMode(), label: "Picked on the map" })) {
        nameInput.value = "";
        showUndo(`Added ${name}.`);
      }
    }, TAP_DELAY_MS);
  });
  map.on("dblclick", () => { clearTimeout(tapTimer); tapTimer = null; });
}

function defaultName() {
  const names = new Set(people.map((p) => p.name));
  let n = people.length + 1;
  while (names.has(`Person ${n}`)) n++;
  return `Person ${n}`;
}

// ── The group ───────────────────────────────────────────────────
// `quiet`: the example or a shared group is being set up, not edited by the visitor.
// `named`: the name was typed, not made up ("Person 2"); only typed names go in a share link.
function addPerson({ name, named = false, lon, lat, mode, label }, { quiet = false } = {}) {
  if (people.length >= MAX_PARTICIPANTS) {
    setAddStatus(`A group can have up to ${MAX_PARTICIPANTS} people.`, "error");
    return null;
  }
  const id = nextId++;
  const outside = mode === "transit" && !transitOk({ lon, lat });
  const p = { id, name, named, lon, lat, mode: outside ? "walking" : mode, label,
              color: COLORS[(id - 1) % COLORS.length] };
  people.push(p);
  addMarker(p);
  renderPeople();
  if (!quiet) groupChanged();
  if (outside) setAddStatus(`${LONDON_ONLY}, so ${name} is walking. Pick another way if they travel differently.`);
  return p;
}

// ── Public transport ────────────────────────────────────────────
// May this place start a public transport trip? Inside the tables' area, while they last.
function transitOk(pt) {
  return Boolean(transit.available && insideArea(transit.coverage, pt.lon, pt.lat));
}

const anyTransit = () => people.some((p) => p.mode === "transit");

// The When and Where controls show while someone is on public transport. Every answer shows
// the fairest place and, when it is another place, the quickest on average; the venue hint
// says that itself.
function updateTransitControls() {
  const on = anyTransit();
  const atVenue = venueKinds.length > 0 && venueId !== "station";
  $("window-field").hidden = !on;
  $("venue-field").hidden = !on || venueKinds.length === 0;
  $("venue-hint").hidden = !on || !atVenue;
  $("both-hint").hidden = on && atVenue;
}

async function loadTransitStatus() {
  const later = () => { setTimeout(loadTransitStatus, statusRetryMs); statusRetryMs = Math.min(statusRetryMs * 2, 60000); };
  let status;
  try {
    const resp = await fetch("/api/transit/status");
    if (!resp.ok) { settleTransit(); updateSwitcher(null); later(); return; }
    status = await resp.json();
  } catch { settleTransit(); updateSwitcher(null); later(); return; }
  statusRetryMs = 5000;
  updateSwitcher(status);
  if (status.loading) { setTimeout(loadTransitStatus, 3000); return; }
  // Whole city may still be loading: the switcher asks alone, so this handler (and the people
  // list it redraws) runs once.
  if (!statusSeen) { statusSeen = true; watchCity(status); }
  transit = status.available && !expired(status.valid_until) ? status : { available: false };
  $("transit-chip").hidden = !transit.available;
  if (!transit.available && addMode() === "transit") setAddMode("walking");
  if (transit.available) {
    const ids = transit.windows.map((w) => w.id);
    if (!ids.includes(windowId)) windowId = ids.includes(DEFAULT_WINDOW) ? DEFAULT_WINDOW : ids[0];
    windowOptions($("window-select"), transit.windows, windowId);
    // Venue kinds the build has at least one of, then "Just a station".
    const counts = (transit.venues && transit.venues.kinds) || {};
    const kinds = Object.keys(VENUE_LABEL).filter((k) => Number(counts[k]) > 0);
    venueKinds = kinds;
    const venueSel = $("venue-select");
    venueSel.replaceChildren(...kinds.map((k) => new Option(VENUE_LABEL[k], k)), new Option("Just a station", "station"));
    if (!kinds.includes(venueId) && venueId !== "station") venueId = kinds.includes(DEFAULT_VENUE) ? DEFAULT_VENUE : (kinds[0] || "station");
    venueSel.value = venueId;
  } else if (anyTransit()) {
    for (const p of people) {
      if (p.mode !== "transit") continue;
      p.mode = "walking";
      updateMarker(p);     // the marker's popup says how they travel
    }
    groupChanged();
    setAddStatus("Public transport times aren't available right now, so everyone on it is walking.");
  }
  renderPeople();
  updateTransitControls();
  settleTransit();
}

$("window-select").addEventListener("change", (e) => {
  windowId = e.target.value;
  writeHandoff({ when: windowId });
  groupChanged();
});

$("venue-select").addEventListener("change", (e) => {
  venueId = e.target.value;
  updateTransitControls();
  groupChanged();
});

function removePerson(id) {
  const index = people.findIndex((p) => p.id === id);
  if (index < 0) return;          // already gone (an Undo for a group that has since changed)
  people = people.filter((p) => p.id !== id);
  const m = markers.get(id);
  if (m) { m.remove(); markers.delete(id); }
  renderPeople();
  groupChanged();
  // Keep keyboard focus nearby: the next person's remove button, or the add form.
  const buttons = peopleList.querySelectorAll(".remove");
  (buttons[Math.min(index, buttons.length - 1)] || locInput).focus();
}

function clearGroup() {
  for (const m of markers.values()) m.remove();
  markers.clear();
  people = [];
  renderPeople();
  groupChanged();
}

function showUndo(text) {
  const toast = $("toast");
  $("toast-text").textContent = text;
  toast.hidden = false;
  const id = people[people.length - 1]?.id;
  $("toast-undo").onclick = () => {
    toast.hidden = true;
    if (id !== undefined) removePerson(id);
  };
  clearTimeout(undoTimer);
  undoTimer = setTimeout(() => { toast.hidden = true; }, 8000);
}

// Anything that changes who is going, from where, how, or what matters most.
// The answer on screen and any answer on its way are for the old group. If a solve was
// on its way, the newest group is solved once the edits settle; otherwise Find is needed.
function groupChanged() {
  touched = true;
  openSeq++;                     // a shared link still waiting for the tables is dropped
  forgetLink();
  handOn();
  const wasSolving = solves.busy || resolveTimer !== null;
  const hadAnswer = lastAnswer !== null;
  solves.cancel();
  clearTimeout(resolveTimer);
  resolveTimer = null;
  clearResults();
  exampleNote.hidden = true;
  updateFind();
  updateTransitControls();
  renderShareChoices();
  if (wasSolving && people.length >= 2) {
    setStatus("The group changed. Finding a place for the new group…", "busy");
    const later = () => {
      if (markerDrags > 0) { resolveTimer = setTimeout(later, RESOLVE_DELAY_MS); return; }
      resolveTimer = null;
      solve();
    };
    resolveTimer = setTimeout(later, RESOLVE_DELAY_MS);
  } else if (hadAnswer || wasSolving) {
    setStatus(people.length >= 2 ? "The group changed. Press Find again." : "");
  } else if (statusEl.classList.contains("busy")) {
    setStatus("");
  }
}

// The first person in the group (not the saved example's) goes to the other tools: where they
// start, how they travel and, when anyone is on public transport, when the group meets. An empty
// group takes the pin (and its mode) back. The group itself is kept for this tab, so coming back
// through the switcher finds it again (keepGroup).
function handOn() {
  const p = people.find((q) => !q.example);
  if (p) {
    writeHandoff({ pin: { lon: p.lon, lat: p.lat, label: p.label }, mode: p.mode,
                   ...(anyTransit() ? { when: windowId } : {}) });
  } else if (!people.length) {
    writeHandoff({ pin: null });
  }
  keepGroup();
}

// Called only after the visitor has changed something (the example loads quietly), so an edited
// example is kept too; its people keep their example flag, so they still aren't handed on.
function keepGroup() {
  if (!people.length) { writeGroup(null); return; }
  writeGroup({ people: people.map(({ name, named, lon, lat, mode, label, example }) =>
                 ({ name, named, lon, lat, mode, label, example: example === true })),
               window: windowId, venue: venueId });
}

// Each person's way of travelling: the same chips as the add form, smaller.
function modeChips(p) {
  const modes = Object.keys(MODE_LABEL).filter((m) => m !== "transit" || p.mode === "transit" || transitOk(p));
  return `<fieldset class="chips compact p-mode">
      <legend class="visually-hidden">How ${esc(p.name)} travels</legend>
      ${modes.map((m) => `<label class="chip"><input type="radio" name="mode-${p.id}" value="${m}"${
        p.mode === m ? " checked" : ""} /><span>${MODE_LABEL[m]}</span></label>`).join("")}
    </fieldset>`;
}

function renderPeople() {
  peopleList.innerHTML = people.map((p, i) => `
    <li class="person" style="--color:${p.color}" data-id="${p.id}">
      <span class="dot" aria-hidden="true"></span>
      <input type="text" class="p-name" value="${esc(p.name)}" maxlength="40" aria-label="Name of person ${i + 1}" />
      <button type="button" class="remove" aria-label="Remove ${esc(p.name)}">×</button>
      <div class="where">
        ${modeChips(p)}
        <span>from ${esc(p.label)}</span>
      </div>
    </li>`).join("");

  for (const li of peopleList.querySelectorAll(".person")) {
    const p = people.find((pp) => pp.id === +li.dataset.id);
    li.querySelector(".remove").addEventListener("click", () => removePerson(p.id));
    const nameEl = li.querySelector(".p-name");
    nameEl.addEventListener("change", () => {
      // A name doesn't change the answer: keep it and show the new name. A cleared name gets
      // a made-up one again, which a share link leaves out.
      const typed = nameEl.value.trim();
      p.named = typed !== "";
      p.name = typed || defaultName();
      nameEl.value = p.name;
      li.querySelector(".remove").setAttribute("aria-label", `Remove ${p.name}`);
      li.querySelector(".p-mode legend").textContent = `How ${p.name} travels`;
      updateMarker(p);
      touched = true;
      renderShareChoices({ keep: true });
      hideShareUrl();               // a link shown for copying has the old name
      forgetLink();                 // and a reload must not bring the old name back
      keepGroup();                  // but a trip to another tool and back does
      if (lastAnswer) renderAnswer();
    });
    for (const radio of li.querySelectorAll(".p-mode input")) {
      radio.addEventListener("change", () => {
        if (!radio.checked) return;
        p.mode = radio.value;
        updateMarker(p);
        groupChanged();
      });
    }
  }
  $("people-empty").hidden = people.length > 0;
  // Ticks are kept by person id; a change to the group (groupChanged) unticks them all.
  renderShareChoices({ keep: true });
}

function updateFind() {
  if (solves.busy) return;
  const enough = people.length >= 2;
  findBtn.disabled = !enough;
  findBtn.textContent = enough ? "Find a place to meet" : "Add at least 2 people";
  const full = people.length >= MAX_PARTICIPANTS;
  addBtn.disabled = full;
  addBtn.textContent = full ? `Up to ${MAX_PARTICIPANTS} people` : "Add person";
}

// ── Markers ─────────────────────────────────────────────────────
function popupHtml(p) {
  return `<strong>${esc(p.name)}</strong><br>${MODE_ING[p.mode]}`;
}

function addMarker(p) {
  const el = document.createElement("div");
  el.className = "p-marker";
  el.style.background = p.color;
  el.title = p.name;
  el.textContent = (p.name[0] || "?").toUpperCase();
  const marker = new maplibregl.Marker({ element: el, draggable: true })
    .setLngLat([p.lon, p.lat])
    .setPopup(new maplibregl.Popup({ offset: 25 }).setHTML(popupHtml(p)))
    .addTo(map);
  marker.on("dragstart", () => { markerDrags++; groupChanged(); });
  marker.on("dragend", () => {
    markerDrags = Math.max(0, markerDrags - 1);
    const { lng, lat } = marker.getLngLat();
    p.lon = lng; p.lat = lat;
    p.label = "Picked on the map";
    p.example = false;            // moved by the visitor: theirs now
    const left = p.mode === "transit" && !transitOk(p);
    if (left) { p.mode = "walking"; updateMarker(p); }
    renderPeople();
    groupChanged();
    if (left) setAddStatus(`${LONDON_ONLY}, so ${p.name} is walking now. Pick another way if they travel differently.`);
  });
  markers.set(p.id, marker);
}

function updateMarker(p) {
  const marker = markers.get(p.id);
  if (!marker) return;
  const el = marker.getElement();
  el.title = p.name;
  el.textContent = (p.name[0] || "?").toUpperCase();
  marker.setPopup(new maplibregl.Popup({ offset: 25 }).setHTML(popupHtml(p)));
}

// The part of the map you can see: on a phone the header covers its top and the sheet its
// bottom (style.css).
function mapPadding(px = 60) {
  if (window.innerWidth > 640) return { top: px, bottom: px, left: px, right: px };
  const top = $("top").getBoundingClientRect().bottom + 16;
  const bottom = Math.max(16, window.innerHeight - $("panel").getBoundingClientRect().top + 16);
  const free = window.innerHeight - top - bottom;
  return free > 80 ? { top, bottom, left: 24, right: 24 } : { top: 16, bottom: 16, left: 24, right: 24 };
}

function fitTo(points, maxZoom = 14) {
  if (!points.length) return;
  if (points.length === 1) {
    map.easeTo({ center: points[0], zoom: Math.max(map.getZoom(), 13), padding: mapPadding(0) });
    return;
  }
  const lngs = points.map((pt) => pt[0]), lats = points.map((pt) => pt[1]);
  map.fitBounds([[Math.min(...lngs), Math.min(...lats)], [Math.max(...lngs), Math.max(...lats)]],
    { padding: mapPadding(), maxZoom, duration: 600 });
}

// ── Adding by search or location ────────────────────────────────
// A search and a location fix replace each other, and "Start a new group" abandons both.
function newAddition() {
  searches.cancel();
  return ++addSeq;
}

$("add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  // Snapshot the form: a reply for these fields must not add someone else.
  const form = { name: nameInput.value.trim(), q: locInput.value.trim(), mode: addMode() };
  if (!form.q) {
    setAddStatus("Type where they start, or tap the map.", "error");
    locInput.focus();
    return;
  }
  if (people.length >= MAX_PARTICIPANTS) {
    setAddStatus(`A group can have up to ${MAX_PARTICIPANTS} people.`, "error");
    return;
  }
  const mine = newAddition();
  setAddStatus("Searching…", "busy");
  let res;
  try {
    res = await searches.run(form, (signal) => geocode(form.q, signal));
  } catch (err) {
    if (mine === addSeq) setAddStatus(friendly(err), "error");
    return;
  }
  if (res.stale || mine !== addSeq) return;
  if (!res.value) {
    setAddStatus("We couldn't find that place. Try another name, or tap the map to put the person there.", "error");
    return;
  }
  setAddStatus("");
  const p = addPerson({ name: form.name || defaultName(), named: !!form.name, lon: res.value.lon,
                        lat: res.value.lat, mode: form.mode, label: form.q });
  if (!p) return;
  // Clear the fields only if they still hold what was searched for.
  if (nameInput.value.trim() === form.name) nameInput.value = "";
  if (locInput.value.trim() === form.q) locInput.value = "";
  nameInput.focus();
  fitTo(people.map((pp) => [pp.lon, pp.lat]));
});

// Add whoever is holding the phone, where they are. `replaceExample`: the location card's
// answer, which starts the group from you instead of the example.
function addMyLocation({ replaceExample = false } = {}) {
  const form = { name: nameInput.value.trim(), mode: addMode() };
  const mine = newAddition();
  setAddStatus("Finding your location…", "busy");
  locate({
    onPlace: (lon, lat) => {
      if (mine !== addSeq) return;
      addSeq++;
      setAddStatus("");
      if (replaceExample && !touched) clearGroup();
      const p = addPerson({ name: form.name || defaultName(), named: !!form.name, lon, lat, mode: form.mode,
                            label: replaceExample ? "your location" : "their location" });
      if (!p) return;
      if (nameInput.value.trim() === form.name) nameInput.value = "";
      fitTo(people.map((pp) => [pp.lon, pp.lat]));
    },
    onError: (words) => {
      if (mine === addSeq) setAddStatus(`${words} Search for a place or tap the map.`, "error");
    },
  });
}

$("locate-btn").addEventListener("click", () => addMyLocation());

$("new-group-btn").addEventListener("click", () => {
  newAddition();
  clearTimeout(tapTimer); tapTimer = null;
  clearGroup();
  setStatus("");
  setAddStatus("");
  locInput.focus();
});

// ── Find ────────────────────────────────────────────────────────
async function solve() {
  if (people.length < 2) return;
  const stations = anyTransit();
  const snap = {
    objective: "minimax",     // the quickest on average comes with it
    window: stations ? windowId : null,
    venue: stations ? venueId : null,
    people: people.map(({ id, name, lon, lat, mode, color }) => ({ id, name, lon, lat, mode, color })),
  };
  findBtn.disabled = true;
  findBtn.textContent = "Looking for a place…";
  setStatus("");
  let res;
  try {
    res = await solves.run(snap, (signal) => postJSON("/api/meetup/solve", {
      participants: snap.people.map((p) => ({ id: String(p.id), lon: p.lon, lat: p.lat, mode: p.mode })),
      objective: snap.objective,
      search: { top_k: stations ? 5 : 4 },
      ...(snap.window ? { window: snap.window } : {}),
      ...(snap.venue ? { venue: snap.venue } : {}),
    }, signal));
  } catch (err) {
    updateFind();
    // "No place works" names who is stuck; other errors use the server's own sentence.
    setStatus(noPlaceSentence(err, snap.people.map(nameOf)) || friendly(err), "error");
    // The tables went away (expired, say) while the page was open: ask again, which takes
    // public transport off the page and moves anyone on it to walking.
    if (err && err.code === "transit_unavailable") loadTransitStatus();
    return;
  }
  if (res.stale) return;          // the group changed; groupChanged() has taken over
  updateFind();
  showResults(res.value, snap);
}

findBtn.addEventListener("click", () => {
  if (solves.busy) return;
  clearTimeout(resolveTimer);
  resolveTimer = null;
  solve();
});

// ── Answer ──────────────────────────────────────────────────────
// The sentences live in words.js.
// The answer is for the people in its snapshot; names are read live because a rename
// doesn't change the answer (any other edit clears it).
function nameOf(sp) {
  const live = people.find((p) => p.id === sp.id);
  return live ? live.name : sp.name;
}

function showResults(data, snap) {
  if (isPlace(data.best)) { showStations(data, snap); return; }
  clearResults();
  lastAnswer = { data, snap, reach: null };
  const best = data.best;
  const alts = data.top.slice(1, 4);

  const el = document.createElement("div");
  el.className = "best-marker";
  el.textContent = "★";
  el.setAttribute("aria-hidden", "true");
  resultMarkers.push(new maplibregl.Marker({ element: el })
    .setLngLat([best.lon, best.lat])
    .setPopup(new maplibregl.Popup({ offset: 24 }).setHTML(`<strong>Meet here</strong><br>${esc(altText(best, snap.objective))}`))
    .addTo(map));
  alts.forEach((c, i) => {
    const m = document.createElement("div");
    m.className = "cand-marker";
    m.textContent = String(i + 2);
    m.setAttribute("aria-hidden", "true");
    resultMarkers.push(new maplibregl.Marker({ element: m })
      .setLngLat([c.lon, c.lat])
      .setPopup(new maplibregl.Popup({ offset: 16 }).setHTML(`<strong>Option ${i + 2}</strong><br>${esc(altText(c, snap.objective))}`))
      .addTo(map));
  });
  const quick = data.quickest;
  if (quick) {
    const q = document.createElement("div");
    q.className = "cand-marker quick-marker";
    q.textContent = "Q";
    q.setAttribute("aria-hidden", "true");
    resultMarkers.push(new maplibregl.Marker({ element: q })
      .setLngLat([quick.lon, quick.lat])
      .setPopup(new maplibregl.Popup({ offset: 16 }).setHTML(`<strong>Quickest on average</strong><br>${esc(altText(quick, "sum"))}`))
      .addTo(map));
  }

  renderAnswer();
  fitTo([...snap.people.map((p) => [p.lon, p.lat]), [best.lon, best.lat],
         ...(quick ? [[quick.lon, quick.lat]] : [])], 14);
}

// ── Answer at a station or a venue ──────────────────────────────
// Fairest is the star; Quickest on average gets its own card and marker when it is another
// place; close alternatives are numbered. Each person's time comes with a link to plan the
// journey. No reach areas: they would come from a different service than these times.
function placeMarker(c, className, text, title) {
  const el = document.createElement("div");
  el.className = className;
  el.textContent = text;
  el.setAttribute("aria-hidden", "true");
  return new maplibregl.Marker({ element: el })
    .setLngLat([c.lon, c.lat])
    .setPopup(new maplibregl.Popup({ offset: 20 }).setHTML(`<strong>${esc(title)}</strong><br>${esc(stationAltText(c))}`))
    .addTo(map);
}

function showStations(data, snap) {
  clearResults();
  lastAnswer = { data, snap, reach: null };
  const kind = isVenue(data.best) ? VENUE_WORD[data.best.kind] : "station";
  resultMarkers.push(placeMarker(data.best, "best-marker", "★", `Meet at ${data.best.name || `this ${kind}`}`));
  if (data.quickest) resultMarkers.push(placeMarker(data.quickest, "cand-marker quick-marker", "Q", "Quickest on average"));
  data.top.slice(1).forEach((c, i) => resultMarkers.push(placeMarker(c, "cand-marker", String(i + 2), `Option ${i + 2}`)));
  renderAnswer();
  // The server lists the quickest place once, apart from the numbered alternatives.
  const places = [...data.top, ...(data.quickest ? [data.quickest] : [])];
  fitTo([...snap.people.map((p) => [p.lon, p.lat]), ...places.map((c) => [c.lon, c.lat])], 14);
}

// `links`: a "Plan journey" link per person (station and venue answers, which say what it does).
function tripRows(place, snap, { links = true } = {}) {
  const times = snap.people.map((p) => ({ p, t: place.times[String(p.id)] }));
  const longest = Math.max(...times.map((x) => x.t));
  const width = (t) => (longest > 0 ? Math.max(4, (t / longest) * 100) : 100).toFixed(1);
  return `
    <ul class="bars" aria-label="Each person's trip">
      ${times.map(({ p, t }) => `
        <li class="bar-row" style="--color:${p.color}">
          <span><strong>${esc(nameOf(p))}</strong> · ${mins(t)} ${MODE_ING[p.mode] || p.mode}${t === longest && times.length > 1 ? " (longest)" : ""}${links ? `
            · <a href="${esc(journeyLink(p, place))}" target="_blank" rel="noopener noreferrer">Plan journey</a>` : ""}</span>
          <span class="track" aria-hidden="true"><span class="fill" style="width:${width(t)}%"></span></span>
        </li>`).join("")}
    </ul>`;
}

function renderStations() {
  const { data, snap } = lastAnswer;
  const alts = data.top.slice(1);
  // When every alternative is a tie, one sentence says so instead of a label on each.
  const allTies = alts.length > 0 && alts.every((c) => c.tie);
  const credits = Array.isArray(data.credits) ? data.credits.filter((c) => typeof c === "string") : [];
  answerEl.innerHTML = `
    <div class="answer-card">
      <p class="headline">${esc(placeHeadline(data.best))}</p>
      <p class="hint">${esc(meaning("minimax"))}</p>
      ${answerNotes(data).map((n) => `<p class="hint answer-note">${esc(n)}</p>`).join("")}
      ${tripRows(data.best, snap)}
      <p class="hint">${esc(journeyNote(data.window_label, data.best))}</p>
      ${isVenue(data.best) ? `<p class="hint">${esc(OPEN_HOURS_HINT)}</p>` : ""}
    </div>
    ${data.quickest ? `
      <div class="answer-card quick-card">
        <p class="headline"><span class="alt-num" aria-hidden="true">Q</span> ${esc(quickestText(data.quickest))}</p>
        ${tripRows(data.quickest, snap)}
      </div>` : ""}
    ${alts.length ? `
      <h2>Close alternatives</h2>
      <ul class="alternatives">
        ${alts.map((c, i) => `
          <li>
            <button type="button" class="btn" data-lon="${c.lon}" data-lat="${c.lat}">
              <span class="alt-num" aria-hidden="true">${i + 2}</span> Option ${i + 2} · ${esc(stationAltText(c, !allTies))}
            </button>
            <details><summary>Everyone's trip there</summary>${tripRows(c, snap)}</details>
          </li>`).join("")}
      </ul>
      ${allTies ? `<p class="hint">${esc(ALL_TIES_TEXT)}</p>`
        : alts.some((c) => c.tie) ? `<p class="hint">${esc(TIE_TEXT)}</p>` : ""}` : ""}
    ${credits.length ? `<p class="hint credits">Data: ${credits.map(esc).join(" · ")}</p>` : ""}`;
  answerEl.hidden = false;
  for (const btn of answerEl.querySelectorAll(".alternatives .btn")) {
    btn.addEventListener("click", () => map.easeTo({ center: [+btn.dataset.lon, +btn.dataset.lat], zoom: Math.max(map.getZoom(), 14) }));
  }
}

function renderAnswer() {
  if (isPlace(lastAnswer.data.best)) { renderStations(); return; }
  const { data, snap } = lastAnswer;
  const best = data.best;
  const alts = data.top.slice(1, 4);
  const times = snap.people.map((p) => ({ p, t: best.times[String(p.id)] }));
  const longest = Math.max(...times.map((x) => x.t));
  const width = (t) => (longest > 0 ? Math.max(4, (t / longest) * 100) : 100).toFixed(1);
  answerEl.innerHTML = `
    <div class="answer-card">
      <p class="headline">${esc(headline(best, snap.objective))}</p>
      ${namedOverBest(data) ? "" : `<p class="hint">${esc(meaning(snap.objective))}</p>`}
      ${answerNotes(data).map((n) => `<p class="hint answer-note">${esc(n)}</p>`).join("")}
      <ul class="bars" aria-label="Each person's trip">
        ${times.map(({ p, t }) => `
          <li class="bar-row" style="--color:${p.color}">
            <span><strong>${esc(nameOf(p))}</strong> · ${mins(t)} ${MODE_ING[p.mode]}${t === longest && times.length > 1 ? " (longest)" : ""}</span>
            <span class="track" aria-hidden="true"><span class="fill" style="width:${width(t)}%"></span></span>
          </li>`).join("")}
      </ul>
      <button type="button" class="btn" id="reach-btn" aria-pressed="${reachShown}">${reachShown ? "Hide reach" : REACH_LABEL}</button>
      <p class="hint">${esc(REACH_HINT)}</p>
    </div>
    ${data.quickest ? `
      <div class="answer-card quick-card">
        <p class="headline"><span class="alt-num" aria-hidden="true">Q</span> ${esc(quickestText(data.quickest))}</p>
        ${tripRows(data.quickest, snap, { links: false })}
      </div>` : ""}
    ${alts.length ? `
      <h2>Close alternatives</h2>
      <ul class="alternatives">
        ${alts.map((c, i) => `
          <li><button type="button" class="btn" data-lon="${c.lon}" data-lat="${c.lat}">
            <span class="alt-num" aria-hidden="true">${i + 2}</span> Option ${i + 2} · ${esc(altText(c, snap.objective))}
          </button></li>`).join("")}
      </ul>` : ""}`;
  answerEl.hidden = false;

  for (const btn of answerEl.querySelectorAll(".alternatives .btn")) {
    btn.addEventListener("click", () => map.easeTo({ center: [+btn.dataset.lon, +btn.dataset.lat], zoom: Math.max(map.getZoom(), 14) }));
  }
  $("reach-btn").addEventListener("click", toggleReach);
}

function clearResults() {
  resultMarkers.forEach((m) => m.remove());
  resultMarkers = [];
  lastAnswer = null;
  answerEl.hidden = true;
  answerEl.innerHTML = "";
  reachShown = false;
  reaches.cancel();
  clearReach();
}

// ── How far each person can get ─────────────────────────────────
function clearReach() {
  if (!map) return;
  for (const id of reachLayers) {
    if (map.getLayer(`reach-fill-${id}`)) map.removeLayer(`reach-fill-${id}`);
    if (map.getLayer(`reach-line-${id}`)) map.removeLayer(`reach-line-${id}`);
    if (map.getSource(`reach-${id}`)) map.removeSource(`reach-${id}`);
  }
  reachLayers.clear();
}

function setReachButton(on) {
  const btn = $("reach-btn");
  if (!btn) return;
  btn.textContent = on ? "Hide reach" : REACH_LABEL;
  btn.setAttribute("aria-pressed", String(on));
}

function drawReach(results) {
  for (const r of results) {
    if (!r.geo) continue;
    const id = r.p.id;
    map.addSource(`reach-${id}`, { type: "geojson", data: r.geo });
    map.addLayer({ id: `reach-fill-${id}`, type: "fill", source: `reach-${id}`,
                   paint: { "fill-color": r.p.color, "fill-opacity": 0.14 } });
    map.addLayer({ id: `reach-line-${id}`, type: "line", source: `reach-${id}`,
                   paint: { "line-color": r.p.color, "line-width": 2, "line-opacity": 0.8 } });
    reachLayers.add(id);
  }
}

async function toggleReach() {
  if (reachShown) {
    reachShown = false;
    reaches.cancel();
    clearReach();
    setReachButton(false);
    setStatus("");
    return;
  }
  const answer = lastAnswer;
  if (!answer) return;
  const { data, snap } = answer;

  const tooFar = [];
  const jobs = snap.people.map((p) => {
    const minutes = Math.ceil(data.best.times[String(p.id)]);
    if (!minutes || minutes <= 0) return null;
    if (minutes > MAX_REACH_MINUTES) { tooFar.push(nameOf(p)); return null; }
    return { p, minutes };
  }).filter(Boolean);
  const tooFarNote = tooFar.length
    ? `Reach is shown up to ${MAX_REACH_MINUTES} minutes; ${tooFar.join(", ")} ${tooFar.length === 1 ? "needs" : "need"} longer.`
    : "";
  if (!jobs.length) { setStatus(tooFarNote); return; }

  reachShown = true;
  setReachButton(true);

  // Already fetched for this answer: draw it again without asking the server.
  if (answer.reach) {
    drawReach(answer.reach);
    setStatus(tooFarNote);
    return;
  }

  setStatus("Working out how far everyone can get…", "busy");
  const res = await reaches.run(snap, async (signal) => {
    // Layers need the style, not every tile: isStyleLoaded() is also false while tiles load
    // after a pan, and "load" fires only once. A cancel stops the wait.
    if (!styleReady) {
      await new Promise((resolve, reject) => {
        map.once("load", resolve);
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      });
    }
    return Promise.all(jobs.map(async ({ p, minutes }) => {
      try {
        const geo = await postJSON("/api/isochrones", {
          longitude: p.lon, latitude: p.lat, profile: p.mode, travel_times: [minutes],
        }, signal);
        return { p, geo };
      } catch (err) {
        if (err.name === "AbortError") throw err;
        return { p, error: friendly(err) };
      }
    }));
  });
  // The answer was replaced, or reach was switched off, while this was on its way.
  if (res.stale || lastAnswer !== answer || !reachShown) return;

  const failures = res.value.filter((r) => r.error).map((r) => r.error);
  if (!failures.length) answer.reach = res.value;
  drawReach(res.value);

  const notes = [];
  if (failures.length) notes.push(`Reach isn't available right now: ${failures[0]}`);
  if (tooFarNote) notes.push(tooFarNote);
  setStatus(notes.join(" "), failures.length ? "error" : "");
  if (failures.length === jobs.length) {
    reachShown = false;
    setReachButton(false);
  }
}

// ── Share links ─────────────────────────────────────────────────
// The share panel: one "exact start point" box per person, unticked, rebuilt whenever the
// group changes (`keep` keeps the ticks when only a name changed). A link made for an older
// group is hidden then too.
function renderShareChoices({ keep = false } = {}) {
  const list = $("share-exact-list");
  const was = keep ? new Set([...list.querySelectorAll("input:checked")].map((i) => i.value)) : new Set();
  list.innerHTML = people.map((p) => `
    <label class="check"><input type="checkbox" value="${p.id}"${was.has(String(p.id)) ? " checked" : ""} />
      ${esc(p.name)}</label>`).join("");
  $("share").hidden = people.length < 2;
  if (!keep) hideShareUrl();
}

// A link shown for copying by hand is for the boxes as they were: any change hides it, and a
// copy still on its way for older boxes is dropped (shareSeq).
function hideShareUrl() {
  shareSeq++;
  const urlEl = $("share-url");
  urlEl.hidden = true;
  urlEl.value = "";
}
$("share-exact-list").addEventListener("change", hideShareUrl);

// Copy first, as the button says. Only where there is no clipboard does the share sheet open
// (some phones), and only if that fails too is the link shown to copy by hand.
async function shareGroup() {
  hideShareUrl();
  const mine = shareSeq;
  const exact = new Set([...$("share-exact-list").querySelectorAll("input:checked")].map((i) => +i.value));
  const hash = encodeGroup({
    people: people.map((p) => ({ ...p, exact: exact.has(p.id) })), window: windowId, venue: venueId,
  });
  const url = `${location.origin}${location.pathname}#${hash}`;
  try {
    await navigator.clipboard.writeText(url);
    if (mine === shareSeq) setStatus(SHARE_COPIED);
    return;
  } catch { /* no clipboard here: try the share sheet */ }
  if (mine !== shareSeq) return;
  if (navigator.share) {
    try {
      await navigator.share({ title: "Where should we meet?", url });
      return;
    } catch (err) {
      if (err && err.name === "AbortError") return;     // the visitor closed the share sheet
    }
  }
  if (mine !== shareSeq) return;
  const urlEl = $("share-url");
  urlEl.value = url;
  urlEl.hidden = false;
  urlEl.focus();
  urlEl.select();
  setStatus(SHARE_FAILED);
}

$("share-btn").addEventListener("click", shareGroup);

// The group is no longer the link's: a reload shouldn't bring the link's group back.
function forgetLink() {
  if (history.state && history.state[STATE_KEY]) history.replaceState(null, "", location.href);
}

// Open a group from a link: replace the group, then find a place for it once (`solveNow`;
// a group restored after a reload waits for Find). If this page can't give someone the public
// transport the link asks for (or its time of day or kind of place), say what changed and wait
// for Find instead. Returns false for a link that isn't a whole, valid group (nothing is
// changed then).
async function openShared(hash, { solveNow = true } = {}) {
  const group = decodeGroup(hash);
  // Out of the address bar, so it never shows an old group; into this tab's history entry, so
  // a reload (or a phone reloading a discarded tab) brings the group back without finding
  // again. groupChanged() forgets it once the visitor edits the group.
  history.replaceState(group ? { [STATE_KEY]: hash.replace(/^#/, "") } : null, "",
                       location.pathname + location.search);
  if (!group) {
    setStatus(SHARE_BAD_LINK, "error");
    return false;
  }
  // groupChanged() bumps openSeq too: any edit by the visitor (a new group, an added person)
  // while this link waits for the tables wins over the link.
  const mine = ++openSeq;
  touched = true;
  newAddition();
  clearTimeout(tapTimer); tapTimer = null;          // a map tap about to add someone
  clearTimeout(undoTimer); $("toast").hidden = true;  // Undo is for the old group
  hideShareUrl();                                     // so is a link shown for copying
  clearTimeout(resolveTimer);
  resolveTimer = null;
  solves.cancel();
  for (const m of markers.values()) m.remove();
  markers.clear();
  people = [];
  clearResults();
  exampleNote.hidden = true;
  setAddStatus("");
  renderPeople();
  updateFind();
  if (group.people.some((q) => q.mode === "transit")) {
    // Until the tables' status is known, nobody can be told public transport isn't here: a
    // server still loading them answers "loading", and this waits for the real answer.
    setStatus(SHARE_WAITING, "busy");
    await transitSettled;
    if (mine !== openSeq) return true;
  }
  const r = reconcileGroup(group, {
    transitOk, windows: (transit.windows || []).map((w) => w.id), venues: venueKinds,
  });
  const added = r.people.map((q) => addPerson({ name: q.name || defaultName(), named: !!q.name,
    lon: q.lon, lat: q.lat, mode: q.mode, label: SHARE_LABEL }, { quiet: true }));
  if (r.window) windowId = r.window;
  if (r.venue) venueId = r.venue;
  $("window-select").value = windowId;
  $("venue-select").value = venueId;
  renderPeople();
  updateTransitControls();
  updateFind();
  fitTo(people.map((p) => [p.lon, p.lat]));
  handOn();          // the group on screen is what the other tools get, and what comes back
  const changed = shareChanged(r.walkers.map((i) => added[i] && added[i].name).filter(Boolean),
                               r.windowGone, r.venueGone);
  if (changed) {
    setStatus(changed);
    return true;
  }
  if (!solveNow) {
    setStatus(SHARE_RESTORED);
    return true;
  }
  setStatus("");
  solve();
  return true;
}

// ── Example ─────────────────────────────────────────────────────
// Shown only on an untouched page: if the visitor has already started, it is dropped.
async function loadExample() {
  let ex;
  try {
    const resp = await fetch("example.json");
    if (!resp.ok) throw new Error("missing");
    ex = await resp.json();
    if (!Array.isArray(ex.participants) || !ex.result || !ex.result.best) throw new Error("invalid");
  } catch {
    if (!touched) setStatus("The example didn't load. Add people by searching or tapping the map.", "error");
    return;
  }
  if (touched || people.length) return;

  const added = ex.participants.map((q) =>
    addPerson({ name: q.name, lon: q.lon, lat: q.lat, mode: q.mode, label: q.label }, { quiet: true }));
  if (added.some((p) => !p)) return;
  for (const p of added) p.example = true;      // never handed on to the other tools
  const snap = {
    objective: ex.objective,
    people: added.map(({ id, name, lon, lat, mode, color }) => ({ id, name, lon, lat, mode, color })),
  };
  // The saved answer uses the example's own ids; map them onto the people just added.
  const ids = new Map(ex.participants.map((q, i) => [String(q.id), String(added[i].id)]));
  const remap = (c) => ({
    ...c,
    times: Object.fromEntries(Object.entries(c.times).map(([k, v]) => [ids.get(k), v])),
  });
  showResults({ ...ex.result, best: remap(ex.result.best), top: ex.result.top.map(remap),
                quickest: ex.result.quickest ? remap(ex.result.quickest) : null }, snap);
  updateFind();
  exampleNote.hidden = false;
}

// ── A pin from another tool ─────────────────────────────────────
// The pin becomes the group's first person, travelling the way they chose there. Public
// transport waits for the tables' status (addPerson says when it can't be had there). No
// solve: a group needs two people.
async function startFromHandoff() {
  touched = true;                       // the example must not land
  const mine = openSeq;
  if (HANDOFF.mode === "transit") await transitSettled;
  if (mine !== openSeq || people.length) return;    // the visitor started something meanwhile
  const { lon, lat, label } = HANDOFF.pin;
  setAddStatus("");
  const p = addPerson({ name: defaultName(), lon, lat, mode: HANDOFF.mode || "walking", label: label || "your pin" },
                      { quiet: true });
  if (!p) return;
  updateFind();
  updateTransitControls();
  handOn();          // outside London a transit pin walks here: the other tools hear walking too
  map.jumpTo({ center: [lon, lat], zoom: 13, padding: mapPadding(0) });
  if (!addStatusEl.textContent) setAddStatus("We started the group from your pin. Add at least one more person.");
}

// ── The group, back from another tool ───────────────────────────
// The group as it was when the visitor left for another tool in this tab: everyone, how they
// travel, when and where. It wins over a pin handed on since (a group is never overwritten), and
// waits for Find, as a shared group restored after a reload does.
async function restoreGroup(saved) {
  touched = true;
  const mine = openSeq;
  if (saved.people.some((q) => q.mode === "transit")) await transitSettled;
  if (mine !== openSeq || people.length) return;
  for (const q of saved.people) {
    const p = addPerson({ name: q.name || defaultName(), named: q.named, lon: q.lon, lat: q.lat, mode: q.mode,
                          label: q.label || "Picked on the map" }, { quiet: true });
    if (p && q.example) p.example = true;
  }
  if (saved.window) windowId = saved.window;
  if (saved.venue && (saved.venue === "station" || venueKinds.includes(saved.venue))) venueId = saved.venue;
  if ([...$("window-select").options].some((o) => o.value === windowId)) $("window-select").value = windowId;
  if ([...$("venue-select").options].some((o) => o.value === venueId)) $("venue-select").value = venueId;
  renderPeople();
  updateTransitControls();
  updateFind();
  handOn();
  fitTo(people.map((p) => [p.lon, p.lat]));
  setStatus(people.length >= 2 ? "Your group is back. Press Find to look for a place again." : "");
}

// ── Boot ────────────────────────────────────────────────────────
if (typeof maplibregl === "undefined") {
  setStatus("The map couldn't load. Check your connection and reload the page.", "error");
  findBtn.disabled = true;
  addBtn.disabled = true;
} else {
  initMap();
  renderPeople();
  updateFind();
  updateTransitControls();      // the hints for a page with no public transport, until the status says
  // A shared link opens its group instead of the example (a link opened before a reload comes
  // back without finding again); a broken one leaves the example.
  if (HANDOFF.when) windowId = HANDOFF.when;
  if (HANDOFF.mode && HANDOFF.mode !== "transit") setAddMode(HANDOFF.mode);
  // Then the group this tab left for another tool comes back; failing that, a pin handed on from
  // From here or Whole city starts the group; otherwise the example, and the offer to start from
  // where you are.
  const link = linkAtLoad(location.hash, history.state);
  const fresh = () => {
    const saved = readGroup();
    if (saved) { restoreGroup(saved); return; }
    if (HANDOFF.pin) { startFromHandoff(); return; }
    loadExample();
    offerLocation($("location-card"), { onYes: () => addMyLocation({ replaceExample: true }), onNo: () => locInput.focus() });
  };
  if (link) openShared(link.hash, { solveNow: link.solve }).then((ok) => { if (!ok) fresh(); });
  else fresh();
  loadTransitStatus();
  // A link pasted into this tab changes only the fragment.
  window.addEventListener("hashchange", () => { if (isGroupHash(location.hash)) openShared(location.hash); });
}
