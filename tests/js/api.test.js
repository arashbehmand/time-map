import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiError, NETWORK_MESSAGE, friendly, readError } from "../../static/shared/api.js";

test("the server's own sentence is shown", () => {
  const err = new ApiError(429, "rate_limited", "Too many requests. Please wait a moment.");
  assert.equal(friendly(err), "Too many requests. Please wait a moment.");
});

test("field paths and jargon from the server are rewritten", () => {
  assert.match(friendly(new ApiError(422, "invalid_request", "Invalid request. body.zoom: ...")), /wasn't valid/);
  assert.match(friendly(new ApiError(422, "no_isochrone", "No travel-time area")), /Move the pin onto a street/);
});

test("only a failed fetch is called a network problem", () => {
  assert.equal(friendly(new TypeError("Failed to fetch")), NETWORK_MESSAGE);
  // A reply that isn't JSON is not a connection problem.
  assert.equal(friendly(new SyntaxError("Unexpected token <")), "Something went wrong. Please try again.");
});

test("an abort says nothing", () => {
  const abort = new Error("aborted");
  abort.name = "AbortError";
  assert.equal(friendly(abort), "");
});

test("readError keeps why and who for a 'no meeting point' error, and nothing malformed", async () => {
  const resp = new Response(JSON.stringify({
    code: "no_feasible_meeting_point", detail: "No place works.", reason: "no_route",
    who: [{ index: 1, reason: "no_route" }, { index: "0", reason: "no_route" }, { index: -1, reason: "x" }, null],
  }), { status: 422 });
  const err = await readError(resp);
  assert.equal(err.reason, "no_route");
  assert.deepEqual(err.who, [{ index: 1, reason: "no_route" }]);

  const plain = await readError(new Response(JSON.stringify({ code: "busy", detail: "Busy.", who: "x" }), { status: 503 }));
  assert.equal(plain.reason, "");
  assert.deepEqual(plain.who, []);
});
