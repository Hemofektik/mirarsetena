/**
 * L.9 — Plan vs. Realidad chart (pure model + SVG builder).
 * Seam: buildPlanChart / chartSvg from src/chart.js — no DOM, no MapLibre.
 * The model must never invent data: gaps stay gaps, the y axis is the
 * chart's own symmetric ±cm domain, plan and measured stay separate marks.
 */
import { describe, expect, test } from "vitest";

import { buildPlanChart, chartSvg, wrapLabel } from "../src/chart.js";

const TIMELINE = { start: "2026-01-01", works_start: "2026-06-01" };

function entry(date, los) {
  return { date, los_range: 0.5, pois: [{ id: "quarry", los_cm: los }] };
}

describe("buildPlanChart", () => {
  test("y domain is symmetric, fits the points, floors at 0.5 cm", () => {
    const big = buildPlanChart({
      timeline: TIMELINE,
      series: [entry("2026-10-03", 0.8), entry("2026-09-09", -0.6)],
      poi: "quarry",
      date: "2026-10-03",
    });
    // the domain covers |los| ± σ so noise-floor whiskers never clip:
    // values {0.8, -0.6} -> σ 0.7 -> max |los|+σ = 1.5
    expect(big.limit).toBeCloseTo(1.5, 10);

    const small = buildPlanChart({
      timeline: TIMELINE,
      series: [entry("2026-10-03", 0.2)],
      poi: "quarry",
      date: "2026-10-03",
    });
    expect(small.limit).toBeCloseTo(0.5, 10);

    // …and measured values stay inside the y domain, drawn inside the plot
    for (const point of big.segments.flat()) {
      expect(Math.abs(point.los)).toBeLessThanOrEqual(big.limit);
      expect(point.y).toBeGreaterThanOrEqual(big.pad.top);
      expect(point.y).toBeLessThanOrEqual(big.height - big.pad.bottom);
    }
  });

  test("null values split the line into segments (gaps stay gaps)", () => {
    const model = buildPlanChart({
      timeline: TIMELINE,
      series: [
        entry("2026-09-09", 0.1),
        entry("2026-09-21", null),
        entry("2026-10-03", 0.2),
      ],
      poi: "quarry",
      date: "2026-10-03",
    });
    expect(model.segments).toHaveLength(2);
    expect(model.segments[0]).toHaveLength(1);
    expect(model.segments[1]).toHaveLength(1);
    // …and the null POI reads as an explicit gap point, not 0
    const gappy = buildPlanChart({
      timeline: TIMELINE,
      series: [entry("2026-09-09", null)],
      poi: "quarry",
      date: "2026-09-09",
    });
    expect(gappy.segments).toHaveLength(0);
  });

  test("works start and milestones map inside the plot area", () => {
    const model = buildPlanChart({
      timeline: TIMELINE,
      milestones: [
        { date: "2026-06-01", es: "Inicio de obras", en: "Works start" },
      ],
      series: [entry("2026-10-03", 0.3)],
      poi: "quarry",
      date: "2026-10-03",
    });
    const left = model.pad.left;
    const right = model.width - model.pad.right;
    expect(model.worksX).toBeGreaterThan(left);
    expect(model.worksX).toBeLessThan(right);
    expect(model.milestones).toHaveLength(1);
    expect(model.milestones[0].x).toBeCloseTo(model.worksX, 6);
    // works shading spans from works start to the right edge
    expect(model.worksX).toBeLessThan(model.width);
  });

  test("cursor follows the selected date and drops unknown dates", () => {
    const base = {
      timeline: TIMELINE,
      series: [entry("2026-09-09", 0.1), entry("2026-10-03", 0.2)],
      poi: "quarry",
    };
    const early = buildPlanChart({ ...base, date: "2026-09-09" });
    const late = buildPlanChart({ ...base, date: "2026-10-03" });
    expect(early.cursor.date).toBe("2026-09-09");
    expect(late.cursor.date).toBe("2026-10-03");
    expect(early.cursor.x).toBeLessThan(late.cursor.x);
    // date outside the domain -> no cursor (nothing to point at)
    const none = buildPlanChart({ ...base, date: null });
    expect(none.cursor).toBeNull();
  });

  test("empty inputs still yield a renderable model", () => {
    const model = buildPlanChart({
      timeline: TIMELINE,
      milestones: [],
      series: [],
      poi: "quarry",
      date: "2026-10-03",
    });
    expect(model.segments).toEqual([]);
    expect(model.milestones).toEqual([]);
    expect(model.limit).toBeCloseTo(0.5, 10);
    // a minimum span keeps the axis from collapsing when nothing is measured
    expect(model.xDomain[1]).toBeGreaterThan(model.xDomain[0]);
  });
});

describe("chartSvg", () => {
  test("renders axes, cursor, points and milestone labels", () => {
    const model = buildPlanChart({
      timeline: TIMELINE,
      milestones: [
        { date: "2026-06-01", es: "Inicio de obras", en: "Works start" },
      ],
      series: [entry("2026-10-03", 0.3)],
      poi: "quarry",
      date: "2026-10-03",
    });
    const svg = chartSvg(model, { lang: "en", yLabel: "LOS (cm)" });
    expect(svg).toContain('data-chart="plan"');
    expect(svg).toContain('data-date="2026-10-03"');
    expect(svg).toContain("Works start"); // milestone label in `en`
    expect(svg).toContain("LOS (cm)");
    expect(svg).toContain("<polyline");
  });

  test("missing date gets no cursor line", () => {
    const model = buildPlanChart({
      timeline: TIMELINE,
      series: [entry("2026-10-03", 0.3)],
      poi: "quarry",
      date: null,
    });
    const svg = chartSvg(model, { lang: "es", yLabel: "cm" });
    expect(svg).not.toContain('data-cursor');
  });
});

describe("label honesty (regression)", () => {
  test("half-range y ticks are not rounded into lying labels", () => {
    // limit 0.5 -> mid ticks at ±0.25; the old fmt rounded to 0.1 and
    // printed "-0.2"/"0.3" at the ±0.25 gridlines
    const model = buildPlanChart({
      timeline: TIMELINE,
      series: [entry("2026-10-03", 0.4)],
      poi: "quarry",
      date: "2026-10-03",
    });
    const svg = chartSvg(model, { lang: "es", yLabel: "cm" });
    expect(svg).toContain(">-0.25<");
    expect(svg).toContain(">0.25<");
    expect(svg).not.toContain(">-0.2<");
    expect(svg).not.toContain(">0.3<");
  });

  test("long milestone labels wrap and anchor to the roomy side", () => {
    const longEs =
      "Inicio de obras · comienza el primer periodo de informe de regencia";
    const model = buildPlanChart({
      timeline: TIMELINE,
      milestones: [
        { date: "2026-06-01", es: longEs, en: "Works start" },
      ],
      series: [entry("2026-10-03", 0.3)],
      poi: "quarry",
      date: "2026-10-03",
    });
    const ms = model.milestones[0];
    // works start sits right of centre -> label grows left (roomy side)
    expect(ms.labelAnchor).toBe("end");
    // …and the available room wraps the long text into >= 2 lines that
    // all fit the half it grows into
    const lines = wrapLabel(longEs, ms.labelMaxChars);
    expect(lines.length).toBeGreaterThanOrEqual(2);
    for (const line of lines) {
      expect(line.length).toBeLessThanOrEqual(ms.labelMaxChars);
      expect(ms.x - line.length * 4.1).toBeGreaterThanOrEqual(
        model.pad.left - 1,
      );
    }
    // the rendered svg carries one line per wrapped piece (no clipped run-on)
    const svg = chartSvg(model, { lang: "es", yLabel: "cm" });
    expect(svg.match(/plan-msline/g) ?? []).toHaveLength(lines.length);
  });

  test("cursor renders under the data (dots stay visible)", () => {
    const model = buildPlanChart({
      timeline: TIMELINE,
      series: [entry("2026-10-03", 0.3)],
      poi: "quarry",
      date: "2026-10-03", // cursor and dot share the same x
    });
    const svg = chartSvg(model, { lang: "es", yLabel: "cm" });
    expect(svg.indexOf('class="plan-cursor"')).toBeLessThan(
      svg.indexOf('class="plan-line"'),
    );
  });
});

describe("noise-floor whiskers (±1σ across pairs, L.7 validation)", () => {
  const series = [
    { date: "2026-08-04", los_range: 0.8, pois: [{ id: "quarry", los_cm: 0.05 }] },
    { date: "2026-08-16", los_range: 0.7, pois: [{ id: "quarry", los_cm: 0.07 }] },
    { date: "2026-08-28", los_range: 0.7, pois: [{ id: "quarry", los_cm: 0.24 }] },
    { date: "2026-09-09", los_range: 0.7, pois: [{ id: "quarry", los_cm: -0.15 }] },
    { date: "2026-10-03", los_range: 0.6, pois: [{ id: "quarry", los_cm: 0.04 }] },
  ];

  test("attaches the population std over all pairs as each point's sigma", () => {
    const model = buildPlanChart({
      timeline: TIMELINE, series, poi: "quarry", date: "2026-10-03",
    });
    const flat = model.segments.flat();
    expect(flat).toHaveLength(5);
    for (const p of flat) {
      expect(p.sigma).toBeGreaterThan(0.1); // measured quarry floor ~0.12
      expect(p.sigma).toBeLessThan(0.2);
    }
    // measured goldens: values [0.05, 0.07, 0.24, -0.15, 0.04] -> std
    expect(flat[0].sigma).toBeCloseTo(0.128, 2);
    expect(flat[2].sigma).toBeCloseTo(0.128, 2);
  });

  test("no sigma for a lone pair (nothing to spread over)", () => {
    const model = buildPlanChart({
      timeline: TIMELINE, series: series.slice(-1), poi: "quarry",
      date: "2026-10-03",
    });
    expect(model.segments[0][0].sigma).toBeNull();
  });

  test("the y domain covers los ± sigma so whiskers never clip", () => {
    const outlier = [...series.slice(0, 4), {
      date: "2026-10-03", los_range: 0.6,
      pois: [{ id: "quarry", los_cm: 0.9 }],
    }];
    const model = buildPlanChart({
      timeline: TIMELINE, series: outlier, poi: "quarry", date: "2026-10-03",
    });
    const flat = model.segments.flat();
    for (const p of flat) {
      expect(Math.abs(p.los) + p.sigma).toBeLessThanOrEqual(model.limit + 1e-9);
    }
  });

  test("renders whiskers (line + caps) for points with sigma, none without", () => {
    const model = buildPlanChart({
      timeline: TIMELINE, series, poi: "quarry", date: "2026-10-03",
    });
    const svg = chartSvg(model, { lang: "es", yLabel: "cm" });
    expect(svg.match(/class="plan-whisker"/g) ?? []).toHaveLength(5); // line per point
    expect(svg.match(/plan-whisker-cap/g) ?? []).toHaveLength(10); // 2 caps each

    const lone = buildPlanChart({
      timeline: TIMELINE, series: series.slice(-1), poi: "quarry",
      date: "2026-10-03",
    });
    const loneSvg = chartSvg(lone, { lang: "es", yLabel: "cm" });
    expect(loneSvg).not.toContain("plan-whisker");
  });
});
