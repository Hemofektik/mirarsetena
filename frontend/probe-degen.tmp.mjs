import { chromium } from "@playwright/test";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto("http://localhost:8000/p/cdp-rio-general/", { waitUntil: "domcontentloaded" });
await page.waitForFunction(() => window.__mirarsetenaMap, null, { timeout: 30000 });
await page.click('#layer-buttons button:has-text("sigma0")');
await page.waitForFunction(() => !document.getElementById("date-start").hidden &&
  document.querySelectorAll("#cloud-strip span").length > 5, null, { timeout: 30000 });
await page.waitForTimeout(400);

const box = await page.locator("#date-slider").boundingBox();
await page.screenshot({ path: "/tmp/slider-initial.png", clip: { x: 8, y: box.y - 14, width: 310, height: 54 } });

// drag start thumb to its max -> end.min becomes == end.max
const y = box.y + 12;
const startX = box.x + 8 + (16 / 21) * (box.width - 16);
await page.mouse.move(startX, y);
await page.mouse.down();
for (let i = 1; i <= 12; i++) { await page.mouse.move(startX + i * 5, y); await page.waitForTimeout(25); }
await page.mouse.up();
await page.waitForTimeout(300);
const state = await page.evaluate(() => {
  const s = document.getElementById("date-start"), e = document.getElementById("date-end");
  return { sv: s.value, smin: s.min, smax: s.max, ev: e.value, emin: e.min, emax: e.max,
           readout: document.getElementById("date-value").textContent };
});
console.log("after start drag:", JSON.stringify(state));
await page.screenshot({ path: "/tmp/slider-degen.png", clip: { x: 8, y: box.y - 14, width: 310, height: 54 } });

// now grab where the END thumb should be (right side) and see where a click lands
await page.mouse.move(box.x + box.width - 8, y);
await page.mouse.down();
await page.mouse.move(box.x + box.width - 60, y);
await page.mouse.up();
await page.waitForTimeout(300);
const state2 = await page.evaluate(() => {
  const e = document.getElementById("date-end");
  return { ev: e.value, emin: e.min, emax: e.max };
});
console.log("after end drag attempt:", JSON.stringify(state2));
await browser.close();
