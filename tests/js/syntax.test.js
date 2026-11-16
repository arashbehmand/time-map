// Every script the pages load parses (node --check). Most page modules can't be imported here
// (they touch the DOM at load), so a duplicate declaration or a stray brace would otherwise only
// show in a browser.
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const STATIC = new URL("../../static/", import.meta.url).pathname;

function scripts(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "vendor" ? [] : scripts(path);
    return name.endsWith(".js") ? [path] : [];
  });
}

test("every page script parses", () => {
  const files = scripts(STATIC);
  assert.ok(files.length > 20);
  for (const file of files) {
    assert.doesNotThrow(() => execFileSync(process.execPath, ["--check", file], { stdio: "pipe" }), file);
  }
});
