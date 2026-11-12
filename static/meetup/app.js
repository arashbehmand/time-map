// "Where should we meet?"
//
// People are added by search, by "Use my location" or by tapping the map. Find asks the
// server for the place that keeps the longest trip shortest ("Fairest") or the average trip
// shortest. Only the latest answer counts: a change to the group throws away an answer
// still on its way, and if one was on its way, the newest group is solved instead.
import { createLatest } from "/shared/latest.js";
import { esc, friendly, geocode, postJSON, savePref } from "/shared/api.js";

const COLORS = ["#c62828", "#00796b", "#6a1b9a", "#1565c0", "#ef6c00", "#2e7d32", "#ad1457", "#4e342e"];
const MODE_ING = { walking: "walking", cycling: "cycling", driving: "driving" };
const MAX_PARTICIPANTS = 8;        // server cap (server/meetup/models.py)
const MAX_REACH_MINUTES = 60;      // routing limit for "how far each person can get"
const LOCATION_PREF = "timemap-location";
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

// ── DOM ─────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const nameInput = $("name-input");
const locInput = $("loc-input");
const modeSelect = $("mode-select");
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

function objective() {
  return document.querySelector('input[name="objective"]:checked').value;
}

const mins = (t) => Math.round(t);

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
      const name = nameInput.value.trim() || defaultName();
      if (addPerson({ name, lon: lng, lat, mode: modeSelect.value, label: "Picked on the map" })) {
        setAddStatus("");
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
// `quiet`: the example is being set up, not edited by the visitor.
function addPerson({ name, lon, lat, mode, label }, { quiet = false } = {}) {
  if (people.length >= MAX_PARTICIPANTS) {
    setAddStatus(`A group can have up to ${MAX_PARTICIPANTS} people.`, "error");
    return null;
  }
  const id = nextId++;
  const p = { id, name, lon, lat, mode, label, color: COLORS[(id - 1) % COLORS.length] };
  people.push(p);
  addMarker(p);
  renderPeople();
  if (!quiet) groupChanged();
  return p;
}

function removePerson(id) {
  const index = people.findIndex((p) => p.id === id);
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
  const wasSolving = solves.busy || resolveTimer !== null;
  const hadAnswer = lastAnswer !== null;
  solves.cancel();
  clearTimeout(resolveTimer);
  resolveTimer = null;
  clearResults();
  exampleNote.hidden = true;
  updateFind();
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

function renderPeople() {
  peopleList.innerHTML = people.map((p, i) => `
    <li class="person" style="--color:${p.color}" data-id="${p.id}">
      <span class="dot" aria-hidden="true"></span>
      <input type="text" class="p-name" value="${esc(p.name)}" aria-label="Name of person ${i + 1}" />
      <button type="button" class="remove" aria-label="Remove ${esc(p.name)}">×</button>
      <div class="where">
        <select class="p-mode" aria-label="How ${esc(p.name)} travels">
          ${Object.keys(MODE_ING).map((m) =>
            `<option value="${m}"${p.mode === m ? " selected" : ""}>${MODE_ING[m][0].toUpperCase() + MODE_ING[m].slice(1)}</option>`).join("")}
        </select>
        <span>from ${esc(p.label)}</span>
      </div>
    </li>`).join("");

  for (const li of peopleList.querySelectorAll(".person")) {
    const p = people.find((pp) => pp.id === +li.dataset.id);
    li.querySelector(".remove").addEventListener("click", () => removePerson(p.id));
    const nameEl = li.querySelector(".p-name");
    nameEl.addEventListener("change", () => {
      // A name doesn't change the answer: keep it and show the new name.
      p.name = nameEl.value.trim() || defaultName();
      nameEl.value = p.name;
      li.querySelector(".remove").setAttribute("aria-label", `Remove ${p.name}`);
      li.querySelector(".p-mode").setAttribute("aria-label", `How ${p.name} travels`);
      updateMarker(p);
      touched = true;
      if (lastAnswer) renderAnswer();
    });
    const modeEl = li.querySelector(".p-mode");
    modeEl.addEventListener("change", () => {
      p.mode = modeEl.value;
      updateMarker(p);
      groupChanged();
    });
  }
  $("people-empty").hidden = people.length > 0;
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
    renderPeople();
    groupChanged();
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

function fitTo(points, maxZoom = 14) {
  if (!points.length) return;
  if (points.length === 1) {
    map.easeTo({ center: points[0], zoom: Math.max(map.getZoom(), 13) });
    return;
  }
  const lngs = points.map((pt) => pt[0]), lats = points.map((pt) => pt[1]);
  map.fitBounds([[Math.min(...lngs), Math.min(...lats)], [Math.max(...lngs), Math.max(...lats)]],
    { padding: 60, maxZoom, duration: 600 });
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
  const form = { name: nameInput.value.trim(), q: locInput.value.trim(), mode: modeSelect.value };
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
  const p = addPerson({ name: form.name || defaultName(), lon: res.value.lon, lat: res.value.lat,
                        mode: form.mode, label: form.q });
  if (!p) return;
  // Clear the fields only if they still hold what was searched for.
  if (nameInput.value.trim() === form.name) nameInput.value = "";
  if (locInput.value.trim() === form.q) locInput.value = "";
  nameInput.focus();
  fitTo(people.map((pp) => [pp.lon, pp.lat]));
});

$("locate-btn").addEventListener("click", () => {
  if (!navigator.geolocation) {
    setAddStatus("This browser can't share your location. Search for a place or tap the map.", "error");
    return;
  }
  const form = { name: nameInput.value.trim(), mode: modeSelect.value };
  const mine = newAddition();
  setAddStatus("Finding your location…", "busy");
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      if (mine !== addSeq) return;
      addSeq++;
      savePref(LOCATION_PREF, "yes");
      setAddStatus("");
      const p = addPerson({ name: form.name || defaultName(), lon: pos.coords.longitude,
                            lat: pos.coords.latitude, mode: form.mode, label: "their location" });
      if (!p) return;
      if (nameInput.value.trim() === form.name) nameInput.value = "";
      fitTo(people.map((pp) => [pp.lon, pp.lat]));
    },
    (err) => {
      if (mine !== addSeq) return;
      setAddStatus(err.code === 1
        ? "Location is turned off for this site. Search for a place or tap the map."
        : "We couldn't get your location. Search for a place or tap the map.", "error");
    },
    { timeout: 15000, maximumAge: 60000 },
  );
});

$("new-group-btn").addEventListener("click", () => {
  newAddition();
  clearTimeout(tapTimer); tapTimer = null;
  clearGroup();
  setStatus("");
  setAddStatus("");
  locInput.focus();
});

for (const radio of document.querySelectorAll('input[name="objective"]')) {
  radio.addEventListener("change", () => {
    $("objective-hint").textContent = objective() === "sum"
      ? "Quickest on average keeps the total travel time as low as possible."
      : "Fairest keeps the longest trip as short as possible.";
    groupChanged();
  });
}

// ── Find ────────────────────────────────────────────────────────
async function solve() {
  if (people.length < 2) return;
  const snap = {
    objective: objective(),
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
      search: { top_k: 4 },
    }, signal));
  } catch (err) {
    updateFind();
    setStatus(friendly(err), "error");
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
function headline(best, objectiveValue) {
  return objectiveValue === "sum"
    ? `Meet here (★ on the map). The average trip is ${mins(best.mean_time_min)} min; the longest is ${mins(best.max_time_min)} min.`
    : `Meet here (★ on the map). Everyone gets there within ${mins(best.max_time_min)} min.`;
}

function meaning(objectiveValue) {
  return objectiveValue === "sum"
    ? "Quickest on average: the total travel time is as low as it can be."
    : "Fairest: the longest trip is as short as it can be.";
}

function altText(c, objectiveValue) {
  return objectiveValue === "sum"
    ? `average trip ${mins(c.mean_time_min)} min`
    : `longest trip ${mins(c.max_time_min)} min`;
}

// The answer is for the people in its snapshot; names are read live because a rename
// doesn't change the answer (any other edit clears it).
function nameOf(sp) {
  const live = people.find((p) => p.id === sp.id);
  return live ? live.name : sp.name;
}

function showResults(data, snap) {
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

  renderAnswer();
  fitTo([...snap.people.map((p) => [p.lon, p.lat]), [best.lon, best.lat]], 14);
}

function renderAnswer() {
  const { data, snap } = lastAnswer;
  const best = data.best;
  const alts = data.top.slice(1, 4);
  const times = snap.people.map((p) => ({ p, t: best.times[String(p.id)] }));
  const longest = Math.max(...times.map((x) => x.t));
  const width = (t) => (longest > 0 ? Math.max(4, (t / longest) * 100) : 100).toFixed(1);
  answerEl.innerHTML = `
    <div class="answer-card">
      <p class="headline">${esc(headline(best, snap.objective))}</p>
      <p class="hint">${esc(meaning(snap.objective))}</p>
      <ul class="bars" aria-label="Each person's trip">
        ${times.map(({ p, t }) => `
          <li class="bar-row" style="--color:${p.color}">
            <span><strong>${esc(nameOf(p))}</strong> · ${mins(t)} min ${MODE_ING[p.mode]}${t === longest && times.length > 1 ? " (longest)" : ""}</span>
            <span class="track" aria-hidden="true"><span class="fill" style="width:${width(t)}%"></span></span>
          </li>`).join("")}
      </ul>
      <button type="button" class="btn" id="reach-btn" aria-pressed="${reachShown}">${reachShown ? "Hide reach" : "Show how far each person can get"}</button>
    </div>
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
  btn.textContent = on ? "Hide reach" : "Show how far each person can get";
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
  const radio = document.querySelector(`input[name="objective"][value="${ex.objective}"]`);
  if (radio) radio.checked = true;
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
  showResults({ ...ex.result, best: remap(ex.result.best), top: ex.result.top.map(remap) }, snap);
  updateFind();
  exampleNote.hidden = false;
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
  loadExample();
}
