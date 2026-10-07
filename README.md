# Mirar Setena

**Satellite-based environmental monitoring for SETENA projects — starting with *CDP Río General*.**

Mirar Setena is a web GIS tool that shows the geographic scope of a project authorized by Costa Rica's *Secretaría Técnica Nacional Ambiental* (SETENA) on a map, and tracks how the site changes over time using freely available ESA Sentinel satellite data.

> 🚧 Status: early development. The full scope — decisions, architecture and roadmap — is documented in **[docs/SCOPE.md](docs/SCOPE.md)**; the TDD/BDD execution plan with per-increment progress checkboxes is **[docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md)**.

## Why

Resolution **RES-1333-2017-SETENA** authorized the *CDP Río General* project — material extraction from the Río General channel plus a crushing plant in Pérez Zeledón, San José — and it defines exactly where the developer may operate. Earthworks began in August 2026.

Verifying that the ground matches the authorized footprint currently means manual work in desktop GIS over a decade of satellite scenes. Mirar Setena turns that into a map anyone can open:

- **Scope layer** — cadastral parcels reconstructed from the original 1991 plan surveys (derrotero tables), anchored to registry coordinates, plus the project's POIs: office, storage yard, breaker, dumper ramp, internal road endpoints and site-inspection points.
- **Progress over time** — a date scrubber across two missions:
  - **Sentinel-2** (optical): RGB, NDVI, MNDWI and bare-soil index, with per-date cloud scoring over the area of interest;
  - **Sentinel-1** (radar): backscatter change versus a selectable pre-works baseline, plus a coherence change layer computed in the background — cloud-independent, day or night.
- **OGC WMTS service** — a full WMTS 1.0.0 + XYZ tile service, consumable from the built-in MapLibre map *and* desktop clients such as QGIS.
- **Data-aware backend** — scheduled catalog checks, next-acquisition prediction from orbit repeat cycles, and a two-level tile cache (local disk now, S3-compatible later) with a bounded cache budget.

## Architecture at a glance

| Piece | Choice |
|---|---|
| Backend | Python, FastAPI, rasterio/GDAL, pystac-client, Docker Compose |
| Frontend | MapLibre GL, Spanish-first UI (i18n-ready), responsive |
| Optical/radar (no account) | Copernicus Sentinel-2 L2A & Sentinel-1 GRD via AWS open data |
| Coherence (free account) | Sentinel-1 SLC via Copernicus Data Space Ecosystem |
| Projects | Config-driven registry — routes, layers and cache keys are namespaced per project, so other SETENA projects can be added as config files |

## Roadmap

- **v1**: everything in [docs/SCOPE.md](docs/SCOPE.md) §4–5 — AOI reconstruction, both radar/optical pipelines, WMTS, background jobs, frontend.
- **Phase 2**: elevation change (DEM differencing), first-detection heatmap, time-series charts, before/after swipe, flood extent, GeoTIFF export, InSAR.

## Data & attribution

Contains modified Copernicus Sentinel data. Copernicus data © ESA/Copernicus; basemaps © OpenStreetMap contributors and Esri, Maxar, Earthstar Geographics.

## License

[MIT](LICENSE)
