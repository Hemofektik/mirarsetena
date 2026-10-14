/**
 * Plan vs. Realidad chart (INSAR-PLAN L.9) — pure model + SVG builder.
 *
 * Two series stay on their own terms: plan milestones are vertical event
 * ticks (dates from plan.json, labels never derived), measured points are
 * the per-POI InSAR LOS in cm with NULLED gaps — the line is broken where
 * the product has no coherent measurement, never interpolated. The y axis
 * is this chart's own symmetric ±cm domain (floored at 0.5 cm to match the
 * map ramp), independent of any plan unit.
 */

const DAY = 86_400_000;

export function dayMs(iso) {
  return Date.parse(`${iso}T00:00:00Z`);
}

/** First day of each month after `from`, up to and including `to`. */
function monthTicks(from, to) {
  const ticks = [];
  const start = new Date(from);
  let year = start.getUTCFullYear();
  let month = start.getUTCMonth(); // month 0
  for (;;) {
    const t = Date.UTC(year, month, 1);
    if (t > to) break;
    if (t >= from) ticks.push(t);
    month += 1;
    if (month > 11) {
      month = 0;
      year += 1;
    }
    if (ticks.length > 40) break; // pathological domains stay bounded
  }
  return ticks;
}

/**
 * Build the render model.
 *
 * @param {object} input
 * @param {{start: string, works_start: string}} input.timeline
 * @param {Array<{date: string, es?: string, en?: string, kind?: string}>} [input.milestones]
 * @param {Array<{date: string, pois?: Array<{id: string, los_cm: number|null}>}>} [input.series]
 * @param {string} input.poi POI id to plot
 * @param {string|null} [input.date] selected scrubber date (cursor)
 * @param {number} [input.width]
 * @param {number} [input.height]
 */
export function buildPlanChart({
  timeline,
  milestones = [],
  series = [],
  poi,
  date = null,
  width = 360,
  height = 190,
  pad = { top: 16, right: 10, bottom: 24, left: 36 },
}) {
  const t0 = dayMs(timeline.start);
  const endCandidates = [
    dayMs(timeline.works_start),
    ...milestones.map((m) => dayMs(m.date)),
    ...series.map((s) => dayMs(s.date)),
  ];
  if (date) endCandidates.push(dayMs(date));
  const span = Math.max(...endCandidates.filter((t) => Number.isFinite(t)), t0);
  // minimum span: an unmeasured project still gets a readable axis
  const t1 = Math.max(span, t0 + 90 * DAY);
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const xAt = (t) =>
    pad.left + ((t - t0) / (t1 - t0 || 1)) * innerW;

  // measured values for the selected POI (missing/null = gap, NOT 0)
  const points = series.map((entry) => {
    const hit = (entry.pois ?? []).find((p) => p.id === poi);
    const los = hit?.los_cm ?? null;
    return { date: entry.date, los };
  });
  const maxAbs = points.reduce(
    (m, p) => (p.los == null ? m : Math.max(m, Math.abs(p.los))),
    0,
  );
  // axis fits the data, floored like the map ramp (0.5 cm), 0.1 cm steps
  const limit = Math.max(0.5, Math.ceil(maxAbs * 10) / 10);
  const yAt = (los) => pad.top + ((limit - los) / (2 * limit)) * innerH;

  const segments = [];
  let run = [];
  for (const point of points) {
    if (point.los == null) {
      if (run.length) segments.push(run);
      run = [];
      continue;
    }
    run.push({ ...point, x: xAt(dayMs(point.date)), y: yAt(point.los) });
  }
  if (run.length) segments.push(run);

  const worksT = dayMs(timeline.works_start);
  const worksX = worksT >= t0 && worksT <= t1 ? xAt(worksT) : null;

  const placedMilestones = milestones.map((m) => {
    const t = dayMs(m.date);
    const x = Math.min(Math.max(xAt(t), pad.left), width - pad.right);
    // label grows into the roomy half; ~4.1 px average glyph at 8 px font
    const leftRoom = x - pad.left;
    const rightRoom = width - pad.right - x;
    const labelAnchor = leftRoom >= rightRoom ? "end" : "start";
    const room = labelAnchor === "end" ? leftRoom : rightRoom;
    return {
      ...m,
      t,
      x,
      labelAnchor,
      labelMaxChars: Math.max(12, Math.floor(room / 4.1)),
    };
  });

  let cursor = null;
  if (date) {
    const t = dayMs(date);
    if (t >= t0 && t <= t1) cursor = { date, x: xAt(t) };
  }

  let rawMonths = monthTicks(t0, t1);
  // keep labels readable: thin to every 2nd/3rd month when crowded
  let step = 1;
  while (rawMonths.length / step > 9) step += 1;
  const oneYear =
    rawMonths.length === 0 ||
    new Date(rawMonths[0]).getUTCFullYear() ===
      new Date(rawMonths[rawMonths.length - 1]).getUTCFullYear();
  const xTicks = rawMonths
    .filter((_, i) => i % step === 0)
    .map((t) => {
      const d = new Date(t);
      const y = String(d.getUTCFullYear());
      const m = String(d.getUTCMonth() + 1).padStart(2, "0");
      return { t, x: xAt(t), label: oneYear ? m : `${y}-${m}` };
    });

  const yTicks = [-limit, -limit / 2, 0, limit / 2, limit];

  return {
    width,
    height,
    pad,
    xDomain: [t0, t1],
    limit,
    worksX,
    milestones: placedMilestones,
    segments,
    cursor,
    xTicks,
    yTicks,
  };
}

const fmt = (value) => {
  // up to 2 decimals: quarter-grid ticks (-0.25) must not round into
  // labels that sit at a different gridline ("-0.2")
  const rounded = Math.round(value * 100) / 100;
  return Object.is(rounded, -0) ? "0" : String(rounded);
};

/** Greedy word wrap: lines stay <= maxChars (single long words are split). */
export function wrapLabel(text, maxChars) {
  const words = String(text).split(/\s+/).filter(Boolean);
  const lines = [];
  let line = "";
  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word;
    if (candidate.length <= maxChars) {
      line = candidate;
      continue;
    }
    if (line) lines.push(line);
    if (word.length > maxChars) {
      // pathological single word: hard-split so nothing overflows
      for (let i = 0; i < word.length; i += maxChars) {
        lines.push(word.slice(i, i + maxChars));
      }
      line = "";
    } else {
      line = word;
    }
  }
  if (line) lines.push(line);
  return lines.length ? lines : [""];
}

/**
 * Render the model as an SVG string (inner chart, no chrome).
 * `yLabel` is the caller's i18n text; milestone labels are the plan's own
 * `es`/`en` fields (never machine-translated here).
 */
export function chartSvg(model, { lang = "es", yLabel = "cm" } = {}) {
  const { width, height, pad, limit, yTicks, xTicks } = model;
  const parts = [];

  // works period shading (plan side of the story)
  if (model.worksX != null) {
    parts.push(
      `<rect class="plan-works" x="${model.worksX.toFixed(1)}" y="${pad.top}" ` +
        `width="${(width - pad.right - model.worksX).toFixed(1)}" ` +
        `height="${(height - pad.top - pad.bottom).toFixed(1)}" />`,
    );
  }

  // axes + gridlines
  const axisY = (los) =>
    pad.top + ((limit - los) / (2 * limit)) * (height - pad.top - pad.bottom);
  for (const tick of yTicks) {
    const y = axisY(tick);
    parts.push(
      `<line class="${tick === 0 ? "plan-zero" : "plan-grid"}" ` +
        `x1="${pad.left}" y1="${y.toFixed(1)}" x2="${width - pad.right}" y2="${y.toFixed(1)}" />`,
    );
    parts.push(
      `<text class="plan-tick plan-ytick" x="${pad.left - 4}" y="${(y + 3).toFixed(1)}" ` +
        `text-anchor="end">${fmt(tick)}</text>`,
    );
  }
  for (const tick of model.xTicks) {
    parts.push(
      `<text class="plan-tick plan-xtick" x="${tick.x.toFixed(1)}" ` +
        `y="${height - pad.bottom + 13}" text-anchor="middle">${tick.label}</text>`,
    );
  }

  // selected-date cursor (scrubber sync) — drawn UNDER the data so a dot
  // sitting on the cursor date stays visible
  if (model.cursor) {
    parts.push(
      `<line class="plan-cursor" data-cursor="${model.cursor.date}" ` +
        `x1="${model.cursor.x.toFixed(1)}" y1="${pad.top}" ` +
        `x2="${model.cursor.x.toFixed(1)}" y2="${height - pad.bottom}" />`,
    );
  }

  // plan milestones: dashed vertical event ticks with the plan's labels,
  // wrapped to the roomy side so long text never clips at the viewBox edge
  model.milestones.forEach((m) => {
    const label = m[lang] ?? m.es ?? m.date;
    const lines = wrapLabel(label, m.labelMaxChars);
    const anchorX = m.labelAnchor === "end" ? m.x - 3 : m.x + 3;
    const anchor = m.labelAnchor === "end" ? "end" : "start";
    parts.push(
      `<line class="plan-milestone" data-milestone="${m.date}" ` +
        `x1="${m.x.toFixed(1)}" y1="${pad.top}" x2="${m.x.toFixed(1)}" ` +
        `y2="${height - pad.bottom}" />`,
    );
    parts.push(
      `<text class="plan-mslabel" text-anchor="${anchor}" ` +
        `x="${anchorX.toFixed(1)}" y="${pad.top + 9}">` +
        `<title>${escapeXml(label)}</title>` +
        lines
          .map(
            (line, li) =>
              `<tspan class="plan-msline" x="${anchorX.toFixed(1)}" ` +
              `dy="${li === 0 ? 0 : 9}">${escapeXml(line)}</tspan>`,
          )
          .join("") +
        `</text>`,
    );
  });

  // measured series: broken polylines + dots (gaps = no segment at all)
  for (const segment of model.segments) {
    const pts = segment.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
    parts.push(`<polyline class="plan-line" points="${pts}" />`);
  }
  for (const segment of model.segments) {
    for (const p of segment) {
      parts.push(
        `<circle class="plan-dot" data-date="${p.date}" data-los="${p.los}" ` +
          `cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3" />`,
      );
    }
  }

  parts.push(
    `<text class="plan-ylab" x="4" y="${pad.top - 6}">${escapeXml(yLabel)}</text>`,
  );

  return (
    `<svg class="plan-chart-svg" data-chart="plan" viewBox="0 0 ${width} ${height}" ` +
    `preserveAspectRatio="none" role="img">` +
    parts.join("") +
    "</svg>"
  );
}

function escapeXml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}
