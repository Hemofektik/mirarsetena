"""Layer style specifications (SCOPE R2-Q3 layer set).

Radar layers (sigma0, coherence) render as change-vs-baseline class LUTs by
default (SCOPE R3-Q3) and as percentile ramps in raw mode.
"""

CHANGE_LUT = {
    0: (215, 48, 39),   # strong decrease (darkening)
    1: (253, 174, 97),
    2: (254, 224, 144),
    3: (240, 240, 240),  # neutral
    4: (171, 217, 233),
    5: (116, 173, 209),
    6: (69, 117, 180),   # strong increase (brightening)
    255: None,           # unclassified -> transparent
}

STYLES = {
    "rgb": {"kind": "rgb"},
    "ndvi": {
        "kind": "ramp",
        "stops": [
            (-1.0, (149, 69, 12)),
            (-0.2, (215, 254, 174)),
            (0.1, (255, 255, 191)),
            (0.4, (166, 217, 106)),
            (0.8, (0, 104, 55)),
        ],
    },
    "mndwi": {
        "kind": "ramp",
        "stops": [(-1.0, (165, 0, 38)), (0.0, (247, 247, 247)), (1.0, (5, 112, 176))],
    },
    "bsi": {
        "kind": "ramp",
        "stops": [(-1.0, (165, 0, 38)), (0.0, (247, 247, 247)), (1.0, (5, 112, 176))],
    },
    "sigma0": {
        "kind": "ramp",
        "stops": [(-25.0, (15, 15, 15)), (-14.0, (120, 120, 120)), (-5.0, (240, 240, 240))],
        "change_lut": CHANGE_LUT,
    },
    "coherence": {
        "kind": "ramp",
        "stops": [(0.0, (15, 15, 15)), (0.5, (128, 128, 128)), (1.0, (250, 250, 250))],
        "change_lut": CHANGE_LUT,
    },
}

RADAR_LAYERS = {"sigma0", "coherence"}


def style_for(layer: str, *, mode: str = "change") -> dict:
    """Style for a layer; radar layers default to change-vs-baseline LUTs."""
    spec = STYLES[layer]
    if layer in RADAR_LAYERS and mode == "change":
        return {"kind": "lut", "lut": spec["change_lut"]}
    return spec
