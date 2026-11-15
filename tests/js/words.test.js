import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "../../static/shared/api.js";
import {
  ALL_TIES_TEXT, altText, answerNotes, headline, isPlace, isStation, isVenue, joinNames, journeyLink,
  journeyNote, noPlaceSentence, OPEN_HOURS_HINT, placeHeadline,
  quickestText, stationAltText, TIE_TEXT,
} from "../../static/meetup/words.js";

const best = { name: "Baker Street", max_time_min: 27.2, mean_time_min: 22.6 };

test("the headline names the street when there is one", () => {
  assert.equal(headline(best, "minimax"), "Meet on Baker Street (★ on the map). Everyone gets there within 27 min.");
  assert.match(headline(best, "sum"), /^Meet on Baker Street \(★ on the map\)\. The average trip is 23 min/);
});

test("without a street name it says 'Meet here'", () => {
  for (const name of [undefined, null, "", "   "]) {
    assert.match(headline({ ...best, name }, "minimax"), /^Meet here \(★ on the map\)\./);
  }
});

test("options carry the street and the time that matters", () => {
  assert.equal(altText(best, "minimax"), "on Baker Street · longest trip 27 min");
  assert.equal(altText({ ...best, name: null }, "sum"), "average trip 23 min");
});

test("notes are the server's strings, or none", () => {
  assert.deepEqual(answerNotes({ notes: ["Some places couldn't be checked.", 3, ""] }), ["Some places couldn't be checked."]);
  assert.deepEqual(answerNotes({}), []);
  assert.deepEqual(answerNotes({ notes: "nope" }), []);
});

test("names are joined in plain English", () => {
  assert.equal(joinNames(["Ana"]), "Ana");
  assert.equal(joinNames(["Ana", "Ben"]), "Ana and Ben");
  assert.equal(joinNames(["Ana", "Ben", "Cy"]), "Ana, Ben and Cy");
});

const noPlace = (reason, who) =>
  new ApiError(422, "no_feasible_meeting_point", "No place we checked can be reached by everyone.", { reason, who });

test("someone with no route is named, with something they can do", () => {
  const s = noPlace("no_route", [{ index: 1, reason: "no_route" }]);
  assert.equal(noPlaceSentence(s, ["Ana", "Ben"]),
    "From where Ben starts, we couldn't find a route to any place we checked. " +
    "Move Ben's pin onto a street or path, or pick another way for them to travel.");
});

test("several people with no route are named together", () => {
  const s = noPlace("no_route", [{ index: 0, reason: "no_route" }, { index: 2, reason: "no_route" }]);
  assert.match(noPlaceSentence(s, ["Ana", "Ben", "Cy"]), /^From where Ana and Cy start, .* Move their pins/);
});

test("time limits and no common place have their own sentences", () => {
  assert.match(noPlaceSentence(noPlace("over_time_limit", [{ index: 0, reason: "over_time_limit" }]), ["Ana"]),
    /within Ana's time limit/);
  assert.match(noPlaceSentence(noPlace("over_time_limit", [{ index: 0, reason: "x" }, { index: 1, reason: "x" }]),
    ["Ana", "Ben"]), /within the time limits of Ana and Ben/);
  assert.match(noPlaceSentence(noPlace("no_common_place", []), ["Ana", "Ben"]), /no place we checked that everyone can get to/);
});

test("someone cut off with a few places is named, with the counts", () => {
  const s = noPlace("no_common_place", [{ index: 1, reason: "reaches_few", reach: 3, of: 40 }]);
  assert.equal(noPlaceSentence(s, ["Ana", "Ben"]),
    "Ben can only get to 3 of the 40 places we checked, and none of those work for everyone. " +
    "Try a faster way for them to travel, or move someone closer.");
  const two = noPlace("no_common_place", [{ index: 0, reason: "reaches_few" }, { index: 2, reason: "reaches_few" }]);
  assert.match(noPlaceSentence(two, ["Ana", "Ben", "Cy"]), /^Ana and Cy can only get to a few of the places/);
});

test("anything else falls back to the server's sentence", () => {
  assert.equal(noPlaceSentence(new ApiError(502, "upstream_error", "The map service is unavailable."), ["Ana"]), "");
  assert.equal(noPlaceSentence(noPlace("no_route", [{ index: 5, reason: "no_route" }]), ["Ana"]), "");
  assert.equal(noPlaceSentence(noPlace("something_new", []), ["Ana"]), "");
  assert.equal(noPlaceSentence(noPlace("off_street", []), ["Ana"]), "");   // the server's own sentence
});

// ── stations ────────────────────────────────────────────────────
const bank = { kind: "station", name: "Bank", lon: -0.0886, lat: 51.5133, max_time_min: 24.4, mean_time_min: 18.6 };

test("a station answer says 'at' and names the station", () => {
  assert.equal(placeHeadline(bank), "Meet at Bank (★ on the map). Everyone gets there within 24 min.");
  assert.match(placeHeadline({ ...bank, name: "" }), /^Meet at this station/);
  assert.ok(isStation(bank) && !isStation({ name: "Baker Street" }) && !isStation(null));
});

test("the quickest place and ties are said in words", () => {
  assert.equal(quickestText({ ...bank, name: "Moorgate" }),
    "Quickest on average: Moorgate. The average trip is 19 min; the longest is 24 min.");
  assert.equal(stationAltText(bank), "Bank · longest trip 24 min");
  assert.equal(stationAltText({ ...bank, tie: true }), "Bank · longest trip 24 min · about as good");
  assert.match(TIE_TEXT, /can't tell these apart/);
});

test("a journey link opens directions in each person's way of travelling", () => {
  const from = { lon: -0.1, lat: 51.5 };
  const url = (mode) => new URL(journeyLink({ ...from, mode }, bank));
  assert.equal(url("transit").origin + url("transit").pathname, "https://www.google.com/maps/dir/");
  assert.equal(url("transit").searchParams.get("api"), "1");
  assert.equal(url("transit").searchParams.get("origin"), "51.500000,-0.100000");
  assert.equal(url("transit").searchParams.get("destination"), "51.513300,-0.088600");
  for (const [mode, want] of [["transit", "transit"], ["walking", "walking"], ["cycling", "bicycling"],
                              ["driving", "driving"], ["driving-traffic", "driving"], ["boat", "walking"]]) {
    assert.equal(url(mode).searchParams.get("travelmode"), want, mode);
  }
  assert.ok(!journeyLink({ lon: "1;x", lat: 2, mode: "walking" }, bank).includes(";"));
});

test("public transport refusals name who, and fall back to the server's sentence", () => {
  const out = new ApiError(422, "outside_transit_area", "Public transport times cover London only.",
    { who: [{ index: 2, reason: "outside_transit_area" }] });
  assert.equal(noPlaceSentence(out, ["Ana", "Ben", "Cy"]),
    "Public transport times cover London only, and Cy starts outside it. Pick another way for them to travel.");
  const two = new ApiError(422, "no_transit_access", "x",
    { who: [{ index: 0, reason: "no_transit_access" }, { index: 1, reason: "no_transit_access" }] });
  assert.match(noPlaceSentence(two, ["Ana", "Ben"]), /^Ana and Ben start too far from a street or station/);
  assert.equal(noPlaceSentence(new ApiError(422, "outside_transit_area", "x", {}), ["Ana"]), "");
});

test("when every alternative is a tie, rows lose the label and one sentence says it", () => {
  assert.equal(stationAltText({ ...bank, tie: true }, false), "Bank · longest trip 24 min");
  assert.match(ALL_TIES_TEXT, /all about as good/);
});

test("the journey link's note says where it goes and that it plans for now", () => {
  assert.equal(journeyNote("a weekday, 19:00–20:00"),
    "\"Plan journey\" opens Google Maps with that person's start point and the station. " +
    "It plans for now, not for a weekday, 19:00–20:00.");
  assert.match(journeyNote(null), /It plans for now\.$/);
});

test("no table time within three hours is said as that, not as no route", () => {
  const err = noPlace("no_transit_time", [{ index: 1, reason: "no_transit_time" }]);
  assert.equal(noPlaceSentence(err, ["Ana", "Ben"]),
    "From where Ben starts, the public transport times have no trip within 3 hours to any station. " +
    "Pick another way for them to travel.");
  assert.equal(noPlaceSentence(noPlace("no_transit_time", []), ["Ana"]), "");
});

// ── venues (#19) ────────────────────────────────────────────────
const lamb = { kind: "pub", name: "The Lamb", lon: -0.12, lat: 51.52, max_time_min: 22.6, mean_time_min: 17 };

test("a venue answer names the place and says what kind it is", () => {
  assert.equal(placeHeadline(lamb), "Meet at The Lamb, a pub (★ on the map). Everyone gets there within 23 min.");
  assert.equal(placeHeadline({ ...lamb, kind: "cafe", name: "Kaffeine" }),
    "Meet at Kaffeine, a café (★ on the map). Everyone gets there within 23 min.");
  assert.match(placeHeadline({ ...lamb, kind: "park", name: " " }), /^Meet at this park \(/);
  assert.match(placeHeadline({ ...lamb, kind: "restaurant", name: "" }), /^Meet at this restaurant/);
  assert.equal(stationAltText({ ...lamb, kind: "bar" }), "The Lamb · longest trip 23 min");
});

test("venues and stations are places; a street point is not", () => {
  for (const kind of ["pub", "bar", "cafe", "restaurant", "park"]) {
    assert.ok(isVenue({ kind }) && isPlace({ kind }) && !isStation({ kind }), kind);
  }
  assert.ok(isPlace(bank) && !isVenue(bank));
  assert.ok(!isPlace({ kind: "street" }) && !isPlace(null) && !isVenue({ kind: "toString" }));
});

test("the journey note says 'the place' for a venue, and the page admits it can't tell opening hours", () => {
  assert.match(journeyNote("a weekday, 19:00–20:00", lamb), /start point and the place\. /);
  assert.match(journeyNote(null, bank), /start point and the station\. /);
  assert.equal(OPEN_HOURS_HINT, "We don't know whether it's open.");
});
