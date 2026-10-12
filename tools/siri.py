"""Official cadastral perimeter fetcher — SNITCR SIRI API.

Documented recipe (generic for any SETENA project, SCOPE/IMPLEMENTATION):

1. Identify the project's **plano ids** — the 1991 plan numbers from the
   resolution/derrotero, prefixed with ``10``:
   ``SJ-980860-1991`` -> ``109808601991``.
2. ``GET https://www.snitcr.go.cr/Visor/services/siri?tipoquery=plano&plano=<id>``
   returns JSON ``{result, data: {zona_1: FeatureCollection, zona_2: ...}}``
   with cadastral geometry in **CRTM05 (EPSG:5367)** plus registry
   properties (finca, shape_area, plano, ...).
3. Take the first feature of the first non-empty zone, reproject the
   rings to WGS84 (EPSG:4326) and store as ``official.geojson`` next to
   the project's other geometry.

CLI: ``python -m tools.siri [--project-dir DIR]`` fetches every plan in
the project's derrotero.yaml and writes ``official.geojson``.
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

from pyproj import Transformer

SIRI_URL = "https://www.snitcr.go.cr/Visor/services/siri"
CRTM05 = "EPSG:5367"
WGS84 = "EPSG:4326"

_TO_WGS84 = Transformer.from_crs(CRTM05, WGS84, always_xy=True)


class SiriError(RuntimeError):
    """The SIRI API answered but not with usable geometry."""


def plano_id(plan_id: str) -> str:
    """Derrotero plan id -> SIRI plano id: ``SJ-980860-1991`` -> ``109808601991``."""
    digits = "".join(ch for ch in plan_id if ch.isdigit())
    if len(digits) != 10:
        raise SiriError(f"cannot derive a plano id from {plan_id!r}")
    return "10" + digits


def fetch_plano(plano: str, opener=None) -> dict:
    """GET the SIRI payload for one plano id (injectable opener for tests)."""
    url = f"{SIRI_URL}?tipoquery=plano&plano={plano}"
    if opener is None:
        with urllib.request.urlopen(url, timeout=60) as response:
            raw = response.read()
    else:
        raw = opener(url)
    payload = json.loads(raw)
    if not payload.get("result"):
        raise SiriError(f"SIRI returned result=false for plano {plano}")
    return payload


def extract_feature(payload: dict, plano: str) -> dict:
    """First feature of the first non-empty zone, preferring its own plano tag."""
    data = payload.get("data") or {}
    for zone in sorted(data):  # zona_1 first (alphabetical works: zona_1 < zona_2)
        features = (data[zone] or {}).get("features") or []
        if not features:
            continue
        for feature in features:
            if (feature.get("properties") or {}).get("plano") == plano:
                return feature
        return features[0]
    raise SiriError(f"SIRI returned no features for plano {plano}")


def to_wgs84_feature(feature: dict) -> dict:
    """Reproject a CRTM05 feature's rings to WGS84, keeping properties."""
    geometry = feature["geometry"]
    gtype = geometry["type"]

    def ring(points):
        return [list(_TO_WGS84.transform(x, y)) for x, y in points]

    if gtype == "MultiPolygon":
        coords = [[ring(r) for r in poly] for poly in geometry["coordinates"]]
    elif gtype == "Polygon":
        coords = [ring(r) for r in geometry["coordinates"]]
    else:
        raise SiriError(f"unsupported SIRI geometry {gtype!r}")
    return {
        "type": "Feature",
        "properties": {
            **feature.get("properties", {}),
            "source": "snitcr-siri",
        },
        "geometry": {"type": gtype, "coordinates": coords},
    }


def fetch_official_perimeter(project_dir: str | Path, opener=None) -> Path:
    """Fetch every derrotero plan's official perimeter -> official.geojson."""
    import yaml

    project_dir = Path(project_dir)
    plans = yaml.safe_load((project_dir / "derrotero.yaml").read_text(encoding="utf-8"))
    features = []
    for plan in plans["plans"]:
        pid = plan["id"]
        payload = fetch_plano(plano_id(pid), opener=opener)
        feature = to_wgs84_feature(extract_feature(payload, plano_id(pid)))
        feature["properties"]["plan_id"] = pid
        features.append(feature)
    out = project_dir / "official.geojson"
    out.write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": features},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch official cadastral perimeters from the SNITCR SIRI API"
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path("data/projects/cdp-rio-general"),
        help="project directory containing derrotero.yaml",
    )
    args = parser.parse_args()
    path = fetch_official_perimeter(args.project_dir)
    print(f"wrote official perimeter: {path}")


if __name__ == "__main__":
    main()