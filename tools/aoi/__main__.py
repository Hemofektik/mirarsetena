"""CLI: python -m tools.aoi [--project-dir DIR]"""
from __future__ import annotations

import argparse
from pathlib import Path

from tools.aoi import generate


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate canonical AOI GeoJSON for a Mirar Setena project"
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path("data/projects/cdp-rio-general"),
        help="project directory containing derrotero.yaml and reference.yaml",
    )
    args = parser.parse_args()
    for name, path in generate(args.project_dir).items():
        print(f"wrote {name}: {path}")


if __name__ == "__main__":
    main()
