// One app, three tools (#28): what the tools hand each other, and the words they share.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  cleanGroup, cleanHandoff, HANDOFF_KEY, readGroup, readHandoff, writeGroup, writeHandoff,
} from "../../static/shared/handoff.js";
import { APP_NAME, cityShort, cityUnavailable, MAP_WORDS, MODE_LABEL, minutesText, TOOLS } from "../../static/shared/tools.js";
import { mins } from "../../static/meetup/words.js";
import * as W from "../../static/warp/words.js";
import { defaultWindow, expired, pollStatus, WINDOWS, windowText } from "../../static/shared/transit.js";
import * as V from "../../static/warp/views.js";

function memory() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)),
           removeItem: (k) => m.delete(k), m };
}

test("the hand-off keeps a pin, a way of travelling and a time of day", () => {
  const s = memory();
  assert.deepEqual(readHandoff(s), {});
  writeHandoff({ pin: { lon: -0.123456, lat: 51.530812, label: "King's Cross" }, mode: "cycling" }, s);
  writeHandoff({ when: "wd08" }, s);
  assert.deepEqual(readHandoff(s), { pin: { lon: -0.1235, lat: 51.5308, label: "King's Cross" }, mode: "cycling", when: "wd08" });
  // Only rounded points are ever stored (about 11 m).
  assert.equal(JSON.parse(s.m.get(HANDOFF_KEY)).pin.lon, -0.1235);
  // An empty group takes the pin back, and its way of travelling with it (a mode alone would
  // turn From here's example into a request); the time of day stays.
  writeHandoff({ pin: null }, s);
  assert.deepEqual(readHandoff(s), { when: "wd08" });
});

test("a pin must be numbers, not anything that converts to one", () => {
  assert.deepEqual(cleanHandoff({ v: 1, pin: { lon: null, lat: null } }), {});
  assert.deepEqual(cleanHandoff({ v: 1, pin: { lon: "", lat: "0" } }), {});
  assert.deepEqual(cleanHandoff({ v: 1, pin: { lon: 0, lat: 0 } }), { pin: { lon: 0, lat: 0, label: "" } });
});

test("Meet up's group is kept for the trip back, cleaned on the way in", () => {
  const s = memory();
  assert.equal(readGroup(s), null);
  writeGroup({ people: [
    { name: "Ana", named: true, lon: -0.12, lat: 51.5, mode: "walking", label: "Picked on the map" },
    { name: "Alice", lon: -0.1, lat: 51.52, mode: "transit", label: "King's Cross", example: true },
    { name: "x", lon: "bad", lat: 51, mode: "walking" },                   // dropped
    { name: "y", lon: 1, lat: 2, mode: "teleport" },                       // dropped
  ], window: "wd19", venue: "pub" }, s);
  const g = readGroup(s);
  assert.equal(g.people.length, 2);
  assert.deepEqual(g.people[0], { lon: -0.12, lat: 51.5, mode: "walking", name: "Ana", named: true, example: false,
                                  label: "Picked on the map" });
  assert.equal(g.people[1].example, true);
  assert.equal(g.window, "wd19");
  assert.equal(g.venue, "pub");
  // At most 8, as the server; an empty group forgets the record.
  writeGroup({ people: Array.from({ length: 12 }, (_, i) => ({ lon: i, lat: i, mode: "driving" })) }, s);
  assert.equal(readGroup(s).people.length, 8);
  writeGroup({ people: [] }, s);
  assert.equal(readGroup(s), null);
  writeGroup(null, s);
  assert.equal(readGroup(s), null);
  assert.equal(cleanGroup({ v: 2, people: [{ lon: 1, lat: 1, mode: "walking" }] }), null);
  const broken = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); },
                   removeItem() { throw new Error("blocked"); } };
  assert.doesNotThrow(() => writeGroup({ people: [{ lon: 1, lat: 1, mode: "walking" }] }, broken));
  assert.equal(readGroup(broken), null);
});

test("anything that doesn't read back as a valid hand-off is ignored", () => {
  assert.deepEqual(cleanHandoff(null), {});
  assert.deepEqual(cleanHandoff({ v: 2, mode: "walking" }), {});
  assert.deepEqual(cleanHandoff({ v: 1, mode: "teleport", when: "wd99", pin: { lon: 200, lat: 0 } }), {});
  assert.deepEqual(cleanHandoff({ v: 1, pin: { lon: "x", lat: 1 } }), {});
  assert.deepEqual(cleanHandoff({ v: 1, pin: { lon: 1, lat: 2, label: 7 } }), { pin: { lon: 1, lat: 2, label: "" } });
  const s = memory();
  s.setItem(HANDOFF_KEY, "{not json");
  assert.deepEqual(readHandoff(s), {});
  // A bad value in a patch is dropped; the good ones are kept.
  writeHandoff({ mode: "transit", when: "never" }, s);
  assert.deepEqual(readHandoff(s), { mode: "transit" });
});

test("storage that throws means start fresh, never an error", () => {
  const broken = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  assert.deepEqual(readHandoff(broken), {});
  assert.doesNotThrow(() => writeHandoff({ mode: "walking" }, broken));
  assert.deepEqual(readHandoff(null), {});
});

test("one name for the app and each tool, with its question", () => {
  assert.equal(APP_NAME, "Minutes Away");
  assert.deepEqual(Object.values(TOOLS).map((t) => [t.name, t.path]),
    [["From here", "/from-here/"], ["Meet up", "/meet-up/"], ["Whole city", "/whole-city/"]]);
  assert.equal(TOOLS["whole-city"].question, "What does London look like when distance means travel time?");
  assert.deepEqual(MAP_WORDS, { real: "Real map", time: "Time map" });
  // The same labels as From here's own table.
  for (const [mode, label] of Object.entries(MODE_LABEL)) assert.equal(V.MODES[mode].label, label);
});

test("minutes read the same everywhere", () => {
  assert.equal(minutesText(12.4), "12 min");
  assert.equal(minutesText(0.4), "under a minute");
  assert.equal(minutesText(0.7), "under a minute");        // under a minute, not "1 min"
  assert.equal(minutesText(1), "1 min");
  // Meet up's times are the shared words; From here's estimates add "about".
  assert.equal(mins, minutesText);
  assert.equal(W.minutesText(0.7), "under a minute");
  assert.equal(W.minutesText(12.4), "about 12 min");
});

test("why Whole city can't be opened, in plain words", () => {
  assert.equal(cityUnavailable("not_configured"), "Whole city isn't on this server yet.");
  assert.equal(cityUnavailable("missing"), "Whole city isn't on this server yet.");
  assert.equal(cityUnavailable("expired"), "Whole city is out of date until the timetables are rebuilt.");
  for (const r of ["licences_not_cleared", "override_refused_here", "development_only"]) {
    assert.equal(cityUnavailable(r), "Whole city isn't published yet.");
  }
  assert.equal(cityUnavailable("loading"), "Checking for Whole city…");
  assert.equal(cityUnavailable("build_mismatch"), "Whole city isn't available right now.");
  assert.equal(cityUnavailable(""), "Whole city isn't available right now.");
  // The switcher's short form, visible on a phone.
  assert.deepEqual(["not_configured", "expired", "licences_not_cleared", "build_mismatch"].map(cityShort),
    ["not here yet", "out of date", "not published yet", "not available"]);
});

test("the status is asked for again while loading or after an error, but not for long without a network", async () => {
  const real = globalThis.fetch;
  const answers = (list) => {
    let i = 0;
    globalThis.fetch = async () => {
      const a = list[Math.min(i++, list.length - 1)];
      if (a === "down") throw new TypeError("Failed to fetch");
      return { ok: a !== 429, json: async () => a };
    };
    return () => i;
  };
  const fast = { waitMs: 1, maxWaitMs: 2, netWaitMs: 1 };
  try {
    let calls = answers([{ loading: true }, { whole_city: { reason: "loading" } }, { available: true, whole_city: {} }]);
    assert.deepEqual(await pollStatus(fast), { available: true, whole_city: {} });
    assert.equal(calls(), 3);
    calls = answers([429, 429, { available: false }]);
    assert.deepEqual(await pollStatus(fast), { available: false });
    calls = answers(["down"]);
    assert.equal(await pollStatus(fast), null);
    assert.equal(calls(), 2);                       // one quick retry, then the page can go offline
    calls = answers(["down", { available: true }]);
    assert.deepEqual(await pollStatus(fast), { available: true });
    calls = answers([{ loading: true }]);
    assert.deepEqual(await pollStatus({ ...fast, tries: 3 }), { loading: true });   // still loading: the last answer
    assert.equal(calls(), 3);
  } finally {
    globalThis.fetch = real;
  }
});

test("From here's views re-export the shared timetable helpers", () => {
  assert.equal(V.expired, expired);
  assert.equal(V.defaultWindow, defaultWindow);
  assert.deepEqual(V.WINDOWS, WINDOWS);
  assert.equal(windowText("a weekday, 08:00–09:00"), "A weekday, 08:00–09:00");
});
