"""Geocoding + snapping + geofence (docs/06 §3-4, R5, V4, V8). STUB - geography builder."""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from app.pipeline.geo.gazetteer import Gazetteer
from app.pipeline.geo.geofence import Geofence
from app.pipeline.geo.nominatim import NominatimClient
from app.pipeline.types import GeoResult, ItemContext

CacheGet = Callable[[str], Awaitable[Optional[dict[str, Any]]]]
CachePut = Callable[[str, dict[str, Any]], Awaitable[None]]


async def geocode(
    *,
    location_text: str,
    location_kind: str,
    is_private_residence: bool,
    post_text: str,
    context: ItemContext,
    gazetteer: Gazetteer,
    geofence: Geofence,
    nominatim: Optional[NominatimClient],
    cache_get: Optional[CacheGet] = None,
    cache_put: Optional[CachePut] = None,
) -> GeoResult:
    """Resolve the verified location span to a SNAPPED point (docs/06 §4 table):
      named public place -> gazetteer coordinate (precision public_place)
      business/intersection -> nearest gazetteer public place within 250 m, else Nominatim point rounded to
                               3 decimals (public_place); Nominatim result must be in Nassau/Suffolk by
                               address.county AND by polygon
      street-only or private residence (V8) -> hamlet/village centroid (precision hamlet)
      town only -> town centroid (precision town)
    Always finish with geofence.county_at(); set in_geofence and county from the POLYGON, never from text.
    Return GeoResult(source='none') when nothing resolves. Never raises."""
    raise NotImplementedError


async def town_for_self_report(*, post_text: str, context: ItemContext, gazetteer: Gazetteer) -> tuple[Optional[str], Optional[str]]:
    """Self-reports (R7): (town or hamlet name, county) from text/context; no coordinates are returned."""
    raise NotImplementedError
