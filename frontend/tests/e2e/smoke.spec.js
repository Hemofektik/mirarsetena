// Smoke test (I.2 / I.6 / I.7): app boots against the live backend,
// layer switching drives the date scrubber, and the mobile viewport stays usable.
import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

const SLUG = "cdp-rio-general";

// Google-Maps-style basemap picker on the map (bottom-left)
async function pickBasemap(page, key) {
  await page.locator("#btn-basemap").click();
  await page.locator(`.picker-panel [data-basemap="${key}"]`).click();
}

test("app boots, layer switching drives the scrubber", async ({ page }) => {
  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#app-title")).toHaveText("Mirar Setena");
  await expect(page.locator("#project-name")).toContainText("CDP Río General");

  // four configured layers (radar removed 2026-10-12)
  await expect(page.locator("#layer-buttons button")).toHaveCount(4);

  // S2 dates populate the cloud strip (live catalog)
  await expect(page.locator("#cloud-strip span").first()).toBeVisible({
    timeout: 30_000,
  });
  const s2Count = await page.locator("#cloud-strip span").count();
  expect(s2Count).toBeGreaterThan(10);

  // hide-cloudy shrinks the optical strip (dates with a cloud score)
  await page.locator("#hide-cloudy").check();
  const shrunk = await page.locator("#cloud-strip span").count();
  expect(shrunk).toBeLessThan(s2Count);
  await page.locator("#hide-cloudy").uncheck();

  // switch to a second optical layer: the scrubber follows
  await page.locator('#layer-buttons button:has-text("mndwi")').click();
  await expect(page.locator("#date-end")).toBeVisible();
  // the slider rail must render, not just the thumb
  await expect(page.locator("#date-slider .range-track")).toBeVisible();
  await expect(page.locator("#cloud-strip span").first()).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/);

  // About dialog shows the disclaimer (i18n + attribution)
  await page.locator("#about-btn").click();
  await expect(page.locator("#about-disclaimer")).toContainText("SETENA");
  await page.keyboard.press("Escape");

  // works-start marker renders from config
  await expect(page.locator("#works-marker")).toContainText("2026-06-01");

  // POI group toggle + basemap swap (map picker) keep the app alive
  await page.locator("#poi-facilities").uncheck();
  await page.locator("#poi-facilities").check();
  await pickBasemap(page, "esri");
  await expect(page.locator("#btn-basemap")).toHaveAttribute("data-basemap", "esri");
  await pickBasemap(page, "osm");

  // share encodes state into the URL
  await page.locator("#share-btn").click();
  await expect(page).toHaveURL(/layer=mndwi/);

  // status page renders the payload
  await page.locator("#status-link").click();
  await expect(page.locator("body")).toContainText("missions");
  await page.goBack();
});

test("mobile viewport keeps the panel usable (I.7)", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#panel")).toBeVisible();
  await expect(
    page.locator("#layer-buttons button").first()
  ).toBeVisible();
  await expect(page.locator("#date-end")).toBeVisible(); // single thumb
});

test("boot does not race the map style (no render crash)", async ({ page }) => {
  const errors = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/);
  // give the inline style time to parse and the boot sequence to settle
  await page.waitForTimeout(3000);
  await expect(page.locator("body")).not.toContainText("Failed to start");
  expect(
    errors.filter((text) => text.includes("Style is not done loading")),
  ).toEqual([]);
});

test("overlay and basemap switches request real numeric tiles", async ({ page }) => {
  const tileRequests = [];
  const failed = [];
  page.on("request", (request) => {
    tileRequests.push(request.url());
  });
  page.on("response", (response) => {
    if (response.status() >= 400) {
      failed.push(`${response.status()} ${response.url()}`);
    }
  });

  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/);
  await page.waitForTimeout(2500);

  // basemap swap must fetch imagery tiles, not the template as TileJSON
  await pickBasemap(page, "esri");
  await page.waitForTimeout(2500);

  expect(
    tileRequests.filter((url) => url.includes("arcgisonline") && /\/tile\/\d+\/\d+\/\d+/.test(url)).length,
  ).toBeGreaterThan(0);
  // never request a template literally (URL-encoded braces)
  expect(tileRequests.filter((url) => url.includes("%7B"))).toEqual([]);
  expect(failed).toEqual([]);
});

test("active layer button toggles off and back on", async ({ page }) => {
  const tiles = [];
  const failed = [];
  const errors = [];
  page.on("request", (request) => {
    if (request.url().includes("/tiles/")) tiles.push(request.url());
  });
  page.on("response", (response) => {
    if (response.status() >= 400) failed.push(`${response.status()} ${response.url()}`);
  });
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });

  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/);

  // wait for the initial tile wave to quiesce: the style-load wave races
  // the click otherwise (disable applies on map "load", a beat later)
  let prev = -1;
  let stable = 0;
  for (let i = 0; i < 25 && stable < 3; i += 1) {
    await page.waitForTimeout(400);
    const now = tiles.length;
    stable = now === prev ? stable + 1 : 0;
    prev = now;
  }

  const active = page.locator('#layer-buttons [aria-pressed="true"]');
  await expect(active).toHaveCount(1);
  const name = (await active.textContent()).trim();
  const beforeClick = tiles.length;

  // click the active pill -> layer disabled, URL records the off state
  await active.click();
  await expect(page.locator('#layer-buttons [aria-pressed="true"]')).toHaveCount(0);
  await expect(page).toHaveURL(/layer=off/);

  // disabled overlay must not request any further tiles
  await page.waitForTimeout(1500);
  expect(tiles.length).toBe(beforeClick);

  // share URL restores the disabled state (and fetches nothing)
  await page.locator("#share-btn").click();
  const shared = page.url();
  await page.goto(shared);
  await expect(page.locator('#layer-buttons [aria-pressed="true"]')).toHaveCount(0);
  await page.waitForTimeout(1000);
  expect(failed).toEqual([]);
  // MapLibre must not crash on the disabled overlay's empty tile list
  expect(errors).toEqual([]);

  // click the same pill again -> re-enabled with the same layer name
  await page.locator(`#layer-buttons button:text-is("${name}")`).click();
  await expect(
    page.locator('#layer-buttons [aria-pressed="true"]'),
  ).toHaveText(name);
});

test("POI checkboxes toggle the markers on the map", async ({ page }) => {
  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/);
  // 8 facilities + 2 inspection points, each an always-visible dot
  await expect(page.locator("#poi-markers .poi-dot")).toHaveCount(10, {
    timeout: 15000,
  });
  await expect(
    page.locator('#poi-markers .poi-dot[data-group="facilities"]'),
  ).toHaveCount(8);
  await expect(
    page.locator('#poi-markers .poi-dot[data-group="inspection"]'),
  ).toHaveCount(2);

  await page.locator("#poi-facilities").uncheck();
  await expect(
    page.locator('#poi-markers [data-group="facilities"]').first(),
  ).toBeHidden();
  await expect(
    page.locator('#poi-markers .poi-dot[data-group="inspection"]').first(),
  ).toBeVisible();

  await page.locator("#poi-facilities").check();
  await expect(
    page.locator('#poi-markers .poi-dot[data-group="facilities"]').first(),
  ).toBeVisible();

  await page.locator("#poi-inspection").uncheck();
  await expect(
    page.locator('#poi-markers [data-group="inspection"]').first(),
  ).toBeHidden();
});

test("all POI dots and labels stay visible without overlapping", async ({ page }) => {
  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#poi-markers .poi-dot")).toHaveCount(10, {
    timeout: 15000,
  });
  // zoom into the core cluster where breaker/dumper-ramp/storage/channel
  // sit within ~15 m — plain symbol layers would hide colliding labels
  await page.evaluate(() =>
    window.__mirarsetenaMap.jumpTo({ center: [-83.6693, 9.3854], zoom: 16 }),
  );
  await page.waitForTimeout(500);

  const result = await page.evaluate(() => {
    const shown = (el) => getComputedStyle(el).display !== "none";
    const labels = [...document.querySelectorAll("#poi-markers .poi-label")]
      .filter(shown)
      .map((el) => {
        const r = el.getBoundingClientRect();
        return { x: r.x, y: r.y, r: r.right, b: r.bottom };
      });
    let overlapPairs = 0;
    for (let i = 0; i < labels.length; i += 1) {
      for (let j = i + 1; j < labels.length; j += 1) {
        const a = labels[i];
        const b = labels[j];
        if (
          Math.min(a.r, b.r) - Math.max(a.x, b.x) > 1 &&
          Math.min(a.b, b.b) - Math.max(a.y, b.y) > 1
        ) {
          overlapPairs += 1;
        }
      }
    }
    const inView = labels.every(
      (r) => r.x >= 0 && r.y >= 0 && r.r <= innerWidth && r.b <= innerHeight,
    );
    const dots = [...document.querySelectorAll("#poi-markers .poi-dot")]
      .filter(shown)
      .map((el) => {
        const r = el.getBoundingClientRect();
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
      });
    const covered = dots.filter((d) =>
      labels.some((r) => d.x > r.x && d.x < r.r && d.y > r.y && d.y < r.b),
    ).length;
    return { labels: labels.length, dots: dots.length, overlapPairs, inView, covered };
  });

  expect(result.dots).toBe(10);
  expect(result.labels).toBe(10);
  expect(result.overlapPairs).toBe(0);
  expect(result.inView).toBe(true);
  expect(result.covered).toBe(0);
});

test("property perimeter layer renders and toggles", async ({ page }) => {
  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/);
  await page.waitForFunction(
    () => window.__mirarsetenaMap?.getLayer("aoi-outline"),
    null,
    { timeout: 15000 },
  );

  const read = () =>
    page.evaluate(() =>
      window.__mirarsetenaMap.getLayoutProperty("aoi-outline", "visibility"),
    );

  // the perimeter layer draws the OFFICIAL cadastral geometry (SNITCR
  // SIRI) — user 2026-10-12: 'replace our own by the official one'
  const official = await page.request.get(`/p/${SLUG}/official.geojson`);
  const officialPayload = await official.json();
  expect(officialPayload.features).toHaveLength(2);
  expect(
    officialPayload.features.map((f) => f.properties.source),
  ).toEqual(["snitcr-siri", "snitcr-siri"]);
  const sourceData = await page.evaluate(() => {
    const src = window.__mirarsetenaMap.getSource("aoi");
    const data = src.serialize ? src.serialize().data : src._data;
    return typeof data === "string" ? data : JSON.stringify(data);
  });
  expect(sourceData).toContain("snitcr-siri"); // aoi source = official
  await expect.poll(async () => await read(), { timeout: 10000 }).toBe("visible");

  // uncheck -> hidden, URL records it, share URL restores it
  await page.locator("#show-properties").uncheck();
  await expect.poll(async () => await read()).toBe("none");
  await expect(page).toHaveURL(/properties=0/);

  await page.locator("#share-btn").click();
  const shared = page.url();
  await page.goto(shared);
  await page.waitForFunction(
    () => window.__mirarsetenaMap?.getLayer("aoi-outline"),
    null,
    { timeout: 15000 },
  );
  await expect
    .poll(
      async () =>
        page.evaluate(() =>
          window.__mirarsetenaMap.getLayoutProperty("aoi-outline", "visibility"),
        ),
      { timeout: 10000 },
    )
    .toBe("none");

  await page.locator("#show-properties").check();
  await expect
    .poll(
      async () =>
        page.evaluate(() =>
          window.__mirarsetenaMap.getLayoutProperty("aoi-outline", "visibility"),
        ),
      { timeout: 10000 },
    )
    .toBe("visible");
});

test("layer switch reacts immediately while the dates API is stalled", async ({ page }) => {
  // Simulate a busy/slow backend: hold every dates request for 8s once boot
  // has finished. The UI must still switch layers without waiting for it.
  let stall = false;
  await page.route("**/api/dates*", async (route) => {
    if (stall) await new Promise((r) => setTimeout(r, 8000));
    await route.continue();
  });

  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/, {
    timeout: 30_000,
  });

  stall = true;
  await page.locator('#layer-buttons button:has-text("mndwi")').click();

  // the pill must react NOW, and the empty slider must be replaced by the
  // loading spinner while the stalled dates fetch is in flight
  await expect(page.locator('#layer-buttons [aria-pressed="true"]')).toHaveText(
    "mndwi",
    { timeout: 2_000 },
  );
  await expect(page.locator("#date-loading")).toBeVisible({ timeout: 2_000 });
  await expect(page.locator("#date-controls")).toBeHidden();

  // a second switch while the first is still in flight must win
  await page.locator('#layer-buttons button:has-text("bsi")').click();
  await expect(page.locator('#layer-buttons [aria-pressed="true"]')).toHaveText(
    "bsi",
    { timeout: 2_000 },
  );
  await expect(page.locator("#date-loading")).toBeVisible({ timeout: 2_000 });

  // once the stalled responses land, the surviving selection gets its dates
  stall = false;
  await expect(page.locator("#cloud-strip span").first()).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/);
});

test("a failing dates request leaves the UI usable", async ({ page }) => {
  let fail = false;
  const badTiles = [];
  await page.route("**/api/dates*", async (route) => {
    if (fail) await route.abort("failed");
    else await route.continue();
  });
  page.on("response", (response) => {
    if (response.status() >= 400 && response.url().includes("/tiles/")) {
      badTiles.push(`${response.status()} ${response.url().slice(0, 120)}`);
    }
  });

  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/, {
    timeout: 30_000,
  });

  fail = true;
  // mndwi: distinct from the default active layer (rgb) and the recovery
  // layer (ndvi) — clicking the active pill would toggle the layer off
  await page.locator('#layer-buttons button:has-text("mndwi")').click();
  // still reacts: pill switches, no crash, no stuck spinner
  await expect(page.locator('#layer-buttons [aria-pressed="true"]')).toHaveText(
    "mndwi",
    { timeout: 2_000 },
  );
  await expect(page.locator("body")).not.toContainText("Failed to start");

  // No valid dates are known for the new layer: the overlay must not keep
  // requesting tiles keyed on the previous layer's date (those all 404 and
  // blank the map), and the scrubber must say so honestly — no empty
  // slider, no perpetual spinner.
  await page.waitForTimeout(1500);
  expect(badTiles).toEqual([]);
  await expect(page.locator("#date-empty")).toBeVisible();
  await expect(page.locator("#date-loading")).toBeHidden();
  await expect(page.locator("#date-controls")).toBeHidden();

  // and it recovers when the backend comes back
  fail = false;
  await page.locator('#layer-buttons button:has-text("ndvi")').click();
  await expect(page.locator("#cloud-strip span").first()).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/);
});

test("basemap switch swaps attribution without console errors", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));

  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/, {
    timeout: 30_000,
  });

  // the active basemap's credit is shown (customAttribution is
  // constructor-only in MapLibre, so the control must follow the basemap)
  await expect(page.locator(".maplibregl-ctrl-attrib")).toContainText(
    "OpenStreetMap",
    { timeout: 15_000 },
  );
  await pickBasemap(page, "esri");
  await expect(page.locator(".maplibregl-ctrl-attrib")).toContainText("Esri", {
    timeout: 10_000,
  });
  await pickBasemap(page, "osm");
  await expect(page.locator(".maplibregl-ctrl-attrib")).toContainText(
    "OpenStreetMap",
    { timeout: 10_000 },
  );

  expect(errors).toEqual([]);
});

test("each in-flight tile request shows a loading animation", async ({ page }) => {
  // Slow server simulation: hold every overlay tile for 3s (the real
  // server can cold-process a scene for much longer) and watch spinners.
  await page.route("**/p/**/tiles/**", async (route) => {
    await new Promise((r) => setTimeout(r, 3000));
    await route.continue();
  });

  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/, {
    timeout: 30_000,
  });

  // while requests are in flight: one positioned loader per pending tile
  const loader = page.locator(".tile-loader").first();
  await expect(loader).toHaveAttribute("data-tile", /^\d+\/\d+\/\d+$/, {
    timeout: 15_000,
  });
  await expect(loader).toHaveCSS("position", "absolute");
  expect(await loader.evaluate((el) => el.style.left)).not.toBe("");

  // once every tile answered, no loader may remain
  await expect(page.locator(".tile-loader")).toHaveCount(0, {
    timeout: 60_000,
  });
});

test("scrubbing away aborts the tiles you no longer want", async ({ page }) => {
  // The slider opens on the newest acquisition — dynamic, the catalog grows.
  const catalog = await (
    await page.request.get(`http://127.0.0.1:4173/p/${SLUG}/api/dates?layer=ndvi`)
  ).json();
  const newest = catalog.dates.at(-1).date;
  await page.addInitScript(() => {
    window.__tileEv = [];
    const real = window.fetch;
    window.fetch = function (input, init) {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      if (!url.includes("/tiles/")) return real.call(this, input, init);
      const m = url.match(/\/tiles\/([^/]+)\/(\d{4}-\d{2}-\d{2})\//);
      const tag = m ? `${m[1]}@${m[2]}` : "other";
      window.__tileEv.push({ e: "start", tag });
      return real.call(this, input, init).then(
        (r) => {
          window.__tileEv.push({ e: r.ok ? "ok" : `http${r.status}`, tag });
          return r;
        },
        (err) => {
          window.__tileEv.push({ e: err.name === "AbortError" ? "abort" : "neterr", tag });
          throw err;
        },
      );
    };
  });
  // Only the SECOND date is slow (a cold scene the user then abandons).
  await page.route("**/p/**/tiles/**/2026-01-09/**", async (route) => {
    await new Promise((r) => setTimeout(r, 10_000));
    await route.continue();
  });

  await page.goto(`/p/${SLUG}/?layer=ndvi`);
  await expect(page.locator("#date-value")).toHaveText(newest, {
    timeout: 30_000,
  });
  // the newest date settles normally (warm)
  await expect(page.locator(".tile-loader")).toHaveCount(0, {
    timeout: 60_000,
  });

  // scrub to the slow date: its tiles go in flight
  await page.locator("#date-end").evaluate((el) => {
    el.value = "1";
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await expect(page.locator("#date-value")).toHaveText("2026-01-09");
  await expect(page.locator(".tile-loader").first()).toBeVisible({
    timeout: 10_000,
  });

  // scrub back to the newest: the abandoned date must be aborted, not
  // waited out, and the already-rendered date must show without it
  await page.locator("#date-end").evaluate((el) => {
    el.value = el.max;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await expect(page.locator("#date-value")).toHaveText(newest);
  await expect
    .poll(
      () =>
        page.evaluate(
          () => window.__tileEv.filter((ev) => ev.e === "abort" && ev.tag.includes("2026-01-09")).length,
        ),
      { timeout: 8_000 },
    )
    .toBeGreaterThan(0);
  // and no spinner lingers for the abandoned date
  await expect(page.locator(".tile-loader")).toHaveCount(0, {
    timeout: 8_000,
  });
});

test("language switcher translates the UI and persists", async ({ page }) => {
  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#about-btn")).toHaveText("Acerca de");
  await expect(page.locator("html")).toHaveAttribute("lang", "es");

  await page.locator("#lang-select").selectOption("en");
  await expect(page.locator("#about-btn")).toHaveText("About");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page.locator("#panel section h2").first()).toHaveText("Layer");
  await expect(
    page.locator('#layer-buttons [data-layer="ndvi"]'),
  ).toHaveAttribute("title", /vegetation/i);

  // the choice survives a fresh visit (localStorage, no URL param)
  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#about-btn")).toHaveText("About");

  // and switching back drops lang=en from the shared URL
  await page.locator("#lang-select").selectOption("es");
  await expect(page.locator("#about-btn")).toHaveText("Acerca de");
  await expect(page).not.toHaveURL(/lang=en/);
});

test("About dialog explains the RES-1333-2017 purpose in both languages", async ({ page }) => {
  await page.goto(`/p/${SLUG}/`);
  await page.locator("#about-btn").click();
  await expect(page.locator("#purpose-title")).toHaveText(
    "Propósito de la Resolución",
  );
  await expect(page.locator("#purpose-p1")).toContainText(/viabilidad ambiental/i);
  await expect(page.locator("#purpose-p2")).toContainText("11 hectáreas");

  // switch language while the dialog is open: the texts follow live
  await page.evaluate(() => {
    const select = document.getElementById("lang-select");
    select.value = "en";
    select.dispatchEvent(new Event("change"));
  });
  await expect(page.locator("#purpose-title")).toHaveText(
    "Purpose of the Resolution",
  );
  await expect(page.locator("#purpose-p1")).toContainText(/environmental viability/i);
  await expect(page.locator("#purpose-p2")).toContainText("11-hectare");
});

test("layer and mode buttons carry help tooltips", async ({ page }) => {
  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#layer-buttons button")).toHaveCount(4);

  // every layer pill explains what it shows, in the active language
  await expect(page.locator('#layer-buttons [data-layer="ndvi"]')).toHaveAttribute(
    "title",
    /vegetación/,
  );
  await expect(page.locator('#layer-buttons [data-layer="bsi"]')).toHaveAttribute(
    "title",
    /superficie expuesta/,
  );
  await expect(page.locator("#layer-buttons .help-dot")).toHaveCount(4);

  await page.locator("#lang-select").selectOption("en");
  await expect(page.locator('#layer-buttons [data-layer="ndvi"]')).toHaveAttribute(
    "title",
    /vegetation/,
  );
  await expect(page.locator('#layer-buttons [data-layer="bsi"]')).toHaveAttribute(
    "title",
    /Bare Soil/,
  );
});

test("overlay overzooms past the service cap instead of 400ing", async ({ page }) => {
  const bad = [];
  page.on("response", (r) => {
    if (r.url().includes("/tiles/") && r.status() >= 400) {
      bad.push(`${r.status()} ${r.url()}`);
    }
  });
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e)));

  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/, {
    timeout: 30000,
  });
  // the map object is created after the geo fetches — wait for it
  await page.waitForFunction(() => window.__mirarsetenaMap, null, {
    timeout: 30000,
  });

  // the overlay source must declare the service's zoom cap so MapLibre
  // overzooms the deepest tiles instead of requesting z19+ (400s)
  const maxzoom = await page.evaluate(
    () => window.__mirarsetenaMap.getSource("overlay").maxzoom,
  );
  expect(maxzoom).toBe(18);

  // deep zoom (z19+) over the site: no tile may fail
  await page.evaluate(() =>
    window.__mirarsetenaMap.jumpTo({ center: [-83.6693, 9.3854], zoom: 19.5 }),
  );
  await page.waitForTimeout(4000);
  expect(bad).toEqual([]);
  expect(pageErrors).toEqual([]);
});

test("restored URL state drives every control (no inverted checkboxes)", async ({ page }) => {
  // A shared URL says: inspection OFF, hide-cloudy ON, max cloud 50,
  // Esri basemap, raw mode. Every input must SHOW that state — otherwise
  // the next click inverts what the user thinks they are changing.
  await page.goto(
    `/p/${SLUG}/?layer=ndvi&date=2026-01-04&hideCloudy=1&maxCloud=50&properties=1&pois=facilities&basemap=esri`,
  );
  await expect(page.locator("#date-value")).toHaveText("2026-01-04");
  await expect(page.locator("#poi-inspection")).not.toBeChecked();
  await expect(page.locator("#poi-facilities")).toBeChecked();
  await expect(page.locator("#hide-cloudy")).toBeChecked();
  await expect(page.locator("#max-cloud")).toHaveValue("50");
  await expect(page.locator("#show-properties")).toBeChecked();
  await expect(page.locator("#btn-basemap")).toHaveAttribute("data-basemap", "esri");
  // raw/change mode selection no longer exists anywhere
  await expect(page.locator('input[name="mode"]')).toHaveCount(0);
  await expect(page.locator("#baseline-select")).toHaveCount(0);

  // state and UI agree on the map itself
  await page.waitForFunction(() => window.__mirarsetenaMap, null, { timeout: 15000 });
  const view = await page.evaluate(() => ({
    inspection:
      document.querySelector('#poi-markers [data-group="inspection"]') &&
      getComputedStyle(document.querySelector('#poi-markers [data-group="inspection"]')).display !== "none",
    base: window.__mirarsetenaMap.getSource("base").tiles?.[0] ?? "",
  }));
  expect(view.inspection).toBe(false);
  expect(view.base).toContain("arcgisonline");

  // flipping the checkbox now flips the map (not the other way around)
  await page.locator("#poi-inspection").check();
  await expect
    .poll(
      () =>
        page.evaluate(
          () =>
            getComputedStyle(document.querySelector('#poi-markers [data-group="inspection"]')).display !== "none",
        ),
      { timeout: 5000 },
    )
    .toBe(true);
});

test("settings are memorized for bare-URL visits", async ({ page }) => {
  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/, {
    timeout: 30000,
  });

  await pickBasemap(page, "esri");
  await page.locator("#poi-inspection").uncheck();
  await page.locator("#hide-cloudy").check();
  await page.waitForTimeout(300);

  // fresh visit WITHOUT any query string: remembered settings re-apply
  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/, {
    timeout: 30000,
  });
  await expect(page.locator("#btn-basemap")).toHaveAttribute("data-basemap", "esri");
  await expect(page.locator("#poi-inspection")).not.toBeChecked();
  await expect(page.locator("#hide-cloudy")).toBeChecked();
  await page.waitForFunction(() => window.__mirarsetenaMap, null, { timeout: 15000 });
  const base = await page.evaluate(
    () => window.__mirarsetenaMap.getSource("base").tiles?.[0] ?? "",
  );
  expect(base).toContain("arcgisonline");
});

test("PNG export captures the rendered map, not a transparent image", async ({ page }) => {
  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/, {
    timeout: 30_000,
  });
  // wait until the map really painted (style + raster tiles) before capture
  await page.waitForFunction(
    () => {
      const map = window.__mirarsetenaMap;
      return Boolean(map) && map.loaded() && map.areTilesLoaded();
    },
    null,
    { timeout: 60_000 },
  );
  await page.waitForTimeout(1500); // let the final frame composite

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.click("#png-btn"),
  ]);
  const base64 = readFileSync(await download.path()).toString("base64");
  const opaqueRatio = await page.evaluate(async (payload) => {
    const image = new Image();
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = reject;
      image.src = `data:image/png;base64,${payload}`;
    });
    const canvas = document.createElement("canvas");
    canvas.width = image.width;
    canvas.height = image.height;
    const context = canvas.getContext("2d");
    context.drawImage(image, 0, 0);
    const { data } = context.getImageData(0, 0, canvas.width, canvas.height);
    let opaque = 0;
    let total = 0;
    for (let index = 3; index < data.length; index += 4 * 97) {
      total += 1;
      if (data[index] > 10) opaque += 1;
    }
    return opaque / total;
  }, base64);
  expect(opaqueRatio).toBeGreaterThan(0.5);
});

test("POI pins speak the active language", async ({ page }) => {
  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#poi-markers .poi-dot")).toHaveCount(10, {
    timeout: 30000,
  });

  // default Spanish-first UI shows the official RES-1333-2017 names
  await expect(page.locator('#poi-markers .poi-label:text-is("Quebrador")')).toHaveCount(1);
  await expect(page.locator('#poi-markers .poi-label:text-is("Acopio")')).toHaveCount(1);
  await expect(page.locator('#poi-markers .poi-label:text-is("Breaker")')).toHaveCount(0);

  // switching language rewrites the pins live
  await page.locator("#lang-select").selectOption("en");
  await expect(page.locator('#poi-markers .poi-label:text-is("Breaker")')).toHaveCount(1);
  await expect(page.locator('#poi-markers .poi-label:text-is("Storage")')).toHaveCount(1);
  await expect(page.locator('#poi-markers .poi-label:text-is("Quebrador")')).toHaveCount(0);
});








test("Rio General work-area line renders as an overlay", async ({ page }) => {
  // The plan's work area follows the Río General between the project
  // start/end POIs (the POIs sit on the river within metres): the clipped
  // OSM way must be served and drawn as its own line source.
  const rio = await page.request.get(`/p/${SLUG}/rio.geojson`);
  expect(rio.status()).toBe(200);
  const payload = await rio.json();
  const line = payload.features[0];
  expect(line.geometry.type).toBe("LineString");
  expect(line.geometry.coordinates.length).toBeGreaterThanOrEqual(2);

  await page.goto(`/p/${SLUG}/`);
  await page.waitForFunction(() => window.__mirarsetenaMap, null, {
    timeout: 30_000,
  });
  await expect
    .poll(() =>
      page.evaluate(() => {
        const map = window.__mirarsetenaMap;
        const src = map.getSource("rio");
        if (!src) return 0;
        const data = src.serialize ? src.serialize().data : src._data;
        return data?.features?.[0]?.geometry?.coordinates?.length ?? 0;
      }),
      { timeout: 30_000 },
    )
    .toBeGreaterThanOrEqual(2);
  const layers = await page.evaluate(() =>
    ["rio-casing", "rio-outline"].filter((id) => window.__mirarsetenaMap.getLayer(id)),
  );
  expect(layers).toEqual(["rio-casing", "rio-outline"]);
});

test("Rio start-end line has a toggle checkbox, is 50% transparent, and remembers itself", async ({ page }) => {
  await page.goto(`/p/${SLUG}/`);
  await page.waitForFunction(() => window.__mirarsetenaMap, null, {
    timeout: 30_000,
  });
  const rioVisible = () =>
    page.evaluate(() =>
      ["rio-casing", "rio-outline"].every((id) => {
        const map = window.__mirarsetenaMap;
        if (!map.getLayer(id)) return false;
        return map.getLayoutProperty(id, "visibility") !== "none";
      }),
    );

  // default: on, and the line is 50% transparent (user 2026-10-09)
  await expect.poll(rioVisible).toBe(true);
  const opacity = await page.evaluate(() => [
    window.__mirarsetenaMap.getPaintProperty("rio-casing", "line-opacity"),
    window.__mirarsetenaMap.getPaintProperty("rio-outline", "line-opacity"),
  ]);
  expect(opacity).toEqual([0.5, 0.5]);

  // the checkbox turns it off and the URL carries the choice
  await page.locator("#show-rio-line").uncheck();
  await expect.poll(rioVisible).toBe(false);
  await expect(page).toHaveURL(/rio=0/);

  // ...and back on, surviving a reload (memorized)
  await page.locator("#show-rio-line").check();
  await expect.poll(rioVisible).toBe(true);
  await page.reload();
  await page.waitForFunction(() => window.__mirarsetenaMap, null, {
    timeout: 30_000,
  });
  await expect.poll(rioVisible).toBe(true);
  await expect(page.locator("#show-rio-line")).toBeChecked();
});

test("date picker hides entirely when no layer is selected", async ({ page }) => {
  // With no layer active there is nothing to scrub: the whole Fecha
  // section (title, empty message, controls) must disappear — not show
  // 'Sin fechas disponibles' for a layer the user deliberately turned off.
  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#date-section")).toBeVisible();

  await page.locator('#layer-buttons [aria-pressed="true"]').click();
  await expect(page.locator("#date-section")).toBeHidden();

  await page.locator('#layer-buttons button:has-text("mndwi")').click();
  await expect(page.locator("#date-section")).toBeVisible();
});



test("3D terrain toggle drapes the map over the elevation DEM", async ({ page }) => {
  // User (2026-10-12): 'is it possible to get the elevation data for this
  // region and show a 3d map based on that'. Yes: AWS elevation-tiles
  // (terrarium, SRTM-based) covers the AOI (centre ~1169 m) and
  // MapLibre v6 setTerrain drapes every layer over it.
  const demRequests = [];
  page.on("request", (request) => {
    if (request.url().includes("elevation-tiles-prod")) demRequests.push(request.url());
  });

  await page.goto(`/p/${SLUG}/`);
  await page.waitForFunction(() => window.__mirarsetenaMap, null, {
    timeout: 30_000,
  });

  const terrain = () =>
    page.evaluate(() => window.__mirarsetenaMap.getTerrain());
  const pitch = () =>
    page.evaluate(() => window.__mirarsetenaMap.getPitch());

  // default: flat map, no DEM traffic, the map button reads 3D
  expect(await terrain()).toBeNull();
  await expect(page.locator("#btn-3d")).toHaveText("3D");
  await expect(page.locator("#btn-3d")).toHaveAttribute("aria-pressed", "false");

  // click 3D -> terrain engages, camera tilts, DEM tiles are fetched
  await page.locator("#btn-3d").click();
  await expect.poll(terrain, { timeout: 15_000 }).toEqual({
    source: "terrain-dem",
    exaggeration: 1.3,
  });
  await expect.poll(() => demRequests.length, { timeout: 15_000 }).toBeGreaterThan(0);
  await expect.poll(pitch, { timeout: 15_000 }).toBeGreaterThan(50);
  await expect(page).toHaveURL(/d3=1/);
  await expect(page.locator("#btn-3d")).toHaveText("2D");

  // click 2D -> flat again
  await page.locator("#btn-3d").click();
  expect(await terrain()).toBeNull();
  await expect.poll(pitch, { timeout: 15_000 }).toBe(0);

  // the choice survives a reload
  await page.locator("#btn-3d").click();
  await page.reload();
  await page.waitForFunction(() => window.__mirarsetenaMap, null, {
    timeout: 30_000,
  });
  await expect.poll(terrain, { timeout: 15_000 }).toEqual({
    source: "terrain-dem",
    exaggeration: 1.3,
  });
  await expect(page.locator("#btn-3d")).toHaveText("2D");
});

test("basemap zooms cap at the provider's deepest real tile (no gray tiles)", async ({ page }) => {
  // Measured 2026-10-12: Esri World Imagery serves uniform gray #CCCCCC
  // placeholders from z19 up over this AOI, OSM 400s from z20. The base
  // source must declare maxzoom so MapLibre OVERZOOMS the last real tile
  // instead of fetching gray/404 tiles on deep zoom.
  const grayOrFailed = [];
  page.on("response", (response) => {
    const url = response.url();
    if (url.includes("arcgisonline") && /\/tile\/(19|2\d)\//.test(url)) {
      grayOrFailed.push(`${response.status()} ${url}`);
    }
    if (url.includes("tile.openstreetmap.org")) {
      const m = /\/(\d+)\/\d+\/\d+\.png/.exec(url);
      if (m && Number(m[1]) > 19) grayOrFailed.push(`${response.status()} ${url}`);
    }
  });

  await page.goto(`/p/${SLUG}/`);
  await page.waitForFunction(() => window.__mirarsetenaMap, null, {
    timeout: 30_000,
  });

  // OSM (default): source declares maxzoom 19 (poll — the style loads
  // async, the source reports the 22 default until loadTileJson lands)
  await expect
    .poll(() =>
      page.evaluate(() => window.__mirarsetenaMap.getSource("base").maxzoom),
    )
    .toBe(19);

  // Esri: source declares maxzoom 18 and a deep zoom requests no z19+ tiles
  await pickBasemap(page, "esri");
  await expect
    .poll(() =>
      page.evaluate(() => window.__mirarsetenaMap.getSource("base").maxzoom),
    )
    .toBe(18);
  await page.evaluate(() =>
    window.__mirarsetenaMap.jumpTo({ center: [-83.6693, 9.3854], zoom: 21 }),
  );
  await page.waitForTimeout(3000);
  expect(grayOrFailed).toEqual([]);
});

test("retargeting over errored tiles never crashes the renderer", async ({ page }) => {
  // maplibre-gl-js #7775: setTiles() over a source holding `errored` tiles
  // re-promotes them to a renderable state without a texture → the raster
  // renderer throws `...reading 'bind'` on the next frame. Slow 404s keep
  // the crash window wide open during the optical layer-switch retargets.
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  await page.route(`**/p/${SLUG}/tiles/**`, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1200));
    await route.fulfill({ status: 404, body: "" });
  });

  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/, {
    timeout: 30_000,
  });
  // let the 404s settle so the source cache holds errored tiles in view
  await page.waitForTimeout(8000);

  // setTiles() with those errored tiles present — the crash window
  await page.locator('#layer-buttons button:has-text("bsi")').click();
  await expect(page).toHaveURL(/layer=bsi/);
  await page.waitForTimeout(3000);
  // one more retarget while the first refetch wave may still be in flight
  await page.locator('#layer-buttons button:has-text("ndvi")').click();
  await page.waitForTimeout(3000);

  expect(pageErrors.filter((error) => error.includes("bind"))).toEqual([]);
});
