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

export const BASEMAPS = ["osm", "esri"];

export function makeState(config) {
  return {
    slug: config.slug,
    layers: [...config.layers],
    layer: config.layers[0],
    lang: "es", // UI locale: es | en (Spanish-first)
    dates: [],
    cloudByDate: {},
    date: null,
    baseline: null, // null -> server default (latest pre-works scene)
    mode: "change", // radar render mode: change | raw
    hideCloudy: false,
    maxCloud: 20,
    showProperties: true, // two 1991 plan parcels drawn as outlines
    poiGroups: { facilities: true, inspection: true },
    basemap: "osm",
    bbox: null,
    zoom: null,
  };
}

export function setLayer(state, layer, dates) {
  const list = dates ?? [];
  const remembered = state.date;
  const stillValid = list.some((entry) => entry.date === remembered);
  return {
    ...state,
    layer,
    dates: list,
    cloudByDate: Object.fromEntries(
      list.map((entry) => [entry.date, entry.cloud])
    ),
    date: stillValid ? remembered : list[0]?.date ?? null,
  };
}

/** Disable the active layer (click the pressed pill): overlay off, scrubber kept. */
export function disableLayer(state) {
  return { ...state, layer: null };
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

export function setMode(state, mode) {
  if (mode !== "change" && mode !== "raw") return state;
  return { ...state, mode };
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

export function setViewport(state, bbox, zoom) {
  return { ...state, bbox: bbox ?? null, zoom: zoom ?? null };
}

export function visibleDates(state) {
  if (!state.hideCloudy) return state.dates;
  return state.dates.filter(
    (entry) => entry.cloud != null && entry.cloud <= state.maxCloud
  );
}

export function tileUrl(state, z, x, y) {
  const params = new URLSearchParams({ mode: state.mode });
  if (state.baseline) params.set("baseline", state.baseline);
  return (
    `/p/${state.slug}/tiles/${state.layer}/${state.date}/${z}/${x}/${y}.png` +
    `?${params.toString()}`
  );
}

const URL_KEYS = ["layer", "date", "baseline", "mode"];

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
  const mode = params.get("mode");
  if (mode && mode !== "change" && mode !== "raw") return null;
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
    mode: mode ?? "change",
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
    baseline: "Línea base",
    mode_change: "Cambio",
    mode_raw: "Crudo",
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
    mode_change_help:
      "Diferencia entre la fecha seleccionada y la línea base: muestra " +
      "qué cambió desde entonces.",
    mode_raw_help:
      "La imagen de la fecha sin comparar (valor absoluto), igual que " +
      "se recibe del satélite.",
    baseline_help:
      "Fecha de comparación; por defecto la última escena anterior al " +
      "inicio de obras.",
  },
  en: {
    app: "Mirar Setena",
    layer: "Layer",
    date: "Date",
    baseline: "Baseline",
    mode_change: "Change",
    mode_raw: "Raw",
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
    mode_change_help:
      "Difference between the selected date and the baseline: shows " +
      "what changed since then.",
    mode_raw_help:
      "The date's image without comparison (absolute value), as " +
      "received from the satellite.",
    baseline_help:
      "Comparison date; by default the last scene before works started.",
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
