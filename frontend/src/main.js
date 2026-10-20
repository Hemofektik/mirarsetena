/* Mirar Setena frontend: MapLibre shell wired to the pure state model. */
// maplibre-gl v6 is ESM-only: namespace import + an explicit worker URL for
// bundlers (import.meta.url doesn't reach the worker file inside Vite's graph).
import * as maplibregl from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import "maplibre-gl/dist/maplibre-gl.css";
import "./styles.css";
import * as S from "./state.js";
import { composePng } from "./png-export.js";
import { installPoiMarkers } from "./poi-markers.js";
import { installTileLoaders, retireTileRequests } from "./tile-loaders.js";

maplibregl.setWorkerUrl(workerUrl);

const LANG_KEY = "mirarsetena.lang";
const slug = location.pathname.split("/").filter(Boolean)[1];
let config;
let state;
let map;
let poisData = null;
let aoiData = null;
let rioData = null;

// maxzoom = the deepest tile the provider actually serves over this AOI
// (measured 2026-10-12: Esri World Imagery returns uniform gray #CCCCCC
// placeholders from z19 up, OSM 400s from z20). MapLibre then OVERZOOMS
// — upscaling the last real tile — instead of requesting gray/404 tiles.
const BASEMAPS = {
  osm: {
    tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
    attribution: "© OpenStreetMap contributors",
    maxzoom: 19,
  },
  esri: {
    tiles: [
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    ],
    attribution: "Esri, Maxar, Earthstar Geographics · Río General © OpenStreetMap contributors",
    maxzoom: 18,
  },
};

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return response.json();
}

function overlayTemplate() {
  return (
    `${location.origin}/p/${state.slug}/tiles/${state.layer}/` +
    `${state.date}/{z}/{x}/{y}.png`
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

function applyOverlayOpacity() {
  if (!map || !map.getLayer("overlay")) return;
  map.setPaintProperty("overlay", "raster-opacity", state.overlayOpacity);
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
  for (const id of ["rio-casing", "rio-outline"]) {
    if (!map.getLayer(id)) continue;
    map.setLayoutProperty(id, "visibility", state.rioLine ? "visible" : "none");
  }
}

/** Drape the map over the elevation DEM (3D view toggle). */
function applyTerrain() {
  if (!map) return;
  if (state.show3d) {
    if (!map.getSource("terrain-dem")) return; // style not ready yet
    map.setTerrain({ source: "terrain-dem", exaggeration: 1.3 });
  } else {
    map.setTerrain(null);
  }
}

/** 3D view = terrain relief + camera tilt (the map's 3D/2D button). */
function apply3dView() {
  applyTerrain();
  if (!map) return;
  map.easeTo({ pitch: state.show3d ? 60 : 0, duration: 400 });
}

function updateBasemap() {
  if (!map || !map.getLayer("base")) return;
  const source = map.getSource("base");
  if (!source) return;
  const basemap = BASEMAPS[state.basemap] ?? BASEMAPS.osm;
  if (!basemap.tiles.every((t, i) => source.tiles?.[i] === t)) {
    // the reload after setTiles() re-extends maxzoom from _options — keep
    // both in sync or the new cap is silently reverted one frame later
    source._options.maxzoom = basemap.maxzoom;
    source.setTiles(basemap.tiles);
  }
  // setTiles() reloads from the boot _options and never touches maxzoom;
  // each provider caps at a different depth (Esri 18, OSM 19), so the
  // overzoom ceiling must follow the active basemap too
  source.maxzoom = basemap.maxzoom;
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
  // No layer selected: there is nothing to scrub — hide the whole Fecha
  // section (title, empty message, controls), not just its innards.
  const section = document.getElementById("date-section");
  section.hidden = !state.layer;
  if (!state.layer) return;

  // Cloud filter UI only where dates carry cloud scores (optical); the
  // radar/InSAR scrubber has none — hide the controls, not just disable them.
  const showCloud = S.showCloudControls(state);
  document.getElementById("hide-cloudy-row").hidden = !showCloud;
  document.getElementById("max-cloud-row").hidden = !showCloud;


  const loading = state.datesLoading;
  const dates = S.visibleDates(state); // slider list: skips disabled dates
  const shown = S.stripDates(state); // strip list: keeps them (re-enable)
  // "empty" only when NOTHING is strippable: a fully-disabled list keeps
  // the crossed-out strip visible so double-clicks can bring dates back
  const empty = !loading && shown.length === 0;
  document.getElementById("date-loading").hidden = !loading;
  document.getElementById("date-empty").hidden = !empty;
  document.getElementById("date-controls").hidden = loading || empty;
  if (loading || empty) return;

  // single thumb on the acquisition date (static bounds; Chrome paints a
  // range thumb at value/max so the position follows only its own value)
  const endInput = document.getElementById("date-end");
  endInput.min = "0";
  endInput.max = String(Math.max(dates.length - 1, 0));
  endInput.value = String(
    Math.max(dates.findIndex((entry) => entry.date === state.date), 0),
  );

  document.getElementById("date-value").textContent = state.date ?? "";

  const cloud = state.cloudByDate?.[state.date];
  document.getElementById("cloud-badge").textContent =
    cloud == null ? "" : `${S.t(state.lang, "cloud")}: ${cloud}%`;

  const strip = document.getElementById("cloud-strip");
  const disabled = new Set(S.disabledDatesFor(state));
  // Rebuild the strip ONLY when its content changes. Stable nodes are what
  // makes a double-click possible at all: the single click re-renders the
  // panel, and if the dots were recreated in between, the browser retargets
  // the dblclick to the common ancestor and the toggle never fires.
  const signature = [
    shown.map((entry) => `${entry.date}:${entry.cloud}`).join(","),
    [...disabled].sort().join(","),
  ].join("|");
  if (strip.dataset.signature !== signature) {
    strip.dataset.signature = signature;
    strip.innerHTML = "";
    for (const entry of shown) {
      const dot = document.createElement("span");
      dot.dataset.cloud = cloudClass(entry.cloud);
      dot.dataset.date = entry.date;
      if (disabled.has(entry.date)) dot.classList.add("disabled");

      dot.addEventListener("click", () => {
        if (disabled.has(entry.date)) return; // only a double-click toggles
        applyDate(entry.date);
      });
      strip.appendChild(dot);
    }
  }
  // Selected tick highlight: toggled on the EXISTING nodes every render —
  // folding it into the signature above would rebuild the strip on every
  // scrub tick, replacing the nodes a double-click needs to land.
  for (const dot of strip.children) {
    dot.classList.toggle("current", dot.dataset.date === state.date);
  }
  document.getElementById("works-marker").textContent =
    `${S.t(state.lang, "works_start")}: ${config.timeline.works_start}`;
}

function renderAll() {
  renderLayerButtons();
  renderScrubber();
  applyOverlayOpacity();
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
  document.getElementById("show-rio-line").checked = state.rioLine;
  // overlay opacity: slider position, % readout, and (like the scrubber)
  // the row only exists while a layer is active
  const opacity = Math.round(state.overlayOpacity * 100);
  document.getElementById("overlay-opacity").value = String(opacity);
  document.getElementById("opacity-value").textContent = `${opacity}%`;
  document.getElementById("opacity-row").hidden = !state.layer;
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

  }
  state = S.setDate(state, date);
  renderAll();
}

/** A small preview tile of a basemap, centred on the project bbox. */
function basemapThumb(key) {
  const [w, s, e, n] = config.bbox;
  const lon = (w + e) / 2;
  const lat = (s + n) / 2;
  const z = 13;
  const scale = 2 ** z;
  const x = Math.floor(((lon + 180) / 360) * scale);
  const y = Math.floor(
    ((1 - Math.asinh(Math.tan((lat * Math.PI) / 180)) / Math.PI) / 2) * scale,
  );
  return key === "esri"
    ? `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/${z}/${y}/${x}`
    : `https://tile.openstreetmap.org/${z}/${x}/${y}.png`;
}

/**
 * Google-Maps-style 3D/2D toggle (bottom-right): 3D engages the terrain
 * relief and tilts the camera; 2D returns to a flat north-up view.
 */
class Toggle3dControl {
  onAdd(mapRef) {
    this.map = mapRef;
    const wrap = document.createElement("div");
    wrap.className = "mapctrl";
    this.btn = document.createElement("button");
    this.btn.id = "btn-3d";
    this.btn.type = "button";
    this.btn.className = "map-btn toggle-3d";
    this.btn.addEventListener("click", () => {
      state = S.setShow3d(state, !state.show3d);
      apply3dView();
      renderAll();
      this.refresh();
    });
    wrap.appendChild(this.btn);
    this.refresh();
    return wrap;
  }

  refresh() {
    this.btn.textContent = state.show3d ? "2D" : "3D";
    this.btn.setAttribute("aria-pressed", String(state.show3d));
    this.btn.title = S.t(state.lang, state.show3d ? "btn_2d" : "btn_3d");
  }
}

/**
 * Google-Maps-style basemap picker (bottom-left): a thumbnail of the
 * active basemap; clicking it expands Map / Satellite options with their
 * own preview tiles.
 */
class BasemapPickerControl {
  onAdd(mapRef) {
    this.map = mapRef;
    const wrap = document.createElement("div");
    wrap.className = "mapctrl basemap-picker";
    this.btn = document.createElement("button");
    this.btn.id = "btn-basemap";
    this.btn.type = "button";
    this.btn.className = "map-btn picker-toggle";
    this.btn.addEventListener("click", (event) => {
      event.stopPropagation();
      this.panel.hidden = !this.panel.hidden;
    });
    this.panel = document.createElement("div");
    this.panel.className = "picker-panel";
    this.panel.hidden = true;
    wrap.appendChild(this.btn);
    wrap.appendChild(this.panel);
    this._outside = (event) => {
      if (!wrap.contains(event.target)) this.panel.hidden = true;
    };
    document.addEventListener("click", this._outside);
    this.render();
    return wrap;
  }

  render() {
    this.btn.dataset.basemap = state.basemap;
    this.btn.title = S.t(state.lang, "picker_basemap");
    this.btn.innerHTML =
      `<span class="thumb-wrap">` +
      `<img alt="" src="${basemapThumb(state.basemap)}">` +
      `<span class="thumb-credit">${state.basemap === "esri" ? "Esri" : "OSM"}</span>` +
      `</span>` +
      `<span class="picker-label">${S.t(state.lang, `basemap_${state.basemap}`)}</span>`;
    this.panel.innerHTML = "";
    for (const key of ["osm", "esri"]) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "picker-row" + (key === state.basemap ? " selected" : "");
      row.dataset.basemap = key;
      row.innerHTML =
        `<span class="thumb-wrap">` +
        `<img alt="" src="${basemapThumb(key)}">` +
        `<span class="thumb-credit">${key === "esri" ? "Esri" : "OSM"}</span>` +
        `</span>` +
        `<span>${S.t(state.lang, `basemap_${key}`)}</span>`;
      row.addEventListener("click", () => {
        if (key !== state.basemap) {
          state = S.setBasemap(state, key);
          updateBasemap();
          pushUrl();
        }
        this.panel.hidden = true;
        this.render();
      });
      this.panel.appendChild(row);
    }
  }
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
          // cap at the provider's deepest real tile: past it MapLibre
          // overzooms (upscales) instead of fetching gray placeholders
          maxzoom: basemap.maxzoom,
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
        // Río General between the project start/end POIs: the plan's
        // work-area line (empty collection when the project has none)
        rio: {
          type: "geojson",
          data: rioData ?? { type: "FeatureCollection", features: [] },
        },
        // elevation DEM (AWS elevation-tiles, terrarium encoding, SRTM-
        // based; covers the AOI — centre ~1169 m). Only fetched when the
        // 3D toggle engages setTerrain.
        "terrain-dem": {
          type: "raster-dem",
          tiles: [
            "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png",
          ],
          tileSize: 256,
          maxzoom: 15,
          encoding: "terrarium",
          attribution: "Elevation: AWS Terrain Tiles (SRTM)",
        },
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
          paint: {
            "raster-opacity": state.overlayOpacity,
            "raster-fade-duration": 150,
          },
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
        // the Río General work-area line: white casing + indigo, drawn on
        // top of the parcels so the boundary stays readable on both the
        // OSM and the imagery basemap
        {
          id: "rio-casing",
          type: "line",
          source: "rio",
          layout: {
            "line-join": "round",
            "line-cap": "round",
            visibility: state.rioLine ? "visible" : "none",
          },
          paint: { "line-color": "#ffffff", "line-width": 7, "line-opacity": 0.5 },
        },
        {
          id: "rio-outline",
          type: "line",
          source: "rio",
          layout: {
            "line-join": "round",
            "line-cap": "round",
            visibility: state.rioLine ? "visible" : "none",
          },
          paint: { "line-color": "#4f46e5", "line-width": 3.5, "line-opacity": 0.5 },
        },
      ],
    },
    center: [-83.668, 9.383],
    zoom: 13,
    // v6 moved preserveDrawingBuffer into canvasContextAttributes — as a
    // top-level option it is silently ignored and toDataURL exports a
    // transparent image (the drawing buffer is cleared after each frame)
    canvasContextAttributes: { preserveDrawingBuffer: true },
    // managed explicitly so it can follow basemap switches
    attributionControl: false,
  });
  // terrain can only engage once the style (and its DEM source) exists;
  // calling setTerrain synchronously here throws and aborts buildMap
  map.on("load", apply3dView);
  map.addControl(new maplibregl.NavigationControl(), "top-right");
  // Google-Maps-style controls ON THE MAP: 3D/2D toggle (bottom-right,
  // above the attribution) and the basemap picker (bottom-left)
  map.addControl(new Toggle3dControl(), "bottom-right");
  map.addControl(new BasemapPickerControl(), "bottom-left");
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
  // Delegated: the single click above re-renders the strip, REPLACING the
  // dot nodes between the two clicks — a per-dot dblclick listener would
  // never fire (the browser retargets to the common ancestor).
  document
    .getElementById("cloud-strip")
    .addEventListener("dblclick", (event) => {
      const dot = event.target.closest("span[data-date]");
      if (!dot) return;
      state = S.toggleDisabledDate(state, dot.dataset.date);
      renderAll();
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
  document.getElementById("show-rio-line").addEventListener("change", (event) => {
    state = S.setShowRioLine(state, event.target.checked);
    renderAll();
  });
  document.getElementById("overlay-opacity").addEventListener("input", (event) => {
    state = S.setOverlayOpacity(state, Number(event.target.value) / 100);
    renderAll();
  });
  document.getElementById("share-btn").addEventListener("click", async () => {
    pushUrl();
    await navigator.clipboard?.writeText(location.href);
  });
  document.getElementById("png-btn").addEventListener("click", () => {
    if (!map) return; // WebGL unavailable: there is no canvas to export
    const canvas = map.getCanvas();
    const rect = canvas.getBoundingClientRect();
    // backing-store px per CSS px (devicePixelRatio); the marker plan is
    // in CSS px and must be scaled to the exported bitmap
    const scale = rect.width ? canvas.width / rect.width : 1;
    const plan = poiMarkers?.exportState() ?? {
      leaders: [],
      labels: [],
      dots: [],
    };
    const link = document.createElement("a");
    link.download = `${state.slug}-${state.layer}-${state.date}.png`;
    // map bitmap + POI markers: the raw toDataURL() alone drops the DOM
    // overlay (user 2026-10-14: "the PNG download is missing the POIs")
    link.href = composePng(canvas, plan, scale);
    link.click();
  });
  document.getElementById("status-link").href = `/p/${state.slug}/status/page`;
  // Arrow keys step the scrubber from ANYWHERE (user 2026-10-14). Capture
  // phase: the step must land BEFORE MapLibre's canvas key handler (same
  // key would otherwise pan the map) and before the focused range input's
  // native handling (which would move the thumb a second time).
  window.addEventListener(
    "keydown",
    (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      const target = event.target;
      const tag = target?.tagName;
      if (
        tag === "INPUT" ||
        tag === "SELECT" ||
        tag === "TEXTAREA" ||
        target?.isContentEditable
      ) {
        return; // form fields keep their native arrow behaviour
      }
      if (document.getElementById("about-dialog")?.open) return;
      if (!state.layer) return; // no scrubber: arrows belong to the map
      const date = S.stepDate(state, event.key === "ArrowRight" ? 1 : -1);
      if (date && date !== state.date) applyDate(date);
      // swallow the key while a layer is active — even at the clamped
      // ends, so arrows never half-step the date and half-pan the map
      event.preventDefault();
      event.stopPropagation();
    },
    true,
  );
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
    state.showProperties = restored.properties;
    state.rioLine = restored.rioLine;
    state.show3d = restored.show3d;
    if (restored.opacity != null) {
      state = S.setOverlayOpacity(state, restored.opacity);
    }
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
  if (
    restored?.date &&
    S.visibleDates(state).some((d) => d.date === restored.date)
  ) {
    state = S.setDate(state, restored.date);
  }
  poisData = await getJSON(`/p/${state.slug}/pois.geojson`);
  // the perimeter is the OFFICIAL cadastral geometry when the project
  // fetched it (SNITCR SIRI); the derrotero reconstruction is the
  // fallback for projects without an official fetch (user 2026-10-12:
  // 'replace our own by the official one — no need to have two')
  aoiData = await getJSON(
    config.official_path
      ? `/p/${state.slug}/official.geojson`
      : `/p/${state.slug}/aoi.geojson`,
  );
  if (config.rio_path) {
    // optional: the Río General work-area line (OSM); boot must survive its absence
    try {
      rioData = await getJSON(`/p/${state.slug}/rio.geojson`);
    } catch {
      rioData = null;
    }
  }
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
