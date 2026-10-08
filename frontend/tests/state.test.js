/**
 * I.1-I.6 — the frontend's pure state model (shell, scrubber dates, POIs,
 * baseline, URL sharing, i18n catalog).
 * Everything MapLibre/DOM touches goes through these functions.
 */
import { describe, expect, it } from "vitest";
import {
  MESSAGES,
  applyDates,
  disableLayer,
  makeState,
  missionForLayer,
  parseState,
  serializeState,
  setBasemap,
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
    expect(state.date).toBe("2026-07-03");
    state = setLayer(state, "sigma0", DATES_S1);
    expect(state.date).toBe("2026-07-11");
    state = setLayer(state, "ndvi", DATES_S2);
    expect(state.date).toBe("2026-07-03"); // remembered per layer? falls back to first
  });

  it("hide-cloudy filters by maxCloud and drops cloudless dates", () => {
    let state = setLayer(makeState({ slug: "p", layers: ["ndvi"] }), "ndvi", DATES_S2);
    state = setHideCloudy(state, true, 20);
    expect(visibleDates(state).map((d) => d.date)).toEqual([
      "2026-07-03",
      "2026-07-10",
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
  it("tileUrl encodes layer, date, mode and baseline", () => {
    const config = { slug: "cdp-rio-general", layers: ["sigma0"] };
    let state = setLayer(makeState(config), "sigma0", DATES_S1);
    expect(tileUrl(state, 0, 0, 0)).toBe(
      "/p/cdp-rio-general/tiles/sigma0/2026-07-11/0/0/0.png?mode=change"
    );
  });

  it("serialize -> parse round-trips the whole state", () => {
    let state = setLayer(
      makeState({ slug: "cdp-rio-general", layers: ["ndvi", "sigma0"] }),
      "ndvi",
      DATES_S2,
    );
    state = { ...state, date: "2026-07-10", baseline: "2026-07-03", mode: "raw" };
    state = setHideCloudy(state, true, 15);
    state = togglePoiGroup(state, "inspection");
    state = setBasemap(state, "esri");

    const restored = parseState(serializeState(state));
    expect(restored).toEqual({
      layer: "ndvi",
      date: "2026-07-10",
      baseline: "2026-07-03",
      mode: "raw",
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

  it("mode and baseline controls have help texts in both languages", () => {
    for (const key of ["mode_change_help", "mode_raw_help", "baseline_help"]) {
      for (const locale of ["es", "en"]) {
        expect(t(locale, key), `${locale}/${key}`).not.toBe(key);
      }
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
