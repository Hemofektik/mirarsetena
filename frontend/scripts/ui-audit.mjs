/* UI audit via Playwright/CDP: console + network + contrast + render checks.
 *
 * Run from frontend/:  node scripts/ui-audit.mjs [url]
 * Screenshots land in frontend/.audit/.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { chromium } from "playwright";

const url =
  process.argv[2] ??
  "http://localhost:8000/p/cdp-rio-general/?layer=sigma0&date=2026-07-23&mode=change&hideCloudy=0&maxCloud=20&pois=facilities%2Cinspection&basemap=osm&bbox=-83.67515%2C9.37268%2C-83.65731%2C9.39741&zoom=14.781640175728981";

mkdirSync(".audit", { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

const console_ = [];
const pageErrors = [];
const badResponses = [];
const failedRequests = [];
page.on("console", (m) => {
  if (m.type() === "error" || m.type() === "warning") {
    console_.push({ type: m.type(), text: m.text().slice(0, 300) });
  }
});
page.on("pageerror", (e) => pageErrors.push(String(e).slice(0, 300)));
page.on("response", (r) => {
  if (r.status() >= 400) badResponses.push(`${r.status()} ${r.url().slice(0, 160)}`);
});
page.on("requestfailed", (r) =>
  failedRequests.push(`${r.failure()?.errorText} ${r.url().slice(0, 160)}`),
);

await page.goto(url, { waitUntil: "networkidle", timeout: 60_000 });
await page.waitForTimeout(6_000);

const report = await page.evaluate(() => {
  const rgb = (s) => s.match(/\d+(\.\d+)?/g)?.slice(0, 3).map(Number) ?? null;
  const lum = ([r, g, b]) => {
    const f = (c) => {
      c /= 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const ratio = (a, b) => {
    const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m);
    return (x + 0.05) / (y + 0.05);
  };
  const effBg = (el) => {
    for (let n = el; n; n = n.parentElement) {
      const bg = getComputedStyle(n).backgroundColor;
      const c = rgb(bg);
      const alpha = bg.match(/rgba?\([^)]*?,\s*([\d.]+)\s*\)/);
      const a = alpha ? Number(alpha[1]) : 1;
      if (c && !/rgba\(0, 0, 0, 0\)|transparent/.test(bg) && a >= 0.9) return c;
      // gradients: backgroundColor is transparent — use the first stop color
      const img = getComputedStyle(n).backgroundImage;
      if (img && img !== "none") {
        const rgbm = img.match(/rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/);
        if (rgbm) return [rgbm[1], rgbm[2], rgbm[3]].map(Number);
        const hex = img.match(/#[0-9a-fA-F]{3,8}/);
        if (hex) {
          let h = hex[0].slice(1);
          if (h.length === 3) h = h.split("").map((ch) => ch + ch).join("");
          return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
        }
      }
    }
    return [255, 255, 255];
  };

  // contrast offenders: visible text elements failing WCAG AA (4.5:1)
  const offenders = [];
  for (const el of document.querySelectorAll(
    "body *:not(script):not(style):not(canvas)",
  )) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const hasOwnText = [...el.childNodes].some(
      (n) => n.nodeType === 3 && n.textContent.trim().length > 1,
    );
    if (!hasOwnText) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || cs.display === "none") continue;
    const fg = rgb(cs.color);
    if (!fg) continue;
    const bg = effBg(el);
    const cr = ratio(fg, bg);
    const size = parseFloat(cs.fontSize);
    const large = size >= 24 || (size >= 18.66 && Number(cs.fontWeight) >= 700);
    const need = large ? 3 : 4.5;
    if (cr < need) {
      offenders.push({
        sel: `${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}${
          el.className && typeof el.className === "string"
            ? "." + el.className.trim().split(/\s+/).join(".")
            : ""
        }`,
        text: el.textContent.trim().slice(0, 40),
        fg: cs.color,
        bg: `rgb(${bg.join(", ")})`,
        ratio: Math.round(cr * 100) / 100,
      });
    }
  }

  const mapCanvas = document.querySelector("#map canvas");
  let webgl = false;
  if (mapCanvas) {
    webgl = !!(
      mapCanvas.getContext("webgl2") || mapCanvas.getContext("webgl")
    );
  }

  const headings = [...document.querySelectorAll("h2")].map((h) => h.textContent);
  const rawKeys = headings.filter((h) => /^[a-z_.]+$/.test(h));
  const strip = document.getElementById("cloud-strip");
  const pressed = document.querySelector('#layer-buttons [aria-pressed="true"]');

  return {
    colorScheme: getComputedStyle(document.documentElement).colorScheme,
    bodyColor: getComputedStyle(document.body).color,
    headings,
    rawI18nKeys: rawKeys,
    layerButtons: document.querySelectorAll("#layer-buttons button").length,
    pressedLayer: pressed?.textContent ?? null,
    radarVisible: !document.getElementById("radar-controls").hidden,
    dateValue: document.getElementById("date-value")?.textContent,
    worksMarker: document.getElementById("works-marker")?.textContent,
    stripSpans: strip?.children.length ?? 0,
    stripHeight: strip ? Math.round(strip.getBoundingClientRect().height) : 0,
    mapCanvasPresent: !!mapCanvas,
    mapFallbackText: !mapCanvas
      ? document.getElementById("map")?.textContent.slice(0, 80)
      : null,
    webgl,
    overflowX: document.documentElement.scrollWidth > window.innerWidth + 1,
    contrastOffenders: offenders.slice(0, 20),
    contrastOffenderCount: offenders.length,
  };
});

await page.screenshot({ path: ".audit/audit-full.png", fullPage: false });
await page.screenshot({
  path: ".audit/audit-panel.png",
  clip: { x: 0, y: 0, width: 340, height: 900 },
});

const summary = { url, report, console: console_, pageErrors, badResponses, failedRequests };
writeFileSync(".audit/audit.json", JSON.stringify(summary, null, 2));
console.log(JSON.stringify(summary, null, 2));
await browser.close();
