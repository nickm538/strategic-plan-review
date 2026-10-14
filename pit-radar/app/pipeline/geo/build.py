"""Build geography data at runtime (docs/06 §2) into paths.geo_dir().

Runs on Railway (which has network access) at first boot and on admin request. Sources: Census TIGERweb
REST (county polygons; county subdivisions; incorporated places + CDPs), Census Gazetteer files as a
fallback for place centroids, and OpenStreetMap Overpass (LIRR stations + public landmarks).
Never fabricates coordinates: every record carries source + source_id from a fetched dataset.

Required: the Nassau/Suffolk county polygons, towns/cities and villages/CDPs (else GeoBuildError).
Optional: Queens/Kings polygons, LIRR stations and landmarks (status 'degraded' when they fail).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
import math
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
from shapely.geometry import Point, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.prepared import PreparedGeometry, prep

from app.paths import COUNTIES_FILE, GEO_MANIFEST_FILE, NYC_COUNTIES_FILE, PLACES_FILE, REPO_DATA_DIR
from app.pipeline.geo.gazetteer import (
    ALIASES_FILE,
    LANDMARK_KINDS,
    PLACES_FORMAT,
    STATION_KIND,
    Gazetteer,
    PlaceRec,
    apply_aliases,
    distance_m,
    kind_rank,
    load_alias_groups,
    normkey,
)
from app.pipeline.geo.geofence import Geofence

logger = logging.getLogger(__name__)

MANIFEST_FORMAT = "pit-radar-geo-manifest/1"

# --- Census TIGERweb (layer ids are discovered by NAME from the service directory) -------------------
TIGERWEB_BASE = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
STATE_COUNTY_SERVICE = "State_County"
PLACES_SERVICE = "Places_CouSub_ConCity_SubMCD"
COUNTIES_LAYER = ("Counties",)
COUSUB_LAYER = ("County Subdivisions",)
INCORPORATED_LAYER = ("Incorporated Places",)
CDP_LAYER = ("Census Designated Places",)
# Used only if the service directory cannot be read. Checked against the live directory on 2026-10-14
# (State_County: Counties=1; Places_CouSub_ConCity_SubMCD: County Subdivisions=1, Incorporated Places=4,
# Census Designated Places=5). [verify] if TIGERweb is reorganized.
FALLBACK_LAYER_IDS = {
    (STATE_COUNTY_SERVICE, "counties"): 1,
    (PLACES_SERVICE, "county subdivisions"): 1,
    (PLACES_SERVICE, "incorporated places"): 4,
    (PLACES_SERVICE, "census designated places"): 5,
}
STATE_FIPS = "36"
IN_SCOPE_COUNTY_CODES = {"059": "Nassau", "103": "Suffolk"}
NYC_COUNTY_CODES = {"081": "Queens", "047": "Kings"}
COUSUB_KIND_BY_LSADC = {"43": "town", "25": "city"}
INCORPORATED_KIND_BY_LSADC = {"47": "village", "25": "city"}
CDP_KIND_BY_LSADC = {"57": "cdp"}
COUNTY_OFFSET_DEG = 0.0002          # docs/06 §2.1: simplify with <= 0.0002 deg (~20 m)
PLACE_OFFSET_DEG = 0.0005
GEOMETRY_PRECISION = 6
PAGE_SIZE = 1000
MAX_PAGES = 60

# --- Census Gazetteer fallback (place centroids only; no polygons) -----------------------------------
# File names seen in the 2024 directory listing on 2026-10-14 were lower-case ("2024_gaz_place_36.txt");
# the capitalized and zipped variants are tried too. [verify]
GAZETTEER_BASE = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/"
GAZETTEER_PLACE_FILES = ("2024_gaz_place_36.txt", "2024_Gaz_place_36.txt", "2024_Gaz_place_36.zip",
                         "2024_Gaz_place_national.zip")
GAZETTEER_COUSUB_FILES = ("2024_gaz_cousubs_36.txt", "2024_Gaz_cousubs_36.txt", "2024_Gaz_cousubs_36.zip",
                          "2024_Gaz_cousubs_national.zip")
PLACE_KIND_BY_LSAD = {"47": "village", "57": "cdp", "25": "city"}

# --- OpenStreetMap Overpass ------------------------------------------------------------------------------
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
OVERPASS_TIMEOUT_S = 180
_LIRR_RE = re.compile(r"long\s+island\s+rail\s*road|\blirr\b", re.I)
_STATION_SUFFIX_RE = re.compile(r"\s*(?:\(LIRR\)|LIRR\s+station|LIRR|railroad\s+station|train\s+station|station)\s*$", re.I)
# Only these OSM tags are read (allow-list).
_OSM_TAGS = ("name", "alt_name", "official_name", "short_name", "railway", "network", "operator", "leisure",
             "natural", "shop", "amenity", "public_transport", "bus", "access")

# --- HTTP ------------------------------------------------------------------------------------------------
TIGER_TIMEOUT = httpx.Timeout(90.0, connect=20.0)
FILE_TIMEOUT = httpx.Timeout(120.0, connect=20.0)
OVERPASS_HTTP_TIMEOUT = httpx.Timeout(OVERPASS_TIMEOUT_S + 30.0, connect=20.0)
RETRY_DELAYS = (3.0, 10.0)          # between attempts; tests set these to 0
USER_AGENT_BASE = "PIT-Radar/1.0 (Long Island Coalition for the Homeless outreach; geography build"

TOWN_SNAP_M = 3000.0                # a point just outside every town polygon (shoreline, reservation)
PARENT_SNAP_M = 500.0               # a landmark just outside its village/CDP polygon
PARENT_POINT_SNAP_M = 2000.0        # fallback when only centroids (Gazetteer files) are available
DEDUPE_M = 1500.0


class GeoBuildError(RuntimeError):
    pass


class _SourceError(Exception):
    """One source failed (message is safe to show: no secrets, no post text)."""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
async def _request(http: httpx.AsyncClient, method: str, url: str, *, ua: str, timeout: httpx.Timeout,
                   params: Optional[dict[str, str]] = None, data: Optional[dict[str, str]] = None,
                   attempts: int = 3) -> httpx.Response:
    headers = {"User-Agent": ua, "Accept": "application/json, text/plain, */*"}
    last = "no attempt made"
    for i in range(max(1, attempts)):
        try:
            resp = await http.request(method, url, params=params, data=data, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            last = f"network error ({type(exc).__name__})"
        else:
            if resp.status_code < 400:
                return resp
            last = f"HTTP {resp.status_code}"
            if resp.status_code not in (408, 425, 429, 500, 502, 503, 504):
                break
        if i + 1 < attempts:
            await asyncio.sleep(RETRY_DELAYS[min(i, len(RETRY_DELAYS) - 1)] if RETRY_DELAYS else 0)
    raise _SourceError(f"{_host(url)}: {last}")


def _host(url: str) -> str:
    try:
        return httpx.URL(url).host or url
    except Exception:  # noqa: BLE001
        return url


async def _get_json(http: httpx.AsyncClient, url: str, *, ua: str, params: dict[str, str],
                    timeout: httpx.Timeout = TIGER_TIMEOUT) -> dict[str, Any]:
    resp = await _request(http, "GET", url, ua=ua, timeout=timeout, params=params)
    try:
        data = resp.json()
    except ValueError as exc:
        raise _SourceError(f"{_host(url)}: invalid JSON") from exc
    if not isinstance(data, dict):
        raise _SourceError(f"{_host(url)}: unexpected response")
    err = data.get("error")
    if isinstance(err, dict):          # ArcGIS reports errors with HTTP 200
        raise _SourceError(f"{_host(url)}: ArcGIS error {err.get('code')}: {str(err.get('message'))[:120]}")
    return data


# ---------------------------------------------------------------------------
# TIGERweb
# ---------------------------------------------------------------------------
async def discover_layers(http: httpx.AsyncClient, service: str, *, ua: str) -> list[dict[str, Any]]:
    data = await _get_json(http, f"{TIGERWEB_BASE}{service}/MapServer", ua=ua, params={"f": "json"})
    layers = data.get("layers")
    if not isinstance(layers, list):
        raise _SourceError(f"TIGERweb {service}: no layer list")
    return [lyr for lyr in layers if isinstance(lyr, dict)]


def pick_layer(layers: list[dict[str, Any]], names: tuple[str, ...]) -> Optional[int]:
    """Lowest-id layer whose name matches (exact, else contains), preferring current top-level feature
    layers over the vintage groups (BAS/ACS/Census)."""
    wanted = [n.casefold() for n in names]

    def score(lyr: dict[str, Any]) -> Optional[tuple[int, int, int]]:
        name = str(lyr.get("name") or "").strip().casefold()
        if not name or not isinstance(lyr.get("id"), int):
            return None
        if name in wanted:
            exact = 0
        elif any(w in name for w in wanted):
            exact = 1
        else:
            return None
        if lyr.get("subLayerIds"):
            return None             # a group layer
        top = 0 if lyr.get("parentLayerId", -1) in (-1, None) else 1
        return (exact, top, int(lyr["id"]))

    scored = sorted(s for s in (score(lyr) for lyr in layers) if s is not None)
    return scored[0][2] if scored else None


async def _layer_id(http: httpx.AsyncClient, service: str, names: tuple[str, ...], *, ua: str,
                    cache: dict[str, Any], warnings: list[str]) -> int:
    if service not in cache:
        try:
            cache[service] = await discover_layers(http, service, ua=ua)
        except _SourceError as exc:
            cache[service] = exc
    got = cache[service]
    if isinstance(got, list):
        lid = pick_layer(got, names)
        if lid is not None:
            return lid
        msg = f"TIGERweb {service}: no layer named {names[0]!r}"
    else:
        msg = f"TIGERweb {service} directory unavailable ({got})"
    fallback = FALLBACK_LAYER_IDS.get((service, names[0].casefold()))
    if fallback is None:
        raise _SourceError(msg)
    warnings.append(f"{msg}; using known layer id {fallback}")
    return fallback


async def query_layer(http: httpx.AsyncClient, service: str, layer_id: int, *, ua: str, where: str,
                      out_fields: tuple[str, ...], offset_deg: float,
                      envelope: Optional[tuple[float, float, float, float]] = None) -> list[dict[str, Any]]:
    """All GeoJSON features of a TIGERweb layer query, paginated with resultOffset/resultRecordCount while
    the server reports exceededTransferLimit."""
    url = f"{TIGERWEB_BASE}{service}/MapServer/{layer_id}/query"
    features: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    for _page in range(MAX_PAGES):
        params = {
            "where": where, "outFields": ",".join(out_fields), "returnGeometry": "true", "outSR": "4326",
            "maxAllowableOffset": str(offset_deg), "geometryPrecision": str(GEOMETRY_PRECISION),
            "orderByFields": "GEOID", "resultOffset": str(offset), "resultRecordCount": str(PAGE_SIZE),
            "f": "geojson",
        }
        if envelope is not None:
            params.update({
                "geometry": ",".join(f"{v:.6f}" for v in envelope), "geometryType": "esriGeometryEnvelope",
                "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
            })
        data = await _get_json(http, url, ua=ua, params=params)
        feats = [f for f in (data.get("features") or []) if isinstance(f, dict)]
        new = 0
        for f in feats:
            props = f.get("properties") or {}
            key = str(props.get("GEOID") or f.get("id") or json.dumps(props, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            features.append(f)
            new += 1
        exceeded = bool(data.get("exceededTransferLimit")) or bool((data.get("properties") or {}).get("exceededTransferLimit"))
        if not exceeded or not feats or new == 0:
            return features
        offset += len(feats)
    raise _SourceError(f"TIGERweb {service}/{layer_id}: more than {MAX_PAGES} pages")


# ---------------------------------------------------------------------------
# Census Gazetteer fallback
# ---------------------------------------------------------------------------
def parse_gazetteer(content: bytes) -> list[dict[str, str]]:
    """Rows of a Census Gazetteer file (tab-separated, header row; plain or zipped)."""
    raw = content
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".txt")]
            if not members:
                raise _SourceError("gazetteer zip has no .txt member")
            raw = zf.read(members[0])
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    header = [h.strip().upper() for h in lines[0].split("\t")]
    if "GEOID" not in header or "NAME" not in header:
        raise _SourceError("gazetteer file has no GEOID/NAME header")
    return [dict(zip(header, (v.strip() for v in ln.split("\t")), strict=False)) for ln in lines[1:]]


async def _fetch_gazetteer(http: httpx.AsyncClient, names: tuple[str, ...], *, ua: str) -> tuple[str, list[dict[str, str]]]:
    errors = []
    for name in names:
        url = GAZETTEER_BASE + name
        try:
            resp = await _request(http, "GET", url, ua=ua, timeout=FILE_TIMEOUT, attempts=2)
            rows = parse_gazetteer(resp.content)
        except (_SourceError, zipfile.BadZipFile) as exc:
            errors.append(f"{name}: {exc}")
            continue
        rows = [r for r in rows if str(r.get("GEOID", "")).startswith(STATE_FIPS)]
        if rows:
            return url, rows
        errors.append(f"{name}: no New York rows")
    raise _SourceError("Census Gazetteer unavailable (" + "; ".join(errors)[:300] + ")")


_SUFFIX_RE = re.compile(r"\s+(village|cdp|city|town)$", re.I)


def _gaz_name_kind(name: str, lsad: str, kind_by_lsad: dict[str, str]) -> tuple[str, Optional[str]]:
    m = _SUFFIX_RE.search(name)
    base = name[:m.start()].strip() if m else name.strip()
    kind = kind_by_lsad.get(lsad.strip()) if lsad else None
    if kind is None and m:
        kind = {"village": "village", "cdp": "cdp", "city": "city", "town": "town"}[m.group(1).lower()]
    return base, kind


# ---------------------------------------------------------------------------
# Overpass
# ---------------------------------------------------------------------------
def overpass_station_query(bbox: tuple[float, float, float, float]) -> str:
    s, w, n, e = bbox[1], bbox[0], bbox[3], bbox[2]
    b = f"({s:.5f},{w:.5f},{n:.5f},{e:.5f})"
    return (f"[out:json][timeout:{OVERPASS_TIMEOUT_S}];\n(\n"
            f'  nwr["railway"="station"]["network"~"Long Island Rail ?Road|LIRR",i]{b};\n'
            f'  nwr["railway"="station"]["operator"~"Long Island Rail ?Road|LIRR",i]{b};\n'
            ");\nout center tags;")


def overpass_landmark_query(bbox: tuple[float, float, float, float]) -> str:
    s, w, n, e = bbox[1], bbox[0], bbox[3], bbox[2]
    b = f"({s:.5f},{w:.5f},{n:.5f},{e:.5f})"
    sel = [
        '["leisure"="park"]', '["leisure"="nature_reserve"]', '["natural"="beach"]', '["shop"="mall"]',
        '["amenity"="bus_station"]', '["public_transport"="station"]["bus"="yes"]', '["amenity"="library"]',
        '["amenity"="hospital"]',
    ]
    body = "\n".join(f'  nwr{s_}["name"]{b};' for s_ in sel)
    return f"[out:json][timeout:{OVERPASS_TIMEOUT_S}];\n(\n{body}\n);\nout center tags;"


async def _overpass(http: httpx.AsyncClient, query: str, *, ua: str) -> tuple[str, list[dict[str, Any]], Optional[str]]:
    errors = []
    for mirror in OVERPASS_MIRRORS:
        try:
            resp = await _request(http, "POST", mirror, ua=ua, timeout=OVERPASS_HTTP_TIMEOUT,
                                  data={"data": query}, attempts=2)
            try:
                data = resp.json()
            except ValueError as exc:
                raise _SourceError(f"{_host(mirror)}: invalid JSON") from exc
            if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
                raise _SourceError(f"{_host(mirror)}: unexpected response")
            remark = str(data.get("remark") or "")
            if "error" in remark.lower() or "timed out" in remark.lower():
                raise _SourceError(f"{_host(mirror)}: incomplete result ({remark[:80]})")
            stamp = (data.get("osm3s") or {}).get("timestamp_osm_base") if isinstance(data.get("osm3s"), dict) else None
            return mirror, [e for e in data["elements"] if isinstance(e, dict)], stamp
        except _SourceError as exc:
            errors.append(str(exc))
    raise _SourceError("all Overpass mirrors failed (" + "; ".join(errors)[:300] + ")")


def _osm_point(el: dict[str, Any]) -> Optional[tuple[float, float]]:
    src = el if el.get("type") == "node" else el.get("center")
    if not isinstance(src, dict):
        return None
    lat, lon = _num(src.get("lat")), _num(src.get("lon"))
    if lat is None or lon is None:
        return None
    return lat, lon


def _osm_tags(el: dict[str, Any]) -> dict[str, str]:
    tags = el.get("tags") if isinstance(el.get("tags"), dict) else {}
    return {k: str(tags[k]).strip() for k in _OSM_TAGS if isinstance(tags.get(k), (str, int, float))}


def _osm_aliases(tags: dict[str, str], name: str) -> list[str]:
    out: list[str] = []
    for k in ("alt_name", "official_name", "short_name"):
        for part in (tags.get(k) or "").split(";"):
            p = " ".join(part.split())
            if p and normkey(p) != normkey(name) and p not in out:
                out.append(p)
    return out


def landmark_kind(tags: dict[str, str]) -> Optional[str]:
    amenity, leisure = tags.get("amenity", ""), tags.get("leisure", "")
    if amenity == "hospital":
        return "hospital"
    if amenity == "library":
        return "library"
    if tags.get("shop") == "mall":
        return "mall"
    if amenity == "bus_station" or (tags.get("public_transport") == "station" and tags.get("bus") == "yes"):
        return "transit"
    if tags.get("natural") == "beach":
        return "beach"
    if leisure in ("park", "nature_reserve"):
        return "park"
    return None


def clean_station_name(name: str) -> str:
    n = " ".join((name or "").split())
    prev = None
    while n and n != prev:
        prev = n
        n = _STATION_SUFFIX_RE.sub("", n).strip()
    return n


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def _num(v: Any) -> Optional[float]:
    try:
        f = float(str(v).strip()) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _geom(feature: dict[str, Any]) -> Optional[BaseGeometry]:
    g = feature.get("geometry")
    if not isinstance(g, dict):
        return None
    try:
        geom = shape(g)
    except Exception:  # noqa: BLE001
        return None
    if geom.is_empty:
        return None
    if not geom.is_valid:
        geom = geom.buffer(0)
    return geom if not geom.is_empty and geom.area > 0 else None


def _deg_to_m(deg: float, lat: float) -> float:
    return deg * 111_320.0 * max(0.2, math.cos(math.radians(lat)))


@dataclass
class _Area:
    name: str
    kind: str
    county: str
    geom: BaseGeometry
    prepared: PreparedGeometry = field(init=False)

    def __post_init__(self) -> None:
        self.prepared = prep(self.geom)


def _containing(areas: list[_Area], lat: float, lon: float, snap_m: float) -> Optional[_Area]:
    pt = Point(lon, lat)
    inside = [a for a in areas if a.prepared.covers(pt)]
    if inside:
        return min(inside, key=lambda a: (a.geom.area, a.name))
    best: Optional[tuple[float, _Area]] = None
    for a in areas:
        d = _deg_to_m(a.geom.distance(pt), lat)
        if d <= snap_m and (best is None or d < best[0]):
            best = (d, a)
    return best[1] if best else None


def _round_coords(obj: Any, nd: int = GEOMETRY_PRECISION) -> Any:
    if isinstance(obj, (list, tuple)):
        if obj and all(isinstance(v, (int, float)) for v in obj):
            return [round(float(v), nd) for v in obj]
        return [_round_coords(v, nd) for v in obj]
    return obj


def _feature(name: str, geoid: str, full: str, geom: BaseGeometry) -> dict[str, Any]:
    gj = mapping(geom)
    return {"type": "Feature", "properties": {"county": name, "geoid": geoid, "name": full},
            "geometry": {"type": gj["type"], "coordinates": _round_coords(gj["coordinates"])}}


def _atomic_write(path: Path, payload: Any) -> None:
    """Write JSON to a temp file in the same folder, fsync, then os.replace (readers never see a partial file)."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
@dataclass
class _BuildState:
    ua: str
    warnings: list[str] = field(default_factory=list)
    sources: dict[str, Any] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)
    layer_cache: dict[str, Any] = field(default_factory=dict)
    gaz_cache: dict[str, Any] = field(default_factory=dict)

    def drop(self, source: str, n: int = 1) -> None:
        self.dropped[source] = self.dropped.get(source, 0) + n


def _user_agent(contact_email: Optional[str]) -> str:
    email = (contact_email or "").strip()
    if email and "@" in email and " " not in email:
        return f"{USER_AGENT_BASE}; {email})"
    return f"{USER_AGENT_BASE})"


async def _fetch_counties(http: httpx.AsyncClient, st: _BuildState, codes: dict[str, str]
                          ) -> tuple[list[dict[str, Any]], dict[str, BaseGeometry], int]:
    lid = await _layer_id(http, STATE_COUNTY_SERVICE, COUNTIES_LAYER, ua=st.ua, cache=st.layer_cache,
                          warnings=st.warnings)
    where = f"STATE='{STATE_FIPS}' AND COUNTY IN ({','.join(repr(c) for c in codes)})"
    feats = await query_layer(http, STATE_COUNTY_SERVICE, lid, ua=st.ua, where=where,
                              out_fields=("GEOID", "BASENAME", "NAME", "STATE", "COUNTY"),
                              offset_deg=COUNTY_OFFSET_DEG)
    out_feats: list[dict[str, Any]] = []
    geoms: dict[str, list[BaseGeometry]] = {}
    for f in feats:
        props = f.get("properties") or {}
        name = codes.get(str(props.get("COUNTY") or "").zfill(3))
        geom = _geom(f)
        if name is None or geom is None:
            continue
        geoms.setdefault(name, []).append(geom)
    merged = {n: unary_union(g) for n, g in geoms.items()}
    for code, name in codes.items():
        if name in merged:
            out_feats.append(_feature(name, f"{STATE_FIPS}{code}", f"{name} County", merged[name]))
    for name, geom in merged.items():
        minx, miny, maxx, maxy = geom.bounds
        if not (-75.5 <= minx <= maxx <= -71.0 and 40.0 <= miny <= maxy <= 41.6):
            raise _SourceError(f"{name} County polygon is not in WGS84 lon/lat (bounds {geom.bounds})")
    return out_feats, merged, lid


async def _towns(http: httpx.AsyncClient, st: _BuildState, gf: Geofence) -> tuple[list[PlaceRec], list[_Area]]:
    """Towns and cities (county subdivisions) + their polygons for town assignment."""
    recs: list[PlaceRec] = []
    areas: list[_Area] = []
    where = f"STATE='{STATE_FIPS}' AND COUNTY IN ({','.join(repr(c) for c in IN_SCOPE_COUNTY_CODES)})"
    try:
        lid = await _layer_id(http, PLACES_SERVICE, COUSUB_LAYER, ua=st.ua, cache=st.layer_cache, warnings=st.warnings)
        feats = await query_layer(http, PLACES_SERVICE, lid, ua=st.ua, where=where,
                                  out_fields=("GEOID", "BASENAME", "NAME", "LSADC", "COUNTY", "COUSUB",
                                              "INTPTLAT", "INTPTLON"),
                                  offset_deg=PLACE_OFFSET_DEG)
        for f in feats:
            p = f.get("properties") or {}
            name = " ".join(str(p.get("BASENAME") or "").split())
            full = str(p.get("NAME") or "")
            kind = COUSUB_KIND_BY_LSADC.get(str(p.get("LSADC") or ""))
            if kind is None:
                kind = "town" if full.lower().endswith(" town") else "city" if full.lower().endswith(" city") else None
            county_code = str(p.get("COUNTY") or "").zfill(3)
            geom = _geom(f)
            if not name or kind is None or county_code not in IN_SCOPE_COUNTY_CODES:
                continue          # reservations and undefined (water) subdivisions are not towns
            lat, lon = _num(p.get("INTPTLAT")), _num(p.get("INTPTLON"))
            if (lat is None or lon is None) and geom is not None:
                rp = geom.representative_point()
                lat, lon = rp.y, rp.x
            if lat is None or lon is None:
                continue
            county = gf.county_at(lat, lon)
            if county is None:
                st.drop("county_subdivisions")
                continue
            recs.append(PlaceRec(name=name, kind=kind, county=county, lat=round(lat, 6), lon=round(lon, 6),
                                 source="census_tigerweb", source_id=f"cousub:{p.get('GEOID')}", town=name))
            if geom is not None:
                areas.append(_Area(name, kind, county, geom))
        if not recs:
            raise _SourceError("TIGERweb county subdivisions: no towns returned")
        st.sources["county_subdivisions"] = {"source": "census_tigerweb", "status": "ok", "layer_id": lid,
                                             "url": f"{TIGERWEB_BASE}{PLACES_SERVICE}/MapServer/{lid}"}
        return recs, areas
    except _SourceError as exc:
        st.warnings.append(f"county subdivisions from TIGERweb failed ({exc}); using the Census Gazetteer")
    url, rows = await _gazetteer_rows(http, st, "cousub")
    for r in rows:
        geoid = r.get("GEOID", "")
        county_code = geoid[2:5]
        if county_code not in IN_SCOPE_COUNTY_CODES:
            continue
        name, kind = _gaz_name_kind(r.get("NAME", ""), "", {})
        if kind not in ("town", "city"):
            continue
        lat, lon = _num(r.get("INTPTLAT")), _num(r.get("INTPTLONG", r.get("INTPTLON")))
        if lat is None or lon is None:
            continue
        county = gf.county_at(lat, lon)
        if county is None:
            st.drop("county_subdivisions")
            continue
        recs.append(PlaceRec(name=name, kind=kind, county=county, lat=round(lat, 6), lon=round(lon, 6),
                             source="census_gazetteer", source_id=f"cousub:{geoid}", town=name))
    st.sources["county_subdivisions"] = {"source": "census_gazetteer", "status": "fallback", "url": url}
    st.warnings.append("no town polygons: villages/CDPs and landmarks have no town assignment")
    return recs, areas


async def _gazetteer_rows(http: httpx.AsyncClient, st: _BuildState, which: str) -> tuple[str, list[dict[str, str]]]:
    if which not in st.gaz_cache:
        files = GAZETTEER_COUSUB_FILES if which == "cousub" else GAZETTEER_PLACE_FILES
        try:
            st.gaz_cache[which] = await _fetch_gazetteer(http, files, ua=st.ua)
        except _SourceError as exc:
            st.gaz_cache[which] = exc
    got = st.gaz_cache[which]
    if isinstance(got, Exception):
        raise got
    return got


async def _localities(http: httpx.AsyncClient, st: _BuildState, gf: Geofence,
                      envelope: tuple[float, float, float, float], towns: list[PlaceRec],
                      town_areas: list[_Area]) -> tuple[list[PlaceRec], list[_Area]]:
    """Villages (incorporated places) and CDPs + their polygons for parent assignment."""
    recs: list[PlaceRec] = []
    areas: list[_Area] = []
    city_recs = {(normkey(t.name), t.county): t for t in towns if t.kind == "city"}

    def add(name: str, kind: str, geoid: str, lat: Optional[float], lon: Optional[float],
            geom: Optional[BaseGeometry], source: str, label: str) -> None:
        if (lat is None or lon is None) and geom is not None:
            rp = geom.representative_point()
            lat, lon = rp.y, rp.x
        if not name or lat is None or lon is None:
            return
        county = gf.county_at(lat, lon)
        if county is None:
            st.drop(label)
            return
        if kind == "city":
            city = city_recs.get((normkey(name), county))
            if city is not None:            # Glen Cove / Long Beach: already a county subdivision record
                if geom is not None:
                    areas.append(_Area(city.name, "city", county, geom))
                return
        town_area = _containing(town_areas, lat, lon, TOWN_SNAP_M) if town_areas else None
        rec = PlaceRec(name=name, kind=kind, county=county, lat=round(lat, 6), lon=round(lon, 6), source=source,
                       source_id=f"place:{geoid}", town=town_area.name if town_area else None)
        recs.append(rec)
        if geom is not None:
            areas.append(_Area(name, kind, county, geom))

    for label, layer, kinds, suffix_kind in (
        ("incorporated_places", INCORPORATED_LAYER, INCORPORATED_KIND_BY_LSADC, ("village", "city")),
        ("census_designated_places", CDP_LAYER, CDP_KIND_BY_LSADC, ("cdp",)),
    ):
        try:
            lid = await _layer_id(http, PLACES_SERVICE, layer, ua=st.ua, cache=st.layer_cache, warnings=st.warnings)
            feats = await query_layer(http, PLACES_SERVICE, lid, ua=st.ua, where=f"STATE='{STATE_FIPS}'",
                                      out_fields=("GEOID", "BASENAME", "NAME", "LSADC", "INTPTLAT", "INTPTLON"),
                                      offset_deg=PLACE_OFFSET_DEG, envelope=envelope)
            if not feats:
                raise _SourceError(f"TIGERweb {layer[0]}: no features in the Long Island envelope")
            for f in feats:
                p = f.get("properties") or {}
                name = " ".join(str(p.get("BASENAME") or "").split())
                full = str(p.get("NAME") or "")
                kind = kinds.get(str(p.get("LSADC") or ""))
                if kind is None:
                    _, kind = _gaz_name_kind(full, "", {})
                    if kind not in suffix_kind:
                        continue
                add(name, kind, str(p.get("GEOID") or ""), _num(p.get("INTPTLAT")), _num(p.get("INTPTLON")),
                    _geom(f), "census_tigerweb", label)
            st.sources[label] = {"source": "census_tigerweb", "status": "ok", "layer_id": lid,
                                 "url": f"{TIGERWEB_BASE}{PLACES_SERVICE}/MapServer/{lid}"}
        except _SourceError as exc:
            st.warnings.append(f"{label} from TIGERweb failed ({exc}); using the Census Gazetteer")
            url, rows = await _gazetteer_rows(http, st, "place")
            for r in rows:
                name, kind = _gaz_name_kind(r.get("NAME", ""), r.get("LSAD", ""), PLACE_KIND_BY_LSAD)
                if kind not in suffix_kind:
                    continue
                add(name, kind, r.get("GEOID", ""), _num(r.get("INTPTLAT")),
                    _num(r.get("INTPTLONG", r.get("INTPTLON"))), None, "census_gazetteer", label)
            st.sources[label] = {"source": "census_gazetteer", "status": "fallback", "url": url}
    return recs, areas


def _assign_context(rec: PlaceRec, loc_areas: list[_Area], localities: list[PlaceRec],
                    town_areas: list[_Area]) -> None:
    parent = _containing(loc_areas, rec.lat, rec.lon, PARENT_SNAP_M) if loc_areas else None
    if parent is not None and parent.county == rec.county:
        rec.parent_name = parent.name
    else:
        # Localities known only by their centroid (Census Gazetteer fallback): nearest within 2 km.
        with_poly = {(normkey(a.name), a.county) for a in loc_areas}
        point_only = [p for p in localities if (normkey(p.name), p.county) not in with_poly and p.county == rec.county]
        if point_only:
            near = min(point_only, key=lambda p: distance_m(rec.lat, rec.lon, p.lat, p.lon))
            if distance_m(rec.lat, rec.lon, near.lat, near.lon) <= PARENT_POINT_SNAP_M:
                rec.parent_name = near.name
    town = _containing(town_areas, rec.lat, rec.lon, TOWN_SNAP_M) if town_areas else None
    if town is not None:
        rec.town = town.name
    elif rec.parent_name:
        par = next((p for p in localities if p.name == rec.parent_name and p.county == rec.county), None)
        rec.town = par.town if par else None


_OsmFetch = Any   # (mirror, elements, osm_timestamp) or the _SourceError that stopped the fetch


async def _fetch_osm(http: httpx.AsyncClient, ua: str, bbox: tuple[float, float, float, float]) -> dict[str, _OsmFetch]:
    """Both Overpass queries, one after the other (public instances allow few parallel slots per client).
    Runs while the Census layers download."""
    out: dict[str, _OsmFetch] = {}
    for key, query in (("lirr_stations", overpass_station_query(bbox)), ("landmarks", overpass_landmark_query(bbox))):
        try:
            out[key] = await _overpass(http, query, ua=ua)
        except _SourceError as exc:
            out[key] = exc
    return out


def _stations(fetched: _OsmFetch, st: _BuildState, gf: Geofence, loc_areas: list[_Area],
              localities: list[PlaceRec], town_areas: list[_Area]) -> list[PlaceRec]:
    if isinstance(fetched, Exception):
        st.warnings.append(f"LIRR stations unavailable ({fetched}); station matching disabled until a rebuild")
        st.sources["lirr_stations"] = {"source": "osm_overpass", "status": "failed"}
        return []
    mirror, elements, stamp = fetched
    best: dict[tuple[str, str], tuple[tuple[int, int], PlaceRec]] = {}
    for el in elements:
        tags = _osm_tags(el)
        if tags.get("railway") != "station":
            continue
        if not _LIRR_RE.search(f"{tags.get('network', '')} | {tags.get('operator', '')}"):
            continue
        name = clean_station_name(tags.get("name", ""))
        pt = _osm_point(el)
        if not name or pt is None or el.get("id") is None:
            continue
        county = gf.county_at(*pt)
        if county is None:
            st.drop("lirr_stations")
            continue
        rec = PlaceRec(name=name, kind=STATION_KIND, county=county, lat=round(pt[0], 6), lon=round(pt[1], 6),
                       source="osm_overpass", source_id=f"{el.get('type')}/{el.get('id')}",
                       aliases=_osm_aliases(tags, name))
        pref = ({"node": 0, "way": 1, "relation": 2}.get(str(el.get("type")), 3), int(el.get("id") or 0))
        key = (normkey(name), county)
        if key not in best or pref < best[key][0]:
            best[key] = (pref, rec)
    recs = [r for _, r in best.values()]
    for rec in recs:
        _assign_context(rec, loc_areas, localities, town_areas)
    st.sources["lirr_stations"] = {"source": "osm_overpass", "status": "ok", "endpoint": mirror,
                                   "osm_timestamp": stamp, "elements": len(elements)}
    if not recs:
        st.warnings.append("Overpass returned no LIRR stations inside Nassau/Suffolk")
    return recs


def _landmarks(fetched: _OsmFetch, st: _BuildState, gf: Geofence, loc_areas: list[_Area],
               localities: list[PlaceRec], town_areas: list[_Area]) -> list[PlaceRec]:
    if isinstance(fetched, Exception):
        st.warnings.append(f"public landmarks unavailable ({fetched}); locations snap to hamlets until a rebuild")
        st.sources["landmarks"] = {"source": "osm_overpass", "status": "failed"}
        return []
    mirror, elements, stamp = fetched
    cands: list[tuple[tuple[int, int], PlaceRec]] = []
    for el in elements:
        tags = _osm_tags(el)
        name = " ".join(tags.get("name", "").split())
        kind = landmark_kind(tags)
        if not name or kind is None or el.get("id") is None:
            continue
        if tags.get("railway") == "station" and _LIRR_RE.search(f"{tags.get('network', '')} {tags.get('operator', '')}"):
            continue
        if tags.get("access") in ("private", "no"):
            continue
        pt = _osm_point(el)
        if pt is None:
            continue
        county = gf.county_at(*pt)
        if county is None:
            st.drop("landmarks")
            continue
        rec = PlaceRec(name=name, kind=kind, county=county, lat=round(pt[0], 6), lon=round(pt[1], 6),
                       source="osm_overpass", source_id=f"{el.get('type')}/{el.get('id')}",
                       aliases=_osm_aliases(tags, name))
        _assign_context(rec, loc_areas, localities, town_areas)
        pref = ({"relation": 0, "way": 1, "node": 2}.get(str(el.get("type")), 3), int(el.get("id") or 0))
        cands.append((pref, rec))
    cands.sort(key=lambda pr: pr[0])
    kept: list[PlaceRec] = []
    groups: dict[tuple[str, str, str], list[PlaceRec]] = {}
    for _, rec in cands:
        key = (normkey(rec.name), normkey(rec.parent_name or ""), rec.county if rec.parent_name else
               f"{rec.county}|{normkey(rec.town or '')}")
        group = groups.setdefault(key, [])
        if any(distance_m(rec.lat, rec.lon, g.lat, g.lon) <= DEDUPE_M for g in group):
            continue
        group.append(rec)
        kept.append(rec)
    st.sources["landmarks"] = {"source": "osm_overpass", "status": "ok", "endpoint": mirror, "osm_timestamp": stamp,
                               "elements": len(elements)}
    return kept


async def build_geography(http: httpx.AsyncClient, out_dir: Path, *, aliases_path: Path | None = None,
                          contact_email: Optional[str] = None) -> dict[str, Any]:
    """Fetch, validate (every place inside the county polygons), and atomically write li_counties.geojson,
    nyc_li_counties.geojson, li_places.json and manifest.json. Returns the manifest
    {built_at, sources:{...}, counts:{kind:{county:n}}, dropped_outside:n}. Raises GeoBuildError on failure."""
    if contact_email is None:
        try:
            from app.config import get_settings
            contact_email = get_settings().nominatim_contact_email
        except Exception:  # noqa: BLE001 - the contact address is optional for the build
            contact_email = ""
    st = _BuildState(ua=_user_agent(contact_email))
    out = Path(out_dir)
    try:
        return await _build(http, out, st, aliases_path)
    except GeoBuildError:
        raise
    except _SourceError as exc:
        raise GeoBuildError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface as a build error with a safe message
        logger.exception("geo build: unexpected failure")
        raise GeoBuildError(f"unexpected {type(exc).__name__}: {str(exc)[:200]}") from exc


async def _build(http: httpx.AsyncClient, out: Path, st: _BuildState, aliases_path: Optional[Path]) -> dict[str, Any]:
    started = _utcnow_iso()
    logger.info("geo build: starting")

    # 1. County polygons (required) and NYC polygons (explanations only).
    try:
        county_feats, county_geoms, counties_lid = await _fetch_counties(http, st, IN_SCOPE_COUNTY_CODES)
    except _SourceError as exc:
        raise GeoBuildError(f"county polygons unavailable: {exc}") from exc
    missing = [n for n in IN_SCOPE_COUNTY_CODES.values() if n not in county_geoms]
    if missing:
        raise GeoBuildError(f"county polygons missing: {', '.join(missing)}")
    st.sources["counties"] = {"source": "census_tigerweb", "status": "ok", "layer_id": counties_lid,
                              "url": f"{TIGERWEB_BASE}{STATE_COUNTY_SERVICE}/MapServer/{counties_lid}"}
    counties_fc = {"type": "FeatureCollection", "features": county_feats}
    try:
        nyc_feats, _, _ = await _fetch_counties(http, st, NYC_COUNTY_CODES)
        st.sources["nyc_counties"] = {"source": "census_tigerweb", "status": "ok"}
    except _SourceError as exc:
        nyc_feats = []
        st.warnings.append(f"Queens/Kings polygons unavailable ({exc}); outside-area explanations are generic")
        st.sources["nyc_counties"] = {"source": "census_tigerweb", "status": "failed"}
    nyc_fc = {"type": "FeatureCollection", "features": nyc_feats}
    gf = Geofence.from_feature_collections(counties_fc, nyc_fc)
    min_lon, min_lat, max_lon, max_lat = gf.bbox()
    bbox = (min_lon - 0.02, min_lat - 0.02, max_lon + 0.02, max_lat + 0.02)

    # 2. Towns/cities and villages/CDPs (required), while Overpass (optional) downloads alongside.
    osm_task = asyncio.create_task(_fetch_osm(http, st.ua, bbox))
    try:
        towns, town_areas = await _towns(http, st, gf)
        localities, loc_areas = await _localities(http, st, gf, bbox, towns, town_areas)
    except BaseException as exc:
        osm_task.cancel()
        await asyncio.gather(osm_task, return_exceptions=True)
        if isinstance(exc, _SourceError):
            raise GeoBuildError(f"towns/localities unavailable: {exc}") from exc
        raise
    for county in IN_SCOPE_COUNTY_CODES.values():
        if not any(t.county == county for t in towns):
            raise GeoBuildError(f"no towns found in {county} County")
        if not any(p.county == county for p in localities):
            raise GeoBuildError(f"no villages or hamlets found in {county} County")
    if len(towns) != 15:
        st.warnings.append(f"expected 13 towns + 2 cities, got {len(towns)}")

    # 3. LIRR stations and landmarks (optional).
    osm = await osm_task
    stations = _stations(osm["lirr_stations"], st, gf, loc_areas, localities, town_areas)
    landmarks = _landmarks(osm["landmarks"], st, gf, loc_areas, localities, town_areas)

    # 4. Validate, alias, write.
    places = towns + localities + stations + landmarks
    valid: list[PlaceRec] = []
    for rec in places:
        if gf.county_at(rec.lat, rec.lon) != rec.county:
            st.drop("validation")
            continue
        valid.append(rec)
    groups = load_alias_groups(aliases_path if aliases_path is not None else REPO_DATA_DIR / ALIASES_FILE)
    apply_aliases(valid, groups)
    valid.sort(key=lambda r: (kind_rank(r.kind), r.county, r.name.casefold(), r.source, r.source_id))

    counts: dict[str, dict[str, int]] = {}
    for rec in valid:
        counts.setdefault(rec.kind, {}).setdefault(rec.county, 0)
        counts[rec.kind][rec.county] += 1
    status = "ok"
    if any(isinstance(s, dict) and s.get("status") in ("failed", "fallback") for s in st.sources.values()):
        status = "degraded"
    built_at = _utcnow_iso()
    manifest: dict[str, Any] = {
        "format": MANIFEST_FORMAT,
        "built_at": built_at,
        "started_at": started,
        "status": status,
        "sources": st.sources,
        "counts": counts,
        "totals": {"places": len(valid), "landmarks": sum(1 for r in valid if r.kind in LANDMARK_KINDS),
                   "lirr_stations": sum(1 for r in valid if r.kind == STATION_KIND)},
        "degraded_sources": sorted(k for k, v in st.sources.items()
                                   if isinstance(v, dict) and v.get("status") in ("failed", "fallback")),
        "dropped_outside": sum(st.dropped.values()),
        "dropped_outside_by_source": dict(sorted(st.dropped.items())),
        "bbox": [round(v, 5) for v in gf.bbox()],
        "warnings": st.warnings,
        "attribution": [
            "U.S. Census Bureau, TIGERweb and Gazetteer files (public domain)",
            "© OpenStreetMap contributors (ODbL), via the Overpass API",
        ],
    }
    out.mkdir(parents=True, exist_ok=True)
    _atomic_write(out / COUNTIES_FILE, counties_fc)
    _atomic_write(out / NYC_COUNTIES_FILE, nyc_fc)
    _atomic_write(out / PLACES_FILE, {"format": PLACES_FORMAT, "built_at": built_at, "count": len(valid),
                                      "places": [r.to_dict() for r in valid]})
    _atomic_write(out / GEO_MANIFEST_FILE, manifest)
    logger.info("geo build: %d places (%s), dropped %d outside, status %s", len(valid),
                ", ".join(f"{k}={sum(v.values())}" for k, v in sorted(counts.items())), manifest["dropped_outside"],
                status)
    return manifest


def geo_files_present(out_dir: Path) -> bool:
    """True when a completed build is on disk (counties, places and the manifest, which is written last)."""
    d = Path(out_dir)
    try:
        for name in (COUNTIES_FILE, PLACES_FILE, GEO_MANIFEST_FILE):
            p = d / name
            if not p.is_file() or p.stat().st_size == 0:
                return False
        manifest = json.loads((d / GEO_MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(manifest, dict) and bool(manifest.get("built_at"))


def read_manifest(out_dir: Path) -> Optional[dict[str, Any]]:
    """The manifest of the build on disk, or None."""
    try:
        data = json.loads((Path(out_dir) / GEO_MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_geography(out_dir: Path, *, aliases_path: Optional[Path] = None) -> tuple[Gazetteer, Geofence]:
    """(Gazetteer, Geofence) from a completed build in out_dir. Raises FileNotFoundError / ValueError when
    the files are missing or unreadable. CPU-bound: call it via asyncio.to_thread from async code."""
    d = Path(out_dir)
    geofence = Geofence.load(d / COUNTIES_FILE, d / NYC_COUNTIES_FILE)
    gazetteer = Gazetteer.load(d / PLACES_FILE, aliases_path=aliases_path)
    return gazetteer, geofence

