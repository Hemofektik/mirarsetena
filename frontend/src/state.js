/**
 * Pure frontend state model: layers, scrubber dates, POI groups, basemaps,
 * baseline selection, URL sharing and the ES/EN message catalogs.
 * No DOM, no MapLibre — this module is the app's single source of truth.
 */

const MISSION_BY_LAYER = {
  rgb: "sentinel-2-l2a",
  ndvi: "sentinel-2-l2a",
  mndwi: "sentinel-2-l2a",
  bsi: "sentinel-2-l2a",
  sigma0: "sentinel-1-grd",
  coherence: "sentinel-1-slc",
};

export function missionForLayer(layer) {
  return MISSION_BY_LAYER[layer] ?? null;
}

export function isRadarLayer(layer) {
  const mission = missionForLayer(layer);
  return mission === "sentinel-1-grd" || mission === "sentinel-1-slc";
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
    baseline: null, // range start; null -> server default (last pre-works scene)
    worksStart: config.timeline?.works_start ?? null,
    hideCloudy: false,
    maxCloud: 20,
    showProperties: true, // two 1991 plan parcels drawn as outlines
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
  const stillValid = list.some((entry) => entry.date === remembered);
  const radar = isRadarLayer(layer);
  // Radar sliders are a start->end range: when the remembered date does not
  // exist in this mission's list, the range END starts at the latest scene
  // (change against the pre-works default baseline). Optical keeps its
  // oldest-first behaviour.
  const fallback =
    radar && list.length ? list[list.length - 1].date : (list[0]?.date ?? null);
  return {
    ...state,
    layer,
    dates: list,
    datesLoading: false,
    cloudByDate: Object.fromEntries(
      list.map((entry) => [entry.date, entry.cloud])
    ),
    date: stillValid ? remembered : fallback,
    // a baseline from another mission's list would 404 every tile
    baseline:
      state.baseline && list.some((entry) => entry.date === state.baseline)
        ? state.baseline
        : null,
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

export function setBaseline(state, baseline) {
  return { ...state, baseline };
}

/**
 * Baseline = range start of the change comparison. Mirrors the server's
 * pipeline.change.default_baseline_date: latest acquisition before works
 * started, earliest overall as fallback (index within `dates`, ascending).
 */
export function defaultBaselineIndex(dates, worksStart) {
  if (!dates.length) return 0;
  if (!worksStart) return 0;
  let found = 0;
  for (let i = 0; i < dates.length; i += 1) {
    if (dates[i].date < worksStart) found = i;
  }
  return found;
}

/** Which slider position the range start currently sits at. */
export function baselineIndex(state, dates) {
  if (!dates.length) return 0;
  if (state.baseline) {
    const idx = dates.findIndex((entry) => entry.date === state.baseline);
    if (idx >= 0) return idx;
  }
  return defaultBaselineIndex(dates, state.worksStart);
}

/**
 * Keep the radar range strictly start < end: when the resolved baseline is
 * not before the end date (default baseline == latest scene, or the end sits
 * earlier in the list), materialize the start one step before the end.
 */
export function ensureRadarRange(state, dates) {
  if (!state.layer || !isRadarLayer(state.layer) || !dates.length) return state;
  let endIdx = dates.findIndex((entry) => entry.date === state.date);
  if (endIdx < 0) return state; // setLayer() owns date validity
  let startIdx = baselineIndex(state, dates);
  if (startIdx < endIdx) return state;
  if (endIdx === 0) {
    // nothing before the end: move the end to the latest scene first
    const latest = dates[dates.length - 1].date;
    endIdx = dates.length - 1;
    startIdx = baselineIndex({ ...state, date: latest }, dates);
    if (startIdx >= endIdx) {
      return { ...state, date: latest, baseline: dates[endIdx - 1]?.date ?? null };
    }
    return { ...state, date: latest };
  }
  return { ...state, baseline: dates[endIdx - 1].date };
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
  return next;
}

export function setViewport(state, bbox, zoom) {
  return { ...state, bbox: bbox ?? null, zoom: zoom ?? null };
}

export function visibleDates(state) {
  if (!state.hideCloudy) return state.dates;
  // Cloudless dates (Sentinel-1 radar, missing scores) are NOT cloudy —
  // hiding them would empty the whole radar scrubber.
  return state.dates.filter(
    (entry) => entry.cloud == null || entry.cloud <= state.maxCloud
  );
}

export function tileUrl(state, z, x, y) {
  const params = new URLSearchParams();
  if (state.baseline) params.set("baseline", state.baseline);
  const query = params.toString();
  return (
    `/p/${state.slug}/tiles/${state.layer}/${state.date}/${z}/${x}/${y}.png` +
    (query ? `?${query}` : "")
  );
}

const URL_KEYS = ["layer", "date", "baseline"];

export function serializeState(state) {
  const params = new URLSearchParams();
  for (const key of URL_KEYS) {
    if (state[key]) params.set(key, String(state[key]));
  }
  if (!state.layer) params.set("layer", "off"); // disabled layer travels as "off"
  params.set("hideCloudy", state.hideCloudy ? "1" : "0");
  params.set("maxCloud", String(state.maxCloud));
  params.set("properties", state.showProperties ? "1" : "0");
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

  const on = new Set(
    (params.get("pois") ?? "facilities,inspection").split(",").filter(Boolean)
  );
  return {
    layer: params.get("layer"),
    date: params.get("date"),
    baseline: params.get("baseline"),
    hideCloudy: params.get("hideCloudy") === "1",
    maxCloud,
    properties: params.get("properties") !== "0",
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
    basemap: "Mapa base",
    basemap_osm: "OpenStreetMap",
    basemap_esri: "Imagen",
    pois: "Puntos de interés",
    pois_facilities: "Instalaciones",
    pois_inspection: "Puntos de inspección",
    properties: "Propiedades",
    properties_label: "Perímetro de propiedades",
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
    range_start: "Inicio del rango",
    range_end: "Fin del rango",
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
    layer_sigma0_help:
      "Retrodispersión Sigma0 (Sentinel-1, radar): intensidad del eco " +
      "radar. Las diferencias frente a la línea base indican movimiento " +
      "de tierra o obras.",
    layer_coherence_help:
      "Coherencia temporal (Sentinel-1): estabilidad del terreno entre " +
      "la línea base y la fecha. Alta = sin cambios; baja = el terreno " +
      "cambió (excavación, acopio, maquinaria).",
  },
  en: {
    app: "Mirar Setena",
    layer: "Layer",
    date: "Date",
    hide_cloudy: "Hide cloudy dates",
    max_cloud: "Max cloud %",
    basemap: "Basemap",
    basemap_osm: "OpenStreetMap",
    basemap_esri: "Imagery",
    pois: "Points of interest",
    pois_facilities: "Facilities",
    pois_inspection: "Inspection points",
    properties: "Properties",
    properties_label: "Property perimeter",
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
    range_start: "Range start",
    range_end: "Range end",
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
    layer_sigma0_help:
      "Sigma0 backscatter (Sentinel-1, radar): strength of the radar " +
      "echo. Differences against the baseline indicate earthworks or " +
      "construction.",
    layer_coherence_help:
      "Temporal coherence (Sentinel-1): how stable the ground is " +
      "between baseline and date. High = unchanged; low = the ground " +
      "changed (excavation, stockpiles, machinery).",
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
