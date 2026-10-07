"""Minimal OGC WMTS 1.0.0 GetCapabilities document builder.

One WMTS layer per (layer, date) — the layer name encodes both
("ndvi_2026-07-03"), which keeps the Time dimension out of v1 and matches
the XYZ route naming exactly.
"""
from __future__ import annotations

WEB_MERCATOR_LIMIT = 20037508.342789244
SCALE_DENOMINATOR_0 = 559082264.0287178
WGS84_PIXEL = 0.00028  # nominal OGC raster resolution (degrees)


def _tile_matrix_set(max_zoom: int) -> str:
    rows = []
    for z in range(max_zoom + 1):
        size = 2**z
        scale = SCALE_DENOMINATOR_0 / (2**z)
        rows.append(
            "      <TileMatrix>\n"
            f"        <ows:Identifier>{z}</ows:Identifier>\n"
            f"        <ScaleDenominator>{scale:.10f}</ScaleDenominator>\n"
            f"        <TopLeftCorner>-{WEB_MERCATOR_LIMIT} {WEB_MERCATOR_LIMIT}</TopLeftCorner>\n"
            "        <TileWidth>256</TileWidth>\n"
            "        <TileHeight>256</TileHeight>\n"
            f"        <MatrixWidth>{size}</MatrixWidth>\n"
            f"        <MatrixHeight>{size}</MatrixHeight>\n"
            "      </TileMatrix>"
        )
    return (
        "    <TileMatrixSet>\n"
        "      <ows:Identifier>GoogleMapsCompatible</ows:Identifier>\n"
        "      <ows:SupportedCRS>urn:ogc:def:crs:EPSG::3857</ows:SupportedCRS>\n"
        + "\n".join(rows)
        + "\n    </TileMatrixSet>"
    )


def _layer(name: str, endpoint: str) -> str:
    template = (
        f"{endpoint}?SERVICE=WMTS&amp;REQUEST=GetTile&amp;TILEMATRIXSET=GoogleMapsCompatible"
        f"&amp;LAYER={name}&amp;TILEMATRIX={{TileMatrix}}&amp;TILEROW={{TileRow}}"
        "&amp;TILECOL={{TileCol}}"
    )
    return (
        "    <Layer>\n"
        f"      <ows:Identifier>{name}</ows:Identifier>\n"
        f"      <ows:Title>{name}</ows:Title>\n"
        '      <Style isDefault="true"><ows:Identifier>default</ows:Identifier></Style>\n'
        "      <Format>image/png</Format>\n"
        "      <TileMatrixSet>GoogleMapsCompatible</TileMatrixSet>\n"
        f'      <ResourceURL format="image/png" resourceType="tile" template="{template}"/>\n'
        "    </Layer>"
    )


def build_capabilities(layer_names: list[str], max_zoom: int, endpoint: str) -> bytes:
    layers = "\n".join(_layer(name, endpoint) for name in layer_names)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Capabilities xmlns="http://www.opengis.net/wmts/1.0"\n'
        '              xmlns:ows="http://www.opengis.net/ows/1.1"\n'
        '              xmlns:xlink="http://www.w3.org/1999/xlink"\n'
        '              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        '              version="1.0.0">\n'
        "  <ows:ServiceMetadata>\n"
        "    <ows:OperationsMetadata>\n"
        '      <ows:Operation name="GetCapabilities">\n'
        "        <ows:DCP><ows:HTTP><ows:Get xlink:href=\""
        f"{endpoint}?SERVICE=WMTS&amp;REQUEST=GetCapabilities"
        '"/></ows:HTTP></ows:DCP>\n'
        "      </ows:Operation>\n"
        '      <ows:Operation name="GetTile">\n'
        "        <ows:DCP><ows:HTTP><ows:Get xlink:href=\""
        f"{endpoint}?SERVICE=WMTS&amp;REQUEST=GetTile"
        '"/></ows:HTTP></ows:DCP>\n'
        "      </ows:Operation>\n"
        "    </ows:OperationsMetadata>\n"
        "  </ows:ServiceMetadata>\n"
        "  <Contents>\n"
        f"{layers}\n"
        f"{_tile_matrix_set(max_zoom)}\n"
        "  </Contents>\n"
        "</Capabilities>\n"
    )
    return xml.encode("utf-8")
