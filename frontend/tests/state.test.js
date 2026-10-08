/**
 * I.1-I.6 — the frontend's pure state model (shell, scrubber dates, POIs,
 * baseline, URL sharing, i18n catalog).
 * Everything MapLibre/DOM touches goes through these functions.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  MESSAGES,
  applyDates,
  baselineIndex,
  defaultBaselineIndex,
  disableLayer,
  ensureRadarRange,
  isRadarLayer,
  makeState,
  missionForLayer,
  parseState,
  restoreSettings,
  serializeState,
  settingsFromState,
  setBasemap,
  setDatesLoading,
  setHideCloudy,
  setLayer,
  setLang,
  setShowProperties,
  t,
  tileUrl,
  togglePoiGroup,
  visibleDates,
} from "../src/state.js";

const DATES_S2 = [
  { date: "2026-07-03", cloud: 2.5 },
  { date: "2026-07-08", cloud: 45.0 },
  { date: "2026-07-10", cloud: 10.0 },
];
const DATES_S1 = [
  { date: "2026-07-11", cloud: null },
  { date: "2026-07-23", cloud: null },
];

describe("layer implies mission (SCOPE R4-Q2)", () => {
  it("maps every project layer to its mission", () => {
    expect(missionForLayer("ndvi")).toBe("sentinel-2-l2a");
    expect(missionForLayer("rgb")).toBe("sentinel-2-l2a");
    expect(missionForLayer("mndwi")).toBe("sentinel-2-l2a");
    expect(missionForLayer("bsi")).toBe("sentinel-2-l2a");
    expect(missionForLayer("sigma0")).toBe("sentinel-1-grd");
    expect(missionForLayer("coherence")).toBe("sentinel-1-slc");
    expect(missionForLayer("nope")).toBeNull();
  });
});

describe("scrubber date model", () => {
  it("switching layer replaces the date list and keeps a valid date", () => {
    let state = makeState({ slug: "cdp-rio-general", layers: ["ndvi", "sigma0"] });
    state = setLayer(state, "ndvi", DATES_S2);
    expect(state.date).toBe("2026-07-03"); // optical: earliest first
    state = setLayer(state, "sigma0", DATES_S1);
    expect(state.date).toBe("2026-07-23"); // radar: range END = latest scene
    state = setLayer(state, "ndvi", DATES_S2);
    expect(state.date).toBe("2026-07-03"); // remembered date invalid -> earliest again
  });

  it("loading flag: new layer starts loading, dates arriving clear it", () => {
    let state = makeState({ slug: "p", layers: ["ndvi"] });
    expect(state.datesLoading).toBe(true);
    state = setDatesLoading(state, true);
    expect(state.datesLoading).toBe(true);
    state = setLayer(state, "ndvi", DATES_S2);
    expect(state.datesLoading).toBe(false);
    state = setDatesLoading(state, false);
    expect(state.datesLoading).toBe(false);
    expect(disableLayer(state).datesLoading).toBe(false);
  });

  it("hide-cloudy filters by maxCloud and keeps cloudless dates", () => {
    let state = setLayer(makeState({ slug: "p", layers: ["ndvi"] }), "ndvi", DATES_S2);
    state = setHideCloudy(state, true, 20);
    expect(visibleDates(state).map((d) => d.date)).toEqual([
      "2026-07-03",
      "2026-07-10",
    ]);
    // radar dates have no cloud score — they are not cloudy and must stay
    let radar = setLayer(makeState({ slug: "p", layers: ["sigma0"] }), "sigma0", DATES_S1);
    radar = setHideCloudy(radar, true, 20);
    expect(visibleDates(radar).map((d) => d.date)).toEqual([
      "2026-07-11",
      "2026-07-23",
    ]);
  });

  it("refreshed date payloads replace clouds without touching toggles", () => {
    let state = setLayer(makeState({ slug: "p", layers: ["ndvi"] }), "ndvi", DATES_S2);
    state = setHideCloudy(state, true, 50);
    state = applyDates(state, [
      { date: "2026-07-03", cloud: 0.1 },
      { date: "2026-07-08", cloud: 90.0 },
    ]);
    expect(state.hideCloudy).toBe(true);
    expect(visibleDates(state).map((d) => d.date)).toEqual(["2026-07-03"]);
  });
});

describe("tile URLs and URL state (I.5)", () => {
  it("tileUrl carries layer and date; baseline only when chosen (always change)", () => {
    const config = { slug: "cdp-rio-general", layers: ["sigma0"] };
    let state = setLayer(makeState(config), "sigma0", DATES_S1);
    expect(tileUrl(state, 0, 0, 0)).toBe(
      "/p/cdp-rio-general/tiles/sigma0/2026-07-23/0/0/0.png"
    );
    state = { ...state, baseline: "2026-07-11" };
    expect(tileUrl(state, 0, 0, 0)).toBe(
      "/p/cdp-rio-general/tiles/sigma0/2026-07-23/0/0/0.png?baseline=2026-07-11"
    );
  });

  it("serialize -> parse round-trips the whole state", () => {
    let state = setLayer(
      makeState({ slug: "cdp-rio-general", layers: ["ndvi", "sigma0"] }),
      "ndvi",
      DATES_S2,
    );
    state = { ...state, date: "2026-07-10", baseline: "2026-07-03" };
    state = setHideCloudy(state, true, 15);
    state = togglePoiGroup(state, "inspection");
    state = setBasemap(state, "esri");

    const restored = parseState(serializeState(state));
    expect(restored).toEqual({
      layer: "ndvi",
      date: "2026-07-10",
      baseline: "2026-07-03",
      hideCloudy: true,
      maxCloud: 15,
      properties: true,
      pois: "facilities",
      basemap: "esri",
      lang: "es",
      bbox: state.bbox,
      zoom: state.zoom,
    });
  });

  it("garbage input parses to null", () => {
    expect(parseState("layer=%%%&")).toBeNull();
    expect(parseState("")).toBeNull();
  });
});

describe("POI groups and basemap (I.3)", () => {
  it("toggling one group leaves the other untouched", () => {
    let state = makeState({ slug: "p", layers: ["ndvi"] });
    state = togglePoiGroup(state, "inspection");
    expect(state.poiGroups).toEqual({ facilities: true, inspection: false });
    state = togglePoiGroup(state, "facilities");
    expect(state.poiGroups).toEqual({ facilities: false, inspection: false });
  });

  it("basemap swap keeps overlays and selection", () => {
    let state = setLayer(makeState({ slug: "p", layers: ["ndvi"] }), "ndvi", DATES_S2);
    const before = { ...state };
    const after = setBasemap(state, "esri");
    expect(after.basemap).toBe("esri");
    expect(after.layer).toBe(before.layer);
    expect(after.date).toBe(before.date);
    expect(after.poiGroups).toEqual(before.poiGroups);
  });
});



describe("property perimeter toggle (two 1991 plan parcels)", () => {
  const config = { slug: "cdp-rio-general", layers: ["rgb"] };

  it("defaults to visible and serializes properties=1", () => {
    const state = makeState(config);
    expect(state.showProperties).toBe(true);
    expect(serializeState(state)).toContain("properties=1");
  });

  it("setShowProperties(false) round-trips through the URL", () => {
    const off = setShowProperties(makeState(config), false);
    const query = serializeState(off);
    expect(query).toContain("properties=0");
    expect(parseState(query).properties).toBe(false);
    expect(parseState(serializeState(off)).properties).toBe(false);
  });

  it("an absent properties param still parses as visible (legacy links)", () => {
    expect(parseState("layer=rgb").properties).toBe(true);
  });
});

describe("layer toggle: disable the active layer (user request)", () => {
  const config = { slug: "cdp-rio-general", layers: ["rgb", "sigma0"] };

  it("disableLayer clears the layer but keeps the scrubber dates", () => {
    let state = setLayer(makeState(config), "rgb", DATES_S2);
    state = { ...state, date: "2026-07-08" };
    const off = disableLayer(state);
    expect(off.layer).toBeNull();
    expect(off.dates).toEqual(DATES_S2);
    expect(off.date).toBe("2026-07-08");
  });

  it("serializes the disabled state as layer=off and parses it back", () => {
    let state = setLayer(makeState(config), "rgb", DATES_S2);
    state = disableLayer(state);
    const query = serializeState(state);
    expect(query).toContain("layer=off");
    expect(query).not.toContain("layer=null");
    expect(parseState(query).layer).toBe("off");
  });

  it("an enabled layer still serializes its own name (no off token)", () => {
    const state = setLayer(makeState(config), "rgb", DATES_S2);
    expect(serializeState(state)).toContain("layer=rgb");
    expect(serializeState(state)).not.toContain("off");
  });
});

describe("i18n catalog (I.6)", () => {
  it("ES and EN catalogs have identical key sets", () => {
    const es = Object.keys(MESSAGES.es).sort();
    const en = Object.keys(MESSAGES.en).sort();
    expect(en).toEqual(es);
    expect(es.length).toBeGreaterThan(10);
  });

  it("t() resolves keys in both languages and survives missing keys", () => {
    expect(t("es", "layer")).toBe("Capa");
    expect(t("en", "layer")).toBe("Layer");
    expect(t("es", "does.not.exist")).toBe("does.not.exist");
  });

  it("section headings used in index.html all resolve (no raw keys)", () => {
    // <h2 data-i18n="pois"> rendered as the literal "pois" before this key.
    expect(t("es", "pois")).toBe("Puntos de interés");
    expect(t("en", "pois")).toBe("Points of interest");
  });
});

describe("language selection (ES/EN switch)", () => {
  it("defaults to Spanish and setLang switches only to a known locale", () => {
    const state = makeState({ slug: "cdp-rio-general", layers: ["ndvi"] });
    expect(state.lang).toBe("es");
    expect(setLang(state, "en").lang).toBe("en");
    expect(setLang(state, "kl").lang).toBe("es"); // unknown locale rejected
  });

  it("keeps the default es out of shared URLs but round-trips lang=en", () => {
    const es = makeState({ slug: "cdp-rio-general", layers: ["ndvi"] });
    expect(serializeState(es)).not.toContain("lang=");
    const en = setLang(es, "en");
    const query = serializeState(en);
    expect(query).toContain("lang=en");
    expect(parseState(query).lang).toBe("en");
    expect(parseState("layer=rgb").lang).toBe("es");
  });

  it("rejects an invalid lang param in a shared URL", () => {
    expect(parseState("lang=zz")).toBeNull();
  });
});

describe("layer help tooltips and resolution purpose text", () => {
  const LAYERS = ["rgb", "ndvi", "mndwi", "bsi", "sigma0", "coherence"];

  it("every configured layer has a help tooltip in both languages", () => {
    for (const layer of LAYERS) {
      for (const locale of ["es", "en"]) {
        const key = `layer_${layer}_help`;
        expect(t(locale, key), `${locale}/${key}`).not.toBe(key);
        expect(t(locale, key).length).toBeGreaterThan(20);
      }
    }
  });

  it("mode radio help texts are gone with the radios", () => {
    // raw/change selection was removed: only change makes sense
    for (const locale of ["es", "en"]) {
      expect(MESSAGES[locale].mode_change).toBeUndefined();
      expect(MESSAGES[locale].mode_raw).toBeUndefined();
      expect(MESSAGES[locale].mode_change_help).toBeUndefined();
      expect(MESSAGES[locale].baseline_help).toBeUndefined();
    }
  });

  it("the RES-1333-2017 purpose text resolves in both languages", () => {
    expect(t("es", "purpose_title")).toContain("Propósito");
    expect(t("en", "purpose_title")).toContain("Purpose");
    expect(t("es", "purpose_p1")).toContain("1333-2017");
    expect(t("en", "purpose_p1")).toContain("1333-2017");
    expect(t("es", "purpose_p2")).toContain("11 hectáreas");
    expect(t("en", "purpose_p2")).toContain("11-hectare");
    expect(t("es", "purpose_p3")).toContain("semestrales");
    expect(t("en", "purpose_p3")).toContain("every six months");
  });
});

describe("POI pin labels are localized (RES-1333-2017 names)", () => {
  it("every project POI id resolves in both languages", () => {
    const pois = JSON.parse(
      readFileSync(
        new URL("../../data/projects/cdp-rio-general/pois.geojson", import.meta.url),
        "utf8",
      ),
    );
    expect(pois.features.length).toBeGreaterThan(0);
    for (const feature of pois.features) {
      const key = `poi_${feature.properties.id}`;
      for (const locale of ["es", "en"]) {
        const text = MESSAGES[locale][key];
        expect(text, `${locale}/${key} missing`).toBeTruthy();
        expect(text, `${locale}/${key} is a raw key`).not.toBe(key);
      }
    }
    // official Spanish names from the resolution's coordinate table
    expect(t("es", "poi_breaker")).toBe("Quebrador");
    expect(t("es", "poi_channel")).toBe("Cauce");
    expect(t("es", "poi_storage")).toBe("Acopio");
    expect(t("es", "poi_road-start")).toBe("Camino interno existente inicio");
    expect(t("en", "poi_breaker")).toBe("Breaker");
  });
});

describe("remembered settings (bare-URL loads)", () => {
  it("restores only valid values and ignores garbage", () => {
    let state = makeState({ slug: "cdp-rio-general", layers: ["ndvi"] });
    state = restoreSettings(state, {
      basemap: "esri",
      hideCloudy: true,
      maxCloud: 55,
      showProperties: false,
      poiGroups: { facilities: false, inspection: true },
    });
    expect(state.basemap).toBe("esri");
    expect(state.hideCloudy).toBe(true);
    expect(state.maxCloud).toBe(55);
    expect(state.showProperties).toBe(false);
    expect(state.poiGroups).toEqual({ facilities: false, inspection: true });
    expect(state.mode).toBeUndefined(); // raw/change mode was removed

    // garbage from an old/broken localStorage payload must never corrupt state
    const dirty = restoreSettings(makeState({ slug: "p", layers: ["ndvi"] }), {
      basemap: "satellite",
      hideCloudy: "yes",
      maxCloud: "banana",
      showProperties: 0,
      mode: "weird", // legacy field from an older build: ignored
      poiGroups: { bogus: false, facilities: 1 },
    });
    expect(dirty.basemap).toBe("osm");
    expect(dirty.hideCloudy).toBe(false);
    expect(dirty.maxCloud).toBe(20);
    expect(dirty.showProperties).toBe(true); // 0 is not a boolean -> default kept
    expect(dirty.poiGroups).toEqual({ facilities: true, inspection: true });
  });

  it("settings round-trip through the stored payload shape", () => {
    let state = makeState({ slug: "cdp-rio-general", layers: ["ndvi", "sigma0"] });
    state = setBasemap(state, "esri");
    state = setHideCloudy(state, true, 30);
    state = setShowProperties(state, false);
    state = togglePoiGroup(state, "inspection");
    const saved = settingsFromState(state);
    const restored = restoreSettings(makeState({ slug: "cdp-rio-general", layers: ["ndvi"] }), saved);
    expect(restored.basemap).toBe("esri");
    expect(restored.hideCloudy).toBe(true);
    expect(restored.maxCloud).toBe(30);
    expect(restored.showProperties).toBe(false);
    expect(restored.poiGroups).toEqual({ facilities: true, inspection: false });
    // settings payload never carries transient view state
    expect(Object.keys(saved).sort()).toEqual(
      ["basemap", "hideCloudy", "maxCloud", "poiGroups", "showProperties"],
    );
  });
});

describe("radar range model (start -> end change)", () => {
  const WORKS = "2026-08-01";
  const S1_DATES = [
    { date: "2026-01-05", cloud: null },
    { date: "2026-07-11", cloud: null },
    { date: "2026-07-23", cloud: null },
  ];
  const radarConfig = {
    slug: "cdp-rio-general",
    layers: ["sigma0"],
    timeline: { start: "2026-01-01", works_start: WORKS },
  };

  it("isRadarLayer marks the two Sentinel-1 layers only", () => {
    expect(isRadarLayer("sigma0")).toBe(true);
    expect(isRadarLayer("coherence")).toBe(true);
    expect(isRadarLayer("ndvi")).toBe(false);
    expect(isRadarLayer(null)).toBe(false);
  });

  it("default baseline mirrors the server: latest acquisition before works", () => {
    // all three dates pre-date works -> the latest one wins
    expect(defaultBaselineIndex(S1_DATES, WORKS)).toBe(2);
    // post-works dates exist -> the last pre-works date wins
    const later = [...S1_DATES, { date: "2026-10-01", cloud: null }];
    expect(defaultBaselineIndex(later, WORKS)).toBe(2);
    // no pre-works date -> earliest overall (server fallback)
    expect(defaultBaselineIndex([{ date: "2026-09-01", cloud: null }], WORKS)).toBe(0);
    expect(defaultBaselineIndex([], WORKS)).toBe(0);
  });

  it("baselineIndex prefers an explicit baseline over the default", () => {
    let state = setLayer(makeState(radarConfig), "sigma0", S1_DATES);
    expect(state.date).toBe("2026-07-23"); // range end = latest
    expect(baselineIndex(state, S1_DATES)).toBe(2); // default (== end here)
    state = { ...state, baseline: "2026-07-11" };
    expect(baselineIndex(state, S1_DATES)).toBe(1);
    state = { ...state, baseline: "1999-01-01" }; // not in this list -> default
    expect(baselineIndex(state, S1_DATES)).toBe(2);
  });

  it("ensureRadarRange keeps start strictly before end", () => {
    // default baseline equals the latest end -> materialize start at end-1
    let state = setLayer(makeState(radarConfig), "sigma0", S1_DATES);
    state = ensureRadarRange(state, S1_DATES);
    expect(state.baseline).toBe("2026-07-11");

    // a mid-list end with the default sitting after it -> start moves to end-1
    state = { ...state, baseline: null, date: "2026-07-11" };
    state = ensureRadarRange(state, S1_DATES);
    expect(state.baseline).toBe("2026-01-05");

    // a valid range passes through untouched
    state = { ...state, baseline: "2026-01-05", date: "2026-07-23" };
    const untouched = ensureRadarRange(state, S1_DATES);
    expect(untouched.baseline).toBe("2026-01-05");
    expect(untouched.date).toBe("2026-07-23");

    // optical layers never get a range
    const optical = setLayer(makeState({ slug: "p", layers: ["ndvi"] }), "ndvi", DATES_S2);
    expect(ensureRadarRange(optical, DATES_S2).baseline).toBeNull();
  });

  it("setLayer drops a baseline that is not part of the new date list", () => {
    let state = setLayer(makeState(radarConfig), "sigma0", S1_DATES);
    state = { ...state, baseline: "2026-07-11" };
    const other = [
      { date: "2026-02-01", cloud: null },
      { date: "2026-02-13", cloud: null },
    ];
    state = setLayer(state, "coherence", other);
    expect(state.baseline).toBeNull();
  });
});
