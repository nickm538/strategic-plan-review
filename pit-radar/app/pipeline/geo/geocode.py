"""Geocoding + snapping + geofence (docs/06 §3-4, R5, V4, V8).

The stored coordinate is never a raw geocode of a street address (R5):
  named public place          -> gazetteer coordinate of that place            precision public_place
  business / intersection     -> nearest gazetteer public place within 250 m, else the Nominatim point
                                 rounded to 3 decimals (~100 m)                  precision public_place
  street only / residence (V8)-> hamlet/village centroid                         precision hamlet
  hamlet/village named only   -> its centroid (docs/06 §3 step 5)                precision hamlet
  town only                   -> town centroid (feed only, never a pin)          precision town
County and in_geofence always come from the polygon, never from text. Logs carry no post text.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from app.pipeline import textnorm
from app.pipeline.geo.gazetteer import LOCALITY_KINDS, Gazetteer, PlaceRec
from app.pipeline.geo.geofence import Geofence
from app.pipeline.geo.nominatim import NominatimClient
from app.pipeline.types import GeoResult, ItemContext

logger = logging.getLogger(__name__)

CacheGet = Callable[[str], Awaitable[Optional[dict[str, Any]]]]
CachePut = Callable[[str, dict[str, Any]], Awaitable[None]]

SNAP_PUBLIC_PLACE_M = 250.0          # docs/06 §4: nearest gazetteer public place within 250 m
ROUND_DECIMALS = 3                   # docs/06 §4: otherwise round the Nominatim point to 3 decimals
CACHE_DECIMALS = 4                   # geocode_cache keeps ~11 m precision, never the raw point
AREA_LABEL_M = 5000.0                # nearest hamlet used to label a rounded point
CACHE_PREFIX = "nominatim:"

_LEAD_FILLER = re.compile(
    r"^(?:(?:the|a|an|at|in|on|near|by|behind|outside|inside|across|from|front|of|next|to|beside|under|"
    r"underneath|around|off|along|over|corner|right|just|area|spot)\b[\s,]*)+", re.I)
_TRAIL_FILLER = re.compile(r"(?:[\s,]+(?:in|at|on|near|by|of|the|area|off|around))+\s*$", re.I)


def _none(outside_reason: Optional[str] = None) -> GeoResult:
    return GeoResult(lat=None, lon=None, county=None, precision=None, source="none", in_geofence=False,
                     outside_reason=outside_reason)


def _town_name(rec: PlaceRec) -> Optional[str]:
    if rec.kind in ("town", "city"):
        return rec.name
    return rec.town or (rec.name if rec.kind in LOCALITY_KINDS else None)


def _label(rec: PlaceRec, precision: str) -> str:
    if precision == "town" and rec.kind == "town":
        return f"Town of {rec.name}"
    return rec.label


def _from_place(rec: PlaceRec, precision: str, source: str) -> GeoResult:
    return GeoResult(lat=float(rec.lat), lon=float(rec.lon), county=None, precision=precision,  # type: ignore[arg-type]
                     place_id=rec.id, place_label=_label(rec, precision), town=_town_name(rec),
                     source=source, in_geofence=False)  # type: ignore[arg-type]


def _finish(res: GeoResult, geofence: Geofence) -> GeoResult:
    """County and in_geofence from the polygon (V4)."""
    if res.lat is None or res.lon is None:
        return res
    county = geofence.county_at(res.lat, res.lon)
    res.county = county  # type: ignore[assignment]
    res.in_geofence = county is not None
    if county is None:
        res.outside_reason = geofence.outside_reason(res.lat, res.lon)
    return res


def _context_text(post_text: str, context: ItemContext) -> str:
    parts = [context.platform_place_tag or "", context.query_place_name or "", context.parent_title or "",
             (context.subreddit or "").replace("_", " "), post_text or ""]
    return "\n".join(p for p in parts if p)


def _hamlet_from(gazetteer: Gazetteer, place: Optional[PlaceRec], loc: str, ctx: str) -> Optional[PlaceRec]:
    """The village/CDP/city (or town) for a hamlet-level result: from the matched place, else the first
    one named in the location span, else in the post/context."""
    if place is not None:
        hit = gazetteer.containing_locality(place) if place.is_public_place else place
        if hit is not None:
            return hit
    for text, strict in ((loc, False), (ctx, True)):
        found = gazetteer.mentioned_localities(text, strict=strict) if text else []
        locs = [p for p in found if p.kind in LOCALITY_KINDS]
        if locs:
            return locs[0]
        towns = [p for p in found if p.kind == "town"]
        if towns:
            return towns[0]
    return None


def _hamlet_result(rec: Optional[PlaceRec], source: str) -> Optional[GeoResult]:
    if rec is None:
        return None
    if rec.kind == "town":
        return _from_place(rec, "town", source)
    if rec.is_public_place:
        return None
    return _from_place(rec, "hamlet", source)


def nominatim_query(location_text: str, hamlet: Optional[str]) -> str:
    """'<what> , <hamlet>, NY' with leading filler words and the hamlet name itself removed from <what>."""
    t = " ".join((location_text or "").split())
    if hamlet:
        t = re.sub(rf"(?<!\w){re.escape(hamlet)}(?!\w)", " ", t, flags=re.I)
    t = re.sub(r"\b(?:NY|N\.Y\.|New York|Long Island|LI)\b", " ", t, flags=re.I)
    t = " ".join(t.split()).strip(" ,.;:-")
    prev = None
    while t and t != prev:
        prev = t
        t = _LEAD_FILLER.sub("", t).strip(" ,.;:-")
        t = _TRAIL_FILLER.sub("", t).strip(" ,.;:-")
    if len(re.sub(r"\W", "", t)) < 3:
        return ""
    return ", ".join(x for x in (t, hamlet, "NY") if x)


def _cache_key(q: str) -> str:
    return CACHE_PREFIX + " ".join(textnorm.norm_for_match(q).split())[:400]


async def _cache_get(cache_get: Optional[CacheGet], key: str) -> Optional[dict[str, Any]]:
    if cache_get is None:
        return None
    try:
        got = await cache_get(key)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a cache failure must not stop geocoding
        logger.warning("geocode: cache read failed (%s)", type(exc).__name__)
        return None
    return got if isinstance(got, dict) else None


async def _cache_put(cache_put: Optional[CachePut], key: str, value: dict[str, Any]) -> None:
    if cache_put is None:
        return
    try:
        await cache_put(key, value)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("geocode: cache write failed (%s)", type(exc).__name__)


def _accept(result: dict[str, Any], geofence: Geofence) -> tuple[Optional[tuple[float, float, str]], Optional[str]]:
    """(lat, lon, county) when the Nominatim result is in Nassau/Suffolk by address.county AND by polygon;
    otherwise (None, reason)."""
    try:
        lat, lon = float(result["lat"]), float(result["lon"])
    except (KeyError, TypeError, ValueError):
        return None, None
    addr_county = str((result.get("address") or {}).get("county") or "")
    poly = geofence.county_at(lat, lon)
    named = addr_county.casefold()
    if poly is not None and ("nassau" in named or "suffolk" in named):
        return (lat, lon, poly), None
    nyc = geofence.nyc_county_at(lat, lon)
    if nyc:
        return None, f"nominatim result in {nyc} County (New York City)"
    if poly is None:
        return None, "nominatim result outside the Nassau/Suffolk polygons"
    return None, "nominatim county mismatch"


async def _nominatim_point(loc: str, hamlet: Optional[PlaceRec], *, geofence: Geofence,
                           nominatim: Optional[NominatimClient], cache_get: Optional[CacheGet],
                           cache_put: Optional[CachePut]) -> tuple[Optional[tuple[float, float, str]], Optional[str]]:
    q = nominatim_query(loc, hamlet.name if hamlet is not None else None)
    if not q:
        return None, None
    key = _cache_key(q)
    cached = await _cache_get(cache_get, key)
    if cached is not None:
        if cached.get("miss"):
            return None, cached.get("reason") if cached.get("reason") not in (None, "no_result") else None
        try:
            lat, lon = float(cached["lat"]), float(cached["lon"])
        except (KeyError, TypeError, ValueError):
            cached = None
        else:
            county = geofence.county_at(lat, lon)
            if county is not None:
                return (lat, lon, county), None
            return None, geofence.outside_reason(lat, lon)
    if nominatim is None or not nominatim.enabled:
        return None, None
    results, ok = await nominatim.search_with_status(q, viewbox=geofence.bbox())
    accepted: Optional[tuple[float, float, str]] = None
    refused: Optional[str] = None
    for r in results:
        got, why = _accept(r, geofence)
        if got is not None:
            accepted = got
            break
        if refused is None and why:
            refused = why
    if ok:
        if accepted is not None:
            await _cache_put(cache_put, key, {"source": "nominatim", "lat": round(accepted[0], CACHE_DECIMALS),
                                              "lon": round(accepted[1], CACHE_DECIMALS), "county": accepted[2]})
        else:
            await _cache_put(cache_put, key, {"miss": True, "reason": refused or "no_result"})
    return accepted, (None if accepted is not None else refused)


def _snap_point(lat: float, lon: float, gazetteer: Gazetteer, geofence: Geofence) -> Optional[GeoResult]:
    """Business/intersection snapping (docs/06 §4)."""
    near = gazetteer.nearest_public_place(lat, lon, SNAP_PUBLIC_PLACE_M)
    if near is not None:
        return _from_place(near, "public_place", "nominatim")
    rlat, rlon = round(lat, ROUND_DECIMALS), round(lon, ROUND_DECIMALS)
    if geofence.county_at(rlat, rlon) is None:
        return None
    area = gazetteer.nearest_locality(rlat, rlon, AREA_LABEL_M)
    return GeoResult(lat=rlat, lon=rlon, county=None, precision="public_place", place_id=None,
                     place_label=f"{area.name} area" if area is not None else None,
                     town=_town_name(area) if area is not None else None, source="nominatim", in_geofence=False)


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
    try:
        res = await _geocode(location_text=location_text, location_kind=location_kind,
                             is_private_residence=is_private_residence, post_text=post_text, context=context,
                             gazetteer=gazetteer, geofence=geofence, nominatim=nominatim,
                             cache_get=cache_get, cache_put=cache_put)
        return _finish(res, geofence)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - never raises
        logger.warning("geocode: failed (%s)", type(exc).__name__)
        return _none()


async def _geocode(*, location_text: str, location_kind: str, is_private_residence: bool, post_text: str,
                   context: ItemContext, gazetteer: Gazetteer, geofence: Geofence,
                   nominatim: Optional[NominatimClient], cache_get: Optional[CacheGet],
                   cache_put: Optional[CachePut]) -> GeoResult:
    loc = textnorm.clean(location_text or "")
    if not loc or gazetteer is None or geofence is None:
        return _none()
    if context is None:
        context = ItemContext(query="")
    kind = (location_kind or "none").strip()
    ctx = _context_text(post_text, context)
    place = gazetteer.match_location(loc, context_text=ctx, query_place_id=context.query_place_id)

    # Private residence (V8) or street only: hamlet/village centroid, never Nominatim, never a precise pin.
    if is_private_residence or kind == "street":
        return _hamlet_result(_hamlet_from(gazetteer, place, loc, ctx), "hamlet_fallback") or _none()

    if place is not None:
        if place.is_public_place:
            return _from_place(place, "public_place", "gazetteer")
        if kind == "town_only" or place.kind == "town":
            return _hamlet_result(place, "gazetteer") or _none()
        if kind in ("named_place", "none") and not gazetteer.unmatched_words(loc):
            return _hamlet_result(place, "gazetteer") or _none()
        # else: a business/intersection, or a name the gazetteer does not know, inside a known hamlet
    elif kind == "town_only":
        return _none()          # a town/hamlet name that is not in the Nassau/Suffolk gazetteer

    # Business, intersection, or a name the gazetteer does not know: bounded Nominatim, then snap.
    hamlet = place if place is not None else _hamlet_from(gazetteer, None, loc, ctx)
    point_hamlet = hamlet if hamlet is not None and hamlet.kind != "town" else None
    point, refused = await _nominatim_point(loc, point_hamlet or hamlet, geofence=geofence, nominatim=nominatim,
                                            cache_get=cache_get, cache_put=cache_put)
    if point is not None:
        snapped = _snap_point(point[0], point[1], gazetteer, geofence)
        if snapped is not None:
            return snapped
    fallback = _hamlet_result(hamlet, "hamlet_fallback")
    if fallback is not None:
        return fallback
    return _none(refused)


async def town_for_self_report(*, post_text: str, context: ItemContext, gazetteer: Gazetteer) -> tuple[Optional[str], Optional[str]]:
    """Self-reports (R7): (town or hamlet name, county) from text/context; no coordinates are returned."""
    try:
        if gazetteer is None:
            return None, None
        for text in (post_text, context.platform_place_tag, context.parent_title):
            if not text:
                continue
            found = gazetteer.mentioned_localities(text, strict=True)
            locs = [p for p in found if p.kind in LOCALITY_KINDS]
            pick = locs[0] if locs else next((p for p in found if p.kind == "town"), None)
            if pick is not None:
                return pick.name, pick.county
        return None, None
    except Exception as exc:  # noqa: BLE001 - never raises
        logger.warning("geocode: self-report town lookup failed (%s)", type(exc).__name__)
        return None, None


__all__ = ["geocode", "town_for_self_report", "nominatim_query"]
