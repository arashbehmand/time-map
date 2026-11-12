import { test } from "node:test";
import assert from "node:assert/strict";
import { createLatest } from "../../static/shared/latest.js";

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

test("a run overtaken by a newer one resolves stale and is aborted", async () => {
  const latest = createLatest();
  const a = deferred(), b = deferred();
  let signalA;
  const runA = latest.run("A", (s) => { signalA = s; return a.promise; });
  const runB = latest.run("B", () => b.promise);
  assert.equal(signalA.aborted, true);
  a.resolve("answer for A");
  b.resolve("answer for B");
  assert.deepEqual(await runA, { stale: true });
  assert.deepEqual(await runB, { stale: false, value: "answer for B" });
  assert.equal(latest.current(), "B");
});

test("of three quick runs only the last applies", async () => {
  const latest = createLatest();
  const ds = [deferred(), deferred(), deferred()];
  const runs = ["A", "B", "C"].map((name, i) => latest.run(name, () => ds[i].promise));
  // Resolve out of order: the newest first, the oldest last.
  ds[2].resolve("C"); ds[0].resolve("A"); ds[1].resolve("B");
  const out = await Promise.all(runs);
  assert.deepEqual(out.map((r) => r.stale), [true, true, false]);
  assert.equal(out[2].value, "C");
});

test("an error from an overtaken run is stale, not thrown", async () => {
  const latest = createLatest();
  const a = deferred();
  const runA = latest.run("A", () => a.promise);
  const runB = latest.run("B", async () => "B");
  a.reject(new Error("old failure"));
  assert.deepEqual(await runA, { stale: true });
  assert.equal((await runB).value, "B");
});

test("an error from the newest run is thrown", async () => {
  const latest = createLatest();
  await assert.rejects(latest.run("A", async () => { throw new Error("boom"); }), /boom/);
});

test("after cancel nothing applies", async () => {
  const latest = createLatest();
  const a = deferred();
  let signal;
  const runA = latest.run("A", (s) => { signal = s; return a.promise; });
  assert.equal(latest.busy, true);
  latest.cancel();
  assert.equal(signal.aborted, true);
  assert.equal(latest.busy, false);
  assert.equal(latest.current(), undefined);
  a.resolve("late");
  assert.deepEqual(await runA, { stale: true });
});
