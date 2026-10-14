"""Build geography data at runtime (docs/06 §2) into paths.geo_dir(). STUB - geography builder.

Runs on Railway (which has network access) at first boot and on admin request. Sources: Census TIGERweb
REST (county polygons; county subdivisions; incorporated places + CDPs), Census Gazetteer files as a
fallback for place centroids, and OpenStreetMap Overpass (LIRR stations + public landmarks).
Never fabricates coordinates: every record carries source + source_id from a fetched dataset.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


async def build_geography(http: httpx.AsyncClient, out_dir: Path, *, aliases_path: Path | None = None) -> dict[str, Any]:
    """Fetch, validate (every place inside the county polygons), and atomically write li_counties.geojson,
    nyc_li_counties.geojson, li_places.json and manifest.json. Returns the manifest
    {built_at, sources:{...}, counts:{kind:{county:n}}, dropped_outside:n}. Raises GeoBuildError on failure."""
    raise NotImplementedError


class GeoBuildError(RuntimeError):
    pass


def geo_files_present(out_dir: Path) -> bool:
    raise NotImplementedError
