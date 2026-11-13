// SYNTHETIC base-map geometry for the lab (?geom=synthetic): a rough London made of the toy
// network's rail lines, ring and radial roads, a Thames band, a lake with an island (a
// hole), parks and place names. It feeds the same tessellate → pack → draw path as real
// tiles, so the lab's checks and benchmark run with no tiles token and no Mapbox data.
// Pure: the worker builds one from stations.json.
import { mercator } from "../../shared/mesh/frame.js";

const EXTENT = 4096;
const CENTRE = [-0.11, 51.505];
const M_PER_DEG_LAT = 111_320;
const M_PER_DEG_LON = 111_320 * Math.cos((51.5 * Math.PI) / 180);

const offset = ([lon, lat], dx, dy) => [lon + dx / M_PER_DEG_LON, lat - dy / M_PER_DEG_LAT];
const circle = (c, r, n = 48) => Array.from({ length: n }, (_, i) =>
  offset(c, r * Math.cos((2 * Math.PI * i) / n), r * Math.sin((2 * Math.PI * i) / n)));
const rect = (c, w, h) => [offset(c, -w / 2, -h / 2), offset(c, w / 2, -h / 2), offset(c, w / 2, h / 2), offset(c, -w / 2, h / 2)];

const THAMES = [
  [-0.338, 51.403], [-0.305, 51.410], [-0.302, 51.433], [-0.307, 51.458], [-0.290, 51.475],
  [-0.255, 51.488], [-0.230, 51.487], [-0.215, 51.475], [-0.195, 51.468], [-0.175, 51.480],
  [-0.150, 51.485], [-0.125, 51.493], [-0.120, 51.505], [-0.100, 51.509], [-0.080, 51.507],
  [-0.060, 51.505], [-0.040, 51.500], [-0.030, 51.507], [-0.010, 51.507], [0.000, 51.498],
  [-0.005, 51.487], [0.010, 51.483], [0.012, 51.495], [0.030, 51.503], [0.070, 51.497],
  [0.110, 51.505], [0.150, 51.500],
];
const PARKS = [
  ["Hyde Park", [-0.165, 51.508], 700], ["Regent's Park", [-0.154, 51.528], 600],
  ["Richmond Park", [-0.275, 51.442], 1800], ["Hampstead Heath", [-0.165, 51.563], 900],
  ["Greenwich Park", [0.0, 51.477], 450], ["Victoria Park", [-0.04, 51.536], 500],
  ["Fryent Country Park", [-0.275, 51.578], 600], ["Lloyd Park", [-0.08, 51.366], 400],
  ["Osterley Park", [-0.352, 51.488], 500], ["Wimbledon Common", [-0.235, 51.435], 1000],
];

// A band polygon around a polyline, half-width w metres.
function band(line, w) {
  const left = [], right = [];
  for (let i = 0; i < line.length; i++) {
    const a = line[Math.max(0, i - 1)], b = line[Math.min(line.length - 1, i + 1)];
    const dx = (b[0] - a[0]) * M_PER_DEG_LON, dy = -(b[1] - a[1]) * M_PER_DEG_LAT;
    const L = Math.hypot(dx, dy) || 1;
    const nx = -dy / L, ny = dx / L;
    left.push(offset(line[i], nx * w, ny * w));
    right.push(offset(line[i], -nx * w, -ny * w));
  }
  return [...left, ...right.reverse()];
}

function signedArea(ring) {
  let a = 0;
  for (let i = 0; i < ring.length; i++) {
    const p = ring[i], q = ring[(i + 1) % ring.length];
    a += p[0] * q[1] - q[0] * p[1];
  }
  return a / 2;
}
const wound = (ring, positive) => ((signedArea(ring) > 0) === positive ? ring : [...ring].reverse());

export function createSyntheticSource(network) {
  const features = [];
  const add = (layer, props, type, parts) => {
    // parts: arrays of [lon, lat] → mercator, with a bbox for the per-tile prefilter.
    const merc = parts.map((p) => p.map(([lon, lat]) => mercator(lon, lat)));
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const p of merc) for (const [x, y] of p) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
    features.push({ layer, props, type, merc, bbox: [x0, y0, x1, y1] });
  };
  const polygon = (layer, props, outer, holes = []) => {
    // Mercator flips y, so wind in lon/lat and let the flip make outer and holes opposite.
    add(layer, props, 3, [wound(outer, true), ...holes.map((h) => wound(h, false))]);
  };

  for (const line of network.lines) {
    const props = line.kind === "minor_rail" ? { class: "minor_rail" }
      : line.kind === "tunnel" ? { class: "major_rail", structure: "tunnel" } : { class: "major_rail" };
    add("road", props, 2, [line.stops.map(([, lon, lat]) => [lon, lat])]);
    for (const [name, lon, lat] of line.stops) {
      add("transit_stop_label", { name, stop_type: "station", mode: line.kind === "major_rail" ? "rail" : "metro_rail", filterrank: 1 }, 1, [[[lon, lat]]]);
    }
  }
  for (const [name, lon, lat, rank] of network.places || []) {
    add("place_label", { name, class: "settlement_subdivision", symbolrank: rank }, 1, [[[lon, lat]]]);
  }
  // Roads: an outer motorway ring, an inner trunk ring, radials, a primary grid in the middle
  // and a secondary grid in the centre (drawn only in near-field tiles, as the schema says).
  const ring = (r) => [...circle(CENTRE, r, 160), circle(CENTRE, r, 160)[0]];
  add("road", { class: "motorway" }, 2, [ring(26_000)]);
  add("road", { class: "trunk" }, 2, [ring(9_000)]);
  for (let k = 0; k < 12; k++) {
    const a = (k * Math.PI) / 6;
    const line = [offset(CENTRE, 1000 * Math.cos(a), 1000 * Math.sin(a)), offset(CENTRE, 28_000 * Math.cos(a), 28_000 * Math.sin(a))];
    add("road", { class: k % 2 ? "primary" : "trunk" }, 2, [line]);
  }
  for (let d = -12_000; d <= 12_000; d += 2000) {
    add("road", { class: "primary" }, 2, [[offset(CENTRE, d, -12_000), offset(CENTRE, d, 12_000)]]);
    add("road", { class: "primary" }, 2, [[offset(CENTRE, -12_000, d), offset(CENTRE, 12_000, d)]]);
  }
  for (let d = -4000; d <= 4000; d += 400) {
    add("road", { class: "secondary" }, 2, [[offset(CENTRE, d, -4000), offset(CENTRE, d, 4000)]]);
    add("road", { class: "secondary" }, 2, [[offset(CENTRE, -4000, d), offset(CENTRE, 4000, d)]]);
  }
  // Water: the river, a lake with an island (a hole), a reservoir.
  polygon("water", { class: "river" }, band(THAMES, 130));
  polygon("water", { class: "lake" }, rect([-0.170, 51.5055], 900, 160), [rect([-0.168, 51.5055], 120, 50)]);
  polygon("water", { class: "reservoir" }, circle([-0.512, 51.455], 700));
  for (const [, c, r] of PARKS) polygon("landuse", { class: "park" }, circle(c, r, 40));

  // Features of one tile, in its extent units (the tessellator clips to the tile).
  function* syntheticFeatures(z, x, y) {
    const n = Math.pow(2, z), pad = 64 / EXTENT / n;
    const tx0 = x / n - pad, ty0 = y / n - pad, tx1 = (x + 1) / n + pad, ty1 = (y + 1) / n + pad;
    for (const f of features) {
      const b = f.bbox;
      if (b[2] < tx0 || b[0] > tx1 || b[3] < ty0 || b[1] > ty1) continue;
      const parts = f.merc.map((p) => p.map(([mx, my]) => ({ x: (mx * n - x) * EXTENT, y: (my * n - y) * EXTENT })));
      yield { layer: f.layer, props: f.props, type: f.type, extent: EXTENT, geometry: () => parts };
    }
  }
  return { syntheticFeatures, count: features.length };
}
