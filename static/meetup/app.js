(() => {
  "use strict";

  const COLORS = ["#e63946","#2a9d8f","#e9c46a","#4361ee","#f4a261","#457b9d","#a8dadc","#c77dff"];
  const MODES  = { walking: "🚶", cycling: "🚴", driving: "🚗" };

  // ── State ───────────────────────────────────────────────────────
  let participants = [];
  let nextId       = 1;
  let map          = null;
  let pMarkers     = {};        // id → MapLibre Marker
  let pendingMarker = null;     // preview while typing location
  let resultMarkers = [];
  let bestMarker    = null;
  let areaAdded     = false;
  let pickingActive = false;
  let isochronesVisible = false; // toggle for showing isochrones
  let solvedTimes = null; // { participantId: minutes } from best solve result

  // ── DOM ─────────────────────────────────────────────────────────
  const nameInput  = document.getElementById("name-input");
  const locInput   = document.getElementById("loc-input");
  const pickBtn    = document.getElementById("pick-btn");
  const modeSelect = document.getElementById("mode-select");
  const addBtn     = document.getElementById("add-btn");
  const pList      = document.getElementById("participants-list");
  const findBtn    = document.getElementById("find-btn");
  const statusBar  = document.getElementById("status-bar");
  const resultsDiv = document.getElementById("results-section");

  // ── Geocoding (Nominatim + raw lat,lon) ─────────────────────────
  async function geocode(q) {
    const m = q.match(/^\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*$/);
    if (m) return { lat: +m[1], lon: +m[2] };
    const url = "https://nominatim.openstreetmap.org/search?format=json&limit=1&q="
                + encodeURIComponent(q);
    const data = await (await fetch(url)).json();
    if (!data.length) return null;
    return { lat: +data[0].lat, lon: +data[0].lon };
  }

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
        },
      },
      layers: [
        { id: "bg",    type: "background", paint: { "background-color": "#f0ede9" } },
        { id: "water", type: "fill",   source: "streets", "source-layer": "water",
          paint: { "fill-color": "#aad3df" } },
        { id: "green", type: "fill",   source: "streets", "source-layer": "landuse",
          filter: ["match",["get","class"],["park","garden","grass","forest","wood"],true,false],
          paint: { "fill-color": "#c8facc" } },
        { id: "bldg",  type: "fill",   source: "streets", "source-layer": "building",
          paint: { "fill-color": "#dfdbd7", "fill-outline-color": "#c8c4be" } },
        { id: "road-major", type: "line", source: "streets", "source-layer": "road",
          filter: ["match",["get","class"],["motorway","trunk","primary","secondary"],true,false],
          layout: { "line-join": "round", "line-cap": "round" },
          paint: { "line-color": "#fff",
                   "line-width": ["interpolate",["linear"],["zoom"],8,1,16,5] } },
        { id: "road-minor", type: "line", source: "streets", "source-layer": "road",
          filter: ["match",["get","class"],["tertiary","street","pedestrian","footway"],true,false],
          layout: { "line-join": "round", "line-cap": "round" },
          paint: { "line-color": "#fff", "line-width": 0.8 } },
        { id: "place", type: "symbol", source: "streets", "source-layer": "place_label",
          layout: {
            "text-field": ["get","name"],
            "text-font": ["Open Sans Regular","Arial Unicode MS Regular"],
            "text-size": ["interpolate",["linear"],["zoom"],8,10,14,13],
          },
          paint: { "text-color": "#444",
                   "text-halo-color": "rgba(255,255,255,0.85)", "text-halo-width": 1.5 } },
      ],
    };
  }

  function initMap() {
    map = new maplibregl.Map({
      container: "map",
      style: buildMapStyle(),
      center: [-0.118, 51.509],
      zoom: 10,
    });

    map.on("click", (e) => {
      if (!pickingActive) return;
      const { lng, lat } = e.lngLat;
      locInput.value = `${lat.toFixed(5)}, ${lng.toFixed(5)}`;
      showPendingMarker(lng, lat);
      setPicking(false);
    });
  }

  // ── Pick-mode toggle ─────────────────────────────────────────────
  function setPicking(on) {
    pickingActive = on;
    pickBtn.classList.toggle("active", on);
    document.body.classList.toggle("picking", on);
  }

  pickBtn.addEventListener("click", () => setPicking(!pickingActive));

  // ── Pending preview marker ───────────────────────────────────────
  function showPendingMarker(lon, lat) {
    if (pendingMarker) { pendingMarker.remove(); pendingMarker = null; }
    const el = document.createElement("div");
    el.className = "pending-marker";
    pendingMarker = new maplibregl.Marker({ element: el }).setLngLat([lon, lat]).addTo(map);
  }

  // Live geocode preview while typing
  let geoDebounce = null;
  locInput.addEventListener("input", () => {
    clearTimeout(geoDebounce);
    const q = locInput.value.trim();
    if (q.length < 3) return;
    geoDebounce = setTimeout(async () => {
      const geo = await geocode(q).catch(() => null);
      if (geo) showPendingMarker(geo.lon, geo.lat);
    }, 600);
  });

  // ── Render participant list ──────────────────────────────────────
  function renderList() {
    pList.innerHTML = participants.map(p => `
      <div class="participant-item" data-id="${p.id}">
        <div class="p-dot" style="--color:${p.color}"></div>
        <div class="p-info">
          <input class="p-name-edit" data-id="${p.id}" value="${esc(p.name)}" 
                 placeholder="Name" />
          <div class="p-controls">
            <select class="p-mode-edit" data-id="${p.id}">
              <option value="walking" ${p.mode==='walking'?'selected':''}>🚶 Walking</option>
              <option value="cycling" ${p.mode==='cycling'?'selected':''}>🚴 Cycling</option>
              <option value="driving" ${p.mode==='driving'?'selected':''}>🚗 Driving</option>
            </select>
            <small class="p-loc">${esc(p.locLabel)}</small>
          </div>
        </div>
        <button class="p-remove" data-id="${p.id}" title="Remove">×</button>
      </div>
    `).join("");

    // Delegate remove clicks
    pList.querySelectorAll(".p-remove").forEach(btn => {
      btn.addEventListener("click", () => removeParticipant(+btn.dataset.id));
    });

    // Delegate name edits
    pList.querySelectorAll(".p-name-edit").forEach(input => {
      input.addEventListener("change", () => {
        const id = +input.dataset.id;
        const p = participants.find(pp => pp.id === id);
        if (p) {
          p.name = input.value.trim() || "Unnamed";
          updateMarkerFor(p);
          clearResults();
        }
      });
    });

    // Delegate mode changes
    pList.querySelectorAll(".p-mode-edit").forEach(select => {
      select.addEventListener("change", () => {
        const id = +select.dataset.id;
        const p = participants.find(pp => pp.id === id);
        if (p) {
          p.mode = select.value;
          updateMarkerFor(p);
          clearResults();
        }
      });
    });

    findBtn.disabled = participants.length < 2;
  }

  // ── Add participant ──────────────────────────────────────────────
  async function doAdd() {
    const name = nameInput.value.trim();
    const locQ = locInput.value.trim();
    if (!name) { setStatus("Please enter a name.", true); nameInput.focus(); return; }
    if (!locQ) { setStatus("Please enter a location.", true); locInput.focus(); return; }

    addBtn.disabled = true;
    setStatus("Geocoding…");

    let geo;
    try   { geo = await geocode(locQ); }
    catch { geo = null; }
    addBtn.disabled = false;

    if (!geo) { setStatus("Location not found — try a different search.", true); return; }

    const color = COLORS[(nextId - 1) % COLORS.length];
    const p = { id: nextId++, name, lon: geo.lon, lat: geo.lat,
                mode: modeSelect.value, color, locLabel: locQ };
    participants.push(p);
    addMarkerFor(p);
    renderList();
    clearResults();
    setStatus("");

    // Reset form
    nameInput.value = "";
    locInput.value  = "";
    if (pendingMarker) { pendingMarker.remove(); pendingMarker = null; }
    nameInput.focus();
    fitToParticipants();
  }

  addBtn.addEventListener("click", doAdd);
  [nameInput, locInput].forEach(el =>
    el.addEventListener("keydown", e => { if (e.key === "Enter") doAdd(); })
  );

  // ── Remove participant ───────────────────────────────────────────
  function removeParticipant(id) {
    // Clean up isochrone layers for this participant
    const sourceId = `isochrone-${id}`;
    if (map.getLayer(`isochrone-fill-${id}`)) {
      map.removeLayer(`isochrone-fill-${id}`);
    }
    if (map.getLayer(`isochrone-line-${id}`)) {
      map.removeLayer(`isochrone-line-${id}`);
    }
    if (map.getSource(sourceId)) {
      map.removeSource(sourceId);
    }

    participants = participants.filter(p => p.id !== id);
    if (pMarkers[id]) { pMarkers[id].remove(); delete pMarkers[id]; }
    renderList();
    clearResults();
  }

  // ── Map markers for participants ─────────────────────────────────
  function addMarkerFor(p) {
    const el = document.createElement("div");
    el.className = "p-marker";
    el.style.background = p.color;
    el.title = p.name;
    el.textContent = p.name[0].toUpperCase();
    const marker = new maplibregl.Marker({ element: el, draggable: true })
      .setLngLat([p.lon, p.lat])
      .setPopup(new maplibregl.Popup({ offset: 25 })
        .setHTML(`<strong>${esc(p.name)}</strong><br>${MODES[p.mode]} ${p.mode}`))
      .addTo(map);
    
    // Update participant position on drag
    marker.on("dragend", () => {
      const { lng, lat } = marker.getLngLat();
      p.lon = lng;
      p.lat = lat;
      p.locLabel = `${lat.toFixed(5)}, ${lng.toFixed(5)}`;
      renderList();
      clearResults();
    });

    pMarkers[p.id] = marker;
  }

  function updateMarkerFor(p) {
    const marker = pMarkers[p.id];
    if (!marker) return;
    const el = marker.getElement();
    el.title = p.name;
    el.textContent = p.name[0].toUpperCase();
    marker.setPopup(new maplibregl.Popup({ offset: 25 })
      .setHTML(`<strong>${esc(p.name)}</strong><br>${MODES[p.mode]} ${p.mode}`));
  }

  function fitToParticipants() {
    if (participants.length === 0) return;
    if (participants.length === 1) {
      map.easeTo({ center: [participants[0].lon, participants[0].lat], zoom: 13 });
      return;
    }
    const lngs = participants.map(p => p.lon);
    const lats = participants.map(p => p.lat);
    map.fitBounds(
      [[Math.min(...lngs), Math.min(...lats)], [Math.max(...lngs), Math.max(...lats)]],
      { padding: 80 }
    );
  }

  // ── Find meeting point ───────────────────────────────────────────
  findBtn.addEventListener("click", async () => {
    if (participants.length < 2) return;
    findBtn.disabled = true;
    findBtn.textContent = "Finding…";
    setStatus("Computing optimal meeting point…");
    clearResults();

    const body = {
      participants: participants.map(p => ({
        id: String(p.id), lon: p.lon, lat: p.lat, mode: p.mode,
      })),
      objective: document.getElementById("objective").value,
      alpha: 0.65,
      search: { top_k: 5, margin_km: 3 },
    };

    try {
      const resp = await fetch("/api/meetup/solve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        throw new Error(err.detail || resp.statusText);
      }
      showResults(await resp.json());
      setStatus("");
    } catch (e) {
      setStatus("Error: " + e.message, true);
    }

    findBtn.disabled = false;
    findBtn.textContent = "Find Meeting Point";
  });

  // ── Show results ─────────────────────────────────────────────────
  function showResults(data) {
    // Store solved travel times for isochrone display
    solvedTimes = data.best.times || null;

    // Show isochrone toggle button after solving
    const isoBtn = document.getElementById("toggle-isochrones-btn");
    if (isoBtn) isoBtn.hidden = false;

    // Remove old result markers
    resultMarkers.forEach(m => m.remove());
    resultMarkers = [];
    if (bestMarker) { bestMarker.remove(); bestMarker = null; }

    // Meeting area polygon
    if (data.meeting_area_geojson) {
      const src = map.getSource("meeting-area");
      if (src) {
        src.setData(data.meeting_area_geojson);
      } else {
        map.addSource("meeting-area", { type: "geojson", data: data.meeting_area_geojson });
        map.addLayer({ id: "area-fill", type: "fill", source: "meeting-area",
          paint: { "fill-color": "#4361ee", "fill-opacity": 0.18 } });
        map.addLayer({ id: "area-line", type: "line", source: "meeting-area",
          paint: { "line-color": "#4361ee", "line-width": 1.5 } });
        areaAdded = true;
      }
    }

    // Runner-up candidate markers
    data.top.slice(1).forEach((c, i) => {
      const el = document.createElement("div");
      el.className = "cand-marker";
      el.textContent = i + 2;
      const m = new maplibregl.Marker({ element: el })
        .setLngLat([c.lon, c.lat])
        .setPopup(new maplibregl.Popup({ offset: 18 })
          .setHTML(`<b>Option ${i+2}</b><br>Max: ${c.max_time_min} min · Mean: ${c.mean_time_min} min`))
        .addTo(map);
      resultMarkers.push(m);
    });

    // Best marker
    const best = data.best;
    const el = document.createElement("div");
    el.className = "best-marker";
    el.innerHTML = "★";
    bestMarker = new maplibregl.Marker({ element: el })
      .setLngLat([best.lon, best.lat])
      .setPopup(new maplibregl.Popup({ offset: 28 })
        .setHTML(`<b>Best Meeting Point</b><br>Max: ${best.max_time_min} min · Mean: ${best.mean_time_min} min`))
      .addTo(map);

    map.easeTo({ center: [best.lon, best.lat], zoom: Math.max(map.getZoom(), 12) });

    // Sidebar results
    resultsDiv.hidden = false;
    resultsDiv.innerHTML = `
      <h2>Results</h2>
      ${[data.best, ...data.top.slice(1)].map((c, i) => `
        <div class="result-card${i === 0 ? " best" : ""}" data-lon="${c.lon}" data-lat="${c.lat}">
          <div class="result-rank">${i === 0 ? "⭐ Best Meeting Point" : `#${i+1}`}</div>
          <div class="result-times">
            Max travel: <strong>${c.max_time_min} min</strong> ·
            Mean: <strong>${c.mean_time_min} min</strong>
          </div>
          <div class="result-indiv">
            ${Object.entries(c.times).map(([id, t]) => {
              const p = participants.find(pp => String(pp.id) === id);
              return `<span style="color:${p?.color||'#333'}">${esc(p?.name||id)}: ${t} min</span>`;
            }).join("")}
          </div>
        </div>
      `).join("")}
    `;

    // Click card to fly to candidate
    resultsDiv.querySelectorAll(".result-card").forEach(card => {
      card.addEventListener("click", () => {
        map.easeTo({ center: [+card.dataset.lon, +card.dataset.lat], zoom: 14 });
      });
    });
  }

  // ── Clear results ────────────────────────────────────────────────
  function clearResults() {
    resultMarkers.forEach(m => m.remove());
    resultMarkers = [];
    if (bestMarker) { bestMarker.remove(); bestMarker = null; }
    resultsDiv.hidden = true;
    if (areaAdded && map.getSource("meeting-area")) {
      map.getSource("meeting-area").setData({ type: "FeatureCollection", features: [] });
    }
    // Clear solved times and hide isochrones
    solvedTimes = null;
    if (isochronesVisible) {
      isochronesVisible = false;
      const btn = document.getElementById("toggle-isochrones-btn");
      if (btn) { btn.textContent = "\uD83C\uDF10 Show Isochrones"; btn.classList.remove("active"); btn.hidden = true; }
    }
    clearIsochrones();
  }

  // ── Isochrone display ────────────────────────────────────────────
  async function updateIsochrones() {
    if (!isochronesVisible || participants.length === 0 || !solvedTimes) {
      clearIsochrones();
      return;
    }

    setStatus("Fetching isochrones…");

    // Fetch one isochrone per participant at their solved travel time
    const promises = participants.map(async (p) => {
      const minutes = Math.ceil(solvedTimes[String(p.id)]);
      if (!minutes || minutes <= 0) return null;
      try {
        const resp = await fetch("/api/isochrones", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            longitude: p.lon,
            latitude: p.lat,
            profile: p.mode,
            travel_times: [minutes],
          }),
        });
        if (!resp.ok) return null;
        const data = await resp.json();
        return { participant: p, isochrones: data, minutes };
      } catch {
        return null;
      }
    });

    const results = await Promise.all(promises);

    // Add/update layers for each participant
    results.forEach((result) => {
      if (!result) return;
      const { participant, isochrones } = result;
      const sourceId = `isochrone-${participant.id}`;
      const fillId = `isochrone-fill-${participant.id}`;
      const lineId = `isochrone-line-${participant.id}`;

      const src = map.getSource(sourceId);
      if (src) {
        src.setData(isochrones);
      } else {
        map.addSource(sourceId, { type: "geojson", data: isochrones });
        map.addLayer({
          id: fillId,
          type: "fill",
          source: sourceId,
          paint: {
            "fill-color": participant.color,
            "fill-opacity": 0.15,
          },
        });
        map.addLayer({
          id: lineId,
          type: "line",
          source: sourceId,
          paint: {
            "line-color": participant.color,
            "line-width": 1.5,
            "line-opacity": 0.6,
          },
        });
      }
    });

    setStatus("");
  }

  function clearIsochrones() {
    participants.forEach(p => {
      const sourceId = `isochrone-${p.id}`;
      if (map.getLayer(`isochrone-fill-${p.id}`)) {
        map.removeLayer(`isochrone-fill-${p.id}`);
      }
      if (map.getLayer(`isochrone-line-${p.id}`)) {
        map.removeLayer(`isochrone-line-${p.id}`);
      }
      if (map.getSource(sourceId)) {
        map.removeSource(sourceId);
      }
    });
  }

  function toggleIsochrones() {
    isochronesVisible = !isochronesVisible;
    const btn = document.getElementById("toggle-isochrones-btn");
    if (btn) {
      btn.textContent = isochronesVisible ? "🌐 Hide Isochrones" : "🌐 Show Isochrones";
      btn.classList.toggle("active", isochronesVisible);
    }
    updateIsochrones();
  }

  // ── Helpers ──────────────────────────────────────────────────────
  function setStatus(msg, isError = false) {
    statusBar.textContent = msg;
    statusBar.hidden = !msg;
    statusBar.classList.toggle("error", isError);
  }

  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // ── Boot ─────────────────────────────────────────────────────────
  initMap();
  renderList();
  
  // Wire up isochrone toggle button
  const toggleIsoBtn = document.getElementById("toggle-isochrones-btn");
  if (toggleIsoBtn) {
    toggleIsoBtn.addEventListener("click", toggleIsochrones);
  }
})();
