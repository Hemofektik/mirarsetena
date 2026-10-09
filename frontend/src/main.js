/* Mirar Setena frontend: MapLibre shell wired to the pure state model. */
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import "./styles.css";
import * as S from "./state.js";
import { installPoiMarkers } from "./poi-markers.js";
import { installTileLoaders, retireTileRequests } from "./tile-loaders.js";

const LANG_KEY = "mirarsetena.lang";
const slug = location.pathname.split("/").filter(Boolean)[1];
let config;
let state;
let map;
let poisData = null;
let aoiData = null;

const BASEMAPS = {
  osm: {
    tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
    attribution: "© OpenStreetMap contributors",
  },
  esri: {
    tiles: [
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    ],
    attribution: "Esri, Maxar, Earthstar Geographics",
  },
};

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return response.json();
}

function isRadar(layer) {
  return layer === "sigma0" || layer === "coherence";
}

function overlayTemplate() {
  const params = new URLSearchParams();
  if (state.baseline) params.set("baseline", state.baseline);
  const query = params.toString();
  return (
    `${location.origin}/p/${state.slug}/tiles/${state.layer}/` +
    `${state.date}/{z}/{x}/{y}.png${query ? `?${query}` : ""}`
  );
}

let lastOverlayTemplate = null;
let attributionControl = null;
let poiMarkers = null;

// Display settings remembered between visits (bare-URL loads re-apply
// them; an explicit URL always wins because it is restored afterwards).
const SETTINGS_KEY = "mirarsetena.settings";

function loadSettings() {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function saveSettings() {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(S.settingsFromState(state)));
  } catch {
    // private mode / quota — settings simply are not remembered
  }
}

function poiLabelFor(id, fallback) {
  const key = `poi_${id}`;
  const text = S.t(state.lang, key);
  return text === key ? fallback : text;
}

// A gesture (drag/scrub) may change the template many times per second:
// every setTiles() invalidates the style — rapid churn made MapLibre's
// render throw (texture.bind of undefined), spammed AbortErrors and asked
// the server for a derived product per tick. Collapse the gesture into ONE
// retarget: while the pointer is down on the slider nothing commits; the
// final template wins 150 ms after the last change (or right after release).
const OVERLAY_COMMIT_DELAY = 150;
let overlayCommit = null;
let overlayDragging = false;

function scheduleOverlayCommit(template) {
  if (overlayCommit?.timer) clearTimeout(overlayCommit.timer);
  overlayCommit = {
    template,
    timer: setTimeout(() => {
      const pending = overlayCommit?.template;
      overlayCommit = pending ? { template: pending } : null;
      if (overlayDragging) return; // parked: pointerup re-schedules
      if (pending) commitOverlayTemplate(pending);
    }, OVERLAY_COMMIT_DELAY),
  };
}

function commitOverlayTemplate(template) {
  if (!template || template === lastOverlayTemplate) return;
  const source = map?.getSource("overlay");
  if (!source) return;
  // MapLibre never cancels the previous date's fetches — retire them so
  // an abandoned cold scene stops loading (and stops the server work).
  retireTileRequests(template);
  source.setTiles([template]);
  lastOverlayTemplate = template;
}

function updateOverlay() {
  if (!map || !map.getLayer("overlay")) return;
  const source = map.getSource("overlay");
  if (!source) return;
  const enabled = Boolean(state.date && state.layer);
  const template = enabled ? overlayTemplate() : null;
  // Only touch the source when the template actually changed: setTiles()
  // invalidates the style (isStyleLoaded() -> false) and would starve the
  // guards of every later render step.
  if (template && template !== lastOverlayTemplate) {
    scheduleOverlayCommit(template);
  }
  if (!enabled && lastOverlayTemplate !== null) {
    // switched off: drop in-flight tiles and forget the template so
    // re-enabling fetches fresh instead of waiting on aborted requests
    if (overlayCommit?.timer) clearTimeout(overlayCommit.timer);
    overlayCommit = null;
    retireTileRequests(null);
    lastOverlayTemplate = null;
  }
  map.setLayoutProperty("overlay", "visibility", enabled ? "visible" : "none");
}

function updatePoiVisibility() {
  if (!poiMarkers) return;
  poiMarkers.setVisible("facilities", state.poiGroups.facilities);
  poiMarkers.setVisible("inspection", state.poiGroups.inspection);
}

function updateAoiVisibility() {
  if (!map) return;
  for (const id of ["aoi-fill", "aoi-casing", "aoi-outline"]) {
    if (!map.getLayer(id)) continue;
    map.setLayoutProperty(
      id,
      "visibility",
      state.showProperties ? "visible" : "none"
    );
  }
}

function updateBasemap() {
  if (!map || !map.getLayer("base")) return;
  const source = map.getSource("base");
  if (!source) return;
  const basemap = BASEMAPS[state.basemap] ?? BASEMAPS.osm;
  if (!basemap.tiles.every((t, i) => source.tiles?.[i] === t)) {
    source.setTiles(basemap.tiles);
  }
  // AttributionControl takes customAttribution at construction only — swap
  // the tracked control whenever the basemap (and its credit) changes.
  // (Map has no setAttributionControl; the source-level attribution field
  // is static and would keep crediting the boot basemap.)
  if (attributionControl) map.removeControl(attributionControl);
  attributionControl = new maplibregl.AttributionControl({
    customAttribution: basemap.attribution,
  });
  map.addControl(attributionControl, "bottom-right");
}

function pushUrl() {
  const query = S.serializeState(state);
  history.replaceState(null, "", `${location.pathname}?${query}`);
  saveSettings(); // every committed state change leaves the settings behind
}

function cloudClass(cloud) {
  if (cloud == null) return "mid";
  if (cloud <= 20) return "clear";
  if (cloud <= 60) return "mid";
  return "cloudy";
}

function renderLayerButtons() {
  const container = document.getElementById("layer-buttons");
  container.innerHTML = "";
  for (const layer of state.layers) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.layer = layer;
    button.textContent = layer;
    // hover help: what this index actually shows (localized)
    button.title = S.t(state.lang, `layer_${layer}_help`);
    button.setAttribute("aria-pressed", String(layer === state.layer));
    // always-visible affordance that a tooltip exists; CSS renders the "?"
    // so the pill's textContent stays exactly the layer slug
    const dot = document.createElement("span");
    dot.className = "help-dot";
    dot.setAttribute("aria-hidden", "true");
    button.appendChild(dot);
    button.addEventListener("click", () => {
      if (state.layer === layer) {
        // clicking the pressed pill disables the layer entirely; the bump
        // also cancels an in-flight dates fetch for this layer
        datesSeq += 1;
        state = S.disableLayer(state);
        renderAll();
      } else {
        selectLayer(layer);
      }
    });
    container.appendChild(button);
  }
}

function renderScrubber() {
  const loading = state.datesLoading;
  const dates = S.visibleDates(state);
  const empty = !loading && dates.length === 0;
  document.getElementById("date-loading").hidden = !loading;
  document.getElementById("date-empty").hidden = !empty;
  document.getElementById("date-controls").hidden = loading || empty;
  if (loading || empty) return;

  // Radar layers get a start->end range (change between the two thumbs);
  // optical layers keep a single thumb on the acquisition date.
  const { radar, lastIndex, startIdx, endIdx } = S.sliderIndices(state, dates);
  const startInput = document.getElementById("date-start");
  const endInput = document.getElementById("date-end");
  startInput.hidden = !radar;

  // END keeps static bounds (0..last): a dynamic min became degenerate
  // (min == max) whenever the thumbs were adjacent and Chrome parked the
  // thumb at the left edge — "moving one knob moves the other". The
  // start<end wall lives on the START input (max = end - 1) and in
  // applyDate() for the end handler.
  endInput.min = "0";
  endInput.max = String(lastIndex);
  endInput.value = String(Math.max(endIdx, 0));
  startInput.min = "0";
  startInput.max = String(Math.max(endIdx - 1, 0));
  startInput.value = String(startIdx);

  const span = document.getElementById("range-span");
  if (radar && lastIndex > 0) {
    span.hidden = false;
    span.style.left = `calc(8px + ${(startIdx / lastIndex).toFixed(4)} * (100% - 16px))`;
    span.style.width = `calc(${((endIdx - startIdx) / lastIndex).toFixed(4)} * (100% - 16px))`;
  } else {
    span.hidden = true;
  }

  document.getElementById("date-value").textContent = radar
    ? `${dates[startIdx].date} → ${dates[endIdx].date}`
    : dates[endIdx].date;
  const cloud = state.cloudByDate?.[state.date];
  document.getElementById("cloud-badge").textContent =
    cloud == null ? "" : `${S.t(state.lang, "cloud")}: ${cloud}%`;

  const strip = document.getElementById("cloud-strip");
  strip.innerHTML = "";
  for (const entry of dates) {
    const dot = document.createElement("span");
    dot.dataset.cloud = cloudClass(entry.cloud);
    dot.title = `${entry.date} (${entry.cloud == null ? "—" : entry.cloud + "%"})`;
    dot.addEventListener("click", () => applyDate(entry.date));
    strip.appendChild(dot);
  }
  document.getElementById("works-marker").textContent =
    `${S.t(state.lang, "works_start")}: ${config.timeline.works_start}`;
}

function renderAll() {
  // radar: keep the range start strictly before its end before rendering
  state = S.ensureRadarRange(state, S.visibleDates(state));
  renderLayerButtons();
  renderScrubber();
  updateOverlay();
  updatePoiVisibility();
  updateAoiVisibility();
  syncControlsFromState();
  pushUrl();
}

/**
 * The inputs are DOM state that can drift from the model (URL restore,
 * stored settings). Re-derive them on every render so a checkbox always
 * SHOWS what the map is doing — a stale box inverts the next click.
 */
function syncControlsFromState() {
  document.getElementById("poi-facilities").checked = state.poiGroups.facilities;
  document.getElementById("poi-inspection").checked = state.poiGroups.inspection;
  document.getElementById("hide-cloudy").checked = state.hideCloudy;
  document.getElementById("max-cloud").value = String(state.maxCloud);
  document.getElementById("show-properties").checked = state.showProperties;
  document.getElementById("basemap-select").value = state.basemap;
}

// Sequence tokens: a layer switch must never be undone by a slower response
// from a previous one (last click wins, not last response).
let datesSeq = 0;

// Dates are fetched per layer and remembered so a re-switch picks a valid
// date immediately — otherwise the overlay would briefly (or, on a failed
// fetch, permanently) request tiles keyed on the previous layer's date.
const datesByLayer = new Map();

async function fetchDates(layer, { retried = false } = {}) {
  const token = ++datesSeq;
  try {
    const payload = await getJSON(
      `/p/${state.slug}/api/dates?layer=${encodeURIComponent(layer)}`
    );
    if (token !== datesSeq) return; // a newer switch owns the state
    datesByLayer.set(layer, payload.dates);
    state = S.applyDates({ ...state, layer }, payload.dates);
    renderAll();
  } catch (error) {
    if (token !== datesSeq) return;
    console.error("dates request failed", error);
    // Honest state: no valid date is known for this layer — an overlay keyed
    // on the previous layer's date would only 404 and blank the map.
    state = S.setLayer(state, layer, []);
    renderAll();
    // One delayed retry for transient failures, unless the user moved on.
    if (!retried) {
      setTimeout(() => {
        if (token === datesSeq) fetchDates(layer, { retried: true });
      }, 2000);
    }
  }
}

function selectLayer(layer) {
  // React first: the pill and its controls move without waiting for the
  // catalog; known dates apply instantly, unknown ones show the spinner
  // (an empty slider must never appear — user report).
  const cached = datesByLayer.get(layer);
  state = S.setLayer(state, layer, cached ?? []);
  if (!cached) state = S.setDatesLoading(state, true);
  renderAll();
  fetchDates(layer);
}

async function applyDate(date) {
  const dates = S.visibleDates(state);
  if (dates.length) {
    // one choke point for both handlers: the end can never land on or
    // before the range start (drags, cloud-strip clicks) — clamp it up
    // instead of letting ensureRadarRange MOVE THE OTHER KNOB.
    const { radar, startIdx } = S.sliderIndices(state, dates);
    let idx = dates.findIndex((entry) => entry.date === date);
    if (idx < 0) idx = dates.length - 1;
    if (radar && idx <= startIdx) idx = Math.min(startIdx + 1, dates.length - 1);
    date = dates[idx].date;
  }
  state = S.setDate(state, date);
  renderAll();
}

function buildMap() {
  const basemap = BASEMAPS[state.basemap] ?? BASEMAPS.osm;
  map = new maplibregl.Map({
    container: "map",
    style: {
      version: 8,
      glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
      sources: {
        base: {
          type: "raster",
          tiles: basemap.tiles,
          tileSize: 256,
          // no static attribution here: it is frozen to the boot basemap;
          // the tracked AttributionControl follows the active one instead
        },
        overlay: {
          type: "raster",
          // disabled boot (layer=off) has no layer/date for a valid template
          tiles: state.date && state.layer ? [overlayTemplate()] : [],
          tileSize: 256,
          // the tile service caps at cache.max_zoom; without this MapLibre
          // keeps requesting z19+ and the server answers 400 for each tile
          maxzoom: config.cache.max_zoom,
        },
        aoi: { type: "geojson", data: aoiData },
      },
      layers: [
        { id: "base", type: "raster", source: "base" },
        {
          id: "overlay",
          type: "raster",
          source: "overlay",
          // disabled boot: hidden from the first frame — an empty tile list
          // with a visible layer makes MapLibre crash building tile URLs
          layout: {
            visibility: state.date && state.layer ? "visible" : "none",
          },
          paint: { "raster-opacity": 0.85, "raster-fade-duration": 150 },
        },
        // the two 1991 plan parcels: fill + white casing + colored outline
        {
          id: "aoi-fill",
          type: "fill",
          source: "aoi",
          layout: { visibility: state.showProperties ? "visible" : "none" },
          paint: { "fill-color": "#1d4ed8", "fill-opacity": 0.06 },
        },
        {
          id: "aoi-casing",
          type: "line",
          source: "aoi",
          layout: {
            visibility: state.showProperties ? "visible" : "none",
            "line-join": "round",
          },
          paint: { "line-color": "#ffffff", "line-width": 5 },
        },
        {
          id: "aoi-outline",
          type: "line",
          source: "aoi",
          layout: {
            visibility: state.showProperties ? "visible" : "none",
            "line-join": "round",
          },
          paint: { "line-color": "#1d4ed8", "line-width": 2.5 },
        },
      ],
    },
    center: [-83.668, 9.383],
    zoom: 13,
    preserveDrawingBuffer: true,
    // managed explicitly so it can follow basemap switches
    attributionControl: false,
  });
  map.addControl(new maplibregl.NavigationControl(), "top-right");
  // Intentional retirements (drag retargets, layer switches) surface as
  // AbortErrors; with a listener MapLibre stops console.error-ing them.
  // Real errors keep logging.
  map.on("error", (event) => {
    const err = event?.error;
    if (err?.name === "AbortError" || /abort/i.test(String(err?.message))) return;
    console.error(err ?? event);
  });
  // spinners over every in-flight overlay tile (cold scenes take seconds)
  installTileLoaders(map);
  // dots + decluttered labels (symbol layers would hide colliding text)
  poiMarkers = installPoiMarkers(map, poisData, poiLabelFor);
  // the style was built with this template (or none when disabled at boot)
  lastOverlayTemplate = state.date && state.layer ? overlayTemplate() : null;
  // debug/QA hook: inspect the live map from the console or automated checks
  window.__mirarsetenaMap = map;
  // The inline style parses asynchronously: any renderAll() that ran before
  // "load" was skipped by the isStyleLoaded() guards — re-apply it once ready.
  map.once("load", () => {
    renderAll();
    updateBasemap(); // installs the initial basemap's credit
  });
  map.on("moveend", () => {
    const bounds = map.getBounds();
    state = S.setViewport(state, [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()], map.getZoom());
    pushUrl();
  });
}

function wireUi() {
  // While a thumb is being dragged the overlay retarget stays parked
  // (see scheduleOverlayCommit): one setTiles() per gesture.
  document.getElementById("date-slider").addEventListener("pointerdown", () => {
    overlayDragging = true;
  });
  window.addEventListener("pointerup", () => {
    if (!overlayDragging) return;
    overlayDragging = false;
    if (overlayCommit?.template) scheduleOverlayCommit(overlayCommit.template);
  });
  window.addEventListener("pointercancel", () => {
    overlayDragging = false;
  });
  document.getElementById("date-end").addEventListener("input", (event) => {
    const dates = S.visibleDates(state);
    const entry = dates[Number(event.target.value)];
    if (entry) applyDate(entry.date);
  });
  document.getElementById("date-start").addEventListener("input", (event) => {
    // the input's dynamic max keeps this strictly before the end thumb
    const dates = S.visibleDates(state);
    const entry = dates[Number(event.target.value)];
    if (entry) {
      state = S.setBaseline(state, entry.date);
      renderAll();
    }
  });
  document.getElementById("hide-cloudy").addEventListener("change", (event) => {
    state = S.setHideCloudy(
      state,
      event.target.checked,
      Number(document.getElementById("max-cloud").value)
    );
    renderAll();
  });
  document.getElementById("max-cloud").addEventListener("change", (event) => {
    state = S.setHideCloudy(state, state.hideCloudy, Number(event.target.value));
    renderAll();
  });
  for (const group of ["facilities", "inspection"]) {
    document.getElementById(`poi-${group}`).addEventListener("change", () => {
      state = S.togglePoiGroup(state, group);
      renderAll();
    });
  }
  document.getElementById("show-properties").addEventListener("change", (event) => {
    state = S.setShowProperties(state, event.target.checked);
    renderAll();
  });
  document.getElementById("basemap-select").addEventListener("change", (event) => {
    state = S.setBasemap(state, event.target.value);
    updateBasemap();
    pushUrl();
  });
  document.getElementById("share-btn").addEventListener("click", async () => {
    pushUrl();
    await navigator.clipboard?.writeText(location.href);
  });
  document.getElementById("png-btn").addEventListener("click", () => {
    const link = document.createElement("a");
    link.download = `${state.slug}-${state.layer}-${state.date}.png`;
    link.href = map.getCanvas().toDataURL("image/png");
    link.click();
  });
  document.getElementById("status-link").href = `/p/${state.slug}/status/page`;
  document.getElementById("lang-select").addEventListener("change", (event) => {
    state = S.setLang(state, event.target.value);
    localStorage.setItem(LANG_KEY, state.lang);
    applyI18n(); // static texts, tooltips, <html lang>, document title
    renderAll(); // dynamic texts: layer pills, cloud badge, works marker
    pushUrl(); // shared URLs carry lang=en (default es stays implicit)
  });
  document.getElementById("about-btn").addEventListener("click", () => {
    document.getElementById("about-dialog").showModal();
  });
}

function applyI18n() {
  const locale = state?.lang ?? "es";
  document.documentElement.lang = locale;
  document.querySelectorAll("[data-i18n]").forEach((element) => {
    element.textContent = S.t(locale, element.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-title]").forEach((element) => {
    element.title = S.t(locale, element.dataset.i18nTitle);
  });
  document.querySelectorAll("[data-i18n-label]").forEach((element) => {
    element.setAttribute("aria-label", S.t(locale, element.dataset.i18nLabel));
  });
  document.getElementById("lang-select").value = locale;
  document.getElementById("app-title").textContent = S.t(locale, "app");
  document.getElementById("about-disclaimer").textContent = S.t(
    locale,
    "disclaimer"
  );
  // POI pins carry localized names too — re-translate on every switch
  poiMarkers?.updateLabels(poiLabelFor);
  document.title = config
    ? `${S.t(locale, "app")} — ${config.name}`
    : S.t(locale, "app");
}

async function boot() {
  if (!slug) {
    document.body.textContent = "Missing project slug (/p/{slug}/)";
    return;
  }
  config = await getJSON(`/p/${slug}/config`);
  state = S.makeState(config);

  // language: explicit URL param > stored preference > Spanish
  const urlLang = new URLSearchParams(location.search).get("lang");
  const storedLang = localStorage.getItem(LANG_KEY);
  state = S.setLang(
    state,
    ["es", "en"].includes(urlLang)
      ? urlLang
      : ["es", "en"].includes(storedLang)
        ? storedLang
        : "es"
  );

  const restored = S.parseState(location.search.slice(1));
  // remembered display settings first (basemap, checkboxes, ...) — an
  // explicit URL below overwrites them, so shared links stay faithful
  state = S.restoreSettings(state, loadSettings());
  if (restored) {
    if (restored.layer && config.layers.includes(restored.layer)) {
      state.layer = restored.layer;
    } else if (restored.layer === "off") {
      state.layer = null; // shared URL with the layer toggled off
      state.datesLoading = false; // nothing will be fetched
    }
    state.hideCloudy = restored.hideCloudy;
    state.maxCloud = restored.maxCloud;
    state.baseline = restored.baseline;
    state.showProperties = restored.properties;
    state = S.setBasemap(state, restored.basemap);
    const groups = restored.pois.split(",");
    state.poiGroups = {
      facilities: groups.includes("facilities"),
      inspection: groups.includes("inspection"),
    };
  }

  document.getElementById("project-name").textContent = config.name;
  applyI18n();
  wireUi();

  if (state.layer) await fetchDates(state.layer);
  if (restored?.date && state.dates.some((d) => d.date === restored.date)) {
    state = S.setDate(state, restored.date);
  }
  poisData = await getJSON(`/p/${state.slug}/pois.geojson`);
  aoiData = await getJSON(`/p/${state.slug}/aoi.geojson`);

  try {
    buildMap();
  } catch (error) {
    console.error("map failed to initialize", error);
    document.getElementById("map").textContent =
      `Map unavailable (WebGL): ${error.message}`;
  }
  if (restored?.bbox && restored.zoom != null && map) {
    const [w, s, e, n] = restored.bbox;
    map.fitBounds([[w, s], [e, n]], { zoom: restored.zoom, duration: 0 });
  }
  renderAll();
}

boot().catch((error) => {
  console.error(error);
  document.body.insertAdjacentHTML(
    "beforeend",
    `<pre style="color:#a33">Failed to start: ${error.message}</pre>`
  );
});
