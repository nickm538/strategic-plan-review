"""Geocoding + snapping (docs/06 §3-4, R5, V4, V8; docs/10 §1 Geocoding row).

SYNTHETIC geography from tests/fixtures/geo/. Nominatim HTTP is mocked with respx; Nominatim points are
expressed as offsets from fixture places, never typed-in real coordinates.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import httpx
import pytest
import respx

from app.pipeline.geo import nominatim as nom
from app.pipeline.geo.gazetteer import Gazetteer, PlaceRec
from app.pipeline.geo.geocode import geocode, nominatim_query, town_for_self_report
from app.pipeline.geo.geofence import Geofence
from app.pipeline.geo.nominatim import NominatimClient
from app.pipeline.types import GeoResult, ItemContext

FIX = Path(__file__).parent / "fixtures" / "geo"
BASE = "https://nominatim.test"


@pytest.fixture(autouse=True)
def _fast_policy(monkeypatch):
    monkeypatch.setattr(nom, "POLICY_MIN_INTERVAL_S", 0.0)


@pytest.fixture(scope="module")
def gaz() -> Gazetteer:
    g = Gazetteer.load(FIX / "places.json")
    for i, p in enumerate(g.places, start=1):      # as if synced to the DB
        p.id = i
    g._reindex_ids()
    return g


@pytest.fixture(scope="module")
def gf() -> Geofence:
    return Geofence.load(FIX / "counties.geojson", FIX / "nyc_counties.geojson")


def place(gaz: Gazetteer, name: str, kind: str) -> PlaceRec:
    (p,) = gaz.by_name(name, (kind,))
    return p


class Cache:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}
        self.gets = 0

    async def get(self, key: str) -> Optional[dict[str, Any]]:
        self.gets += 1
        return self.data.get(key)

    async def put(self, key: str, value: dict[str, Any]) -> None:
        self.data[key] = value


def nominatim_result(lat: float, lon: float, county: str) -> dict[str, Any]:
    return {"lat": f"{lat:.7f}", "lon": f"{lon:.7f}", "category": "shop", "type": "convenience",
            "address": {"county": county, "state": "New York", "country_code": "us"}}


async def run(gaz, gf, http, *, text, kind, residence=False, post="", ctx=None, email="ops@example.org",
              cache: Optional[Cache] = None) -> GeoResult:
    client = NominatimClient(http, base_url=BASE, contact_email=email, min_interval_s=0)
    return await geocode(location_text=text, location_kind=kind, is_private_residence=residence,
                         post_text=post or text, context=ctx or ItemContext(query="homeless"), gazetteer=gaz,
                         geofence=gf, nominatim=client, cache_get=cache.get if cache else None,
                         cache_put=cache.put if cache else None)


# ---------------------------------------------------------------------------- snapping table rows
async def test_named_public_place_uses_gazetteer_point(gaz, gf):
    st = place(gaz, "Ronkonkoma", "lirr_station")
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as router:
            route = router.get(f"{BASE}/search")
            r = await run(gaz, gf, http, text="the Ronkonkoma train station", kind="named_place")
            assert not route.called
    assert (r.lat, r.lon) == (st.lat, st.lon)
    assert (r.precision, r.source, r.place_id) == ("public_place", "gazetteer", st.id)
    assert (r.place_label, r.town, r.county, r.in_geofence, r.outside_reason) == (
        "Ronkonkoma LIRR station", "Islip", "Suffolk", True, None)


async def test_landmark_preferred_over_hamlet(gaz, gf):
    lib = place(gaz, "Patchogue-Medford Library", "library")
    async with httpx.AsyncClient() as http:
        r = await run(gaz, gf, http, text="the library in Patchogue", kind="named_place")
    assert (r.lat, r.lon, r.precision, r.place_label) == (lib.lat, lib.lon, "public_place", "Patchogue-Medford Library")


async def test_alias_match(gaz, gf):
    bs = place(gaz, "Bay Shore", "cdp")
    async with httpx.AsyncClient() as http:
        r = await run(gaz, gf, http, text="Bayshore", kind="town_only")
    assert (r.lat, r.lon, r.precision, r.source, r.place_label, r.town) == (bs.lat, bs.lon, "hamlet", "gazetteer",
                                                                            "Bay Shore", "Islip")


async def test_business_snaps_to_public_place_within_250m(gaz, gf):
    lib = place(gaz, "Patchogue-Medford Library", "library")
    cache = Cache()
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            route = router.get(f"{BASE}/search").mock(return_value=httpx.Response(
                200, json=[nominatim_result(lib.lat + 0.0008, lib.lon + 0.0004, "Suffolk County")]))
            r = await run(gaz, gf, http, text="the 7-Eleven on Main St in Patchogue", kind="business", cache=cache)
            q = route.calls[0].request.url.params["q"]
    assert q == "7-Eleven on Main St, Patchogue, NY"
    assert (r.lat, r.lon) == (lib.lat, lib.lon)
    assert (r.precision, r.source, r.place_id, r.county, r.in_geofence) == ("public_place", "nominatim", lib.id,
                                                                            "Suffolk", True)


async def test_business_without_nearby_public_place_rounds_to_3_decimals(gaz, gf):
    med = place(gaz, "Medford", "cdp")
    raw_lat, raw_lon = med.lat + 0.012345, med.lon - 0.023456          # >250 m from every public place
    cache = Cache()
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(f"{BASE}/search").mock(return_value=httpx.Response(
                200, json=[nominatim_result(raw_lat, raw_lon, "Suffolk County")]))
            r = await run(gaz, gf, http, text="the Wawa at Route 112 and Granny Rd", kind="intersection",
                          post="tent behind the Wawa at Route 112 and Granny Rd in Medford", cache=cache)
    assert (r.lat, r.lon) == (round(raw_lat, 3), round(raw_lon, 3))
    assert (r.precision, r.source, r.place_id, r.county) == ("public_place", "nominatim", None, "Suffolk")
    assert r.place_label == "Medford area" and r.town == "Brookhaven"
    (cached,) = cache.data.values()
    assert cached == {"source": "nominatim", "lat": round(raw_lat, 4), "lon": round(raw_lon, 4), "county": "Suffolk"}


async def test_private_residence_snaps_to_hamlet_and_never_calls_nominatim(gaz, gf):
    med = place(gaz, "Medford", "cdp")
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as router:
            route = router.get(f"{BASE}/search")
            r = await run(gaz, gf, http, text="my neighbor's driveway on Example Lane in Medford", kind="street",
                          residence=True)
            r2 = await run(gaz, gf, http, text="the house next to the Town Park in Bay Shore", kind="named_place",
                           residence=True)
            assert not route.called
    assert (r.lat, r.lon, r.precision, r.source, r.place_label) == (med.lat, med.lon, "hamlet", "hamlet_fallback",
                                                                     "Medford")
    bs = place(gaz, "Bay Shore", "cdp")
    assert (r2.lat, r2.lon, r2.precision) == (bs.lat, bs.lon, "hamlet")         # never the park, never the house


async def test_street_only_snaps_to_hamlet(gaz, gf):
    bs = place(gaz, "Bay Shore", "cdp")
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as router:
            route = router.get(f"{BASE}/search")
            r = await run(gaz, gf, http, text="on Main Street", kind="street", post="Someone sleeping on Main Street in Bay Shore")
            assert not route.called
    assert (r.lat, r.lon, r.precision, r.source) == (bs.lat, bs.lon, "hamlet", "hamlet_fallback")


async def test_town_only_uses_town_centroid(gaz, gf):
    islip = place(gaz, "Islip", "town")
    async with httpx.AsyncClient() as http:
        r = await run(gaz, gf, http, text="Town of Islip", kind="town_only")
    assert (r.lat, r.lon, r.precision, r.place_label, r.town, r.county) == (islip.lat, islip.lon, "town",
                                                                           "Town of Islip", "Islip", "Suffolk")


async def test_unknown_town_only_is_unlocated(gaz, gf):
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as router:
            route = router.get(f"{BASE}/search")
            r = await run(gaz, gf, http, text="Jamaica", kind="town_only")
            assert not route.called
    assert (r.lat, r.lon, r.source, r.precision) == (None, None, "none", None)


# ---------------------------------------------------------------------------- Nominatim acceptance
def _queens_point(gf: Geofence) -> tuple[float, float]:
    poly = gf.polygon("Queens")
    pt = poly.representative_point()
    return pt.y, pt.x


async def test_nominatim_result_outside_polygon_rejected(gaz, gf):
    lat, lon = _queens_point(gf)
    cache = Cache()
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(f"{BASE}/search").mock(return_value=httpx.Response(
                200, json=[nominatim_result(lat, lon, "Queens County")]))
            r = await run(gaz, gf, http, text="the Dunkin on Example Ave", kind="business", cache=cache)
    assert (r.lat, r.lon, r.source, r.in_geofence) == (None, None, "none", False)
    assert r.outside_reason and "Queens" in r.outside_reason
    (cached,) = cache.data.values()
    assert cached["miss"] is True and "Queens" in cached["reason"]


async def test_nominatim_county_text_alone_is_not_enough(gaz, gf):
    lat, lon = _queens_point(gf)                       # address says Nassau but the point is outside
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(f"{BASE}/search").mock(return_value=httpx.Response(
                200, json=[nominatim_result(lat, lon, "Nassau County")]))
            r = await run(gaz, gf, http, text="the Dunkin on Example Ave", kind="business")
    assert r.source == "none" and r.lat is None and "Queens" in (r.outside_reason or "")


async def test_nominatim_polygon_alone_is_not_enough(gaz, gf):
    med = place(gaz, "Medford", "cdp")                 # inside the polygon, but address county is elsewhere
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(f"{BASE}/search").mock(return_value=httpx.Response(
                200, json=[nominatim_result(med.lat + 0.03, med.lon + 0.03, "Kings County")]))
            r = await run(gaz, gf, http, text="the Dunkin on Example Ave", kind="business")
    assert r.source == "none" and r.outside_reason == "nominatim county mismatch"


async def test_rejected_result_falls_back_to_named_hamlet(gaz, gf):
    lat, lon = _queens_point(gf)
    hx = place(gaz, "Hicksville", "cdp")
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(f"{BASE}/search").mock(return_value=httpx.Response(
                200, json=[nominatim_result(lat, lon, "Queens County")]))
            r = await run(gaz, gf, http, text="the Dunkin on Example Ave in Hicksville", kind="business")
    assert (r.lat, r.lon, r.precision, r.source, r.outside_reason) == (hx.lat, hx.lon, "hamlet", "hamlet_fallback", None)


async def test_second_result_accepted_when_first_rejected(gaz, gf):
    lat, lon = _queens_point(gf)
    lib = place(gaz, "Patchogue-Medford Library", "library")
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=[
                nominatim_result(lat, lon, "Queens County"),
                nominatim_result(lib.lat + 0.0005, lib.lon, "Suffolk County")]))
            r = await run(gaz, gf, http, text="the Dunkin on Example Ave", kind="business")
    assert (r.place_id, r.precision, r.outside_reason) == (lib.id, "public_place", None)


async def test_cache_hit_skips_http_and_miss_is_cached(gaz, gf):
    lib = place(gaz, "Patchogue-Medford Library", "library")
    cache = Cache()
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            route = router.get(f"{BASE}/search").mock(return_value=httpx.Response(
                200, json=[nominatim_result(lib.lat + 0.0005, lib.lon, "Suffolk County")]))
            r1 = await run(gaz, gf, http, text="the Dunkin on Example Ave in Patchogue", kind="business", cache=cache)
            r2 = await run(gaz, gf, http, text="the  dunkin on example ave in patchogue", kind="business", cache=cache)
            assert route.call_count == 1
        with respx.mock() as router:
            route = router.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=[]))
            r3 = await run(gaz, gf, http, text="the Example Deli in Patchogue", kind="business", cache=cache)
            await run(gaz, gf, http, text="the Example Deli in Patchogue", kind="business", cache=cache)
            assert route.call_count == 1
    assert r1.place_id == r2.place_id == lib.id
    assert {"miss": True, "reason": "no_result"} in cache.data.values()
    assert (r3.precision, r3.source) == ("hamlet", "hamlet_fallback")      # Patchogue centroid


async def test_http_error_is_not_cached_and_falls_back(gaz, gf):
    cache = Cache()
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(f"{BASE}/search").mock(return_value=httpx.Response(503))
            r = await run(gaz, gf, http, text="the Example Deli in Medford", kind="business", cache=cache)
    assert cache.data == {}
    assert (r.precision, r.source, r.place_label) == ("hamlet", "hamlet_fallback", "Medford")


async def test_nominatim_disabled_without_email(gaz, gf):
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as router:
            route = router.get(f"{BASE}/search")
            r = await run(gaz, gf, http, text="the Example Deli in Medford", kind="business", email="")
            assert not route.called
    assert (r.precision, r.place_label) == ("hamlet", "Medford")


async def test_cache_failures_do_not_break_geocoding(gaz, gf):
    lib = place(gaz, "Patchogue-Medford Library", "library")

    async def bad_get(key):
        raise RuntimeError("db down")

    async def bad_put(key, value):
        raise RuntimeError("db down")

    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(f"{BASE}/search").mock(return_value=httpx.Response(
                200, json=[nominatim_result(lib.lat, lib.lon + 0.0005, "Suffolk County")]))
            client = NominatimClient(http, base_url=BASE, contact_email="ops@example.org")
            r = await geocode(location_text="the Example Deli in Patchogue", location_kind="business",
                              is_private_residence=False, post_text="x", context=ItemContext(query="q"),
                              gazetteer=gaz, geofence=gf, nominatim=client, cache_get=bad_get, cache_put=bad_put)
    assert r.place_id == lib.id


# ---------------------------------------------------------------------------- geofence + robustness
async def test_county_always_from_polygon(gf):
    lat, lon = _queens_point(gf)
    bogus = PlaceRec("Synthetic Mislabeled Park", "park", "Nassau", lat, lon, "osm_overpass", "way/1")
    g = Gazetteer([bogus])
    r = await geocode(location_text="Synthetic Mislabeled Park", location_kind="named_place",
                      is_private_residence=False, post_text="", context=ItemContext(query="q"), gazetteer=g,
                      geofence=gf, nominatim=None)
    assert r.lat == lat and r.county is None and r.in_geofence is False
    assert "Queens" in (r.outside_reason or "")


async def test_nothing_resolves(gaz, gf):
    r = await geocode(location_text="", location_kind="none", is_private_residence=False, post_text="x",
                      context=ItemContext(query="q"), gazetteer=gaz, geofence=gf, nominatim=None)
    assert (r.lat, r.lon, r.source, r.precision, r.outside_reason) == (None, None, "none", None, None)
    r = await geocode(location_text="somewhere behind the old mill", location_kind="named_place",
                      is_private_residence=False, post_text="", context=ItemContext(query="q"), gazetteer=gaz,
                      geofence=gf, nominatim=None)
    assert r.source == "none"


async def test_never_raises(gaz, gf):
    class Broken(Gazetteer):
        def match_location(self, *a, **k):
            raise RuntimeError("boom")

    broken = Broken([])
    r = await geocode(location_text="Bay Shore", location_kind="town_only", is_private_residence=False,
                      post_text="", context=ItemContext(query="q"), gazetteer=broken, geofence=gf, nominatim=None)
    assert r.source == "none" and r.lat is None


def test_nominatim_query_builder():
    assert nominatim_query("behind the Wawa in Medford", "Medford") == "Wawa, Medford, NY"
    assert nominatim_query("corner of Main St and Oak Ave", "Bay Shore") == "Main St and Oak Ave, Bay Shore, NY"
    assert nominatim_query("the Example Deli on Route 25, Long Island", None) == "Example Deli on Route 25, NY"
    assert nominatim_query("in Medford", "Medford") == ""


# ---------------------------------------------------------------------------- self-reports (R7)
async def test_town_for_self_report(gaz):
    ctx = ItemContext(query="q")
    assert await town_for_self_report(post_text="I'm homeless in Hempstead, sleeping in my car", context=ctx,
                                      gazetteer=gaz) == ("Hempstead", "Nassau")
    assert await town_for_self_report(post_text="staying by the Patchogue-Medford Library for now", context=ctx,
                                      gazetteer=gaz) == ("Patchogue", "Suffolk")
    assert await town_for_self_report(post_text="lost my apartment, Town of Islip shelter is full", context=ctx,
                                      gazetteer=gaz) == ("Islip", "Suffolk")
    tagged = ItemContext(query="q", platform_place_tag="Bay Shore, New York")
    assert await town_for_self_report(post_text="I have nowhere to sleep tonight", context=tagged,
                                      gazetteer=gaz) == ("Bay Shore", "Suffolk")
    assert await town_for_self_report(post_text="I have nowhere to sleep tonight", context=ctx,
                                      gazetteer=gaz) == (None, None)


async def test_unknown_name_inside_known_hamlet_tries_nominatim(gaz, gf):
    vmp = place(gaz, "Veterans Memorial Park", "park")
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as router:
            route = router.get(f"{BASE}/search").mock(return_value=httpx.Response(
                200, json=[nominatim_result(vmp.lat + 0.001, vmp.lon, "Suffolk County")]))
            r = await run(gaz, gf, http, text="the Wawa in Medford", kind="named_place")
            assert route.call_count == 1
            assert route.calls[0].request.url.params["q"] == "Wawa, Medford, NY"
            r2 = await run(gaz, gf, http, text="downtown Patchogue", kind="named_place")
            r3 = await run(gaz, gf, http, text="Bay Shore, NY", kind="named_place")
            r4 = await run(gaz, gf, http, text="under the overpass on Route 112 in Medford", kind="named_place")
            assert route.call_count == 1                           # no unknown proper name in r2-r4
    assert (r.place_id, r.precision, r.source) == (vmp.id, "public_place", "nominatim")
    assert (r2.place_label, r2.precision, r2.source) == ("Patchogue", "hamlet", "gazetteer")
    assert (r3.place_label, r3.precision) == ("Bay Shore", "hamlet")
    assert (r4.place_label, r4.precision) == ("Medford", "hamlet")


def test_unmatched_words(gaz):
    assert gaz.unmatched_words("the Wawa in Medford") == ["Wawa"]
    assert gaz.unmatched_words("behind the Stop & Shop on Main St, Bay Shore NY") == ["Stop", "Shop"]
    assert gaz.unmatched_words("the Town Park in Bay Shore") == []
    assert gaz.unmatched_words("downtown Patchogue near the LIRR") == []
