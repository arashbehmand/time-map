import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "../../static/shared/api.js";
import { altText, answerNotes, headline, joinNames, noPlaceSentence } from "../../static/meetup/words.js";

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

test("anything else falls back to the server's sentence", () => {
  assert.equal(noPlaceSentence(new ApiError(502, "upstream_error", "The map service is unavailable."), ["Ana"]), "");
  assert.equal(noPlaceSentence(noPlace("no_route", [{ index: 5, reason: "no_route" }]), ["Ana"]), "");
  assert.equal(noPlaceSentence(noPlace("something_new", []), ["Ana"]), "");
});
