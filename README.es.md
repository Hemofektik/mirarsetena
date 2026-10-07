# Mirar Setena

**Monitoreo ambiental satelital para proyectos SETENA — empezando por *CDP Río General*.**

Mirar Setena es una aplicación web SIG que muestra en un mapa el alcance geográfico de un proyecto autorizado por la Secretaría Técnica Nacional Ambiental (SETENA) de Costa Rica, y da seguimiento a los cambios del sitio en el tiempo usando datos libres de los satélites Sentinel de la ESA.

> 🚧 Estado: desarrollo inicial. El alcance completo — decisiones, arquitectura y hoja de ruta — está documentado en **[docs/SCOPE.md](docs/SCOPE.md)**; el plan de ejecución TDD/BDD con casillas de progreso por incremento está en **[docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md)**.

## ¿Por qué?

La Resolución **RES-1333-2017-SETENA** autorizó el proyecto *CDP Río General* — extracción de material del cauce del Río General más una planta de trituración en Pérez Zeledón, San José — y define exactamente dónde puede operar el desarrollador. Los trabajos iniciaron en agosto de 2026.

Verificar que el terreno corresponde a la huella autorizada hoy exige trabajo manual en SIG con años de escenas satelitales. Mirar Setena convierte eso en un mapa accesible:

- **Capa de alcance** — parcelas catastrales reconstruidas a partir de los planos originales de 1991 (tablas de derrotero), ancladas a las coordenadas del registro, más los puntos de interés del proyecto: oficina, patio de almacenamiento, chancadora, rampa de volteo, extremos del camino interno y puntos de inspección de campo.
- **Progreso en el tiempo** — un control deslizante de fechas sobre dos misiones:
  - **Sentinel-2** (óptico): RGB, NDVI, MNDWI e índice de suelo desnudo, con puntaje de nube por fecha sobre el área de interés;
  - **Sentinel-1** (radar): cambio de retrodispersión versus una línea base seleccionable pre-obras, más una capa de cambio de coherencia calculada en segundo plano — independiente de nubes, de día o de noche.
- **Servicio OGC WMTS** — un servicio de mosaicos WMTS 1.0.0 + XYZ completo, consumible desde el mapa MapLibre integrado *y* desde clientes de escritorio como QGIS.
- **Backend con conciencia de datos** — consultas programadas a los catálogos, predicción de la próxima adquisición a partir de los ciclos de repetición orbital, y una caché de mosaicos en dos niveles (disco local ahora, compatible S3 después) con presupuesto acotado.

## Arquitectura en resumen

| Componente | Elección |
|---|---|
| Backend | Python, FastAPI, rasterio/GDAL, pystac-client, Docker Compose |
| Frontend | MapLibre GL, interfaz en español (lista para i18n), responsiva |
| Óptico/radar (sin cuenta) | Copernicus Sentinel-2 L2A y Sentinel-1 GRD vía datos abiertos de AWS |
| Coherencia (cuenta gratuita) | Sentinel-1 SLC vía Copernicus Data Space Ecosystem |
| Proyectos | Registro basado en configuración — rutas, capas y claves de caché con espacio de nombres por proyecto, para cargar otros proyectos SETENA como archivos de configuración |

## Hoja de ruta

- **v1**: todo lo descrito en [docs/SCOPE.md](docs/SCOPE.md) §4–5 — reconstrucción del AOI, ambos procesamientos (óptico y radar), WMTS, trabajos en segundo plano, interfaz.
- **Fase 2**: cambio de elevación (diferenciación de DEM), mapa de primera detección, gráficas de series de tiempo, comparación deslizante, extensión de inundación, exportación GeoTIFF, InSAR.

## Datos y atribución

Incluye datos Copernicus Sentinel modificados. Datos © ESA/Copernicus; mapas base © colaboradores de OpenStreetMap y Esri, Maxar, Earthstar Geographics.

## Licencia

[MIT](LICENSE)
