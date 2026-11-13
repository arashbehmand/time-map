// Coordinate frames shared by the mesh renderer, its worker, the lab and the tests.
// Pure: no DOM, so node, a worker and the page can all import it.
//
//   mercator   Web Mercator in [0, 1): x east, y south (y down, as on screen).
//   local      metres east and south of an origin, at the origin's latitude:
//              (mercator − origin) × metres-per-mercator-unit there. It is Mercator
//              scaled, so it lines up exactly with MapLibre; "metres" is exact only at the
//              origin's latitude (0.4% off 25 km north or south of London).
//   screen     CSS pixels, y down, origin at the top-left of the map container.
//
// MapLibre's world is 512 px wide at zoom 0, so at zoom z one mercator unit is 512·2^z px.

export const WORLD_PX = 512;
export const EARTH_CIRCUMFERENCE_M = 40075016.68557849;
const MAX_LAT = 85.0511287798066;

export function mercator(lon, lat) {
  const phi = (Math.max(-MAX_LAT, Math.min(MAX_LAT, lat)) * Math.PI) / 180;
  return [(lon + 180) / 360, (1 - Math.log(Math.tan(Math.PI / 4 + phi / 2)) / Math.PI) / 2];
}

export function unmercator(mx, my) {
  const lon = mx * 360 - 180;
  const lat = (Math.atan(Math.sinh(Math.PI * (1 - 2 * my))) * 180) / Math.PI;
  return [lon, lat];
}

// Metres per mercator unit at a latitude.
export function metresPerUnit(lat) {
  return EARTH_CIRCUMFERENCE_M * Math.cos((lat * Math.PI) / 180);
}

export function localFrame(origin) {
  const [mx0, my0] = mercator(origin.lon, origin.lat);
  const m = metresPerUnit(origin.lat);
  return {
    origin: { lon: origin.lon, lat: origin.lat },
    mx0, my0, m,
    toLocal(lon, lat) {
      const [mx, my] = mercator(lon, lat);
      return [(mx - mx0) * m, (my - my0) * m];
    },
    fromLocal(x, y) {
      return unmercator(mx0 + x / m, my0 + y / m);
    },
    fromMercator(mx, my) {
      return [(mx - mx0) * m, (my - my0) * m];
    },
  };
}

// The camera the renderer and the JS twin use. `center` is the map centre and `pin` the
// origin's screen position, both from the same projection, so at u = 0 everything
// registers with MapLibre. `k` is CSS px per layout unit on the bent map (minutes for a
// polar warp, metres for a grid warp).
export function makeCamera(frame, { centerLon, centerLat, zoom, width, height, k }) {
  const center = frame.toLocal(centerLon, centerLat);
  const ppm = (WORLD_PX * Math.pow(2, zoom)) / frame.m;
  const pin = [(0 - center[0]) * ppm + width / 2, (0 - center[1]) * ppm + height / 2];
  return { origin: frame.origin, center, ppm, zoom, width, height, pin, k };
}

export function cameraFromMap(map, frame, k) {
  const c = map.getCenter();
  const el = map.getContainer();
  return makeCamera(frame, {
    centerLon: c.lng, centerLat: c.lat, zoom: map.getZoom(),
    width: el.clientWidth, height: el.clientHeight, k,
  });
}

// Local metres → screen on the ordinary (geographic) map.
export function geoScreen(x, y, cam) {
  return [(x - cam.center[0]) * cam.ppm + cam.width / 2, (y - cam.center[1]) * cam.ppm + cam.height / 2];
}

// Tile (z, x, y) → its top-left corner in local metres and metres per tile-extent unit.
export function tilePlacement(frame, z, x, y, extent) {
  const n = Math.pow(2, z);
  return {
    ox: (x / n - frame.mx0) * frame.m,
    oy: (y / n - frame.my0) * frame.m,
    scale: frame.m / (n * extent),
  };
}

// Ground size of a tile's edge in metres at the frame's latitude.
export function tileSizeMetres(frame, z) {
  return frame.m / Math.pow(2, z);
}
