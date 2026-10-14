"""Point-in-polygon geofence (docs/06 §1, V4). STUB - geography builder."""
from __future__ import annotations

from pathlib import Path
from typing import Optional


class Geofence:
    @classmethod
    def load(cls, counties_path: Path, nyc_path: Optional[Path] = None) -> "Geofence":
        """Load li_counties.geojson (features with properties.county = 'Nassau'|'Suffolk') and optionally
        nyc_li_counties.geojson ('Queens'|'Kings'). Uses shapely prepared geometries."""
        raise NotImplementedError

    def county_at(self, lat: float, lon: float) -> Optional[str]:
        """'Nassau' | 'Suffolk' | None."""
        raise NotImplementedError

    def nyc_county_at(self, lat: float, lon: float) -> Optional[str]:
        """'Queens' | 'Kings' | None (for explaining an outside_area drop)."""
        raise NotImplementedError

    def bbox(self) -> tuple[float, float, float, float]:
        """(min_lon, min_lat, max_lon, max_lat) of Nassau ∪ Suffolk, for Nominatim viewbox hints only."""
        raise NotImplementedError

    def to_geojson(self, simplify_tolerance: float = 0.0005) -> dict:
        """Simplified FeatureCollection of the two counties for the map (county outline glow)."""
        raise NotImplementedError
