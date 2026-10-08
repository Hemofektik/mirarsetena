/**
 * On-map POI markers: a dot pinned to the true position plus a label whose
 * placement is solved every frame by the pure poi-layout module — so every
 * label is always visible and never overlaps another one.
 */
import { layoutPois } from "./poi-layout.js";

const SVG_NS = "http://www.w3.org/2000/svg";

export function installPoiMarkers(map, geojson) {
  const container = document.createElement("div");
  container.id = "poi-markers";
  const leaders = document.createElementNS(SVG_NS, "svg");
  leaders.setAttribute("class", "poi-leaders");
  container.appendChild(leaders);
  map.getContainer().appendChild(container);

  const markers = [];
  const hiddenGroups = new Set();

  for (const feature of geojson?.features ?? []) {
    if (feature.geometry?.type !== "Point") continue;
    const { id, label, group } = feature.properties;
    const [lng, lat] = feature.geometry.coordinates;

    const dot = document.createElement("div");
    dot.className = "poi-dot";
    dot.dataset.group = group;
    const text = document.createElement("div");
    text.className = "poi-label";
    text.dataset.group = group;
    text.textContent = label;
    container.append(dot, text);

    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("class", "poi-leader");
    line.style.display = "none";
    leaders.appendChild(line);

    markers.push({ id, lng, lat, group, dot, label: text, line, w: 0, h: 0 });
  }

  // labels never change text: measure once (0 until laid out, so lazily too)
  function measure(marker) {
    if (!marker.w) {
      marker.w = marker.label.offsetWidth;
      marker.h = marker.label.offsetHeight;
    }
  }

  function repaint() {
    const visible = markers.filter((m) => !hiddenGroups.has(m.group));
    const items = [];
    const screen = new Map();
    for (const marker of visible) {
      measure(marker);
      if (!marker.w) continue;
      const p = map.project([marker.lng, marker.lat]);
      screen.set(marker.id, p);
      items.push({ id: marker.id, x: p.x, y: p.y, w: marker.w, h: marker.h });
    }
    const bounds = map.getContainer().getBoundingClientRect();
    const placed = layoutPois(items, {
      width: bounds.width,
      height: bounds.height,
    });
    for (const marker of visible) {
      const p = screen.get(marker.id);
      const box = placed.find((r) => r.id === marker.id);
      if (!p || !box) continue;
      marker.dot.style.transform = `translate(${p.x}px, ${p.y}px)`;
      marker.label.style.transform = `translate(${box.x}px, ${box.y}px)`;
      if (box.leader) {
        // line from the dot to the nearest point of the label box
        const cx = Math.min(Math.max(p.x, box.x), box.x + marker.w);
        const cy = Math.min(Math.max(p.y, box.y), box.y + marker.h);
        marker.line.setAttribute("x1", p.x);
        marker.line.setAttribute("y1", p.y);
        marker.line.setAttribute("x2", cx);
        marker.line.setAttribute("y2", cy);
        marker.line.style.display = "";
      } else {
        marker.line.style.display = "none";
      }
    }
  }

  function setVisible(group, on) {
    if (on) hiddenGroups.delete(group);
    else hiddenGroups.add(group);
    for (const marker of markers) {
      if (marker.group !== group) continue;
      const display = on ? "" : "none";
      marker.dot.style.display = display;
      marker.label.style.display = display;
      if (!on) marker.line.style.display = "none";
    }
    repaint();
  }

  map.on("move", repaint);
  map.on("zoom", repaint);
  map.on("resize", repaint);
  // fonts/layout settle after the first paint — re-measure then
  requestAnimationFrame(() => {
    for (const marker of markers) {
      marker.w = 0;
      measure(marker);
    }
    repaint();
  });

  return {
    setVisible,
    repaint,
    destroy() {
      map.off("move", repaint);
      map.off("zoom", repaint);
      map.off("resize", repaint);
      container.remove();
    },
  };
}
