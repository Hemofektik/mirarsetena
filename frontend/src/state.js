/**
 * Pure frontend state model: layers, scrubber dates, POI groups, basemaps,

 * No DOM, no MapLibre — this module is the app's single source of truth.
 */

const MISSION_BY_LAYER = {
  rgb: "sentinel-2-l2a",
  ndvi: "sentinel-2-l2a",
  mndwi: "sentinel-2-l2a",
  bsi: "sentinel-2-l2a",
  insar: "sentinel-1-slc",
  insar_cum: "sentinel-1-slc",
};

export function missionForLayer(layer) {
  return MISSION_BY_LAYER[layer] ?? null;
}

export const BASEMAPS = ["osm", "esri"];

export function makeState(config) {
  return {
    slug: config.slug,
    layers: [...config.layers],
    layer: config.layers[0],
    lang: "es", // UI locale: es | en (Spanish-first)
    dates: [],
    datesLoading: Boolean(config.layers[0]), // boot fetch starts immediately
    cloudByDate: {},
    date: null,
    worksStart: config.timeline?.works_start ?? null,
    // Satellite overlay transparency (the raster layer's paint opacity);
    // 0.85 is the look the app has shipped with, kept as the default.
    overlayOpacity: 0.85,
    // Dates the user double-clicked out of the scrubber, per layer:
    // { insar: ["2026-09-09"], ... } — skipped by the slider, still shown
    // in the cloud strip so they can be toggled back.
    disabledDates: {},
    hideCloudy: false,
    maxCloud: 20,
    showProperties: true, // two 1991 plan parcels drawn as outlines
    rioLine: true, // Río General start→end work-area line overlay
    showIcesat: true, // ICESat-2 ground-track footprints (open CMR)
    show3d: false, // 3D terrain draped over the elevation DEM (off by default)
    poiGroups: { facilities: true, inspection: true },
    basemap: "osm",
    bbox: null,
    zoom: null,
  };
}

export function setDatesLoading(state, loading) {
  return { ...state, datesLoading: Boolean(loading) };
}

export function setLayer(state, layer, dates) {
  const list = dates ?? [];
  const remembered = state.date;
  // Disabled dates (double-clicked out of the scrubber, per DATA SOURCE)
  // are never picked, not even as the remembered/fallback date.
  const off = state.disabledDates?.[disabledBucket(layer)] ?? [];
  const pool = list.filter((entry) => !off.includes(entry.date));
  const stillValid = pool.some((entry) => entry.date === remembered);
  // The slider opens on the NEWEST date the user can actually pick:
  // newest of the cloud-filtered list, else newest overall (API is ascending).
  const visible = state.hideCloudy
    ? pool.filter((entry) => entry.cloud == null || entry.cloud <= state.maxCloud)
    : pool;
  const fallback = (visible.length ? visible : pool).at(-1)?.date ?? null;
  return {
    ...state,
    layer,
    dates: list,
    datesLoading: false,
    cloudByDate: Object.fromEntries(
      list.map((entry) => [entry.date, entry.cloud])
    ),
    date: stillValid ? remembered : fallback,
  };
}

/** Disable the active layer (click the pressed pill): overlay off, scrubber kept. */
export function disableLayer(state) {
  return { ...state, layer: null, datesLoading: false };
}

export function applyDates(state, dates) {
  return setLayer(state, state.layer, dates);
}

export function setDate(state, date) {
  return { ...state, date };
}

export function setHideCloudy(state, hideCloudy, maxCloud) {
  return {
    ...state,
    hideCloudy: Boolean(hideCloudy),
    maxCloud: maxCloud ?? state.maxCloud,
  };
}

export function togglePoiGroup(state, group) {
  if (!(group in state.poiGroups)) return state;
  return {
    ...state,
    poiGroups: { ...state.poiGroups, [group]: !state.poiGroups[group] },
  };
}

export function setShowProperties(state, on) {
  return { ...state, showProperties: Boolean(on) };
}

export function setShowRioLine(state, on) {
  return { ...state, rioLine: Boolean(on) };
}

export function setShowIcesat(state, on) {
  return { ...state, showIcesat: Boolean(on) };
}

export function setShow3d(state, on) {
  return { ...state, show3d: Boolean(on) };
}

/** Overlay opacity in 0..1; NaN and out-of-range input never corrupt it. */
export function setOverlayOpacity(state, opacity) {
  const value = Number(opacity);
  if (!Number.isFinite(value)) return state;
  return { ...state, overlayOpacity: Math.min(Math.max(value, 0), 1) };
}

export function setBasemap(state, name) {
  if (!BASEMAPS.includes(name)) return state;
  return { ...state, basemap: name };
}

export function setLang(state, lang) {
  if (lang !== "es" && lang !== "en") return state;
  return { ...state, lang };
}

/**
 * The subset of state worth remembering between visits (localStorage):
 * display settings only — never the scrubber position or viewport.
 */
export function settingsFromState(state) {
  return {
    basemap: state.basemap,
    hideCloudy: Boolean(state.hideCloudy),
    maxCloud: state.maxCloud,
    poiGroups: { ...state.poiGroups },
    showProperties: Boolean(state.showProperties),
    rioLine: Boolean(state.rioLine),
    showIcesat: Boolean(state.showIcesat),
    show3d: Boolean(state.show3d),
    overlayOpacity: state.overlayOpacity,
    disabledDates: Object.fromEntries(
      Object.entries(state.disabledDates ?? {}).map(([layer, list]) => [
        layer,
        [...list],
      ])
    ),
  };
}

/**
 * Merge a stored settings payload into state. Type-strict: anything that is
 * not exactly what we wrote (old formats, hand-edited storage, garbage) is
 * ignored so a broken payload can never invert or corrupt the UI.
 */
export function restoreSettings(state, saved) {
  if (!saved || typeof saved !== "object") return state;
  let next = state;
  if (typeof saved.basemap === "string") next = setBasemap(next, saved.basemap);
  if (typeof saved.hideCloudy === "boolean") next = { ...next, hideCloudy: saved.hideCloudy };
  if (typeof saved.showProperties === "boolean") next = { ...next, showProperties: saved.showProperties };
  if (typeof saved.rioLine === "boolean") next = { ...next, rioLine: saved.rioLine };
  if (typeof saved.show3d === "boolean") next = { ...next, show3d: saved.show3d };
  if (typeof saved.showIcesat === "boolean") next = { ...next, showIcesat: saved.showIcesat };
  if (
    typeof saved.overlayOpacity === "number" &&
    Number.isFinite(saved.overlayOpacity)
  ) {
    next = setOverlayOpacity(next, saved.overlayOpacity);
  }
  if (
    typeof saved.maxCloud === "number" &&
    Number.isFinite(saved.maxCloud) &&
    saved.maxCloud >= 0 &&
    saved.maxCloud <= 100
  ) {
    next = { ...next, maxCloud: saved.maxCloud };
  }
  if (saved.poiGroups && typeof saved.poiGroups === "object") {
    const groups = { ...next.poiGroups };
    for (const key of ["facilities", "inspection"]) {
      if (typeof saved.poiGroups[key] === "boolean") groups[key] = saved.poiGroups[key];
    }
    next = { ...next, poiGroups: groups };
  }
  if (
    saved.disabledDates &&
    typeof saved.disabledDates === "object" &&
    !Array.isArray(saved.disabledDates)
  ) {
    // per-mission arrays of date strings; legacy LAYER-keyed entries fold
    // into their mission and UNION with anything already stored
    const map = { ...next.disabledDates };
    for (const [key, list] of Object.entries(saved.disabledDates)) {
      if (!Array.isArray(list) || !list.every((d) => typeof d === "string")) {
        continue;
      }
      const bucket = disabledBucket(key);
      map[bucket] = [...new Set([...(map[bucket] ?? []), ...list])];
    }
    next = { ...next, disabledDates: map };
  }
  return next;
}

export function setViewport(state, bbox, zoom) {
  return { ...state, bbox: bbox ?? null, zoom: zoom ?? null };
}

export function stripDates(state) {
  // What the cloud strip draws: cloud filter only — disabled dates stay
  // VISIBLE here so a double-click can toggle them back on.
  if (!state.hideCloudy) return state.dates;
  // Cloudless dates (Sentinel-1 radar, missing scores) are NOT cloudy —
  // hiding them would empty the whole radar scrubber.
  return state.dates.filter(
    (entry) => entry.cloud == null || entry.cloud <= state.maxCloud
  );
}

/**
 * Disabled dates are keyed per DATA SOURCE (mission): all Sentinel-2
 * layers share one tick state, radar has its own (user 2026-10-14).
 * Legacy layer-name keys still resolve through missionForLayer.
 */
function disabledBucket(layer) {
  return missionForLayer(layer) ?? layer;
}

/** Dates double-clicked out of the scrubber for the ACTIVE layer. */
export function disabledDatesFor(state) {
  return state.disabledDates?.[disabledBucket(state.layer)] ?? [];
}

export function visibleDates(state) {
  // What the SLIDER scrubs: the strip list minus the user-disabled dates.
  const off = disabledDatesFor(state);
  const shown = stripDates(state);
  return off.length
    ? shown.filter((entry) => !off.includes(entry.date))
    : shown;
}

/**
 * The date an arrow-key step should select: prev/next in the SCRUBBER
 * list (disabled dates skipped, ends clamped). Without a selection the
 * right arrow opens the first date, the left one the last; null when
 * there is nothing to step to.
 */
export function stepDate(state, delta) {
  const dates = visibleDates(state);
  if (!dates.length) return null;
  const index = dates.findIndex((entry) => entry.date === state.date);
  const base = index === -1 ? (delta > 0 ? -1 : dates.length) : index;
  const target = Math.min(Math.max(base + delta, 0), dates.length - 1);
  return dates[target].date;
}

/**
 * "2026-09-21 → 2026-10-03": which acquisitions an InSAR product's delta
 * spans (a product is the pair END date; pair_with holds the start
 * date(s)). Null when the pair info is unknown — never invent a span.
 */
export function deltaSpan(entry) {
  if (!entry || !Array.isArray(entry.pair_with) || !entry.pair_with.length) {
    return null;
  }
  return `${entry.pair_with.join(", ")} → ${entry.date}`;
}

/** Double-click toggle: exclude a date from the scrubber (or bring it back). */
export function toggleDisabledDate(state, date) {
  const layer = state.layer;
  if (!layer) return state;
  const bucket = disabledBucket(layer);
  const current = state.disabledDates?.[bucket] ?? [];
  const nextList = current.includes(date)
    ? current.filter((d) => d !== date)
    : [...current, date];
  const next = {
    ...state,
    disabledDates: { ...state.disabledDates, [bucket]: nextList },
  };
  if (next.date !== date) return next;
  // The selected date just left the scrubber: move to the newest remaining
  // date (or no selection when nothing is left — the honest empty state).
  const remaining = visibleDates(next);
  return { ...next, date: remaining.length ? remaining.at(-1).date : null };
}

/**
 * Whether the cloud filter UI (hide-cloudy + max-cloud) applies: only when
 * the loaded dates actually carry cloud scores. Radar/InSAR dates never
 * do, so the controls hide instead of pretending to filter anything.
 */
export function showCloudControls(state) {
  return state.dates.some((entry) => entry.cloud != null);
}

export function tileUrl(state, z, x, y) {
  return `/p/${state.slug}/tiles/${state.layer}/${state.date}/${z}/${x}/${y}.png`;
}



const URL_KEYS = ["layer", "date"];

export function serializeState(state) {
  const params = new URLSearchParams();
  for (const key of URL_KEYS) {
    if (state[key]) params.set(key, String(state[key]));
  }
  if (!state.layer) params.set("layer", "off"); // disabled layer travels as "off"
  params.set("hideCloudy", state.hideCloudy ? "1" : "0");
  params.set("maxCloud", String(state.maxCloud));
  params.set("opacity", String(state.overlayOpacity));
  params.set("properties", state.showProperties ? "1" : "0");
  params.set("rio", state.rioLine ? "1" : "0");
  params.set("icesat", state.showIcesat ? "1" : "0");
  params.set("d3", state.show3d ? "1" : "0");
  const groups = Object.entries(state.poiGroups)
    .filter(([, on]) => on)
    .map(([name]) => name)
    .sort();
  params.set("pois", groups.join(","));
  params.set("basemap", state.basemap);
  if (state.lang && state.lang !== "es") params.set("lang", state.lang);
  if (state.bbox) params.set("bbox", state.bbox.map((n) => n.toFixed(5)).join(","));
  if (state.zoom != null) params.set("zoom", String(state.zoom));
  return params.toString();
}

export function parseState(search) {
  let params;
  try {
    params = new URLSearchParams(search);
  } catch {
    return null;
  }
  if (params.has("layer")) {
    const layer = params.get("layer");
    if (!/^[a-z0-9-]+$/.test(layer)) return null;
  } else if ([...params.keys()].length === 0) {
    return null;
  }
  const lang = params.get("lang");
  if (lang && lang !== "es" && lang !== "en") return null;

  const bboxRaw = params.get("bbox");
  let bbox = null;
  if (bboxRaw) {
    const parts = bboxRaw.split(",").map(Number);
    if (parts.length !== 4 || parts.some((n) => Number.isNaN(n))) return null;
    bbox = parts;
  }
  const zoomRaw = params.get("zoom");
  const zoom = zoomRaw == null ? null : Number(zoomRaw);
  if (zoom != null && Number.isNaN(zoom)) return null;

  const maxCloud = Number(params.get("maxCloud") ?? "20");
  if (Number.isNaN(maxCloud)) return null;
  const opacity = Number(params.get("opacity") ?? "0.85");
  if (Number.isNaN(opacity)) return null;

  const on = new Set(
    (params.get("pois") ?? "facilities,inspection").split(",").filter(Boolean)
  );
  return {
    layer: params.get("layer"),
    date: params.get("date"),
    hideCloudy: params.get("hideCloudy") === "1",
    maxCloud,
    opacity: Math.min(Math.max(opacity, 0), 1),
    properties: params.get("properties") !== "0",
    rioLine: params.get("rio") !== "0",
    showIcesat: params.get("icesat") !== "0",
    show3d: params.get("d3") === "1",
    pois: ["facilities", "inspection"].filter((g) => on.has(g)).join(","),
    basemap: params.get("basemap") ?? "osm",
    lang: lang ?? "es",
    bbox,
    zoom,
  };
}

export const MESSAGES = {
  es: {
    app: "Mirar Setena",
    layer: "Capa",
    date: "Fecha",
    hide_cloudy: "Ocultar fechas nubladas",
    max_cloud: "Máx. nube %",
    basemap_osm: "Mapa",
    picker_basemap: "Cambiar mapa base",
    btn_3d: "Vista 3D con relieve",
    btn_2d: "Vista plana",
    basemap_esri: "Satélite",
    pois: "Puntos de interés",
    pois_facilities: "Instalaciones",
    pois_inspection: "Puntos de inspección",
    properties: "Propiedades",
    properties_label: "Perímetro de propiedades",
    opacity: "Opacidad",
    rio_line: "Línea Río General (inicio–fin)",
    icesat_tracks: "Pasadas ICESat-2",
    works_start: "Inicio de obras",
    about: "Acerca de",
    disclaimer:
      "Herramienta de monitoreo no oficial, sin afiliación con SETENA. " +
      "Datos Copernicus Sentinel © ESA/Copernicus. Resolución " +
      "RES-1333-2017-SETENA (expediente D1-11642-2013-SETENA).",
    share: "Compartir vista",
    download_png: "Descargar PNG",
    no_dates: "Sin fechas disponibles",
    status: "Estado",
    cloud: "Nube",
    loading_dates: "Cargando fechas…",
    // POI pin names — official labels from the RES-1333-2017 coordinate
    // table and the 2016 GPS record (ids from pois.geojson)
    "poi_project-start": "Inicio",
    "poi_project-end": "Final",
    "poi_road-start": "Camino interno existente inicio",
    "poi_road-end": "Camino interno existente final",
    "poi_breaker": "Quebrador",
    "poi_dumper-ramp": "Rampa-quebrador",
    "poi_office": "Oficina",
    "poi_storage": "Acopio",
    "poi_channel": "Cauce",
    "poi_quarry": "Cantera",
    language: "Idioma",
    close: "Cerrar",
    purpose_title: "Propósito de la Resolución",
    purpose_p1:
      "La Resolución Nº 1333-2017-SETENA (7 de julio de 2017) resolvió " +
      "otorgar la VIABILIDAD AMBIENTAL al proyecto CDP Río General, " +
      "tras evaluar el Estudio de Impacto Ambiental (EsIA) presentado " +
      "por la empresa Vientos del Noroeste ABM, S.A. (expediente " +
      "D1-11642-2013-SETENA). El proyecto quedó sujeto a la etapa de " +
      "Gestión Ambiental.",
    purpose_p2:
      "El proyecto consiste en extraer materiales del cauce del río " +
      "General en una zona de 11 hectáreas, depositarlos en la finca " +
      "aledaña de los solicitantes y procesarlos en una planta de " +
      "quebrado para su venta a terceros, en forma húmeda y con agua " +
      "de una concesión. El equipo previsto es un tractor D-8 y una " +
      "excavadora igual o similar a una Cat 345.",
    purpose_p3:
      "La resolución exige como condiciones una garantía ambiental, un " +
      "Responsable Ambiental con Bitácora Ambiental, e informes " +
      "regenciales: uno consolidado de la fase constructiva y " +
      "semestrales durante la extracción y beneficiado, más un informe " +
      "final de cierre. Esta herramienta permite seguir ese avance de " +
      "forma visual comparando imágenes Sentinel.",
    layer_rgb_help:
      "Composición en color natural (rojo, verde, azul): el aspecto del " +
      "terreno tal como lo vería el ojo.",
    layer_ndvi_help:
      "Índice de vegetación NDVI: mide la cantidad y salud de la " +
      "vegetación. Verde intenso = vegetación densa; marrón = suelo " +
      "desnudo o vegetación escasa.",
    layer_mndwi_help:
      "Índice de agua MNDWI: resalta el agua superficial y la humedad. " +
      "Azul = agua (ríos, lagunas de sedimentación); oscuro = seco.",
    layer_bsi_help:
      "Índice de superficie expuesta (BSI): resalta suelo desnudo, roca " +
      "y terreno removido. Útil para ver dónde se expanden el patio de " +
      "acopio y las terrazas de extracción.",
    layer_insar_help:
      "InSAR (Sentinel-1): desplazamiento del terreno a lo largo de la " +
      "línea de visión del satélite, en centímetros. Rojo = movimiento " +
      "alejándose (hundimiento); azul = acercándose. Gris/máscara = baja " +
      "coherencia (zonas muy cambiadas o con vegetación, sin medición fiable).",
    layer_insar_cum_help:
      "InSAR SBAS (Sentinel-1): serie acumulada por inversión de la red " +
      "triangulada (pares de 12 y 24 días) — el ruido atmosférico por " +
      "par se promedia out; los valores son el desplazamiento acumulado " +
      "desde el 2026-07-11, en cm.",
    legend_los_title: "Desplazamiento LOS (cm)",
    legend_los_negative: "−{limit} (hacia el satélite)",
    legend_los_positive: "+{limit} (alejándose / hundimiento)",
    legend_los_mask: "máscara: baja coherencia",
    plan_title: "Plan vs. Realidad",
    plan_poi: "Punto",
    plan_note:
      "Puntos = InSAR medido (cm en la línea de visión; huecos = sin dato). " +
      "Barras = ±1σ entre pares (suelo de ruido). " +
      "Trazo punteado = hitos del plan. El InSAR no mide metros de excavación.",
  },
  en: {
    app: "Mirar Setena",
    layer: "Layer",
    date: "Date",
    hide_cloudy: "Hide cloudy dates",
    max_cloud: "Max cloud %",
    basemap_osm: "Map",
    picker_basemap: "Change basemap",
    btn_3d: "3D terrain view",
    btn_2d: "Flat view",
    basemap_esri: "Satellite",
    pois: "Points of interest",
    pois_facilities: "Facilities",
    pois_inspection: "Inspection points",
    properties: "Properties",
    properties_label: "Property perimeter",
    opacity: "Opacity",
    rio_line: "Río General line (start–end)",
    icesat_tracks: "ICESat-2 passes",
    works_start: "Works start",
    about: "About",
    disclaimer:
      "Unofficial monitoring tool, not affiliated with SETENA. " +
      "Copernicus Sentinel data © ESA/Copernicus. Resolution " +
      "RES-1333-2017-SETENA (file D1-11642-2013-SETENA).",
    share: "Share view",
    download_png: "Download PNG",
    no_dates: "No dates available",
    status: "Status",
    cloud: "Cloud",
    loading_dates: "Loading dates…",
    // POI pin names (ids from pois.geojson; English names as published)
    "poi_project-start": "Project start",
    "poi_project-end": "Project end",
    "poi_road-start": "Internal road start",
    "poi_road-end": "Internal road end",
    "poi_breaker": "Breaker",
    "poi_dumper-ramp": "Dumper ramp",
    "poi_office": "Office",
    "poi_storage": "Storage",
    "poi_channel": "Channel",
    "poi_quarry": "Quarry",
    language: "Language",
    close: "Close",
    purpose_title: "Purpose of the Resolution",
    purpose_p1:
      "Resolution No. 1333-2017-SETENA (July 7, 2017) granted " +
      "ENVIRONMENTAL VIABILITY to the CDP Río General project, after " +
      "evaluating the Environmental Impact Study (EsIA) submitted by " +
      "the company Vientos del Noroeste ABM, S.A. (administrative file " +
      "D1-11642-2013-SETENA). The project remains open to the " +
      "Environmental Management stage.",
    purpose_p2:
      "The project consists of extracting materials from the General " +
      "River channel in an 11-hectare area, depositing them on the " +
      "applicants' neighboring farm, and processing them in a crushing " +
      "plant for sale to third parties, under wet conditions using " +
      "water from a concession. The planned equipment is a D-8 tractor " +
      "and an excavator equal to or similar to a Cat 345.",
    purpose_p3:
      "The resolution requires an environmental guarantee, an " +
      "Environmental Manager with an Environmental Logbook, and " +
      "oversight reports: one consolidated report for the construction " +
      "phase and every six months during extraction and processing, " +
      "plus a final closure report. This tool lets you follow that " +
      "progress visually by comparing Sentinel imagery.",
    layer_rgb_help:
      "Natural colour composition (red, green, blue): how the terrain " +
      "would look to the naked eye.",
    layer_ndvi_help:
      "NDVI vegetation index: measures how much vegetation is present " +
      "and how healthy it is. Bright green = dense vegetation; brown = " +
      "bare ground or sparse plants.",
    layer_mndwi_help:
      "MNDWI water index: highlights surface water and moisture. Blue " +
      "= water (rivers, sedimentation lagoons); dark = dry.",
    layer_bsi_help:
      "Bare Soil Index (BSI): highlights bare soil, rock and disturbed " +
      "ground. Useful for seeing where the storage yard and extraction " +
      "terraces expand.",
    layer_insar_help:
      "InSAR (Sentinel-1): ground displacement along the satellite's " +
      "line of sight, in centimetres. Red = moving away (sinking); blue " +
      "= moving toward the satellite. Grey/masked = low coherence (very " +
      "changed or vegetated areas, no reliable measurement).",
    layer_insar_cum_help:
      "InSAR SBAS (Sentinel-1): cumulative series from the triangulated " +
      "network inversion (12- and 24-day pairs) — per-pair atmospheric " +
      "noise averages out; values are cumulative displacement since " +
      "2026-07-11, in cm.",
    legend_los_title: "LOS displacement (cm)",
    legend_los_negative: "−{limit} (toward satellite)",
    legend_los_positive: "+{limit} (away / sinking)",
    legend_los_mask: "mask: low coherence",
    plan_title: "Plan vs. Actual",
    plan_poi: "Point",
    plan_note:
      "Dots = InSAR measured (cm along the satellite line of sight; gaps = no " +
      "data). Bars = ±1σ across pairs (noise floor). Dashed = plan milestones. " +
      "InSAR does not measure excavation metres.",
  },
};

export function t(locale, key, vars) {
  const table = MESSAGES[locale] ?? MESSAGES.es;
  let text = table[key] ?? key;
  for (const [name, value] of Object.entries(vars ?? {})) {
    text = text.replaceAll(`{${name}}`, String(value));
  }
  return text;
}
