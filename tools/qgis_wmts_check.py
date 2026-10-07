"""F.4 acceptance: QGIS's own WMTS client against the live Mirar Setena service.

Run inside the official qgis/qgis image (headless PyQGIS):

    docker run --rm -i --network host \\
        -e QT_QPA_PLATFORM=offscreen -e QGIS_PREFIX_PATH=/usr \\
        -v "$PWD":/work -w /work qgis/qgis:latest \\
        python3 tools/qgis_wmts_check.py

Scenario (IMPLEMENTATION.md F.4): QGIS adds the WMTS via its URL, the
capabilities list the layers/dates, and tiles render like the browser view.

URI note: QgsDataSourceUri::setEncodedUri parses via QUrlQuery, so params
are '&'-joined and the `url` value itself must not contain a query string
(QGIS appends SERVICE/REQUEST itself).
"""
import sys

from qgis.core import (
    QgsApplication,
    QgsMapRendererCustomPainterJob,
    QgsMapSettings,
    QgsRasterLayer,
)
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor, QImage, QPainter

BASE = "http://127.0.0.1:8000/p/cdp-rio-general/wmts"
SLUG = "cdp-rio-general"
LAYER = "ndvi_2026-07-03"
# AOI centre in WGS84 (transformed to the layer CRS below)
AOI_CENTRE_WGS84 = (-83.669, 9.385)


def main() -> int:
    qgs = QgsApplication([], False)
    QgsApplication.setPrefixPath("/usr")
    qgs.initQgis()

    uri = (
        f"url={BASE}"
        f"&layers={LAYER}&styles=default&format=image/png"
        f"&crs=EPSG:3857&tileMatrixSet=GoogleMapsCompatible"
    )
    layer = QgsRasterLayer(uri, LAYER, "wms")
    if not layer.isValid():
        print(f"FAIL: QGIS could not load the WMTS layer ({layer.error().summary()})")
        return 1
    print(f"OK: QGIS loaded {LAYER!r} via its WMTS provider", flush=True)

    # Render the layer exactly like the QGIS canvas does: the WMTS provider
    # fetches GetTile chunks and paints them. (Raw block.value() is useless
    # here: the provider exposes ARGB32 data whose packed bytes reinterpreted
    # as float are NaN.) A transparent background lets us count painted pixels.
    extent = layer.extent()
    print(f"OK: extent {extent.toString()}", flush=True)

    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setExtent(extent)
    settings.setOutputSize(QSize(256, 256))
    settings.setOutputDpi(96)
    settings.setBackgroundColor(QColor(0, 0, 0, 0))

    image = QImage(256, 256, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    job = QgsMapRendererCustomPainterJob(settings, painter)
    job.start()
    job.waitForFinished()
    painter.end()

    painted = sum(
        1
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).alpha() > 0
    )
    total = image.width() * image.height()
    print(
        f"OK: QGIS rendered the WMTS layer -> {painted}/{total} px painted "
        f"({100 * painted // total}%)",
        flush=True,
    )
    if painted * 20 < total:  # expect at least 5% real tile content
        print("FAIL: rendered tiles contain (almost) no data")
        return 1

    qgs.exitQgis()
    print(f"PASS: F.4 QGIS WMTS check succeeded for {SLUG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
