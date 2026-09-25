// Drive the demo in Chromium: device page -> "Visit" -> panel with the proxied device UI.
// Usage: NODE_PATH=$(npm root -g) node poc/tools/browser_demo.mjs <tokens.json> <out dir>
import fs from "node:fs";
import { createRequire } from "node:module";

// ESM ignores NODE_PATH; resolve a globally installed playwright through it
const { chromium } = createRequire(`${process.env.NODE_PATH}/`)("playwright");

const [tokenFile, outDir] = process.argv.slice(2);
const saved = JSON.parse(fs.readFileSync(tokenFile, "utf8"));
const HA = saved.hassUrl;

const refreshed = await (
  await fetch(`${HA}/auth/token`, {
    method: "POST",
    body: new URLSearchParams({
      grant_type: "refresh_token",
      refresh_token: saved.refresh_token,
      client_id: saved.clientId,
    }),
  })
).json();
const hassTokens = {
  ...refreshed,
  refresh_token: saved.refresh_token,
  hassUrl: HA,
  clientId: saved.clientId,
  expires: Date.now() + refreshed.expires_in * 1000,
};

const browser = await chromium.launch();
async function newPage(viewport) {
  const context = await browser.newContext({ viewport, colorScheme: "dark" });
  await context.addInitScript((t) => localStorage.setItem("hassTokens", JSON.stringify(t)), hassTokens);
  return context.newPage();
}
const deviceFrame = (page) => page.frames().find((f) => f.url().includes("/api/esphome_web_ui/"));

const page = await newPage({ width: 1280, height: 800 });

// Device page: the "Visit" button now leads to the panel
await page.goto(`${HA}/config/devices/dashboard`);
await page.waitForTimeout(3000);
const ws = await page.evaluate(async () => {
  const conn = document.querySelector("home-assistant").hass.connection;
  const devices = await conn.sendMessagePromise({ type: "config/device_registry/list" });
  return devices.filter((d) => d.configuration_url?.startsWith("homeassistant://esphome-web-ui"));
});
const deviceId = ws[0].id;
await page.goto(`${HA}/config/devices/device/${deviceId}`);
await page.waitForTimeout(3000);
await page.screenshot({ path: `${outDir}/1-device-page.png` });

const visit = page.locator(`a[href^="/esphome-web-ui/${deviceId}"]`).first();
if (await visit.count()) {
  console.log("device page link:", await visit.getAttribute("href"), "| text:", (await visit.innerText()).trim());
  await visit.click();
} else {
  console.log("no Visit link found, navigating directly");
  await page.goto(`${HA}/esphome-web-ui/${deviceId}/0`);
}
await page.waitForFunction(() => location.pathname.startsWith("/esphome-web-ui/"));
let frame;
for (let i = 0; i < 50 && !(frame = deviceFrame(page)); i++) await page.waitForTimeout(200);
await frame.waitForFunction(() => document.getElementById("uptime")?.textContent.startsWith("uptime"));
await frame.waitForFunction(() => document.getElementById("echo")?.textContent === "websocket open");
console.log("panel url:", new URL(page.url()).pathname, "| iframe:", new URL(frame.url()).pathname.slice(0, 32) + "…");

// Interact: POST through the proxy, state comes back over SSE; websocket round trip
const before = await frame.locator("#toggle").innerText();
await frame.locator("#toggle").click();
await frame.waitForFunction((b) => document.getElementById("toggle").textContent !== b, before);
await frame.locator("#msg").fill("hello from the HA panel");
await frame.locator("#send").click();
await frame.waitForFunction(() => document.getElementById("echo").textContent.startsWith("device echoes"));
console.log("toggle:", before, "->", await frame.locator("#toggle").innerText(),
            "| websocket:", await frame.locator("#echo").innerText());
await page.waitForTimeout(1500);
await page.screenshot({ path: `${outDir}/2-panel-isolated.png` });

// Isolation: the device page must not reach Home Assistant's origin
const probe = () => {
  const out = {};
  try { out.origin = window.origin; } catch (e) { out.origin = e.name; }
  try { out.parentTokens = window.parent.localStorage.getItem("hassTokens") ? "READABLE" : "absent"; }
  catch (e) { out.parentTokens = "blocked (" + e.name + ")"; }
  try { out.ownStorage = localStorage.getItem("hassTokens") ? "READABLE" : "absent"; }
  catch (e) { out.ownStorage = "blocked (" + e.name + ")"; }
  return out;
};
console.log("isolated probe:", JSON.stringify(await frame.evaluate(probe)));

// Trusted mode: same-origin, like Supervisor add-on ingress
await page.locator("esphome-web-ui-panel").locator("button.isolation").click();
frame = null;
await page.waitForTimeout(500);
for (let i = 0; i < 50 && !(frame = deviceFrame(page)); i++) await page.waitForTimeout(200);
await frame.waitForFunction(() => document.getElementById("uptime")?.textContent.startsWith("uptime"));
console.log("trusted probe: ", JSON.stringify(await frame.evaluate(probe)));
await page.locator("esphome-web-ui-panel").locator("button.isolation").click(); // back to isolated
await page.waitForTimeout(1000);

// List view
await page.goto(`${HA}/esphome-web-ui`);
await page.waitForTimeout(2500);
await page.screenshot({ path: `${outDir}/3-panel-list.png` });

// Phone width
const phone = await newPage({ width: 390, height: 844 });
await phone.goto(`${HA}/esphome-web-ui/${deviceId}/0`);
for (let i = 0; i < 50 && !deviceFrame(phone); i++) await phone.waitForTimeout(200);
await deviceFrame(phone).waitForFunction(() => document.getElementById("uptime")?.textContent.startsWith("uptime"));
await phone.waitForTimeout(1500);
await phone.screenshot({ path: `${outDir}/4-panel-phone.png` });

await browser.close();
console.log("OK");
