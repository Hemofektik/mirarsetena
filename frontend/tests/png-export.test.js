/**
 * PNG export overlay (L: download includes POIs — user 2026-10-14).
 * Seam: paintPoiPlan(ctx, plan, {scale}) — a recording 2D-context fake
 * pins draw order, device scaling and the marker geometry.
 */
import { describe, expect, it } from "vitest";
import { parseCssColor, paintPoiPlan } from "../src/png-export.js";

function recordingCtx() {
  const ops = [];
  const ctx = {
    ops,
    fillStyle: null,
    strokeStyle: null,
    lineWidth: null,
    font: null,
    textBaseline: null,
    shadowColor: null,
    shadowBlur: null,
    shadowOffsetY: null,
    save: () => ops.push(["save"]),
    restore: () => ops.push(["restore"]),
    beginPath: () => ops.push(["beginPath"]),
    closePath: () => ops.push(["closePath"]),
    moveTo: (x, y) => ops.push(["moveTo", x, y]),
    lineTo: (x, y) => ops.push(["lineTo", x, y]),
    arcTo: () => ops.push(["arcTo"]),
    arc: (x, y, r) => ops.push(["arc", x, y, r]),
    stroke: () => ops.push(["stroke", ctx.strokeStyle, ctx.lineWidth]),
    fill: () => ops.push(["fill", ctx.fillStyle]),
    fillText: (text, x, y) => ops.push(["fillText", text, x, y]),
    measureText: (text) => ({ width: text.length * 6 }),
  };
  return ctx;
}

const PLAN = {
  leaders: [{ x1: 10, y1: 20, x2: 40, y2: 20, stroke: "rgba(20,41,32,0.75)", width: 1.5 }],
  labels: [
    {
      x: 40, y: 14, w: 90, h: 16, text: "Cantera",
      font: "400 11px sans-serif", bg: "rgba(255,255,255,0.94)",
      border: "#143321", color: "#111", radius: 5,
      shadow: "rgba(20,41,29,0.25)",
    },
  ],
  dots: [{ x: 10, y: 20, r: 6, fill: "#143321", ring: "#ffffff", shadow: "rgba(20,41,29,0.5)" }],
};

describe("paintPoiPlan", () => {
  it("draws leaders, then labels, then dots (DOM z-order)", () => {
    const ctx = recordingCtx();
    paintPoiPlan(ctx, PLAN, { scale: 1 });
    const order = ctx.ops.filter(
      (op) => op[0] === "stroke" || op[0] === "fillText" || op[0] === "arc",
    );
    // leader stroke, label border stroke, label text, then the dot arcs
    expect(order[0]).toEqual(["stroke", "rgba(20,41,32,0.75)", 1.5]);
    expect(order[1]).toEqual(["stroke", "#143321", 1]);
    expect(order[2]).toEqual(["fillText", "Cantera", 47, 22]);
    expect(order[3][0]).toBe("arc");
    expect(order[4][0]).toBe("arc");
    // dot: white ring disc r6 then fill disc r4 (2px CSS ring)
    expect(order[3][3]).toBe(6);
    expect(order[4][3]).toBe(4);
  });

  it("scales every coordinate by the device scale", () => {
    const ctx = recordingCtx();
    paintPoiPlan(ctx, PLAN, { scale: 2 });
    const arc = ctx.ops.find((op) => op[0] === "arc");
    expect(arc.slice(1)).toEqual([20, 40, 12]); // dot centre doubled, r 6->12
    const text = ctx.ops.find((op) => op[0] === "fillText");
    expect(text[2]).toBe(94); // (40 + 7) * 2: box x and the 7px text inset
    const leader = ctx.ops.find((op) => op[0] === "lineTo");
    expect(leader.slice(1)).toEqual([80, 40]); // x2 40*2, y2 20*2
  });

  it("paints the label with its plan colors (near-white bg, group border)", () => {
    const ctx = recordingCtx();
    paintPoiPlan(ctx, PLAN, { scale: 1 });
    const fills = ctx.ops.filter((op) => op[0] === "fill");
    expect(fills.map((op) => op[1])).toContain("rgba(255,255,255,0.94)");
    expect(fills.map((op) => op[1])).toContain("#143321");
    const strokes = ctx.ops.filter((op) => op[0] === "stroke");
    expect(strokes[0][1]).toBe("rgba(20,41,32,0.75)"); // leader stroke first
    expect(strokes.some((op) => op[1] === "#143321")).toBe(true); // label border
  });

  it("tolerates an empty plan (no visible markers)", () => {
    const ctx = recordingCtx();
    paintPoiPlan(ctx, {}, { scale: 1 });
    expect(ctx.ops.filter((op) => op[0] === "arc")).toHaveLength(0);
  });

  it("parseCssColor reads rgb()/rgba() for consumers", () => {
    expect(parseCssColor("rgb(20, 51, 33)")).toEqual({ r: 20, g: 51, b: 33, a: 1 });
    expect(parseCssColor("rgba(255,255,255,0.94)").a).toBeCloseTo(0.94);
    expect(parseCssColor("blue")).toBeNull();
  });
});

describe("device-scaled font (export must match on-screen labels)", () => {
  it("multiplies the CSS px font size by the scale", () => {
    const ctx = recordingCtx();
    paintPoiPlan(ctx, PLAN, { scale: 2 });
    // PLAN label font: "400 11px sans-serif" → 22px at dpr 2
    expect(ctx.font).toBe("400 22px sans-serif");
  });
  it("leaves the font untouched at scale 1 and keeps weight/family", () => {
    const ctx = recordingCtx();
    paintPoiPlan(ctx, {
      labels: [{ ...PLAN.labels[0], font: "700 11px serif" }],
    }, { scale: 1 });
    expect(ctx.font).toBe("700 11px serif");
  });
});
