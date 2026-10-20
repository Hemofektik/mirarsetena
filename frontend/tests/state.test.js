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
  disableLayer,
  makeState,
  missionForLayer,
  parseState,
  restoreSettings,
  serializeState,
  settingsFromState,
  setBasemap,
  setDatesLoading,
  setDate,
  disabledDatesFor,
  stepDate,
  stripDates,
  t,
  toggleDisabledDate,
  setHideCloudy,
  setLayer,
  setLang,
  setShowProperties,
  setOverlayOpacity,
  tileUrl,
  URL_KEYS,
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
    expect(missionForLayer("nope")).toBeNull();
  });
});

describe("scrubber date model", () => {
  it("switching layer replaces the date list and opens on the newest date", () => {
    let state = makeState({ slug: "cdp-rio-general", layers: ["ndvi", "sigma0"] });
    state = setLayer(state, "ndvi", DATES_S2);
    expect(state.date).toBe("2026-07-10"); // opens on the newest acquisition
    // remembered date stays while the new list still has it
    state = setLayer(state, "ndvi", DATES_S2);
    expect(state.date).toBe("2026-07-10");
    // remembered date the new list does not have -> newest of the new list
    state = setLayer(state, "ndvi", [{ date: "2026-07-20", cloud: 1.0 }]);
    expect(state.date).toBe("2026-07-20");
  });

  it("opens on the newest date the slider actually shows (hide-cloudy)", () => {
    let state = makeState({ slug: "p", layers: ["ndvi"] });
    state = setHideCloudy(state, true, 5); // hides 2026-07-08 (45%) and 2026-07-10 (10%)
    state = setLayer(state, "ndvi", DATES_S2);
    expect(state.date).toBe("2026-07-03"); // newest visible, not a hidden one
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
  it("tileUrl carries layer and date", () => {
    const config = { slug: "cdp-rio-general", layers: ["ndvi"] };
    const state = setLayer(makeState(config), "ndvi", DATES_S2);
    expect(tileUrl(state, 0, 0, 0)).toBe(
      "/p/cdp-rio-general/tiles/ndvi/2026-07-10/0/0/0.png"
    );
  });

  it("serialize -> parse round-trips the whole state", () => {
    let state = setLayer(
      makeState({ slug: "cdp-rio-general", layers: ["ndvi"] }),
      "ndvi",
      DATES_S2,
    );

    state = setHideCloudy(state, true, 15);
    state = togglePoiGroup(state, "inspection");
    state = setBasemap(state, "esri");

    const restored = parseState(serializeState(state));
    expect(restored).toEqual({
      layer: "ndvi",
      date: "2026-07-10",
      hideCloudy: true,
      maxCloud: 15,
      opacity: 0.85,
      properties: true,
      rioLine: true,
      show3d: false,
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
    // the disabled-layer token specifically (other params may contain
    // the substring, e.g. official=1)
    expect(serializeState(state)).not.toContain("layer=off");
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
  const LAYERS = ["rgb", "ndvi", "mndwi", "bsi"];


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
    // settings payload never carries transient view state (dates, viewport)
    expect(Object.keys(saved).sort()).toEqual(
      ["basemap", "disabledDates", "hideCloudy", "maxCloud", "overlayOpacity", "poiGroups", "rioLine", "show3d", "showProperties"],
    );
  });
});

describe("showCloudControls (cloud UI only where cloud data exists)", () => {
  it("shows the cloud filter for optical dates and hides it for radar", async () => {
    const { showCloudControls } = await import("../src/state.js");
    const config = {
      slug: "x",
      layers: ["rgb", "insar"],
      timeline: { start: "2026-01-01", works_start: "2026-06-01" },
    };
    // no dates loaded: nothing to filter — controls stay hidden
    expect(showCloudControls(makeState(config))).toBe(false);
    // optical dates carry cloud scores -> filter relevant
    expect(
      showCloudControls(setLayer(makeState(config), "rgb", DATES_S2)),
    ).toBe(true);
    // radar/InSAR dates never do -> controls hide (user request 2026-10-13)
    expect(
      showCloudControls(setLayer(makeState(config), "insar", DATES_S1)),
    ).toBe(false);
  });
});

describe("disabled dates (double-click a cloud-strip tick)", () => {
  const config = {
    slug: "x",
    layers: ["rgb", "insar"],
    timeline: { start: "2026-01-01", works_start: "2026-06-01" },
  };

  it("toggling disables/enables a date for the CURRENT layer only", () => {
    let state = setLayer(makeState(config), "rgb", DATES_S2);
    state = toggleDisabledDate(state, "2026-07-08");
    expect(disabledDatesFor(state)).toEqual(["2026-07-08"]);
    // the same date on another layer stays enabled (cloud scores are optical)
    const radar = setLayer(state, "insar", DATES_S1);
    expect(disabledDatesFor(radar)).toEqual([]);
    // toggling again re-enables
    expect(
      disabledDatesFor(toggleDisabledDate(state, "2026-07-08")),
    ).toEqual([]);
  });

  it("the scrubber skips disabled dates but the strip keeps showing them", () => {
    const state = toggleDisabledDate(
      setLayer(makeState(config), "rgb", DATES_S2),
      "2026-07-08",
    );
    // slider list: the disabled date is skipped
    expect(visibleDates(state).map((d) => d.date)).toEqual([
      "2026-07-03",
      "2026-07-10",
    ]);
    // strip list: still present (double-click must be able to re-enable)
    expect(stripDates(state).map((d) => d.date)).toEqual([
      "2026-07-03",
      "2026-07-08",
      "2026-07-10",
    ]);
  });

  it("disabling the selected date moves the selection off it", () => {
    const base = setLayer(makeState(config), "rgb", DATES_S2);
    const selected = setDate(base, "2026-07-08");
    const next = toggleDisabledDate(selected, "2026-07-08");
    expect(next.date).toBe("2026-07-10"); // newest remaining
    // disabling the LAST remaining date leaves no selection, honestly
    const only = toggleDisabledDate(
      setDate(
        toggleDisabledDate(selected, "2026-07-08"),
        "2026-07-10",
      ),
      "2026-07-10",
    );
    const last = toggleDisabledDate(only, "2026-07-03");
    expect(visibleDates(last)).toEqual([]);
    expect(last.date).toBeNull();
  });

  it("disabled dates survive the settings round-trip (and reject garbage)", () => {
    const state = toggleDisabledDate(
      setLayer(makeState(config), "rgb", DATES_S2),
      "2026-07-08",
    );
    const saved = settingsFromState(state);
    const restored = restoreSettings(
      setLayer(makeState(config), "rgb", DATES_S2),
      JSON.parse(JSON.stringify(saved)),
    );
    expect(disabledDatesFor(restored)).toEqual(["2026-07-08"]);
    // type-strict merge: a bad shape is ignored, not trusted
    const dirty = restoreSettings(makeState(config), {
      disabledDates: { rgb: "2026-07-08" },
    });
    expect(dirty.disabledDates).toEqual({});
  });
});

describe("disabled dates are shared per data source (mission)", () => {
  const config = {
    slug: "x",
    layers: ["rgb", "ndvi", "insar"],
    timeline: { start: "2026-01-01", works_start: "2026-06-01" },
  };

  it("all Sentinel-2 layers share one tick state; insar has its own", () => {
    let state = toggleDisabledDate(
      setLayer(makeState(config), "rgb", DATES_S2),
      "2026-07-08",
    );
    // switching to another S2 layer keeps the disabled date excluded
    state = setLayer(state, "ndvi", DATES_S2);
    expect(disabledDatesFor(state)).toEqual(["2026-07-08"]);
    expect(visibleDates(state).map((d) => d.date)).toEqual([
      "2026-07-03",
      "2026-07-10",
    ]);
    // toggling from the other S2 layer removes it for ALL of them
    state = toggleDisabledDate(state, "2026-07-08");
    expect(disabledDatesFor(setLayer(state, "rgb", DATES_S2))).toEqual([]);
    // the radar mission never saw it
    expect(disabledDatesFor(setLayer(state, "insar", DATES_S1))).toEqual([]);
  });

  it("legacy layer-keyed settings migrate into the mission bucket", () => {
    // the feature first shipped keyed per LAYER (rgb: [...]); those stored
    // sets must keep working, folded into their mission and UNIONED with
    // anything already there
    const saved = {
      disabledDates: { rgb: ["2026-07-08"], ndvi: ["2026-07-10"] },
    };
    let state = restoreSettings(makeState(config), saved);
    expect(disabledDatesFor(state)).toEqual(
      expect.arrayContaining(["2026-07-08", "2026-07-10"]),
    );
    // union, not replace: an existing mission entry survives too
    state = restoreSettings(
      { ...makeState(config), disabledDates: { "sentinel-2-l2a": ["2026-07-03"] } },
      saved,
    );
    expect(disabledDatesFor(state).sort()).toEqual([
      "2026-07-03",
      "2026-07-08",
      "2026-07-10",
    ]);
  });
});

describe("stepDate (arrow-key stepping over the scrubber dates)", () => {
  const config = {
    slug: "x",
    layers: ["rgb", "insar"],
    timeline: { start: "2026-01-01", works_start: "2026-06-01" },
  };

  it("steps prev/next and clamps at both ends", () => {
    const base = setLayer(makeState(config), "rgb", DATES_S2);
    const mid = setDate(base, "2026-07-08");
    expect(stepDate(mid, 1)).toBe("2026-07-10");
    expect(stepDate(setDate(base, "2026-07-10"), 1)).toBe("2026-07-10"); // end
    expect(stepDate(mid, -1)).toBe("2026-07-03");
    expect(stepDate(setDate(base, "2026-07-03"), -1)).toBe("2026-07-03"); // start
  });

  it("skips disabled dates like the slider does", () => {
    let state = toggleDisabledDate(
      setDate(setLayer(makeState(config), "rgb", DATES_S2), "2026-07-10"),
      "2026-07-08",
    );
    expect(stepDate(state, -1)).toBe("2026-07-03"); // 07-08 is disabled
    expect(
      stepDate(setDate(state, "2026-07-03"), 1),
    ).toBe("2026-07-10");
  });

  it("without a selection: right opens the first, left the last date", () => {
    const base = setLayer(makeState(config), "rgb", DATES_S2);
    expect(stepDate(setDate(base, null), 1)).toBe("2026-07-03");
    expect(stepDate(setDate(base, null), -1)).toBe("2026-07-10");
    // nothing loaded -> nothing to step to
    expect(stepDate(makeState(config), 1)).toBeNull();
  });
});

describe("overlay opacity (map layer transparency)", () => {
  const config = {
    slug: "x",
    layers: ["rgb"],
    timeline: { start: "2026-01-01", works_start: "2026-06-01" },
  };

  it("defaults to the current painted opacity and clamps input", () => {
    const state = makeState(config);
    expect(state.overlayOpacity).toBeCloseTo(0.85, 5);
    expect(setOverlayOpacity(state, 0.4).overlayOpacity).toBeCloseTo(0.4, 5);
    expect(setOverlayOpacity(state, -3).overlayOpacity).toBe(0);
    expect(setOverlayOpacity(state, 7).overlayOpacity).toBe(1);
    expect(setOverlayOpacity(state, NaN).overlayOpacity).toBeCloseTo(0.85, 5);
  });

  it("travels through the share URL and the stored settings", () => {
    const state = setOverlayOpacity(makeState(config), 0.4);
    const parsed = parseState(serializeState(state));
    expect(parsed.opacity).toBeCloseTo(0.4, 5);
    // old URLs without the param keep the default (no NaN, no null state)
    expect(parseState("layer=rgb&date=2026-01-01").opacity).toBeCloseTo(0.85, 5);
    const restored = restoreSettings(makeState(config), settingsFromState(state));
    expect(restored.overlayOpacity).toBeCloseTo(0.4, 5);
    // type-strict: garbage falls back to the default
    const dirty = restoreSettings(makeState(config), { overlayOpacity: "loud" });
    expect(dirty.overlayOpacity).toBeCloseTo(0.85, 5);
  });
});
