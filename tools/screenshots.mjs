// WOL-Server - tools/screenshots.mjs
// Takes the README screenshots (English and German) from the running demo with a headless Chromium.
//
//   Copyright (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
//   Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
//   Written by Anthropic Claude Opus 5.5 - AI generated content.
//
//   Free software under the GNU General Public License, version 3 or later.
//   There is no warranty, to the extent permitted by law. The full text is in
//   LICENSES/GPL-3.0-or-later.txt.
//
// SPDX-FileCopyrightText: (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
// SPDX-FileContributor: Anthropic Claude Opus 5.5 (AI generated content)
// SPDX-License-Identifier: GPL-3.0-or-later

// Usage (Node 22 or later, a Chromium-based browser):
//   python3 tools/demo.py &            # the demo with invented data
//   node tools/screenshots.mjs [--url http://127.0.0.1:8090] [--out docs/screenshots] [--browser /path/to/chromium]
// Writes <out>/<name>.png (English) and <out>/de/<name>.png (German). The browser runs without a window
// and is controlled through the DevTools protocol; set CHROME or --browser if it is not found.

import { spawn, execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";

const arg = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : fallback;
};
const URL = arg("url", "http://127.0.0.1:8090").replace(/\/$/, "");
const OUT = arg("out", "docs/screenshots");
const PORT = 9333;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function findBrowser() {
  const given = arg("browser", process.env.CHROME);
  if (given) return given;
  for (const name of ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "microsoft-edge", "opera"]) {
    for (const dir of ["/usr/bin", "/usr/local/bin", "/snap/bin"]) {
      if (existsSync(join(dir, name))) return join(dir, name);
    }
  }
  try {
    return execFileSync("which", ["chromium"], { encoding: "utf8" }).trim();
  } catch {
    console.error("No Chromium-based browser found: set CHROME=/path/to/browser or use --browser");
    process.exit(2);
  }
}

// The pictures of the README: [file name, hash in the GUI, viewport, dark mode]
const SHOTS = [
  ["devices", "#devices", [1280, 820]],
  ["plugs", "#plugs", [1280, 820]],
  ["schedules", "#schedules", [1280, 820]],
  ["network-overview", "#network/overview", [1280, 820]],
  ["network-reservations", "#network/reservations", [1280, 820]],
  ["network-settings", "#network/settings", [1280, 820]],
  ["system", "#system", [1280, 820]],
  ["devices-dark", "#devices", [1280, 820], true],
  ["phone", "#network/overview", [390, 844]],
];

const browserPath = findBrowser();
// Snap browsers may only write below ~/snap/<name>/
const snap = browserPath.match(/\/snap\/bin\/([\w-]+)/);
const profile = snap ? join(homedir(), "snap", snap[1], "common", "wol-screenshots") : mkdtempSync(join(tmpdir(), "wol-shots-"));
rmSync(profile, { recursive: true, force: true });
mkdirSync(profile, { recursive: true });
const browser = spawn(browserPath, [
  "--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`, "--no-first-run",
  "--no-default-browser-check", "--disable-gpu", "--hide-scrollbars", "--window-size=1280,900", "about:blank",
], { stdio: "ignore" });

let target;
for (let i = 0; i < 80 && !target; i++) {
  try {
    target = (await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()).find((t) => t.type === "page");
  } catch { /* not ready yet */ }
  if (!target) await sleep(250);
}
if (!target) {
  console.error("The browser does not start:", browserPath);
  browser.kill();
  process.exit(1);
}

const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));
let nextId = 1;
const pending = new Map();
const errors = [];
ws.addEventListener("message", (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  if (msg.method === "Runtime.exceptionThrown") errors.push(msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text);
});
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = nextId++;
  pending.set(id, (msg) => (msg.error ? reject(new Error(`${method}: ${msg.error.message}`)) : resolve(msg.result)));
  ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (expr) => {
  const res = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
  if (res.exceptionDetails) throw new Error(res.exceptionDetails.exception?.description || res.exceptionDetails.text);
  return res.result.value;
};
const waitFor = async (expr, what, timeout = 10000) => {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    try { if (await evaluate(expr)) return; } catch { /* page is loading */ }
    await sleep(100);
  }
  throw new Error(`Timeout: ${what}`);
};
const setSize = (w, h) => send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile: w < 600 });
const setDark = (dark) => send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: dark ? "dark" : "light" }] });

await send("Page.enable");
await send("Runtime.enable");
await send("Network.setCacheDisabled", { cacheDisabled: true });

let failed = false;
try {
  for (const lang of ["en", "de"]) {
    const dir = lang === "en" ? OUT : join(OUT, lang);
    mkdirSync(dir, { recursive: true });
    await setSize(1280, 820);
    await send("Page.navigate", { url: `${URL}/` });
    await waitFor("document.readyState === 'complete'", "loading");
    await evaluate(`localStorage.setItem("lang", "${lang}"); location.reload(); 1`);
    await sleep(500);
    await waitFor("document.readyState === 'complete' && !document.querySelector('#app').hidden", "start of the GUI");
    for (const [name, hash, [w, h], dark] of SHOTS) {
      await setSize(w, h);
      await setDark(Boolean(dark));
      await evaluate(`location.hash = "#"; location.hash = ${JSON.stringify(hash)}; 1`);
      // Content is there: the view is visible and not empty, the device status has been checked
      await waitFor(`(() => { const v = document.querySelector(".view:not([hidden])"); return v && v.innerText.trim().length > 40; })()`, name);
      if (hash === "#devices") await waitFor(`/·/.test(document.querySelector("#status-line").textContent)`, "device status");
      await sleep(900);
      await evaluate(`document.querySelector("#banner").hidden = true; window.scrollTo(0, 0); 1`);
      await sleep(150);
      const res = await send("Page.captureScreenshot", { format: "png" });
      writeFileSync(join(dir, `${name}.png`), Buffer.from(res.data, "base64"));
      console.log("Screenshot:", join(dir, `${name}.png`));
    }
    await setDark(false);
  }
} catch (e) {
  failed = true;
  console.error("ERROR:", e.message);
}
for (const line of errors) console.error("[page error]", line);
try { await Promise.race([send("Browser.close"), sleep(2000)]); } catch { /* already closed */ }
ws.close();
browser.kill();
if (!snap) rmSync(profile, { recursive: true, force: true });
process.exit(failed || errors.length ? 1 : 0);
