// Drive the renderer lab (/warp/lab/) in a Chrome you started with remote debugging, and
// print the result as JSON. This is how the desktop numbers in docs/warp-renderer-spike.md
// were taken. Node 22+ (global fetch and WebSocket); no packages.
//
//   1. uvicorn server.app:app --port 8765
//      (with ?geom=synthetic, the default, the lab makes no Mapbox request; a placeholder
//      MAPBOX_ACCESS_TOKEN is enough to start the server)
//   2. "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new \
//        --remote-debugging-port=9333 --user-data-dir=/tmp/lab-chrome --enable-gpu --use-angle=metal
//   3. node scripts/lab_headless.mjs --url "http://127.0.0.1:8765/warp/lab/?backend=b" \
//        --eval "__lab.runSelfTest()" [--shot out.png] [--size 1440x900] [--dpr 1] [--throttle 4]
//
// --eval is any expression; it is awaited after the lab is ready. Useful ones:
//   __lab.runSelfTest()                         gates G1–G5
//   __lab.runBench()                            gates G6–G9 (about two minutes)
//   __lab.morphTo(1, 0).then(() => __lab.S.u)   bend the map at once (before --shot)
const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => {
  if (a.startsWith("--")) acc.push([a.slice(2), all[i + 1] && !all[i + 1].startsWith("--") ? all[i + 1] : "1"]);
  return acc;
}, []));
const port = args.port || "9333";
const [w, h] = (args.size || "1440x900").split("x").map(Number);
const dpr = Number(args.dpr || 1);
if (!args.url) { console.error("usage: --url <lab url> [--eval <expr>] [--shot file.png] [--size WxH] [--dpr n] [--throttle n] [--port n]"); process.exit(2); }

const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r, { once: true }));
let id = 0;
const pending = new Map();
const logs = [];
ws.addEventListener("message", (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === "Runtime.consoleAPICalled") logs.push(`${m.params.type}: ${m.params.args.map((a) => a.value ?? a.description).join(" ")}`);
  if (m.method === "Runtime.exceptionThrown") logs.push(`exception: ${m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text}`);
});
const send = (method, params = {}) => new Promise((resolve) => { const i = ++id; pending.set(i, resolve); ws.send(JSON.stringify({ id: i, method, params })); });
const evaluate = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, timeout: 900000 });
  if (r.result && r.result.exceptionDetails) return { error: r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text };
  return r.result ? r.result.result.value : r;
};

await send("Runtime.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: dpr, mobile: false });
if (args.throttle) await send("Emulation.setCPUThrottlingRate", { rate: Number(args.throttle) });
await send("Page.navigate", { url: args.url });
for (let i = 0; i < 200 && (await evaluate("!!(window.__lab && window.__lab.ready)")) !== true; i++) await new Promise((r) => setTimeout(r, 100));
const ready = await evaluate("Promise.race([__lab.ready.then(() => 'ready', (e) => 'boot failed: ' + e.message), new Promise((r) => setTimeout(() => r('not ready after 40 s'), 40000))])");
const value = ready === "ready" && args.eval ? await evaluate(args.eval) : null;
if (args.shot) {
  await new Promise((r) => setTimeout(r, 300));
  const s = await send("Page.captureScreenshot", { format: "png" });
  (await import("node:fs")).writeFileSync(args.shot, Buffer.from(s.result.data, "base64"));
}
const u = ready === "ready" ? await evaluate("__lab.S.u") : null;
console.log(JSON.stringify({ url: args.url, size: `${w}x${h}`, dpr, u, ready, value, logs }, null, 1));
await send("Page.close").catch(() => {});
ws.close();
process.exit(0);
