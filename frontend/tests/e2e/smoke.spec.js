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

test("POI checkboxes toggle their label layers on the map", async ({ page }) => {
  await page.goto(`/p/${SLUG}/`);
  await expect(page.locator("#date-value")).toHaveText(/^\d{4}-\d{2}-\d{2}$/);
  await page.waitForFunction(
    () => window.__mirarsetenaMap?.getLayer("poi-facilities"),
    null,
    { timeout: 15000 },
  );
  const read = () =>
    page.evaluate(() => ({
      facilities: window.__mirarsetenaMap.getLayoutProperty("poi-facilities", "visibility"),
      inspection: window.__mirarsetenaMap.getLayoutProperty("poi-inspection", "visibility"),
    }));

  // both groups default on: the property must be explicitly set (it used to
  // stay undefined because setTiles() invalidated the style mid-render)
  await expect
    .poll(async () => await read(), { timeout: 10000 })
    .toEqual({ facilities: "visible", inspection: "visible" });

  await page.locator("#poi-facilities").uncheck();
  await expect.poll(async () => (await read()).facilities).toBe("none");

  await page.locator("#poi-facilities").check();
  await expect.poll(async () => (await read()).facilities).toBe("visible");

  await page.locator("#poi-inspection").uncheck();
  await expect.poll(async () => (await read()).inspection).toBe("none");
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
