import { test } from "node:test";
import assert from "node:assert/strict";
import {
  cleanName, decodeGroup, encodeGroup, isGroupHash, linkAtLoad, MAX_LINK, MAX_NAME, MAX_PEOPLE, reconcileGroup, roundCoord, STATE_KEY,
} from "../../static/meetup/share.js";

const ana = { lat: 51.532212, lon: -0.105844, mode: "cycling", name: "Ana", named: true };
const p2 = { lat: 51.486123, lon: -0.125377, mode: "walking", name: "Person 2", named: false };

test("a group comes back from its link", () => {
  const hash = encodeGroup({ people: [ana, p2] });
  const g = decodeGroup(`#${hash}`);
  assert.deepEqual(g, {
    people: [
      { lat: 51.532, lon: -0.106, mode: "cycling", name: "Ana" },
      { lat: 51.486, lon: -0.125, mode: "walking", name: null },
    ],
    window: null, venue: null,
  });
});

test("start points are rounded to 3 decimals unless that person opted in", () => {
  const hash = encodeGroup({ people: [{ ...ana, exact: true }, p2] });
  const g = decodeGroup(hash);
  assert.equal(g.people[0].lat, 51.53221);
  assert.equal(g.people[0].lon, -0.10584);
  assert.equal(g.people[1].lat, 51.486);
  assert.equal(roundCoord(-0.12549), -0.125);
  assert.equal(roundCoord(-0.12551), -0.126);
});

test("only typed names go in, and they survive as text", () => {
  for (const name of ["Ana, Ben's friend", "R&D #2", "Zoë 🚲", "<img src=x onerror=alert(1)>"]) {
    const g = decodeGroup(encodeGroup({ people: [{ ...ana, name }, p2] }));
    assert.equal(g.people[0].name, name);
  }
  assert.ok(!encodeGroup({ people: [ana, p2] }).includes("Person"));
});

test("names lose control characters and are capped", () => {
  assert.equal(cleanName(" A\u0000n\na "), "Ana");
  // Invisible characters that reorder or hide text go too.
  assert.equal(cleanName("\u202eAna\u202c\u200b\u2066B\u2069\u2028\ufeff"), "AnaB");
  assert.equal(cleanName("\u200b\u200b"), "");
  const long = "x".repeat(MAX_NAME + 10);
  assert.equal(decodeGroup(`g=1&p=51.5,-0.1,w,${long}&p=51.5,-0.1,w`).people[0].name.length, MAX_NAME);
  assert.equal(decodeGroup("g=1&p=51.5,-0.1,w,%20%20&p=51.5,-0.1,w").people[0].name, null);
});

test("the time of day and the kind of place go in only with someone on public transport", () => {
  assert.equal(encodeGroup({ people: [ana, p2], window: "wd08", venue: "pub" }).includes("w="), false);
  const hash = encodeGroup({ people: [{ ...ana, mode: "transit" }, p2], window: "wd08", venue: "pub" });
  const g = decodeGroup(hash);
  assert.equal(g.window, "wd08");
  assert.equal(g.venue, "pub");
  assert.equal(g.people[0].mode, "transit");
});

test("anything that isn't a whole, valid group is refused", () => {
  const ok = "p=51.5,-0.1,w&p=51.6,-0.2,c";
  assert.ok(decodeGroup(`g=1&${ok}`));
  const bad = [
    "", "#", ok, `g=2&${ok}`, `g=1&g=1&${ok}`,
    "g=1&p=51.5,-0.1,w",
    `g=1&${Array.from({ length: 9 }, () => "p=51.5,-0.1,w").join("&")}`,
    "g=1&p=91,-0.1,w&p=51.6,-0.2,c", "g=1&p=51.5,NaN,w&p=51.6,-0.2,c",
    "g=1&p=51.5,181,w&p=51.6,-0.2,c", "g=1&p=51.5,1e2,w&p=51.6,-0.2,c",
    "g=1&p=51.5,-0.1,x&p=51.6,-0.2,c", "g=1&p=51.5,-0.1&p=51.6,-0.2,c",
    // Inherited keys of a plain object are not modes.
    "g=1&p=51.5,-0.1,constructor&p=51.6,-0.2,c", "g=1&p=51.5,-0.1,__proto__&p=51.6,-0.2,c",
    "g=1&p=51.5,-0.1,toString&p=51.6,-0.2,c", "g=1&p=51.5,-0.1,hasOwnProperty&p=51.6,-0.2,c",
    `g=1&${ok}&w=xx`, `g=1&${ok}&v=casino`, `g=1&${ok}&w=wd08&w=wd13`,
    `g=1&${ok}&zoom=3`,
    `g=1&${ok}&p=51.5,-0.1,w,${"y".repeat(MAX_LINK)}`,
  ];
  for (const hash of bad) assert.equal(decodeGroup(hash), null, hash);
});

test("only a fragment with g is taken for a group", () => {
  assert.equal(isGroupHash(""), false);
  assert.equal(isGroupHash("#"), false);
  assert.equal(isGroupHash("#foo"), false);
  assert.equal(isGroupHash("#g=1"), true);
});

test("every group the page allows makes a link it can open", () => {
  // The longest link: 8 people, exact points with the most digits, 40 four-byte characters each.
  const people = Array.from({ length: MAX_PEOPLE }, () => ({
    lat: -89.12345, lon: -179.12345, mode: "transit", name: "😀".repeat(MAX_NAME), named: true, exact: true,
  }));
  const hash = encodeGroup({ people, window: "wd19", venue: "restaurant" });
  assert.ok(hash.length <= MAX_LINK, `${hash.length} > ${MAX_LINK}`);
  const g = decodeGroup(hash);
  assert.ok(g);
  assert.equal(g.people.length, MAX_PEOPLE);
  assert.equal(g.people[0].name, "😀".repeat(MAX_NAME));
  for (const name of ["محمدرضا".repeat(6), "李".repeat(MAX_NAME)]) {
    const h = encodeGroup({ people: people.map((p) => ({ ...p, name })) });
    assert.ok(decodeGroup(h), name);
  }
});

test("a shared group gets what this page can give it", () => {
  const group = {
    people: [
      { lat: 51.5, lon: -0.12, mode: "transit", name: "Ana" },
      { lat: 48.86, lon: 2.35, mode: "transit", name: null },
      { lat: 51.5, lon: -0.08, mode: "cycling", name: null },
    ],
    window: "wd08", venue: "pub",
  };
  const inLondon = (p) => p.lon < 0;
  const r = reconcileGroup(group, { transitOk: inLondon, windows: ["wd08", "wd19"], venues: ["pub"] });
  assert.deepEqual(r.people.map((p) => p.mode), ["transit", "walking", "cycling"]);
  assert.deepEqual(r.walkers, [1]);
  assert.equal(r.window, "wd08");
  assert.equal(r.venue, "pub");
  assert.equal(r.windowGone || r.venueGone, false);

  const gone = reconcileGroup(group, { transitOk: inLondon, windows: ["wd19"], venues: ["cafe"] });
  assert.equal(gone.windowGone, true);
  assert.equal(gone.venueGone, true);
  assert.equal(gone.window, null);
  assert.equal(gone.venue, null);
  // A station needs no venue list.
  assert.equal(reconcileGroup({ ...group, venue: "station" }, { transitOk: inLondon, windows: ["wd08"] }).venueGone, false);

  // No tables: everyone on public transport walks, and the time and place no longer matter.
  const none = reconcileGroup(group, { transitOk: () => false });
  assert.deepEqual(none.walkers, [0, 1]);
  assert.equal(none.windowGone || none.venueGone, false);
  assert.equal(none.window, null);
});

test("a link opened before a reload comes back without finding again", () => {
  const link = "g=1&p=51.5,-0.1,w&p=51.6,-0.2,c";
  // A link in the address bar wins, and is solved.
  assert.deepEqual(linkAtLoad(`#${link}`, { [STATE_KEY]: "g=1&p=1,1,w&p=2,2,w" }), { hash: `#${link}`, solve: true });
  // No link in the bar: the one kept in this tab's history, not solved again.
  assert.deepEqual(linkAtLoad("", { [STATE_KEY]: link }), { hash: link, solve: false });
  // Nothing kept, or something that isn't a group link: the saved example.
  for (const state of [null, undefined, {}, "g=1", { [STATE_KEY]: 3 }, { [STATE_KEY]: "foo=1" }]) {
    assert.equal(linkAtLoad("", state), null);
  }
  assert.equal(linkAtLoad("#foo", null), null);
});
