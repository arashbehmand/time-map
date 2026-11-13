// Probe A: the same buffers drawn by deck.gl 9 with a LayerExtension that bends vertices in
// deck's documented hook, vs:DECKGL_FILTER_GL_POSITION. Lab only (?backend=a), time-boxed:
// it answers "does an existing library with a vertex hook do the job?", not "make it nice".
//
// The hook runs after projection and after LineLayer has extruded the line's width. This
// adapter only translates each extruded corner by its end's displacement, so where the bend
// turns a segment its width skews; the lab measures that against back-ends B and C. A hook
// that rebuilt the extrusion from extra attributes (or a LineLayer subclass with its own
// vertex shader) could keep the width; that was not built, as it is our line shader again.
// The worker supplies per-vertex targets (as for back-end C).
import { hexToRgba, STYLES, widthAt } from "../../shared/mesh/schema-streets-v8.js";

export const DECK_URL = "https://unpkg.com/deck.gl@9.4.1/dist.min.js";
export const DECK_SRI = "sha384-MgmfghaFhzRlOya+lTLbCWa1NESjFsHp55UTFgT7doYqkH8cvOGK/V8+5SyeryGb";

function loadDeck() {
  if (window.deck) return Promise.resolve(window.deck);
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = DECK_URL;
    s.integrity = DECK_SRI;
    s.crossOrigin = "anonymous";
    s.onload = () => resolve(window.deck);
    s.onerror = () => reject(new Error("deck.gl didn't load"));
    document.head.appendChild(s);
  });
}

const BEND_UNIFORMS = `uniform bendUniforms {
  vec2 cam;
  float ppm;
  vec2 view;
  vec2 pin;
  float k;
  float u;
} bend;
`;

// Displace a clip-space position by (bent − ordinary) × u, both in CSS px. The worker's
// placement flag: 1 known, 0.5 fading onto the outer ring, 0 fading in place (as in B and C);
// DECKGL_FILTER_COLOR does the fading.
const DISPLACE = `
  if (t.z > 0.25) {
    vec2 geo = (world - bend.cam) * bend.ppm + 0.5 * bend.view;
    vec2 bent = bend.pin + bend.k * t.xy;
    vec2 d = (bent - geo) * bend.u;
    position.xy += d * 2.0 / bend.view * vec2(1.0, -1.0) * position.w;
  }
`;

export async function createDeckBackend(canvas) {
  const deck = await loadDeck();
  const bendModule = {
    name: "bend", vs: BEND_UNIFORMS,
    uniformTypes: { cam: "vec2<f32>", ppm: "f32", view: "vec2<f32>", pin: "vec2<f32>", k: "f32", u: "f32" },
  };

  class BendLines extends deck.LayerExtension {
    getShaders() {
      return {
        modules: [bendModule],
        inject: {
          "vs:#decl": "in vec3 bentA;\nin vec3 bentB;\n",
          // geometry.uv.x is LineLayer's segment end (0 = source, 1 = target), set before
          // this hook runs. Naming the layer's own attributes (positions,
          // instanceSourcePositions) here stops the shader compiling.
          "vs:DECKGL_FILTER_GL_POSITION": `
            vec2 world = mix(geometry.worldPosition.xy, geometry.worldPositionAlt.xy, geometry.uv.x);
            vec3 t = geometry.uv.x < 0.5 ? bentA : bentB;
            ${DISPLACE}`,
          "vs:DECKGL_FILTER_COLOR": "if (bentA.z < 0.75 || bentB.z < 0.75) color.a *= 1.0 - bend.u;",
        },
      };
    }
    initializeState() {
      this.getAttributeManager().addInstanced({
        bentA: { size: 3, accessor: "getBentA" },
        bentB: { size: 3, accessor: "getBentB" },
      });
    }
    draw() {
      for (const model of this.getModels()) model.shaderInputs.setProps({ bend: this.props.bend });
    }
  }

  class BendFills extends deck.LayerExtension {
    getShaders() {
      return {
        modules: [bendModule],
        inject: {
          "vs:#decl": "in vec3 bent;\n",
          "vs:DECKGL_FILTER_GL_POSITION": `
            vec2 world = geometry.worldPosition.xy;
            vec3 t = bent;
            ${DISPLACE}`,
          "vs:DECKGL_FILTER_COLOR": "if (bent.z < 0.75) color.a *= 1.0 - bend.u;",
        },
      };
    }
    initializeState() {
      this.getAttributeManager().add({ bent: { size: 3, accessor: "getBent" } });
    }
    draw() {
      for (const model of this.getModels()) model.shaderInputs.setProps({ bend: this.props.bend });
    }
  }

  const view = new deck.OrthographicView({ id: "o", flipY: true });
  const instance = new deck.Deck({
    canvas, views: [view], controller: false, useDevicePixels: Math.min(2, devicePixelRatio || 1),
    viewState: { target: [0, 0, 0], zoom: 0 }, layers: [],
    parameters: { cullMode: "none" },
  });
  let data = null;
  let stats = { drawCalls: 0 };
  const extLines = new BendLines(), extFills = new BendFills();

  function setScene(scene) {
    const t0 = performance.now();
    data = {};
    for (const [name, p] of Object.entries(scene.passes)) {
      if (!p.count) continue;
      if (p.kind === "line") {
        const colors = new Uint8Array(p.count * 4), casing = new Uint8Array(p.count * 4);
        for (let i = 0; i < p.count; i++) {
          const s = STYLES[p.style[i]];
          colors.set(hexToRgba(s.color).map((v) => Math.round(v * 255)), 4 * i);
          casing.set(hexToRgba(s.casing || s.color).map((v) => Math.round(v * 255)), 4 * i);
        }
        data[name] = { kind: "line", p, colors, casing };
      } else {
        // Triangles as three-vertex polygons, with per-vertex targets.
        const nt = p.count / 3, pos = new Float32Array(nt * 6), bent = new Float32Array(nt * 9);
        for (let t = 0; t < nt; t++) {
          for (let c = 0; c < 3; c++) {
            const v = p.index[3 * t + c];
            pos[6 * t + 2 * c] = p.pos[2 * v]; pos[6 * t + 2 * c + 1] = p.pos[2 * v + 1];
            bent.set(p.targets.subarray(3 * v, 3 * v + 3), 9 * t + 3 * c);
          }
        }
        const starts = new Uint32Array(nt + 1);
        for (let t = 0; t <= nt; t++) starts[t] = 3 * t;
        data[name] = { kind: "fill", n: nt, pos, bent, starts, color: hexToRgba(STYLES[p.style[0]].color).map((v) => Math.round(v * 255)) };
      }
    }
    return performance.now() - t0;
  }

  // Rebuilds the layer objects every frame: fine for a probe, not a pattern for product code.
  function draw(cam, u) {
    if (!data) return stats;
    const bend = { cam: cam.center, ppm: cam.ppm, view: [cam.width, cam.height], pin: cam.pin, k: cam.k, u };
    const layers = [];
    for (const name of ["water", "park"]) {
      const d = data[name];
      if (!d) continue;
      layers.push(new deck.SolidPolygonLayer({
        id: name, _normalize: false, _windingOrder: "CCW", extensions: [extFills], bend,
        data: { length: d.n, startIndices: d.starts, attributes: { getPolygon: { value: d.pos, size: 2 }, getBent: { value: d.bent, size: 3 } } },
        getFillColor: d.color,
      }));
    }
    const lineLayer = (id, d, casing) => {
      const widths = new Float32Array(d.p.count);
      for (let i = 0; i < d.p.count; i++) {
        const s = STYLES[d.p.style[i]];
        widths[i] = widthAt(casing ? s.casingWidth || s.width : s.width, cam.zoom);
      }
      return new deck.LineLayer({
        id, widthUnits: "pixels", extensions: [extLines], bend,
        data: { length: d.p.count, attributes: {
          getSourcePosition: { value: d.p.seg, size: 2, stride: 16, offset: 0 },
          getTargetPosition: { value: d.p.seg, size: 2, stride: 16, offset: 8 },
          getBentA: { value: d.p.targets, size: 3, stride: 24, offset: 0 },
          getBentB: { value: d.p.targets, size: 3, stride: 24, offset: 12 },
          getColor: { value: casing ? d.casing : d.colors, size: 4, normalized: true },
          getWidth: { value: widths, size: 1 },
        } },
        updateTriggers: { getWidth: cam.zoom.toFixed(1) },
      });
    };
    if (data.rail) layers.push(lineLayer("rail", data.rail, false));
    if (data.road) { layers.push(lineLayer("roadCase", data.road, true)); layers.push(lineLayer("road", data.road, false)); }
    instance.setProps({
      width: cam.width, height: cam.height,
      viewState: { target: [cam.center[0], cam.center[1], 0], zoom: Math.log2(cam.ppm) },
      layers,
    });
    instance.redraw("lab");
    stats = { drawCalls: layers.length };
    return stats;
  }

  return { supported: true, mode: "deck", caps: null, setScene, draw, setWarp() {} };
}
