/**
 * Pure geometry behind the on-map POI markers: dots stay at their true
 * positions, labels never overlap, everything stays inside the viewport.
 */
import { describe, expect, it } from "vitest";
import { layoutPois } from "../src/poi-layout.js";

const VIEW = { width: 800, height: 600 };

function rectsFor(items, viewport = VIEW) {
  const placed = layoutPois(items, viewport);
  return placed.map((p) => {
    const item = items.find((i) => i.id === p.id);
    return { ...p, w: item.w, h: item.h };
  });
}

function overlapPairs(rects) {
  const hits = [];
  for (let i = 0; i < rects.length; i += 1) {
    for (let j = i + 1; j < rects.length; j += 1) {
      const a = rects[i];
      const b = rects[j];
      const ox = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
      const oy = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
      if (ox > 1 && oy > 1) hits.push([a.id, b.id]);
    }
  }
  return hits;
}

describe("POI label declutter (pure geometry)", () => {
  it("places a tight four-point cluster without overlap, inside the viewport", () => {
    // breaker/dumper-ramp/storage/channel sit within ~15 m of each other
    const items = [
      { id: "breaker", x: 400, y: 300, w: 74, h: 20 },
      { id: "dumper-ramp", x: 412, y: 305, w: 96, h: 20 },
      { id: "storage", x: 405, y: 312, w: 68, h: 20 },
      { id: "channel", x: 395, y: 308, w: 66, h: 20 },
    ];
    const rects = rectsFor(items);
    expect(rects).toHaveLength(4);
    expect(new Set(rects.map((r) => r.id)).size).toBe(4);
    expect(overlapPairs(rects)).toEqual([]);
    for (const r of rects) {
      expect(r.x).toBeGreaterThanOrEqual(0);
      expect(r.y).toBeGreaterThanOrEqual(0);
      expect(r.x + r.w).toBeLessThanOrEqual(VIEW.width);
      expect(r.y + r.h).toBeLessThanOrEqual(VIEW.height);
    }
  });

  it("keeps an isolated point's label right next to its dot", () => {
    const items = [
      { id: "a", x: 200, y: 200, w: 70, h: 20 },
      { id: "b", x: 620, y: 460, w: 70, h: 20 },
    ];
    const a = rectsFor(items).find((r) => r.id === "a");
    // first-fit E anchor: GAP+DOT_R right of the dot, vertically centred
    expect(Math.abs(a.x - (200 + 6 + 7))).toBeLessThanOrEqual(1);
    expect(Math.abs(a.y + 10 - 200)).toBeLessThanOrEqual(1);
    expect(a.leader).toBe(false);
  });

  it("never leaves a label outside the viewport when ten dots collapse together", () => {
    const items = Array.from({ length: 10 }, (_, i) => ({
      id: `p${i}`,
      x: 400,
      y: 300,
      w: 90,
      h: 20,
    }));
    const rects = rectsFor(items);
    expect(rects).toHaveLength(10);
    for (const r of rects) {
      expect(r.x).toBeGreaterThanOrEqual(0);
      expect(r.y).toBeGreaterThanOrEqual(0);
      expect(r.x + r.w).toBeLessThanOrEqual(VIEW.width);
      expect(r.y + r.h).toBeLessThanOrEqual(VIEW.height);
    }
    // degenerate input still yields (near) disjoint boxes
    expect(overlapPairs(rects)).toEqual([]);
  });

  it("never covers any dot with a label", () => {
    const items = [
      { id: "a", x: 400, y: 300, w: 90, h: 20 },
      { id: "b", x: 404, y: 303, w: 90, h: 20 },
      { id: "c", x: 397, y: 306, w: 90, h: 20 },
      { id: "d", x: 410, y: 298, w: 90, h: 20 },
    ];
    const rects = rectsFor(items);
    const DOT_R = 6;
    for (const r of rects) {
      for (const item of items) {
        const covers =
          item.x > r.x && item.x < r.x + r.w && item.y > r.y && item.y < r.y + r.h;
        expect(covers, `label ${r.id} covers dot ${item.id}`).toBe(false);
      }
      // labels keep a visible gap from every dot, incl. their own
      for (const item of items) {
        const dx = Math.max(r.x - item.x, 0, item.x - (r.x + r.w));
        const dy = Math.max(r.y - item.y, 0, item.y - (r.y + r.h));
        expect(Math.hypot(dx, dy), `label ${r.id} touches dot ${item.id}`)
          .toBeGreaterThanOrEqual(DOT_R - 1);
      }
    }
  });

  it("returns nothing for an empty list", () => {
    expect(layoutPois([], VIEW)).toEqual([]);
  });
});
