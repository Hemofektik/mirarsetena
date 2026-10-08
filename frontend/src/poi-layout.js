/**
 * Pure geometry for POI markers: every POI keeps a dot at its true position
 * and gets a label placed in the first free anchor around that dot; labels
 * that still collide are relaxed apart. No DOM — the caller measures label
 * sizes and paints the results (position + leader line).
 */

export const DOT_R = 6; // dot radius incl. ring
const GAP = 7; // min space between dot edge and label box
const MARGIN = 6; // keep labels inside the viewport by this margin
const PAD = 2; // extra padding between two label boxes
const RELAX_ITERS = 80;
const STEP = 3; // relaxation push per iteration (px)

const overlaps = (a, b, pad = 0) =>
  a.x < b.x + b.w + pad &&
  b.x < a.x + a.w + pad &&
  a.y < b.y + b.h + pad &&
  b.y < a.y + a.h + pad;

const overlapArea = (a, b) => {
  const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  return w > 0 && h > 0 ? w * h : 0;
};

const inside = (box, viewport) =>
  box.x >= MARGIN &&
  box.y >= MARGIN &&
  box.x + box.w <= viewport.width - MARGIN &&
  box.y + box.h <= viewport.height - MARGIN;

/** Candidate label boxes (first-fit order: horizontal before vertical). */
function anchors(dot, w, h) {
  const { x, y } = dot;
  const right = x + DOT_R + GAP;
  const left = x - DOT_R - GAP - w;
  const top = y - DOT_R - GAP - h;
  const bottom = y + DOT_R + GAP;
  const midY = y - h / 2;
  const midX = x - w / 2;
  return [
    { x: right, y: midY }, // E
    { x: left, y: midY }, // W
    { x: midX, y: bottom }, // S
    { x: midX, y: top }, // N
    { x: right, y: top }, // NE
    { x: left, y: top }, // NW
    { x: right, y: bottom }, // SE
    { x: left, y: bottom }, // SW
  ];
}

/**
 * Place every label without overlap, all inside the viewport.
 *
 * items: [{ id, x, y, w, h }] — dot centre and measured label size.
 * viewport: { width, height }.
 * Returns [{ id, x, y, leader }] where x/y is the label box's top-left and
 * leader is the line from the dot to the box (null when it sits adjacent).
 */
export function layoutPois(items, viewport) {
  if (!items.length) return [];

  // most-constrained first: points with a close neighbour claim anchors
  // before isolated ones (whose placement rarely conflicts anyway)
  const nearest = (a) =>
    Math.min(
      ...items
        .filter((b) => b.id !== a.id)
        .map((b) => Math.hypot(b.x - a.x, b.y - a.y))
    );
  const order = [...items].sort(
    (a, b) => (items.length > 1 ? nearest(a) - nearest(b) : 0) || a.id.localeCompare(b.id)
  );

  const dots = items.map((it) => ({
    id: it.id,
    x: it.x - DOT_R,
    y: it.y - DOT_R,
    w: DOT_R * 2,
    h: DOT_R * 2,
  }));

  const placed = new Map(); // id -> box
  for (const item of order) {
    const size = { w: item.w, h: item.h };
    const dotObstacle = dots.find((d) => d.id === item.id);
    const others = () => [
      ...[...placed.values()],
      ...dots.filter((d) => d.id !== item.id),
    ];
    let box = null;
    for (const a of anchors(item, size.w, size.h)) {
      const cand = { ...a, w: size.w, h: size.h, id: item.id };
      if (!inside(cand, viewport)) continue;
      if (others().some((o) => overlaps(cand, o, PAD))) continue;
      box = cand;
      break;
    }
    if (!box) {
      // no anchor is free: pick the least-blocking one, relaxation will
      // push it out (dotObstacle keeps the intent: never sit on the dot)
      void dotObstacle;
      const cands = anchors(item, size.w, size.h).map((a) => ({
        ...a,
        w: size.w,
        h: size.h,
        id: item.id,
      }));
      box = cands.reduce((best, cand) => {
        const cost = others().reduce((sum, o) => sum + overlapArea(cand, o), 0);
        const bestCost = others().reduce(
          (sum, o) => sum + overlapArea(best, o),
          0
        );
        return cost < bestCost ? cand : best;
      });
    }
    placed.set(item.id, box);
  }

  // relaxation: separate any remaining overlaps along the smaller axis,
  // keeping every box inside the viewport
  const boxes = [...placed.values()];
  for (let iter = 0; iter < RELAX_ITERS; iter += 1) {
    let moved = false;
    for (let i = 0; i < boxes.length; i += 1) {
      for (let j = i + 1; j < boxes.length; j += 1) {
        const a = boxes[i];
        const b = boxes[j];
        if (!overlaps(a, b, PAD)) continue;
        moved = true;
        const ox = Math.min(a.x + a.w + PAD, b.x + b.w + PAD) - Math.max(a.x, b.x);
        const oy = Math.min(a.y + a.h + PAD, b.y + b.h + PAD) - Math.max(a.y, b.y);
        if (ox < oy) {
          const push = ox / 2 + STEP / 2;
          const dir = a.x + a.w / 2 <= b.x + b.w / 2 ? -1 : 1;
          a.x += dir * push;
          b.x -= dir * push;
        } else {
          const push = oy / 2 + STEP / 2;
          const dir = a.y + a.h / 2 <= b.y + b.h / 2 ? -1 : 1;
          a.y += dir * push;
          b.y -= dir * push;
        }
      }
    }
    for (const box of boxes) {
      box.x = Math.min(Math.max(box.x, MARGIN), viewport.width - MARGIN - box.w);
      box.y = Math.min(Math.max(box.y, MARGIN), viewport.height - MARGIN - box.h);
    }
    if (!moved) break;
  }

  return boxes.map((box) => {
    const item = items.find((it) => it.id === box.id);
    // leader only when the label drifted away from its natural seat:
    // distance from the dot to the nearest point of the label box
    const cx = Math.min(Math.max(item.x, box.x), box.x + box.w);
    const cy = Math.min(Math.max(item.y, box.y), box.y + box.h);
    const gap = Math.hypot(cx - item.x, cy - item.y);
    return {
      id: box.id,
      x: box.x,
      y: box.y,
      leader: gap > DOT_R + GAP + 6,
    };
  });
}
