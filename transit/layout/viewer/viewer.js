// The stations-only London layout viewer. A classic script, so the page opens from disk:
// layout.js (written by `python -m transit.layout`) sets window.LAYOUT before this runs.
// It draws each solved frame as it is; the slider never shows a layout nobody solved.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const L = window.LAYOUT;
  if (!L || !Array.isArray(L.frames) || !L.frames.length) {
    $("status").textContent = "No layout here: run python -m transit.layout --build <id>, then open the index.html it writes.";
    return;
  }

  // Viridis, low leftover stress dark, high bright.
  const RAMP = ["#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"];
  const n = L.nodes.length;
  const last = L.frames.length - 1;
  const top = L.stress_scale[1];
  const canvas = $("map");
  const ctx = canvas.getContext("2d");
  const slider = $("frame");
  const state = { frame: 0, labels: true, line: false, hover: -1, cx: 0, cy: 0, scale: 1, w: 0, h: 0 };

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function hexToRgb(h) {
    const v = parseInt(h.slice(1), 16);
    return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
  }
  const rampRgb = RAMP.map(hexToRgb);
  function colour(v) {
    const t = Math.max(0, Math.min(1, v / top)) * (rampRgb.length - 1);
    const i = Math.min(rampRgb.length - 2, Math.floor(t));
    const f = t - i;
    const c = rampRgb[i].map((a, k) => Math.round(a + (rampRgb[i + 1][k] - a) * f));
    return `rgb(${c[0]},${c[1]},${c[2]})`;
  }

  // One extent for every frame, so the view stays put while the slider moves.
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const f of L.frames) {
    for (let i = 0; i < n; i++) {
      minX = Math.min(minX, f.x[i]); maxX = Math.max(maxX, f.x[i]);
      minY = Math.min(minY, f.y[i]); maxY = Math.max(maxY, f.y[i]);
    }
  }

  function fit() {
    const pad = 28;
    state.scale = Math.min((state.w - 2 * pad) / (maxX - minX || 1), (state.h - 2 * pad) / (maxY - minY || 1));
    state.cx = (minX + maxX) / 2;
    state.cy = (minY + maxY) / 2;
  }
  function toScreen(x, y) {
    return [(x - state.cx) * state.scale + state.w / 2, (y - state.cy) * state.scale + state.h / 2];
  }

  function resize() {
    const r = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const fresh = state.w === 0;
    state.w = r.width; state.h = r.height;
    canvas.width = Math.round(r.width * dpr);
    canvas.height = Math.round(r.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (fresh) fit();
    draw();
  }

  function niceKm(px) {
    const km = px / state.scale / 1000;
    const p = Math.pow(10, Math.floor(Math.log10(km)));
    for (const m of [5, 2, 1]) if (m * p <= km) return m * p;
    return p;
  }

  function draw() {
    const f = L.frames[state.frame];
    ctx.fillStyle = css("--bg");
    ctx.fillRect(0, 0, state.w, state.h);
    const P = new Array(n);
    for (let i = 0; i < n; i++) P[i] = toScreen(f.x[i], f.y[i]);

    if (state.line) {
      ctx.strokeStyle = css("--met");
      ctx.lineWidth = 2.5;
      ctx.lineJoin = "round";
      for (const run of L.lines) {
        ctx.beginPath();
        run.nodes.forEach((i, k) => (k ? ctx.lineTo(P[i][0], P[i][1]) : ctx.moveTo(P[i][0], P[i][1])));
        ctx.stroke();
      }
    }

    const r = Math.max(2.5, Math.min(5, state.scale * 300));
    ctx.lineWidth = 1;
    ctx.strokeStyle = css("--dot-edge");
    for (let i = 0; i < n; i++) {
      ctx.beginPath();
      ctx.arc(P[i][0], P[i][1], i === state.hover ? r + 2 : r, 0, 2 * Math.PI);
      ctx.fillStyle = colour(f.stress[i]);
      ctx.fill();
      ctx.stroke();
    }

    if (state.labels) {
      ctx.font = "600 12px system-ui, sans-serif";
      ctx.textBaseline = "middle";
      const boxes = [];
      const hits = (box) => box[0] < 0 || box[2] > state.w || box[1] < 0 || box[3] > state.h ||
        boxes.some((b) => box[0] < b[2] && b[0] < box[2] && box[1] < b[3] && b[1] < box[3]);
      for (const i of L.labels) {
        const text = L.nodes[i].label;
        const wdt = ctx.measureText(text).width;
        // Right of the dot, left, above, below; dropped only when all four collide. Labels are
        // placed in checks.toml's order, so the central ones listed first win.
        const [px, py] = P[i];
        const spots = [[px + r + 4, py], [px - r - 4 - wdt, py], [px - wdt / 2, py - r - 11], [px - wdt / 2, py + r + 11]];
        const spot = spots.find(([x, y]) => !hits([x - 2, y - 8, x + wdt + 2, y + 8]));
        if (!spot) continue;
        const [x, y] = spot;
        boxes.push([x - 2, y - 8, x + wdt + 2, y + 8]);
        ctx.lineWidth = 3;
        ctx.strokeStyle = css("--halo");
        ctx.strokeText(text, x, y);
        ctx.fillStyle = css("--fg");
        ctx.fillText(text, x, y);
      }
    }

    // Scale bar: metres on the map, and on the pure time map the minutes they stand for.
    const km = niceKm(110);
    const len = km * 1000 * state.scale;
    const x0 = 16, y0 = state.h - 18;
    ctx.strokeStyle = css("--fg");
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x0, y0); ctx.lineTo(x0 + len, y0);
    ctx.stroke();
    ctx.fillStyle = css("--fg");
    ctx.font = "12px system-ui, sans-serif";
    ctx.textBaseline = "bottom";
    const mins = (km * 1000) / L.s_m_per_min;
    ctx.fillText(state.frame === last ? `${km} km ≈ ${mins.toFixed(mins < 10 ? 1 : 0)} min` : `${km} km`, x0, y0 - 4);
  }

  function fmt(v, d) { return v === null || v === undefined ? "–" : Number(v).toFixed(d); }
  function readout() {
    const f = L.frames[state.frame];
    const m = f.metrics;
    const name = state.frame === 0 ? "Real map" : state.frame === last && f.lam_tilde === 0
      ? "Pure time map (λ = 0)" : `λ̃ = ${Number(f.lam_tilde).toPrecision(2)}`;
    const wem = m.wembley_ratio === null || m.wembley_ratio === undefined ? m.wembley
      : `${fmt(m.wembley_ratio, 2)}× the median ratio (${m.wembley})`;
    // Fold-free is the report's rule: no flipped triangle and no crossing boundary edges.
    const folds = m.folded === 0 && m.boundary_crossings === 0 ? "fold-free"
      : `folds ${m.folded} (${fmt(m.folded_area * 100, 1)}% of the area), ${m.boundary_crossings} boundary crossings`;
    $("readout").textContent = `${name} · stress σ₁ ${fmt(m.sigma1, 3)} · trustworthiness ${fmt(m.T, 3)}, ` +
      `continuity ${fmt(m.C, 3)} · ${folds} · Wembley Park–Baker Street ${wem}` +
      ` · Metropolitan line breaks ${m.met_breaks}`;
  }

  function nearest(px, py) {
    const f = L.frames[state.frame];
    let best = -1, bd = 14 * 14;
    for (let i = 0; i < n; i++) {
      const [x, y] = toScreen(f.x[i], f.y[i]);
      const d = (x - px) ** 2 + (y - py) ** 2;
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  }
  function showTip(i, px, py) {
    const tip = $("tip");
    if (i < 0) { tip.hidden = true; return; }
    const f = L.frames[state.frame], f0 = L.frames[0];
    const moved = Math.hypot(f.x[i] - f0.x[i], f.y[i] - f0.y[i]) / 1000;
    const node = L.nodes[i];
    tip.textContent = `${node.label}${node.name !== node.label ? ` (${node.name})` : ""} · leftover stress ` +
      `${fmt(f.stress[i], 3)}` +
      (state.frame ? ` · moved ${moved.toFixed(1)} km` : "");
    tip.hidden = false;
    tip.style.left = `${Math.min(px + 12, state.w - 270)}px`;
    tip.style.top = `${Math.max(4, py - 34)}px`;
  }

  // A press that barely moves is a tap: it shows the nearest station, which is how touch,
  // with no hover, opens a station's details. Anything longer pans.
  let drag = null;
  canvas.addEventListener("pointerdown", (e) => {
    drag = { x: e.offsetX, y: e.offsetY, cx: state.cx, cy: state.cy, moved: false };
    try { canvas.setPointerCapture(e.pointerId); } catch { /* the pointer is already gone */ }
    canvas.classList.add("dragging");
  });
  canvas.addEventListener("pointermove", (e) => {
    if (drag) {
      if (Math.hypot(e.offsetX - drag.x, e.offsetY - drag.y) > 6) drag.moved = true;
      if (!drag.moved) return;
      state.cx = drag.cx - (e.offsetX - drag.x) / state.scale;
      state.cy = drag.cy - (e.offsetY - drag.y) / state.scale;
      draw();
      return;
    }
    const i = nearest(e.offsetX, e.offsetY);
    if (i !== state.hover) { state.hover = i; draw(); }
    showTip(i, e.offsetX, e.offsetY);
  });
  const endDrag = () => { drag = null; canvas.classList.remove("dragging"); };
  canvas.addEventListener("pointerup", (e) => {
    const tap = drag && !drag.moved;
    endDrag();
    if (tap) {
      const i = nearest(e.offsetX, e.offsetY);
      state.hover = i;
      draw();
      showTip(i, e.offsetX, e.offsetY);
    }
  });
  canvas.addEventListener("pointercancel", endDrag);
  canvas.addEventListener("pointerleave", (e) => {
    if (e.pointerType === "touch") return;    // a touch pointer leaves after every tap; keep its details
    state.hover = -1; showTip(-1); draw();
  });
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const k = Math.exp(-e.deltaY * 0.0015);
    const wx = (e.offsetX - state.w / 2) / state.scale + state.cx;
    const wy = (e.offsetY - state.h / 2) / state.scale + state.cy;
    state.scale *= k;
    state.cx = wx - (e.offsetX - state.w / 2) / state.scale;
    state.cy = wy - (e.offsetY - state.h / 2) / state.scale;
    draw();
  }, { passive: false });
  canvas.addEventListener("dblclick", () => { fit(); draw(); });
  $("fit").addEventListener("click", () => { fit(); draw(); });

  slider.max = String(last);
  slider.addEventListener("input", () => { state.frame = Number(slider.value); readout(); draw(); });
  $("labels").addEventListener("change", (e) => { state.labels = e.target.checked; draw(); });
  $("line").addEventListener("change", (e) => { state.line = e.target.checked; draw(); });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
  // The canvas changes size with the window, and when the readout wraps onto more or fewer lines.
  new ResizeObserver(() => resize()).observe(canvas);

  $("ramp").style.background = `linear-gradient(to right, ${RAMP.join(", ")})`;
  $("ramp-hi").textContent = `${top}+`;
  if (L.notice) $("notice").textContent = L.notice;
  const expired = L.valid_until && new Date() > new Date(`${L.valid_until}T23:59:59`);
  $("credits").textContent = `Build ${L.build}, ${L.window} (departing ${L.departure}); tables valid until ` +
    `${L.valid_until}${expired ? " (EXPIRED: rebuild them)" : ""}. ` + (L.attribution || []).join(" · ");
  if (L.left_out && L.left_out.length) {
    $("left-out").textContent = "Left out, not joined both ways to the rest: " +
      L.left_out.map((g) => `${g.names.join(", ")} (${g.names.length})`).join("; ") + ".";
  }
  readout();
  resize();
})();
