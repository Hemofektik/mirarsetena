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
  await page.locator('#layer-buttons button:has-text("sigma0")').click();
  await expect(page.locator("#radar-controls")).toBeVisible();
  await expect(page.locator("#cloud-strip span")).toHaveCount(8, {
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
  await expect(page.locator("#baseline-select option")).toHaveCount(9); // 8 + default

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
