"""Filesystem locations used across the app.

APP_ROOT is the pit-radar/ folder (the Railway service root). Read-only inputs (config, prompts,
schemas, static files) live under it. Writable runtime data (SQLite file, built geography) lives
under DATA_DIR, which is the Railway volume `/data` in production.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = APP_ROOT / "config"
PROMPTS_DIR = APP_ROOT / "prompts"
SCHEMAS_DIR = APP_ROOT / "schemas"
STATIC_DIR = APP_ROOT / "static"
REPO_DATA_DIR = APP_ROOT / "data"          # committed, hand-curated inputs (e.g. aliases.yaml)


def data_dir() -> Path:
    """Writable data directory: $PIT_DATA_DIR, else /data if it exists, else pit-radar/var."""
    env = os.environ.get("PIT_DATA_DIR")
    if env:
        p = Path(env)
    elif Path("/data").is_dir() and os.access("/data", os.W_OK):
        p = Path("/data")
    else:
        p = APP_ROOT / "var"
    p.mkdir(parents=True, exist_ok=True)
    return p


def geo_dir() -> Path:
    """Where built geography files live: <data_dir>/geo (built at runtime by app.pipeline.geo.build)."""
    p = data_dir() / "geo"
    p.mkdir(parents=True, exist_ok=True)
    return p


# File names inside geo_dir()
COUNTIES_FILE = "li_counties.geojson"          # Nassau + Suffolk polygons (in scope)
NYC_COUNTIES_FILE = "nyc_li_counties.geojson"  # Queens + Kings polygons (negative tests / exclusion)
PLACES_FILE = "li_places.json"                 # gazetteer
GEO_MANIFEST_FILE = "manifest.json"            # sources, retrieval time, counts
