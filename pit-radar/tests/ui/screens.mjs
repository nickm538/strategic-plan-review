// UI screenshots + console/CSP check against the REAL server (TEST-ONLY data from tests/ui/seed.py).
//   PIT_DATA_DIR=<seeded dir> NODE_PATH=/opt/node-tools/node_modules node tests/ui/screens.mjs <out-dir>
// Starts uvicorn, signs in, visits every screen at 1440x900 and 390x844, fails on console errors or CSP
// violations other than base-map tile requests (tile hosts are unreachable from the test sandbox).
import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import { mkdirSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const { chromium } = createRequire(import.meta.url)("playwright");
const OUT = process.argv[2] || "ui-shots";
const PORT = 8765;
const BASE = `http://127.0.0.1:${PORT}`;
const PASSWORD = "synthetic ui test password";
mkdirSync(OUT, { recursive: true });

const server = spawn(".venv/bin/uvicorn", ["app.main:app", "--host", "127.0.0.1", "--port", String(PORT), "--no-server-header", "--no-access-log"], {
  env: { ...process.env, APP_ENV: "development", COOKIE_SECURE: "false", SESSION_SECRET: "ui-test-secret-0123456789abcdef0123456789", LOG_LEVEL: "WARNING" },
  stdio: ["ignore", "inherit", "inherit"],
});
const stop = () => { try { server.kill("SIGTERM"); } catch { /* gone */ } };
process.on("exit", stop);

for (let i = 0; i < 60; i++) {
  try { const r = await fetch(`${BASE}/healthz`); if (r.ok) break; } catch { /* starting */ }
  await sleep(500);
}

const IGNORE = [/tiles\.openfreemap\.org/i, /openfreemap/i, /Failed to load resource: net::ERR_/i, /ERR_TUNNEL|ERR_PROXY|ERR_CONNECTION/i,
  /AJAXError/i, /Failed to fetch/i,
  // Known and expected: deck.gl's bundled 3D-tiles loader probes WebAssembly at load inside try/catch and falls
  // back to JavaScript. The CSP deliberately does not allow wasm compilation; no layer PIT Radar uses needs it.
  /CSP violation: script-src wasm-eval/];
const problems = [];

const browser = await chromium.launch({ args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"] });

async function session(name, viewport, user, shots) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 1, timezoneId: "America/Los_Angeles" });  // proves NY time is explicit
  const page = await ctx.newPage();
  page.on("console", (m) => {
    if (m.type() !== "error" && m.type() !== "warning") return;
    const t = m.text();
    if (IGNORE.some((r) => r.test(t))) return;
    if (m.type() === "warning" && !/Content Security Policy|CSP|Refused/i.test(t)) return;
    problems.push(`[${name}] console.${m.type()}: ${t}`);
  });
  page.on("pageerror", (e) => problems.push(`[${name}] pageerror: ${e.message}`));
  page.on("requestfailed", (r) => {
    const u = r.url();
    if (/openfreemap/.test(u)) return;
    if (r.failure() && /ERR_ABORTED/.test(r.failure().errorText)) return;   // navigation cancels in-flight fetches
    problems.push(`[${name}] request failed: ${u} ${r.failure() && r.failure().errorText}`);
  });
  await page.goto(`${BASE}/login`);
  await page.screenshot({ path: `${OUT}/${name}-login.png` });
  await page.fill("#name", user);
  await page.fill("#password", PASSWORD);
  await Promise.all([page.waitForURL(`${BASE}/`), page.click("#login-submit")]);
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (e) => console.error(`CSP violation: ${e.violatedDirective} ${e.blockedURI}`));
  });
  await page.reload();
  await page.waitForSelector(".card, .empty", { timeout: 20000 });
  await sleep(4500);  // map + deck layers settle
  for (const [shot, fn] of shots) {
    await fn(page);
    await sleep(1800);
    await page.screenshot({ path: `${OUT}/${name}-${shot}.png` });
  }
  const tz = await page.evaluate(() => document.querySelector(".card time") && document.querySelector(".card time").title);
  if (tz && !/E[SD]T/.test(tz)) problems.push(`[${name}] times are not shown in New York time: ${tz}`);
  await ctx.close();
}

const clickView = (v) => async (p) => { await p.locator(`[data-view="${v}"]:visible`).first().click(); };
await session("desktop-admin", { width: 1440, height: 900 }, "ui-admin", [
  ["map", async () => {}],
  ["drawer", async (p) => { await p.click(".card"); }],
  ["hotspot-zoom", async (p) => { await p.keyboard.press("Escape"); await p.keyboard.press("]"); }],
  ["density", async (p) => { await p.keyboard.press("Escape"); await p.click('[aria-label="Density view"]'); }],
  ["review", clickView("review")],
  ["selfreports", clickView("selfreports")],
  ["planner", clickView("planner")],
  ["health", clickView("health")],
  ["admin", clickView("admin")],
  ["list", clickView("list")],
]);
await session("mobile-viewer", { width: 390, height: 844 }, "ui-viewer", [
  ["map", async () => {}],
  ["sheet-full", async (p) => { await p.click(".sheet-handle"); await p.click(".sheet-handle"); }],
  ["planner", clickView("planner")],
]);

await browser.close();
stop();
if (problems.length) {
  console.error(`UI check found ${problems.length} problem(s):\n` + [...new Set(problems)].join("\n"));
  process.exit(1);
}
console.log(`UI check passed. Screenshots in ${OUT}`);
