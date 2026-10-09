import { chromium } from "@playwright/test";
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.on("pageerror", (e) => console.log("PAGEERROR:", e.stack ?? String(e)));
await page.goto("http://localhost:8000/p/cdp-rio-general/", { waitUntil: "domcontentloaded" });
await page.waitForFunction(() => window.__mirarsetenaMap, null, { timeout: 30000 });
await page.click('#layer-buttons button:has-text("sigma0")');
await page.waitForFunction(() => !document.getElementById("date-start").hidden, null, { timeout: 30000 });
await page.waitForTimeout(500);
// drag start to its max (the degenerate case)
const slider = await page.evaluate(() => {
  const r = document.getElementById("date-slider").getBoundingClientRect();
  return { left: r.left, top: r.top, width: r.width, height: r.height };
});
const y = slider.top + slider.height / 2;
const startX = slider.left + 8 + (16 / 21) * (slider.width - 16);
await page.mouse.move(startX, y);
await page.mouse.down();
for (let i = 1; i <= 12; i += 1) {
  await page.mouse.move(startX + (i * 60) / 12, y);
  await page.waitForTimeout(30);
}
await page.mouse.up();
await page.waitForTimeout(800);
console.log("done");
await browser.close();
