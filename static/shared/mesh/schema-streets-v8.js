// Everything the mesh renderer knows about the tile schema (Mapbox Streets v8) lives here:
// which layers and classes are drawn, in which pass, with which colours and widths, and
// which features become labels. If the base map changes (#2), this file changes; the
// renderer, the worker and the warp don't. Pure.
//
// Water, park and road colours and widths are copied from the warp page's MapLibre style
// (static/warp/app.js, buildStyle). Rail is new (that style has none), and buildings, minor
// streets and road names are left out, so the mesh at u = 0 is a reduced ordinary map.

export const SCHEMA = "mapbox-streets-v8";
export const TILE_URL = (z, x, y) => `/tiles/${z}/${x}/${y}`;

// Draw order. Fills first, then lines: road casings under road fills, and rail on top.
export const PASSES = [
  { name: "water", kind: "fill" },
  { name: "park", kind: "fill" },
  { name: "roadCase", kind: "line", buffer: "road", casing: true },
  { name: "road", kind: "line" },
  { name: "rail", kind: "line" },
];

// One entry per style id. Widths: [[zoom, px], …], linear between stops, clamped outside.
// bent: the line's strength on the fully bent map in the scale-aware style. There the base
// map is context and the stations and the network carry the picture, so minor classes
// recede most. Rail recedes too: between stations it is the geometry the bend tears most.
export const STYLES = [
  { name: "water", color: "#aad3df" },
  { name: "park", color: "#c8facc" },
  { name: "major_rail", color: "#8c8c8c", width: [[10, 1], [16, 2.5]], bent: 0.45 },
  { name: "minor_rail", color: "#a6a6a6", width: [[10, 0.8], [16, 1.8]], bent: 0.35 },
  { name: "rail_tunnel", color: "#b6aecb", width: [[10, 1], [16, 2.5]], bent: 0.45 },
  { name: "motorway", color: "#e892a2", width: [[10, 2.5], [16, 7]], casing: "#e05f4f", casingWidth: [[10, 4], [16, 10]], bent: 0.8 },
  { name: "trunk", color: "#f9b29c", width: [[10, 2], [16, 6]], casing: "#c89b7a", casingWidth: [[10, 2], [16, 7]], bent: 0.7 },
  { name: "primary", color: "#fcd6a4", width: [[10, 1.5], [16, 5]], casing: "#c89b7a", casingWidth: [[10, 2], [16, 7]], bent: 0.55 },
  { name: "secondary", color: "#f7fabf", width: [[10, 1], [16, 4]], casing: "#e0d8c0", casingWidth: [[10, 1], [16, 5]], bent: 0.4 },
];
export const STYLE_ID = Object.fromEntries(STYLES.map((s, i) => [s.name, i]));
export const BACKGROUND = "#f0ede9";

const PARK_CLASSES = new Set(["park", "garden", "cemetery", "grass", "scrub", "forest", "wood"]);
// Slip roads draw as their road (Streets v8 has a *_link class for each).
const ROAD_CLASS = { motorway: "motorway", motorway_link: "motorway", trunk: "trunk", trunk_link: "trunk",
  primary: "primary", primary_link: "primary", secondary: "secondary", secondary_link: "secondary" };
const SECONDARY_MIN_ZOOM = 12;     // decoded from z12 up; packScene keeps them near the origin only
export const NEAR_FIELD_M = 2000;  // secondary roads are drawn within this distance of the origin
const STATION_MODES = new Set(["rail", "metro_rail", "light_rail"]);

// Name fallback, as the old server-side warp had it (vector_transform.py, _pick_text).
export function pickName(props) {
  for (const key of ["name", "name_en", "name:en", "ref"]) {
    const v = props[key];
    if (v !== undefined && v !== null && v !== "") return String(v);
  }
  return null;
}

// One decoded feature → what to draw, or null. geomType: 1 point, 2 line, 3 polygon.
export function classify(layer, props, geomType, tileZ) {
  if (layer === "water" && geomType === 3) return { kind: "fill", style: STYLE_ID.water, cat: "water" };
  if (layer === "landuse" && geomType === 3 && PARK_CLASSES.has(props.class)) {
    return { kind: "fill", style: STYLE_ID.park, cat: "park" };
  }
  if (layer === "road" && geomType === 2) {
    const c = props.class;
    if (c === "major_rail" || c === "minor_rail") {
      const style = props.structure === "tunnel" ? STYLE_ID.rail_tunnel : STYLE_ID[c];
      return { kind: "line", pass: "rail", style, cat: c };
    }
    const road = ROAD_CLASS[c];
    if (road && (road !== "secondary" || tileZ >= SECONDARY_MIN_ZOOM)) {
      return { kind: "line", pass: "road", style: STYLE_ID[road], cat: road };
    }
    return null;
  }
  if (layer === "transit_stop_label" && geomType === 1) {
    if (props.stop_type !== "station" || !STATION_MODES.has(props.mode)) return null;
    const text = pickName(props);
    return text ? { kind: "label", type: "station", text, rank: Number(props.filterrank ?? 3) } : null;
  }
  if (layer === "place_label" && geomType === 1) {
    if (props.class !== "settlement" && props.class !== "settlement_subdivision") return null;
    const text = pickName(props);
    return text ? { kind: "label", type: "place", text, rank: Number(props.symbolrank ?? 18) } : null;
  }
  return null;
}

export function widthAt(stops, zoom) {
  if (!stops) return 1;
  if (zoom <= stops[0][0]) return stops[0][1];
  for (let i = 1; i < stops.length; i++) {
    if (zoom <= stops[i][0]) {
      const [z0, w0] = stops[i - 1], [z1, w1] = stops[i];
      return w0 + ((w1 - w0) * (zoom - z0)) / (z1 - z0);
    }
  }
  return stops[stops.length - 1][1];
}

export function hexToRgba(hex, a = 1) {
  const n = parseInt(hex.slice(1), 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255, a];
}

// MapLibre styles for the lab: an empty one (camera and gestures only: no tiles, no glyphs),
// and the same layers as the mesh, to check the two line up.
export function maplibreStyle(origin, { withData = false } = {}) {
  const layers = [{ id: "bg", type: "background", paint: { "background-color": BACKGROUND } }];
  const style = { version: 8, sources: {}, layers };
  if (!withData) return style;
  style.sources.streets = {
    type: "vector", tiles: [`${origin}/tiles/{z}/{x}/{y}`], minzoom: 0, maxzoom: 16,
    attribution: "© Mapbox © OpenStreetMap",
  };
  const interp = (stops) => ["interpolate", ["linear"], ["zoom"], ...stops.flat()];
  const src = { source: "streets" };
  layers.push(
    { id: "water", type: "fill", ...src, "source-layer": "water", paint: { "fill-color": STYLES[0].color } },
    { id: "park", type: "fill", ...src, "source-layer": "landuse",
      filter: ["match", ["get", "class"], [...PARK_CLASSES], true, false],
      paint: { "fill-color": STYLES[1].color } },
  );
  for (const name of ["major_rail", "minor_rail"]) {
    layers.push({ id: name, type: "line", ...src, "source-layer": "road",
      filter: ["==", ["get", "class"], name],
      paint: { "line-color": STYLES[STYLE_ID[name]].color, "line-width": interp(STYLES[STYLE_ID[name]].width) } });
  }
  for (const casing of [true, false]) {
    for (const name of ["secondary", "primary", "trunk", "motorway"]) {
      const s = STYLES[STYLE_ID[name]];
      layers.push({ id: `${name}${casing ? "-case" : ""}`, type: "line", ...src, "source-layer": "road",
        filter: ["match", ["get", "class"], [name, `${name}_link`], true, false], layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": casing ? s.casing : s.color, "line-width": interp(casing ? s.casingWidth : s.width) } });
    }
  }
  return style;
}
