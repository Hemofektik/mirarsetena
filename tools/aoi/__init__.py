"""AOI geometry generator: derrotero + anchors -> canonical GeoJSON.

Output format is the project-geometry contract for the config registry
(aoi_path / pois_path): future SETENA projects skip this generator entirely
and bring their own GeoJSON.
"""
from __future__ import annotations

from pathlib import Path

from mirarsetena.aoi.anchor import load_reference, resolve
from mirarsetena.aoi.emit import (
    feature_collection,
    parcel_feature,
    poi_feature,
    to_wgs84,
    write_geojson,
)
from mirarsetena.aoi.traverse import compute, load_plans


def generate(project_dir: str | Path) -> dict[str, Path]:
    """Generate aoi.geojson and pois.geojson for one project directory.

    Runs the SCOPE §7 hard acceptance gates (closure < 2 m, area ±1 % of
    stated) before writing anything — failures raise, nothing is emitted.
    """
    project_dir = Path(project_dir)
    plans = load_plans(project_dir / "derrotero.yaml")
    reference = load_reference(project_dir / "reference.yaml")
    solution = resolve(plans, reference)

    for plan in plans:
        compute(plan.legs).check(plan.stated_area_m2)

    parcels = [
        parcel_feature(plan, to_wgs84(solution.vertices[plan.id]))
        for plan in plans
    ]
    pois = [poi_feature(poi) for poi in reference.pois.values()]

    aoi_path = project_dir / "aoi.geojson"
    pois_path = project_dir / "pois.geojson"
    write_geojson(aoi_path, feature_collection(parcels))
    write_geojson(pois_path, feature_collection(pois))
    return {"aoi": aoi_path, "pois": pois_path}
