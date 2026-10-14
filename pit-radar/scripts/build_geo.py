"""Build the geography data (docs/06 §2) and print the manifest.

The app builds geography by itself at first boot on Railway; run this to rebuild by hand or to check the
sources. It needs network access to tigerweb.geo.census.gov, www2.census.gov and an Overpass mirror.

Usage (from the pit-radar folder):
    python scripts/build_geo.py                  # writes into <data dir>/geo (PIT_DATA_DIR, /data or ./var)
    python scripts/build_geo.py --out /tmp/geo   # writes somewhere else
Exit code 1 when a required source fails (counties, towns or villages/hamlets).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.paths import REPO_DATA_DIR, geo_dir  # noqa: E402
from app.pipeline.geo.build import GeoBuildError, build_geography  # noqa: E402
from app.pipeline.geo.gazetteer import ALIASES_FILE  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="build_geo.py", description="Build PIT Radar geography data.")
    p.add_argument("--out", type=Path, default=None, help="output folder (default: <data dir>/geo)")
    p.add_argument("--aliases", type=Path, default=REPO_DATA_DIR / ALIASES_FILE,
                   help="curated alias groups (default: data/aliases.yaml)")
    p.add_argument("--quiet", action="store_true", help="only print the manifest")
    return p


async def _run(out: Path, aliases: Path) -> dict:
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(120.0, connect=20.0)) as http:
        return await build_geography(http, out, aliases_path=aliases)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    out = args.out if args.out is not None else geo_dir()
    try:
        manifest = asyncio.run(_run(out, args.aliases))
    except GeoBuildError as exc:
        print(f"geography build failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, indent=2, sort_keys=True))
    print(f"written to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
