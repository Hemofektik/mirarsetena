// Smoke test (I.2 / I.6 / I.7): app boots against the live backend,
// layer switching drives the date scrubber, and the mobile viewport stays usable.
import { expect, test } from "@playwright/test";

const SLUG = "cdp-rio-general";

test("app boots, layer switching drives the scrubber", async ({ page }) => {
  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#app-title")).toHaveText("Mirar Setena");
  await expect(page.locator("#project-name")).toContainText("CDP Río General");

  // six configured layers
  await expect(page.locator("#layer-buttons button")).toHaveCount(6);

  // S2 dates populate the cloud strip (live catalog)
  await expect(page.locator("#cloud-strip span").first()).toBeVisible({
    timeout: 30_000,
  });
  const s2Count = await page.locator("#cloud-strip span").count();
  expect(s2Count).toBeGreaterThan(10);

  // switch to a radar layer: controls appear, dates become the S1 set
  // (count comes from the API — the live catalog grows over time)
  const s1Count = await page.evaluate(async (slug) => {
    const response = await fetch(`/p/${slug}/api/dates?layer=sigma0`);
    return (await response.json()).dates.length;
  }, SLUG);
  await page.locator('#layer-buttons button:has-text("sigma0")').click();
  await expect(page.locator("#radar-controls")).toBeVisible();
  await expect(page.locator("#cloud-strip span")).toHaveCount(s1Count, {
    timeout: 30_000,
  });

  // raw/change mode switch keeps the app alive
  await page.locator('input[name="mode"][value="raw"]').check();
  await expect(page.locator("#date-value")).not.toHaveText("—");

  // About dialog shows the disclaimer (i18n + attribution)
  await page.locator("#about-btn").click();
  await expect(page.locator("#about-disclaimer")).toContainText("SETENA");
  await page.keyboard.press("Escape");

  // works-start marker renders from config
  await expect(page.locator("#works-marker")).toContainText("2026-08-01");

  // hide-cloudy shrinks the strip
  const before = await page.locator("#cloud-strip span").count();
  await page.locator("#hide-cloudy").check();
  const after = await page.locator("#cloud-strip span").count();
  expect(after).toBeLessThan(before);
  await page.locator("#hide-cloudy").uncheck();

  // baseline select lists the S1 dates (radar controls)
  await expect(page.locator("#baseline-select option")).toHaveCount(s1Count + 1); // dates + default

  // POI group toggle + basemap swap keep the app alive
  await page.locator("#poi-facilities").uncheck();
  await page.locator("#poi-facilities").check();
  await page.locator("#basemap-select").selectOption("esri");
  await expect(page.locator("#basemap-select")).toHaveValue("esri");
  await page.locator("#basemap-select").selectOption("osm");

  // share encodes state into the URL
  await page.locator("#share-btn").click();
  await expect(page).toHaveURL(/layer=sigma0/);

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
  await expect(page.locator("#date-range")).toBeVisible();
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
  await page.locator("#basemap-select").selectOption("esri");
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

  // both 1991 plan parcels come from the API and the layer is drawn
  const aoi = await page.request.get(`/p/${SLUG}/aoi.geojson`);
  expect((await aoi.json()).features).toHaveLength(2);
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
  await page.locator('#layer-buttons button:has-text("sigma0")').click();

  // the pill and the radar controls must react now, not after the fetch
  await expect(page.locator('#layer-buttons [aria-pressed="true"]')).toHaveText(
    "sigma0",
    { timeout: 2_000 },
  );
  await expect(page.locator("#radar-controls")).toBeVisible({ timeout: 2_000 });

  // a second switch while the first is still in flight must win
  await page.locator('#layer-buttons button:has-text("ndvi")').click();
  await expect(page.locator('#layer-buttons [aria-pressed="true"]')).toHaveText(
    "ndvi",
    { timeout: 2_000 },
  );
  await expect(page.locator("#radar-controls")).toBeHidden({ timeout: 2_000 });

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
  await page.locator('#layer-buttons button:has-text("coherence")').click();
  // still reacts: pill switches, no crash, no stuck spinner
  await expect(page.locator('#layer-buttons [aria-pressed="true"]')).toHaveText(
    "coherence",
    { timeout: 2_000 },
  );
  await expect(page.locator("body")).not.toContainText("Failed to start");

  // No valid dates are known for the new layer: the overlay must not keep
  // requesting tiles keyed on the previous layer's date (those all 404 and
  // blank the map), and the scrubber must say so honestly.
  await page.waitForTimeout(1500);
  expect(badTiles).toEqual([]);
  await expect(page.locator("#date-value")).toHaveText("—");

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
  await page.locator("#basemap-select").selectOption("esri");
  await expect(page.locator(".maplibregl-ctrl-attrib")).toContainText("Esri", {
    timeout: 10_000,
  });
  await page.locator("#basemap-select").selectOption("osm");
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
  await expect(page.locator("#date-value")).toHaveText("2026-01-04", {
    timeout: 30_000,
  });
  // first date settles normally (warm)
  await expect(page.locator(".tile-loader")).toHaveCount(0, {
    timeout: 60_000,
  });

  // scrub to the slow date: its tiles go in flight
  await page.locator("#date-range").evaluate((el) => {
    el.value = "1";
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await expect(page.locator("#date-value")).toHaveText("2026-01-09");
  await expect(page.locator(".tile-loader").first()).toBeVisible({
    timeout: 10_000,
  });

  // scrub back: the abandoned date must be aborted, not waited out, and
  // the cached first date must render without it
  await page.locator("#date-range").evaluate((el) => {
    el.value = "0";
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await expect(page.locator("#date-value")).toHaveText("2026-01-04");
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
  await expect(page.locator("#layer-buttons button")).toHaveCount(6);

  // every layer pill explains what it shows, in the active language
  await expect(page.locator('#layer-buttons [data-layer="ndvi"]')).toHaveAttribute(
    "title",
    /vegetación/,
  );
  await expect(page.locator('#layer-buttons [data-layer="bsi"]')).toHaveAttribute(
    "title",
    /superficie expuesta/,
  );
  await expect(page.locator("#layer-buttons .help-dot")).toHaveCount(6);

  // the radar mode radios are documented as well
  await expect(
    page.locator('#radar-controls label:has(input[value="change"])'),
  ).toHaveAttribute("title", /\S/);

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
