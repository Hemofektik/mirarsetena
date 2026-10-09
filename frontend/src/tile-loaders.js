/**
 * Per-tile loading animation + retirement for the satellite overlay.
 *
 * The server may cold-process a scene for tens of seconds per (layer, date),
 * so every in-flight overlay tile request gets a spinner pinned to that
 * tile's screen position; it disappears the moment the response lands
 * (success, 404 or abort alike).
 *
 * Requests are tracked at the fetch layer — MapLibre loads raster tiles
 * through window.fetch (verified against maplibre-gl v5 and v6: 40/40 tile
 * requests, zero XHR). Basemaps (OSM/Esri) use other URL shapes and are
 * intentionally not matched: only our own server can be slow.
 *
 * Retirement: MapLibre queues tile requests internally and dispatches them
 * to fetch SECONDS after setTiles() — a scrub away and back would let the
 * abandoned date's whole wave start late and block the map. Two guards:
 * retireTileRequests() aborts what is already in flight, and the dispatch
 * check below drops queued requests whose template is no longer current.
 */

// /p/<slug>/tiles/<layer>/<date>/<z>/<x>/<y>.png?...
const TILE_URL = /\/tiles\/[^/]+\/\d{4}-\d{2}-\d{2}\/(\d+)\/(\d+)\/(\d+)\.png/;

/**
 * Template the overlay is currently supposed to load.
 * undefined = not primed yet (boot); null = overlay switched off.
 * @type {string | null | undefined}
 */
let currentTemplate;

/** In-flight overlay fetches: {templateKey, controller}. */
const inFlightRequests = new Set();

export function retireTileRequests(templateKey) {
  currentTemplate = templateKey;
  for (const record of inFlightRequests) {
    if (templateKey === null || record.templateKey !== templateKey) {
      record.controller.abort();
    }
  }
}

function templateKeyOf(url) {
  return url.replace(/\/\d+\/\d+\/\d+\.png/, "/{z}/{x}/{y}.png");
}

function tileBounds(z, x, y) {
  const n = 2 ** z;
  const lat = (ty) =>
    (Math.atan(Math.sinh(Math.PI * (1 - (2 * ty) / n))) * 180) / Math.PI;
  return {
    west: (x / n) * 360 - 180,
    east: ((x + 1) / n) * 360 - 180,
    north: lat(y),
    south: lat(y + 1),
  };
}

export function installTileLoaders(map) {
  if (!map) return;

  const container = document.createElement("div");
  container.id = "tile-loaders";
  container.setAttribute("aria-hidden", "true");
  document.getElementById("map").appendChild(container);

  /** @type {Map<string, {element: HTMLElement, count: number, z: number, x: number, y: number}>} */
  const pending = new Map();
  const realFetch = window.fetch.bind(window);

  function place(entry) {
    const bounds = tileBounds(entry.z, entry.x, entry.y);
    const nw = map.project([bounds.west, bounds.north]);
    const se = map.project([bounds.east, bounds.south]);
    entry.element.style.left = `${nw.x}px`;
    entry.element.style.top = `${nw.y}px`;
    entry.element.style.width = `${Math.max(se.x - nw.x, 0)}px`;
    entry.element.style.height = `${Math.max(se.y - nw.y, 0)}px`;
  }

  function show(key, z, x, y) {
    const existing = pending.get(key);
    if (existing) {
      existing.count += 1;
      return;
    }
    const element = document.createElement("div");
    element.className = "tile-loader";
    element.dataset.tile = key;
    element.innerHTML = '<span class="tile-loader-spinner"></span>';
    container.appendChild(element);
    const entry = { element, count: 1, z, x, y };
    pending.set(key, entry);
    place(entry);
  }

  function hide(key) {
    const entry = pending.get(key);
    if (!entry) return;
    entry.count -= 1;
    if (entry.count > 0) return;
    entry.element.remove();
    pending.delete(key);
  }

  function reposition() {
    for (const entry of pending.values()) place(entry);
  }

  window.fetch = function (input, init) {
    const url =
      typeof input === "string"
        ? input
        : input instanceof Request
          ? input.url
          : String(input ?? "");
    const match = TILE_URL.exec(url);
    if (!match) return realFetch(input, init);
    const templateKey = templateKeyOf(url);
    // Queued behind a template switch: never dispatch it at all.
    if (currentTemplate !== undefined && templateKey !== currentTemplate) {
      return Promise.reject(
        new DOMException("The operation was aborted.", "AbortError"),
      );
    }
    // Combined signal: MapLibre's own abort or our retire() (scrub/switch).
    const controller = new AbortController();
    const upstream = init?.signal;
    if (upstream) {
      if (upstream.aborted) controller.abort();
      else
        upstream.addEventListener("abort", () => controller.abort(), {
          once: true,
        });
    }
    const record = { templateKey, controller };
    inFlightRequests.add(record);
    const [, z, x, y] = match;
    const key = `${z}/${x}/${y}`;
    show(key, Number(z), Number(x), Number(y));
    const settle = () => {
      hide(key);
      inFlightRequests.delete(record);
    };
    let request;
    try {
      request = realFetch(input, { ...(init ?? {}), signal: controller.signal });
    } catch (error) {
      settle();
      throw error;
    }
    return request.then(
      (response) => {
        settle(); // headers arrived: the server is done thinking
        return response;
      },
      (error) => {
        settle(); // aborts and network failures clear just the same
        throw error;
      },
    );
  };

  map.on("move", reposition);
  map.on("resize", reposition);
}
