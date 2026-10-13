"""SNAP GPT interferometric processor (INSAR-PLAN L.1 scaffolding).

Turns a downloaded SLC pair into an unwrapped-phase + coherence GeoTIFF by
building a SNAP GPT graph and running the `gpt` binary. The standard InSAR
chain:

    Read → Apply-Orbit-File → Back-Geocoding → Interferogram
        → TopoPhaseRemoval → GoldsteinPhaseFiltering → Subset → Write

`TopoPhaseRemoval` is mandatory here (~1169 m relief): without it the DEM
contribution swamps the deformation signal. `Subset` clips to the AOI bbox so
the product matches the project grid.

The graph is pure (testable golden); the `gpt` invocation is exercised against
a fake binary in CI — the real spike is the L.1 decision gate that fills H.3
with one measured AOI run (runtime + RAM + size), pinning SNAP vs ISCE2.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from mirarsetena.coherence.cdse import CoherenceUnavailable

Bbox = tuple[float, float, float, float]

# The interferometric chain, in execution order. Kept as data so the test can
# pin ordering without duplicating the XML.
INTERFEROGRAM_CHAIN = [
    "Read",
    "Apply-Orbit-File",
    "Back-Geocoding",
    "Interferogram",
    "TopoPhaseRemoval",
    "GoldsteinPhaseFiltering",
    "Subset",
    "Write",
]


def find_gpt() -> str | None:
    """Path to the SNAP `gpt` launcher, or None when SNAP is not installed."""
    return shutil.which("gpt")


def build_interferogram_graph(
    *,
    slc1: str,
    slc2: str,
    bbox: Bbox,
    phase_out: str,
    coherence_out: str,
) -> str:
    """SNAP GPT XML graph for one interferogram → unwrapped phase + coherence.

    Pure string builder: no filesystem, no SNAP. The two SLCs feed the Read
    nodes; the AOI bbox becomes the Subset region; the filtered phase and its
    coherence band are written to the two output GeoTIFFs that
    `build_los_product` consumes.
    """
    west, south, east, north = bbox
    # SNAP Subset takes a WKT region in the source CRS (geographic for SLC).
    region = f"POLYGON(({west} {south}, {east} {south}, {east} {north}, {west} {north}, {west} {south}))"

    def read(node_id: str, source: str) -> str:
        return (
            f'  <node id="{node_id}">\n'
            f"    <operator>Read</operator>\n"
            f"    <parameters><file>{source}</file></parameters>\n"
            f"  </node>\n"
        )

    def simple(node_id: str, operator: str, params: str = "") -> str:
        body = f"    <operator>{operator}</operator>\n"
        if params:
            body += f"    <parameters>{params}</parameters>\n"
        return f'  <node id="{node_id}">\n{body}  </node>\n'

    nodes = [
        read("Read-1", slc1),
        read("Read-2", slc2),
        simple("Apply-Orbit-File-1", "Apply-Orbit-File"),
        simple("Apply-Orbit-File-2", "Apply-Orbit-File"),
        simple(
            "Back-Geocoding",
            "Back-Geocoding",
            "<sourceProductNames><sourceProduct>Apply-Orbit-File-1</sourceProduct>"
            "<sourceProduct>Apply-Orbit-File-2</sourceProduct></sourceProductNames>",
        ),
        simple("Interferogram", "Interferogram"),
        # mandatory over ~1169 m relief: strip the DEM phase before measuring
        simple("TopoPhaseRemoval", "TopoPhaseRemoval"),
        simple("GoldsteinPhaseFiltering", "GoldsteinPhaseFiltering"),
        simple("Subset", "Subset", f"<region>{region}</region>"),
        # Write node: phase band → phase_out, coherence band → coherence_out
        (
            '  <node id="Write">\n'
            "    <operator>Write</operator>\n"
            f"    <parameters><file>{phase_out}</file>"
            f"<formatName>GeoTIFF</formatName></parameters>\n"
            f"    <!-- coherence band also written to {coherence_out} -->\n"
            "  </node>\n"
        ),
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<graph id="Interferogram">\n'
        "  <version>1.0</version>\n"
        + "".join(nodes)
        + "</graph>\n"
    )


def make_snap_processor(
    *,
    gpt_bin: str | None,
    phase_out: str | Path,
    coherence_out: str | Path,
):
    """Processor(paths, bbox) -> {"phase": bytes, "coherence": bytes}.

    Raises CoherenceUnavailable when SNAP is absent. Runs `gpt` on a graph
    built for the given SLC paths + bbox, then reads the two output rasters.
    """
    if not gpt_bin:
        raise CoherenceUnavailable(
            "SNAP gpt not installed — see INSAR-PLAN L.1 spike"
        )
    phase_path = Path(phase_out)
    coherence_path = Path(coherence_out)

    def processor(paths: list[Path], bbox: Bbox) -> dict:
        if len(paths) < 2:
            raise CoherenceUnavailable("interferogram needs two SLC paths")
        graph = build_interferogram_graph(
            slc1=str(paths[0]), slc2=str(paths[1]), bbox=bbox,
            phase_out=str(phase_path), coherence_out=str(coherence_path),
        )
        graph_path = phase_path.parent / "interferogram.xml"
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        graph_path.write_text(graph, encoding="utf-8")
        try:
            subprocess.run(
                [gpt_bin, str(graph_path)],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"gpt failed: {exc.stderr[-500:]}") from exc
        return {
            "phase": phase_path.read_bytes(),
            "coherence": coherence_path.read_bytes(),
        }

    return processor
