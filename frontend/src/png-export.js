/**
 * PNG export composition: the map canvas plus the DOM POI overlay, which
 * the raw `map.getCanvas().toDataURL()` never contains (user 2026-10-14:
 * "the PNG download is missing the POIs").
 *
 * `paintPoiPlan` is pure: it draws a marker plan (positions in CSS px, as
 * poi-markers.exportState collected them) onto any 2D context at a device
 * scale — leaders under labels under dots, mirroring the DOM z-order.
 */

/** Parse "rgb(r, g, b)" / "rgba(r, g, b, a)" into channels (for tests). */
export function parseCssColor(value) {
  const m = /rgba?\(([^)]+)\)/.exec(value);
  if (!m) return null;
  const parts = m[1].split(",").map((p) => Number.parseFloat(p.trim()));
  return { r: parts[0], g: parts[1], b: parts[2], a: parts[3] ?? 1 };
}

/** Multiply the px sizes in a CSS font shorthand (device scaling). */
export function scaleFont(font, s) {
  if (!font || s === 1) return font;
  return font.replace(/(\d+(?:\.\d+)?)px/g, (_, size) =>
    `${Number(size) * s}px`,
  );
}

function roundedRectPath(ctx, x, y, w, h, r) {
  const radius = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + w, y, x + w, y + h, radius);
  ctx.arcTo(x + w, y + h, x, y + h, radius);
  ctx.arcTo(x, y + h, x, y, radius);
  ctx.arcTo(x, y, x + w, y, radius);
  ctx.closePath();
}

/**
 * Paint one POI marker plan onto a 2D context.
 *
 * plan: { leaders: [{x1,y1,x2,y2,stroke,width}],
 *         labels: [{x,y,w,h,text,font,bg,border,color,radius}],
 *         dots:   [{x,y,r,fill,ring,shadow}] }
 * scale: device pixels per CSS px (canvas backing / client size).
 */
export function paintPoiPlan(ctx, plan, { scale = 1 } = {}) {
  const s = scale;

  for (const line of plan.leaders ?? []) {
    ctx.save();
    ctx.strokeStyle = line.stroke;
    ctx.lineWidth = line.width * s;
    ctx.beginPath();
    ctx.moveTo(line.x1 * s, line.y1 * s);
    ctx.lineTo(line.x2 * s, line.y2 * s);
    ctx.stroke();
    ctx.restore();
  }

  for (const label of plan.labels ?? []) {
    const x = label.x * s;
    const y = label.y * s;
    const w = label.w * s;
    const h = label.h * s;
    ctx.save();
    if (label.shadow) {
      ctx.shadowColor = label.shadow;
      ctx.shadowBlur = 2 * s;
      ctx.shadowOffsetY = 1 * s;
    }
    roundedRectPath(ctx, x, y, w, h, label.radius * s);
    ctx.fillStyle = label.bg;
    ctx.fill();
    ctx.shadowColor = "transparent";
    ctx.shadowBlur = 0;
    ctx.shadowOffsetY = 0;
    ctx.lineWidth = s;
    ctx.strokeStyle = label.border;
    ctx.stroke();
    ctx.fillStyle = label.color;
    // the label BOX is scaled to the backing store — the font must be
    // too, or text renders sub-scale on hi-dpi displays (user report
    // 2026-10-14: "the text in the POI labels is too small")
    ctx.font = scaleFont(label.font, s);
    ctx.textBaseline = "middle";
    ctx.fillText(label.text, x + 7 * s, y + h / 2);
    ctx.restore();
  }

  for (const dot of plan.dots ?? []) {
    const cx = dot.x * s;
    const cy = dot.y * s;
    ctx.save();
    if (dot.shadow) {
      ctx.shadowColor = dot.shadow;
      ctx.shadowBlur = 3 * s;
      ctx.shadowOffsetY = 1 * s;
    }
    // CSS: 12px box (border-box) with a 2px ring → white disc r6, fill r4
    ctx.beginPath();
    ctx.arc(cx, cy, dot.r * s, 0, Math.PI * 2);
    ctx.fillStyle = dot.ring;
    ctx.fill();
    ctx.shadowColor = "transparent";
    ctx.shadowBlur = 0;
    ctx.shadowOffsetY = 0;
    ctx.beginPath();
    ctx.arc(cx, cy, (dot.r - 2) * s, 0, Math.PI * 2);
    ctx.fillStyle = dot.fill;
    ctx.fill();
    ctx.restore();
  }
}

/**
 * Compose the map canvas + marker plan into one PNG data URL.
 * `scale` = canvas backing px per CSS px (typically devicePixelRatio).
 */
export function composePng(mapCanvas, plan, scale) {
  const out = document.createElement("canvas");
  out.width = mapCanvas.width;
  out.height = mapCanvas.height;
  const ctx = out.getContext("2d");
  ctx.drawImage(mapCanvas, 0, 0);
  paintPoiPlan(ctx, plan, { scale });
  return out.toDataURL("image/png");
}
