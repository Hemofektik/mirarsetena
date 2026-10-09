// Reproduce: dragging one range thumb — does the other move, and which input got the event?
import { chromium } from "@playwright/test";

const BASE = "http://localhost:8000/p/cdp-rio-general/";
const out = { errors: [] };

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.on("pageerror", (e) => out.errors.push(e.stack ?? String(e)));

await page.goto(BASE, { waitUntil: "domcontentloaded" });
await page.waitForFunction(() => window.__mirarsetenaMap, null, { timeout: 30000 });
await page.click('#layer-buttons button:has-text("sigma0")');
await page.waitForFunction(
  () => !document.getElementById("date-start").hidden &&
        document.querySelectorAll("#cloud-strip span").length > 5,
  null,
  { timeout: 30000 },
);
await page.waitForTimeout(400);

// instrument both inputs: which one receives pointerdown / input, and when
await page.evaluate(() => {
  window.__ev = [];
  for (const id of ["date-start", "date-end"]) {
    const el = document.getElementById(id);
    el.addEventListener("pointerdown", (e) =>
      window.__ev.push({ id, t: "pointerdown", x: Math.round(e.clientX) }),
    );
    el.addEventListener("input", () =>
      window.__ev.push({ id, t: "input", v: Number(el.value) }),
    );
  }
});

const read = () =>
  page.evaluate(() => ({
    start: Number(document.getElementById("date-start").value),
    end: Number(document.getElementById("date-end").value),
    startMax: Number(document.getElementById("date-start").max),
    endMin: Number(document.getElementById("date-end").min),
    readout: document.getElementById("date-value").textContent,
    baseline: new URLSearchParams(location.search).get("baseline"),
    date: new URLSearchParams(location.search).get("date"),
  }));

const thumbPos = () =>
  page.evaluate(() => {
    const slider = document.getElementById("date-slider").getBoundingClientRect();
    const info = { slider: { left: slider.left, top: slider.top, width: slider.width, height: slider.height } };
    for (const id of ["date-start", "date-end"]) {
      const el = document.getElementById(id);
      const min = Number(el.min);
      const max = Number(el.max);
      const v = Number(el.value);
      const trackLeft = slider.left + 8;
      const trackW = slider.width - 16;
      const ratio = max > min ? (v - min) / (max - min) : 0;
      info[id] = {
        x: Math.round(trackLeft + ratio * trackW),
        y: Math.round(slider.top + slider.height / 2),
        v, min, max,
      };
    }
    return info;
  });

async function drag(fromX, y, dx) {
  await page.mouse.move(fromX, y);
  await page.mouse.down();
  const steps = 12;
  for (let i = 1; i <= steps; i += 1) {
    await page.mouse.move(fromX + (dx * i) / steps, y);
    await page.waitForTimeout(30);
  }
  await page.mouse.up();
  await page.waitForTimeout(250);
}

out.initial = await read();
out.posInitial = await thumbPos();

// --- drag START right by ~60px ---
await page.evaluate(() => (window.__ev = []));
await drag(out.posInitial["date-start"].x, out.posInitial["date-start"].y, 60);
out.afterStartDrag = await read();
out.eventsStartDrag = await page.evaluate(() => window.__ev);

// --- drag END left by ~60px ---
const pos2 = await thumbPos();
await page.evaluate(() => (window.__ev = []));
await drag(pos2["date-end"].x, pos2["date-end"].y, -60);
out.afterEndDrag = await read();
out.eventsEndDrag = await page.evaluate(() => window.__ev);
out.pos2 = pos2;

// --- adjacent thumbs: drag start right until it sits next to end, then grab "start" again ---
const pos3 = await thumbPos();
// push start close to end: drag start right a lot
await drag(pos3["date-start"].x, pos3["date-start"].y, 200);
out.afterCloseGap = await read();
const pos4 = await thumbPos();
out.pos4 = pos4;
await page.evaluate(() => (window.__ev = []));
// grab at the START thumb's (possibly overlapped) position, drag right
await drag(pos4["date-start"].x, pos4["date-start"].y, 40);
out.afterAdjacentStartDrag = await read();
out.eventsAdjacent = await page.evaluate(() => window.__ev);

console.log(JSON.stringify(out, null, 2));
await browser.close();