/* Mirar Setena frontend: MapLibre shell wired to the pure state model. */
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import "./styles.css";
import * as S from "./state.js";

const LOCALE = "es";
const slug = location.pathname.split("/").filter(Boolean)[1];
let config;
let state;
let map;
let poisData = null;

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
  const params = new URLSearchParams({ mode: state.mode });
  if (state.baseline) params.set("baseline", state.baseline);
  return (
    `${location.origin}/p/${state.slug}/tiles/${state.layer}/` +
    `${state.date}/{z}/{x}/{y}.png?${params}`
  );
}

function updateOverlay() {
  if (!map || !map.isStyleLoaded()) return;
  const source = map && map.getSource("overlay");
  if (!source) return;
  const enabled = Boolean(state.date && state.layer);
  // setTiles for templates: setUrl() would fetch the template as TileJSON.
  if (enabled) source.setTiles([overlayTemplate()]);
  map.setLayoutProperty("overlay", "visibility", enabled ? "visible" : "none");
}

function updatePoiVisibility() {
  if (!map || !map.isStyleLoaded()) return;
  for (const group of ["facilities", "inspection"]) {
    map.setLayoutProperty(
      `poi-${group}`,
      "visibility",
      state.poiGroups[group] ? "visible" : "none"
    );
  }
}

function updateBasemap() {
  if (!map || !map.isStyleLoaded()) return;
  const source = map && map.getSource("base");
  if (!source) return;
  const basemap = BASEMAPS[state.basemap] ?? BASEMAPS.osm;
  source.setTiles(basemap.tiles);
  map.setAttributionControl({
    customAttribution: basemap.attribution,
  });
}

function pushUrl() {
  const query = S.serializeState(state);
  history.replaceState(null, "", `${location.pathname}?${query}`);
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
    button.textContent = layer;
    button.setAttribute("aria-pressed", String(layer === state.layer));
    button.addEventListener("click", () => {
      if (state.layer === layer) {
        // clicking the pressed pill disables the layer entirely
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
  const dates = S.visibleDates(state);
  const range = document.getElementById("date-range");
  range.max = String(Math.max(dates.length - 1, 0));
  const index = Math.max(
    dates.findIndex((entry) => entry.date === state.date),
    0
  );
  range.value = String(index);

  document.getElementById("date-value").textContent = state.date ?? "—";
  const cloud = state.cloudByDate?.[state.date];
  document.getElementById("cloud-badge").textContent =
    cloud == null ? "" : `${S.t(LOCALE, "cloud")}: ${cloud}%`;

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
    `${S.t(LOCALE, "works_start")}: ${config.timeline.works_start}`;
}

function renderRadarControls() {
  const section = document.getElementById("radar-controls");
  section.hidden = !isRadar(state.layer);
  if (section.hidden) return;
  const select = document.getElementById("baseline-select");
  select.innerHTML = "";
  const defaultOption = document.createElement("option");
  defaultOption.value = "";
  defaultOption.textContent = "—";
  select.appendChild(defaultOption);
  for (const entry of S.visibleDates(state)) {
    const option = document.createElement("option");
    option.value = entry.date;
    option.textContent = entry.date;
    select.appendChild(option);
  }
  select.value = state.baseline ?? "";
}

function renderAll() {
  renderLayerButtons();
  renderScrubber();
  renderRadarControls();
  updateOverlay();
  updatePoiVisibility();
  pushUrl();
}

async function fetchDates(layer) {
  const payload = await getJSON(
    `/p/${state.slug}/api/dates?layer=${encodeURIComponent(layer)}`
  );
  state = S.applyDates({ ...state, layer }, payload.dates);
}

async function selectLayer(layer) {
  await fetchDates(layer);
  renderAll();
}

async function applyDate(date) {
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
          attribution: basemap.attribution,
        },
        overlay: {
          type: "raster",
          // disabled boot (layer=off) has no layer/date for a valid template
          tiles: state.date && state.layer ? [overlayTemplate()] : [],
          tileSize: 256,
        },
        pois: { type: "geojson", data: poisData },
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
        poiLayer("poi-facilities", "facilities"),
        poiLayer("poi-inspection", "inspection"),
      ],
    },
    center: [-83.668, 9.383],
    zoom: 13,
    preserveDrawingBuffer: true,
  });
  map.addControl(new maplibregl.NavigationControl(), "top-right");
  // The inline style parses asynchronously: any renderAll() that ran before
  // "load" was skipped by the isStyleLoaded() guards — re-apply it once ready.
  map.once("load", renderAll);
  map.on("moveend", () => {
    const bounds = map.getBounds();
    state = S.setViewport(state, [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()], map.getZoom());
    pushUrl();
  });
}

function poiLayer(id, group) {
  return {
    id,
    type: "symbol",
    source: "pois",
    filter: ["==", ["get", "group"], group],
    layout: {
      "text-field": ["get", "label"],
      "text-font": ["Noto Sans Regular"],
      "text-size": 11,
      "text-offset": [0, 0.9],
      "text-anchor": "top",
      "text-allow-overlap": false,
    },
    paint: {
      "text-color": group === "facilities" ? "#143321" : "#7a2e12",
      "text-halo-color": "#ffffff",
      "text-halo-width": 1.2,
    },
  };
}

function wireUi() {
  document.getElementById("date-range").addEventListener("input", (event) => {
    const dates = S.visibleDates(state);
    const entry = dates[Number(event.target.value)];
    if (entry) applyDate(entry.date);
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
  document.getElementById("baseline-select").addEventListener("change", (event) => {
    state = S.setBaseline(state, event.target.value || null);
    renderAll();
  });
  for (const radio of document.querySelectorAll('input[name="mode"]')) {
    radio.addEventListener("change", (event) => {
      state = S.setMode(state, event.target.value);
      renderAll();
    });
  }
  for (const group of ["facilities", "inspection"]) {
    document.getElementById(`poi-${group}`).addEventListener("change", () => {
      state = S.togglePoiGroup(state, group);
      renderAll();
    });
  }
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
  document.getElementById("about-btn").addEventListener("click", () => {
    document.getElementById("about-dialog").showModal();
  });
}

function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach((element) => {
    element.textContent = S.t(LOCALE, element.dataset.i18n);
  });
  document.getElementById("app-title").textContent = S.t(LOCALE, "app");
  document.getElementById("about-disclaimer").textContent = S.t(
    LOCALE,
    "disclaimer"
  );
}

async function boot() {
  if (!slug) {
    document.body.textContent = "Missing project slug (/p/{slug}/)";
    return;
  }
  config = await getJSON(`/p/${slug}/config`);
  state = S.makeState(config);

  const restored = S.parseState(location.search.slice(1));
  if (restored) {
    if (restored.layer && config.layers.includes(restored.layer)) {
      state.layer = restored.layer;
    } else if (restored.layer === "off") {
      state.layer = null; // shared URL with the layer toggled off
    }
    state.hideCloudy = restored.hideCloudy;
    state.maxCloud = restored.maxCloud;
    state.baseline = restored.baseline;
    state = S.setMode(state, restored.mode);
    state = S.setBasemap(state, restored.basemap);
    const groups = restored.pois.split(",");
    state.poiGroups = {
      facilities: groups.includes("facilities"),
      inspection: groups.includes("inspection"),
    };
  }

  document.getElementById("project-name").textContent = config.name;
  document.title = `${S.t(LOCALE, "app")} — ${config.name}`;
  applyI18n();
  wireUi();

  if (state.layer) await fetchDates(state.layer);
  if (restored?.date && state.dates.some((d) => d.date === restored.date)) {
    state = S.setDate(state, restored.date);
  }
  poisData = await getJSON(`/p/${state.slug}/pois.geojson`);

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
