# Mirar Setena — Implementation Plan

Execution companion to **[SCOPE.md](./SCOPE.md)**. SCOPE answers *what and why*; this document answers *in what order, how each piece is proven, and what is already done*.

## How to use this document

- An **increment** is sized for one focused TDD session and one PR-sized review. Nothing larger.
- **TDD**: the test files listed under an increment are written first (failing), then the implementation makes them pass.
- **BDD**: *Scenario* lines are executable acceptance tests — API-level scenarios run as `pytest -m acceptance`, browser scenarios via Playwright (`npm run test:e2e`); *(manual)* items are checked by hand and noted.
- **Status**: `- [ ]` not started, `- [x]` done. The Progress board below is the at-a-glance view; update both when an increment's tests **and** its scenario pass.
- Coarse todo IDs (`init-repo`, `aoi-reconstruction`, …) map 1:1 to phases A–K.

## Progress board

| Phase | Todo | Increments | Done | Status |
|---|---|---|---|---|
| A. Repository & docs | `init-repo` | 2 | 2 | ✅ |
| B. AOI reconstruction | `aoi-reconstruction` | 3 | 1 | 🟡 |
| C. Backend foundation | `scaffold-backend` | 3 | 1 | 🟡 |
| D. Sentinel-2 pipeline | `s2-pipeline` | 4 | 0 | ⬜ |
| E. Sentinel-1 GRD pipeline | `s1-grd-pipeline` | 2 | 0 | ⬜ |
| F. Tiles & WMTS service | `wmts-service` | 4 | 0 | ⬜ |
| G. Freshness & jobs | `background-jobs` | 4 | 0 | ⬜ |
| H. Coherence (CDSE) | `cdse-coherence` | 4 | 0 | ⬜ |
| I. Frontend | `frontend` | 7 | 0 | ⬜ |
| J. CI | `ci-pipeline` | 1 | 0 | ⬜ |
| K. End-to-end verification | `e2e-verify` | 1 | 0 | ⬜ |
| **Total** | | **35** | **4** | |

**Execution order** (parallel where branches are independent):

```mermaid
flowchart LR
  A[A ✅ done] --> B[B: AOI]
  A --> C[C: Backend]
  B --> D[D: Sentinel-2]
  C --> D
  B --> E[E: Sentinel-1 GRD]
  C --> E
  D --> F[F: Tiles & WMTS]
  E --> F
  C --> G[G: Jobs & freshness]
  E --> H[H: Coherence]
  G --> H
  F --> I[I: Frontend]
  C --> J[J: CI]
  I --> K[K: E2E]
  G --> K
  H --> K
```

**Definition of done (per increment):** tests green, scenario green, checkbox ticked. When a whole phase completes, mark its todo `done` in the session tracker.

---

## Phase A — Repository & docs (`init-repo`) ✅

### A.1 Repository scaffold
- **Deliverable:** git repo on `main`, MIT license, `.gitignore` with PII exclusions for `samples/` (registry printouts/certificates/plan scans stay local; only the public SETENA resolution is committed).
- **Verification:** remote `Hemofektik/mirarsetena` exists, public, first commit pushed over SSH.
- **Status:** [x] done

### A.2 Scope & decision record
- **Deliverable:** `docs/SCOPE.md` — facts, verified data-source measurements, full decision log, architecture, appendices; bilingual README pointing at it.
- **Verification:** scope doc matches the approved plan (incl. corrected derrotero validation results).
- **Status:** [x] done

---

## Phase B — AOI reconstruction (`aoi-reconstruction`)

Depends: A. Produces the canonical project geometry consumed by C/D/E (`tools/aoi/` one-off generator per SCOPE R5-Q2).

### B.1 Derrotero traverse core
- **Deliverable:** pure geometry module: azimuth °′→decimal, forward computation, closure distance, shoelace area, closed-traverse validator with hard gates.
- **TDD:** `tests/aoi/test_traverse.py` — both Appendix A tables must reproduce the measured results: closure ≤ 0.1 m (980860: 0.02 m, 980861: 0.05 m), area within ±0.1 % (measured +0.006 % / −0.010 %), azimuth conversion cases (89°19′ → 89.3167°); empty-leg and open-traverse inputs fail loudly.
- **Status:** [x] done

### B.2 Anchor resolution
- **Deliverable:** algorithm choosing which vertex of each traverse corresponds to its registry CRTM coordinate, scored by the SCOPE §7 consistency checks.
- **TDD:** `tests/aoi/test_anchor.py` — shared edge (980860 leg 13–1 ↔ 980861 leg 4–5) coincides within 0.5 m; parcels land west of the road / east of Río General; output WGS84 bbox within the SCOPE §1 extent (9.3797–9.3953 °N, −83.6734…−83.6621 °).
- **Status:** [ ]

### B.3 GeoJSON + POI emission
- **Deliverable:** `data/projects/cdp-rio-general/aoi.geojson` (two parcel polygons, WGS84) + `pois.geojson` (10 points: 8 facilities, 2 inspection) with labels from Appendix B.
- **TDD:** `tests/aoi/test_emit.py` — valid FeatureCollection, simple (non-self-intersecting) polygons, exactly 10 POIs in two groups, coordinates transformed correctly.
- **Scenario (BDD, acceptance):** *Given the corrected Appendix A tables and Appendix B anchors, when the generator runs, then both parcels close < 2 m with area ±1 % of stated, the shared edge coincides within 0.5 m, and the emitted GeoJSON loads with exactly 8 facility + 2 inspection POIs.*
- **Status:** [ ]

---

## Phase C — Backend foundation (`scaffold-backend`)

Depends: A. Runs in parallel with B.

### C.1 Config-driven project registry
- **Deliverable:** `config/projects/cdp-rio-general.yaml` (slug, name, AOI/POI paths, layer set, timeline start `2026-07-01`, works-start marker, bbox) + loader with schema validation + namespacing helper producing `/p/{slug}/…` route and cache keys.
- **TDD:** `tests/backend/test_registry.py` — loads the real project file; unknown slug → 404/`ProjectNotFound`; missing required field → validation error; two projects namespace independently.
- **Status:** [x] done

### C.2 Storage interface (local now, S3 later)
- **Deliverable:** `Storage` protocol + `LocalStore` (get/put/exists/list/delete) with per-project path namespacing and size accounting hooks; `.env.example`.
- **TDD:** `tests/backend/test_storage.py` — round-trip, project isolation (same key in two projects → two files), size reporting; suite is interface-contract style so a future `S3Store` passes the same tests.
- **Status:** [ ]

### C.3 FastAPI app + Docker Compose
- **Deliverable:** app factory, `/healthz`, project-scoped routers mounted under `/p/{slug}`, `Dockerfile`, `docker-compose.yml` (api + storage volume).
- **TDD:** `tests/backend/test_app.py` — TestClient `/healthz` 200; unknown project route → 404.
- **Scenario (manual):** *`docker compose up` serves `/healthz` within 30 s.*
- **Status:** [ ]

---

## Phase D — Sentinel-2 pipeline (`s2-pipeline`)

Depends: B, C.

### D.1 STAC search & scene grouping
- **Deliverable:** Earth Search client (no-auth STAC): bbox + datetime search, pagination, same-date dual-tile grouping (measured: 47 items → 24 dates).
- **TDD:** `tests/pipeline/test_stac_search.py` — against a committed fixture of the real STAC response: 24 distinct dates, multi-tile dates grouped, empty-result handling, HTTP-error backoff.
- **Status:** [ ]

### D.2 AOI window extraction, mosaicking, L1 cache
- **Deliverable:** scene → AOI-window (bbox + tile-overlap buffer) source extraction via GDAL/rasterio byte ranges; mosaic of co-temporal tiles; stored through the `Storage` interface as **level-1 cache**.
- **TDD:** `tests/pipeline/test_window_mosaic.py` — synthetic GeoTIFFs: window bounds/CRS correct, mosaic extent covers AOI, second request is a cache hit (fake downloader call count stays 1).
- **Status:** [ ]

### D.3 Index computation + SCL cloud scoring
- **Deliverable:** RGB (stretch), NDVI, MNDWI, BSI rasters per scene; per-date cloud % = SCL cloudy classes within AOI / AOI pixels; results cached per (scene, layer).
- **TDD:** `tests/pipeline/test_indices.py` — hand-computed NDVI/MNDWI/BSI on synthetic band arrays; cloud % from a fixture SCL raster with known cloud fraction; nodata handling.
- **Status:** [ ]

### D.4 Layer-driven date list
- **Deliverable:** date-list service: `GET /p/{slug}/api/dates?layer=ndvi` → S2 dates with cloud % + flags; `max_cloud` filter for the hide-cloudy toggle.
- **TDD:** `tests/api/test_dates_api.py` — layer→mission mapping (S2 layers → S2 dates; S1 layers reserved), filter correctness, ordering ascending, works-start marker date always present.
- **Scenario (BDD, acceptance):** *Given 24 S2 dates with computed cloud scores, when the dates API is called with `max_cloud=20`, then only dates ≤ 20 % cloud are returned, each carrying its cloud value.*
- **Status:** [ ]

---

## Phase E — Sentinel-1 GRD pipeline (`s1-grd-pipeline`)

Depends: B, C. Parallel with D.

### E.1 GRD window + σ⁰ backscatter
- **Deliverable:** GRD COG window fetch reusing the D.2 cache path; backscatter in linear and dB; raw σ⁰ layer.
- **TDD:** `tests/pipeline/test_grd.py` — synthetic GRD arrays: dB conversion correct and monotonic; window extraction matches AOI; L1 cache shared behavior with S2 path.
- **Status:** [ ]

### E.2 Change-vs-baseline renderer
- **Deliverable:** pure function: (scene σ⁰, baseline σ⁰) → classified dB-delta ramp; baseline selection defaulting to the last pre-works July 2026 scene, overridable by parameter; raw-grayscale passthrough mode.
- **TDD:** `tests/pipeline/test_change_render.py` — known deltas → expected classes/colors; default-baseline resolution from fixture date lists; override honored; raw mode ignores baseline.
- **Status:** [ ]

---

## Phase F — Tiles & WMTS service (`wmts-service`)

Depends: D, E.

### F.1 Tile renderer
- **Deliverable:** (project, layer, date, z/x/y) → PNG renderer over cached rasters; Web Mercator tile math; zoom cap 18 (requests above → 400); nodata transparent.
- **TDD:** `tests/api/test_tile_render.py` — known z/x/y → known bounds; determinism (same input → same bytes); z > 18 rejected; pixel footprint stays inside AOI.
- **Status:** [ ]

### F.2 Level-2 tile cache + LRU budget
- **Deliverable:** rendered-tile cache through `Storage`; soft budget 2 GB with LRU eviction; **level-1 sources never evicted** (SCOPE R2-Q6).
- **TDD:** `tests/api/test_tile_cache.py` — eviction order under fake sizes, budget enforcement, source-retention invariant, cache-hit avoids re-render (counter).
- **Status:** [ ]

### F.3 WMTS 1.0.0 + XYZ parity
- **Deliverable:** OGC WMTS `GetCapabilities` (layers, styles, tile matrix set, **time dimension = date list**) + `GetTile`; XYZ route `/p/{slug}/tiles/{layer}/{date}/{z}/{x}/{y}.png` on the same handler.
- **TDD:** `tests/api/test_wmts.py` — capabilities XML parses and lists every layer × date; `GetTile` returns PNG for valid requests, 404 for unknown date; XYZ and WMTS return **byte-identical** tiles.
- **Scenario (BDD, acceptance):** *Given the service running with S2 and S1 dates, when a client fetches GetCapabilities, then all layers and dates appear; when the same tile is fetched via WMTS and XYZ, the bytes match.*
- **Status:** [ ]

### F.4 QGIS conformance check
- **Scenario (manual):** *Given the running service, when QGIS adds the WMTS via its URL, then GetCapabilities lists the layers/dates and tiles render identically to the browser view.*
- **Status:** [ ]

---

## Phase G — Freshness & jobs (`background-jobs`)

Depends: C, D.

### G.1 Durable job runner
- **Deliverable:** persistent job queue (SQLite table: pending/running/done/failed, attempts, backoff) + single worker; job types registered by name.
- **TDD:** `tests/jobs/test_runner.py` — enqueue→run→done; failing job retries then marks failed; re-enqueue of a done job is idempotent; crash mid-run → job returns to pending on restart.
- **Status:** [ ]

### G.2 Catalog poll + date index (dual catalog)
- **Deliverable:** scheduled poll writing a local date index with timestamp; on-request reads honor ≤ 6 h TTL (SCOPE R4-Q1): Earth Search drives S2/GRD; CDSE queried only for SLC; CDSE failure degrades gracefully (core date lists unaffected).
- **TDD:** `tests/jobs/test_catalog_poll.py` — mocked clients: index written with timestamp; stale TTL triggers refresh; CDSE outage → S2/GRD dates still served, coherence flagged stale.
- **Status:** [ ]

### G.3 Orbit-based ETA prediction
- **Deliverable:** next-acquisition prediction per mission from last-seen scene + observed repeat cycle (S2 ≈ 5 d across S2A/B/C, S1 = 12 d, orbit-state aware).
- **TDD:** `tests/jobs/test_eta.py` — fixture last-scenes → expected next dates; mission without recent scenes → prediction withheld, not fabricated.
- **Status:** [ ]

### G.4 `/status` endpoint + page
- **Deliverable:** JSON `/status` + minimal HTML: cache sizes, last catalog check, job queue summary, predicted next acquisition, last scene per mission (SCOPE R3-Q7).
- **TDD:** `tests/api/test_status.py` — schema fields present under mocked state; degraded-coherence state visible.
- **Scenario (manual):** *page renders with all sections populated against the live compose stack.*
- **Status:** [ ]

---

## Phase H — Coherence (`cdse-coherence`)

Depends: E, G.

### H.1 CDSE auth client
- **Deliverable:** OAuth token flow against Copernicus CDSE (fetch/cache/refresh), credentials from env; when no token → coherence layer reports *disabled* while everything else keeps working.
- **TDD:** `tests/coherence/test_cdse_auth.py` — mocked HTTP: token cached, refreshed on expiry, disabled state on missing credentials (no crash).
- **Status:** [ ]

### H.2 SLC pair planner
- **Deliverable:** pair selection from scene metadata: same relative orbit **and** same orbit state, nominal 12-day baseline, window from `2026-07-01` (observed passes: 23:47 descending, 11:22 — must never pair across states).
- **TDD:** `tests/coherence/test_pair_planner.py` — fixture metadata covering both pass times → no cross-state pairs; golden expected pair list for Jul–Oct 2026; unpaired scenes reported.
- **Status:** [ ]

### H.3 Compute-and-purge pipeline
- **Deliverable:** download SLC → run `CoherenceProcessor` interface → write coherence GeoTIFF to L1 cache → **always delete SLC sources** (finally-block; SCOPE R4-Q4); disk accounting reported. Includes the processor spike: choose and pin SNAP-gpt vs ISCE2 based on one measured AOI run (record size + runtime here when done).
- **TDD:** `tests/coherence/test_pipeline.py` — fake downloader + fake processor: result cached, sources absent after success *and* after failure, accounting numbers correct.
- **Status:** [ ]
- **Spike result:** _pending_

### H.4 Trigger policy (first-view → eager, then append)
- **Deliverable:** project "viewed" flag set on first plain-data request → enqueue one eager full-window coherence job (no duplicates); afterwards each new SLC scene enqueues exactly one new-pair job (SCOPE R2-Q1).
- **TDD:** `tests/coherence/test_trigger.py` — state machine: not-viewed → viewed once (second request doesn't re-enqueue); new scene → one appended job; old scene re-listed → no duplicate.
- **Status:** [ ]

---

## Phase I — Frontend (`frontend`)

Depends: F. (G/H features plug in as they land.)

### I.1 App shell + namespaced routes
- **Deliverable:** Vite + MapLibre app; router on `/p/{slug}/` prefix; project config load; link to `/status`.
- **TDD:** `tests/frontend/test_shell.test.ts` — route namespacing resolves config; missing project → error state.
- **Status:** [ ]

### I.2 Layer switcher + scrubber date model
- **Deliverable:** pure state logic (layer → mission → dates → selection; works-start marker; cloud badges; hide-cloudy filter) + UI; Playwright smoke.
- **TDD:** `tests/frontend/test_scrubber_model.test.ts` — mission switching per SCOPE R4-Q2, marker insertion, filtering preserves selection when possible.
- **Scenario (Playwright):** *open app → select NDVI → scrubber lists S2 dates with cloud badges → enable hide-cloudy → ≤20 % dates only.*
- **Status:** [ ]

### I.3 POI layer + basemaps
- **Deliverable:** POI layer with two toggle groups and labels; OSM default + Esri imagery toggle; attributions.
- **TDD:** `tests/frontend/test_layers.test.ts` — group toggles independent; basemap swap keeps overlays.
- **Status:** [ ]

### I.4 S1 baseline selector UI
- **Deliverable:** baseline picker (default last pre-works July 2026 scene) + raw-grayscale toggle wired to E.2 request params.
- **TDD:** `tests/frontend/test_baseline.test.ts` — state → request URL params mapping for both modes.
- **Status:** [ ]

### I.5 Shareable URL state + PNG export
- **Deliverable:** encode layer, date, baseline, bbox, zoom, toggles in URL; restore on load; canvas PNG download.
- **TDD:** `tests/frontend/test_url_state.test.ts` — serialize→parse round-trip property (incl. all toggles and baseline).
- **Status:** [ ]

### I.6 About panel + i18n + attribution
- **Deliverable:** About panel (RES-1333-2017 reference, Copernicus/ESA attribution, reconstruction method, unofficial-tool disclaimer — SCOPE R3-Q9); ES strings in i18n catalog with EN scaffold.
- **TDD:** `tests/frontend/test_i18n.test.ts` — no hardcoded UI strings (every string from catalog); catalog key parity ES/EN; Playwright asserts disclaimer + attribution in DOM.
- **Status:** [ ]

### I.7 Responsive/mobile
- **Deliverable:** layout pass for 390 px and 1280 px viewports.
- **Scenario (Playwright + manual):** *at 390 px the scrubber, layer switcher and POI toggle remain usable; real-phone spot check passes.*
- **Status:** [ ]

---

## Phase J — CI (`ci-pipeline`)

Depends: A, C.

### J.1 GitHub Actions pipeline
- **Deliverable:** workflow on push/PR: lint (ruff + eslint) → unit tests (pytest + vitest) → Docker image build (no push); free for this public repo.
- **Verification:** workflow file valid; first run green on this repository (verify via `gh run watch`).
- **Status:** [ ]

---

## Phase K — End-to-end verification (`e2e-verify`)

Depends: G, H, I.

### K.1 Acceptance run on the compose stack
- **Scenario (manual checklist, results recorded here):** *Given `docker compose up` with a warm cache —*
  1. WMTS GetCapabilities loads in browser **and** QGIS; tiles render in both.
  2. All layers render per date: RGB, NDVI, MNDWI, BSI (S2 dates), σ⁰ + coherence (S1 dates).
  3. Scrubber: works-start marker visible; cloud badges correct; hide-cloudy filters.
  4. POI groups toggle; basemap OSM ⇄ Esri; attributions visible.
  5. Baseline selector changes σ⁰ rendering; raw toggle works.
  6. Share URL restores exact view; PNG export downloads.
  7. `/status` shows cache sizes, last poll, job queue, next-acquisition prediction, last scene per mission.
  8. Coherence: first plain-data view triggers the eager job; after a new SLC lands, exactly one pair job appends; sources purged.
  9. Mobile viewport usable.
  10. Container restart → caches and date index survive.
- **Status:** [ ]
