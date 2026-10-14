"""Point-in-polygon geofence (docs/06 §1, V4).

Scope is decided ONLY by point-in-polygon against the Nassau and Suffolk county polygons built by
app.pipeline.geo.build (never by a bounding box or by the words "Long Island"). The Queens and Kings
polygons are loaded only to explain why an on-island point was refused.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Optional

from shapely.geometry import Point, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.prepared import PreparedGeometry, prep

try:  # shapely >= 2.0
    from shapely.validation import make_valid as _make_valid
except ImportError:  # pragma: no cover - shapely < 2
    _make_valid = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

IN_SCOPE_COUNTIES = ("Nassau", "Suffolk")
NYC_COUNTIES = ("Queens", "Kings")
_COUNTY_ALIASES = {
    "nassau": "Nassau", "nassau county": "Nassau", "36059": "Nassau",
    "suffolk": "Suffolk", "suffolk county": "Suffolk", "36103": "Suffolk",
    "queens": "Queens", "queens county": "Queens", "36081": "Queens",
    "kings": "Kings", "kings county": "Kings", "brooklyn": "Kings", "36047": "Kings",
}


def _county_name(props: dict[str, Any]) -> Optional[str]:
    """County name from a feature's properties: county | BASENAME | NAME | GEOID (tolerant)."""
    for key in ("county", "COUNTY_NAME", "BASENAME", "basename", "NAME", "name", "GEOID", "geoid"):
        val = props.get(key)
        if isinstance(val, str) and val.strip():
            hit = _COUNTY_ALIASES.get(val.strip().casefold())
            if hit:
                return hit
    return None


def _valid(geom: BaseGeometry) -> BaseGeometry:
    """A valid (Multi)Polygon: repaired if needed, with any stray lines/points from the repair dropped."""
    if not geom.is_valid:
        geom = _make_valid(geom) if _make_valid is not None else geom.buffer(0)
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in getattr(geom, "geoms", []) if g.geom_type in ("Polygon", "MultiPolygon")]
        geom = unary_union(polys) if polys else Polygon()
    return geom


def _ok_coord(lat: Any, lon: Any) -> bool:
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return math.isfinite(la) and math.isfinite(lo) and -90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0


def _round_coords(obj: Any, nd: int) -> Any:
    if isinstance(obj, (list, tuple)):
        if obj and all(isinstance(v, (int, float)) for v in obj):
            return [round(float(v), nd) for v in obj]
        return [_round_coords(v, nd) for v in obj]
    return obj


def polygons_from_feature_collection(fc: dict[str, Any], allowed: tuple[str, ...]) -> dict[str, BaseGeometry]:
    """{county name: (multi)polygon} for the features whose county is in `allowed` (several features per
    county are unioned). Non-polygon or unreadable features are skipped."""
    parts: dict[str, list[BaseGeometry]] = {}
    for feat in (fc or {}).get("features") or []:
        if not isinstance(feat, dict) or not isinstance(feat.get("geometry"), dict):
            continue
        name = _county_name(feat.get("properties") or {})
        if name not in allowed:
            continue
        try:
            geom = _valid(shape(feat["geometry"]))
        except Exception as exc:  # noqa: BLE001 - one broken feature must not stop loading
            logger.warning("geofence: unreadable %s geometry (%s)", name, type(exc).__name__)
            continue
        if geom.is_empty or geom.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        parts.setdefault(name, []).append(geom)
    out: dict[str, BaseGeometry] = {}
    for name, geoms in parts.items():
        merged = _valid(unary_union(geoms))
        if not merged.is_empty and merged.area > 0:
            out[name] = merged
    return out


class Geofence:
    """Prepared shapely polygons for Nassau and Suffolk (in scope) and Queens/Kings (explanations only)."""

    def __init__(self, counties: dict[str, BaseGeometry], nyc: Optional[dict[str, BaseGeometry]] = None):
        missing = [c for c in IN_SCOPE_COUNTIES if c not in counties]
        if missing:
            raise ValueError(f"geofence: missing county polygon(s): {', '.join(missing)}")
        self._counties: dict[str, BaseGeometry] = {c: counties[c] for c in IN_SCOPE_COUNTIES}
        self._prepared: dict[str, PreparedGeometry] = {c: prep(g) for c, g in self._counties.items()}
        self._nyc: dict[str, BaseGeometry] = {c: g for c, g in (nyc or {}).items() if c in NYC_COUNTIES}
        self._nyc_prepared: dict[str, PreparedGeometry] = {c: prep(g) for c, g in self._nyc.items()}
        self._geojson_cache: dict[float, dict[str, Any]] = {}

    # ------------------------------------------------------------------ loading
    @classmethod
    def from_feature_collections(cls, counties_fc: dict[str, Any],
                                 nyc_fc: Optional[dict[str, Any]] = None) -> "Geofence":
        return cls(polygons_from_feature_collection(counties_fc, IN_SCOPE_COUNTIES),
                   polygons_from_feature_collection(nyc_fc or {}, NYC_COUNTIES))

    @classmethod
    def load(cls, counties_path: Path, nyc_path: Optional[Path] = None) -> "Geofence":
        """Load li_counties.geojson (features with properties.county = 'Nassau'|'Suffolk') and optionally
        nyc_li_counties.geojson ('Queens'|'Kings'). Uses shapely prepared geometries."""
        counties_fc = json.loads(Path(counties_path).read_text(encoding="utf-8"))
        nyc_fc: Optional[dict[str, Any]] = None
        if nyc_path is not None:
            p = Path(nyc_path)
            if p.is_file():
                try:
                    nyc_fc = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    logger.warning("geofence: NYC county file unreadable (%s); explanations disabled",
                                   type(exc).__name__)
        return cls.from_feature_collections(counties_fc, nyc_fc)

    # ------------------------------------------------------------------ queries
    def county_at(self, lat: float, lon: float) -> Optional[str]:
        """'Nassau' | 'Suffolk' | None."""
        if not _ok_coord(lat, lon):
            return None
        pt = Point(float(lon), float(lat))
        for name in IN_SCOPE_COUNTIES:
            if self._prepared[name].covers(pt):
                return name
        return None

    def contains(self, lat: float, lon: float) -> bool:
        return self.county_at(lat, lon) is not None

    def nyc_county_at(self, lat: float, lon: float) -> Optional[str]:
        """'Queens' | 'Kings' | None (for explaining an outside_area drop)."""
        if not _ok_coord(lat, lon) or not self._nyc_prepared:
            return None
        pt = Point(float(lon), float(lat))
        for name in NYC_COUNTIES:
            prepared = self._nyc_prepared.get(name)
            if prepared is not None and prepared.covers(pt):
                return name
        return None

    def outside_reason(self, lat: float, lon: float) -> Optional[str]:
        """None when the point is in scope; otherwise a short explanation (no post text)."""
        if self.county_at(lat, lon) is not None:
            return None
        nyc = self.nyc_county_at(lat, lon)
        if nyc:
            return f"point in {nyc} County (New York City), outside Nassau/Suffolk"
        return "point outside the Nassau/Suffolk county polygons"

    def distance_m(self, lat: float, lon: float) -> Optional[float]:
        """Approximate distance in metres from the point to the nearest in-scope polygon (0 inside)."""
        if not _ok_coord(lat, lon):
            return None
        pt = Point(float(lon), float(lat))
        deg = min(g.distance(pt) for g in self._counties.values())
        return deg * 111_320.0 * max(0.2, math.cos(math.radians(float(lat))))

    def polygon(self, county: str) -> Optional[BaseGeometry]:
        return self._counties.get(county) or self._nyc.get(county)

    def bbox(self) -> tuple[float, float, float, float]:
        """(min_lon, min_lat, max_lon, max_lat) of Nassau ∪ Suffolk, for Nominatim viewbox hints only."""
        min_lon, min_lat, max_lon, max_lat = unary_union(list(self._counties.values())).bounds
        return (float(min_lon), float(min_lat), float(max_lon), float(max_lat))

    def to_geojson(self, simplify_tolerance: float = 0.0005) -> dict:
        """Simplified FeatureCollection of the two counties for the map (county outline glow)."""
        key = float(simplify_tolerance)
        cached = self._geojson_cache.get(key)
        if cached is None:
            feats = []
            for name in IN_SCOPE_COUNTIES:
                geom = self._counties[name]
                if key > 0:
                    simple = geom.simplify(key, preserve_topology=True)
                    geom = simple if not simple.is_empty else geom
                gj = mapping(geom)
                feats.append({
                    "type": "Feature",
                    "properties": {"county": name},
                    "geometry": {"type": gj["type"], "coordinates": _round_coords(gj["coordinates"], 5)},
                })
            cached = {"type": "FeatureCollection", "features": feats}
            self._geojson_cache[key] = cached
        return json.loads(json.dumps(cached))  # defensive copy
