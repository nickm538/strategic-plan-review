"""Bounded Nominatim client (docs/06 §3 step 4): identification, bounded query, throttle, never raises.
All HTTP is mocked with respx; no network."""
from __future__ import annotations

import asyncio
import itertools
import time

import httpx
import pytest
import respx

from app.pipeline.geo import nominatim as nom
from app.pipeline.geo.nominatim import NominatimClient

BASE = "https://nominatim.test"
VIEWBOX = (-73.75, 40.55, -71.85, 41.30)
RESULT = {
    "lat": "40.7660", "lon": "-73.0130", "category": "amenity", "type": "library", "name": "Synthetic Library",
    "display_name": "Synthetic Library, 54 Example Ave, Patchogue, Suffolk County, New York, 11772, USA",
    "address": {"county": "Suffolk County", "state": "New York", "road": "Example Ave", "house_number": "54",
                "village": "Patchogue", "country_code": "us"},
    "extratags": {"phone": "+1 555 0100"},
}


@pytest.fixture(autouse=True)
def _fast_policy(monkeypatch):
    monkeypatch.setattr(nom, "POLICY_MIN_INTERVAL_S", 0.0)


async def test_request_shape_and_allow_listed_result():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[RESULT])

    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(f"{BASE}/search").mock(side_effect=handler)
            c = NominatimClient(http, base_url=BASE + "/", contact_email="ops@example.org", min_interval_s=0)
            assert c.enabled
            out = await c.search("Synthetic Library, Patchogue, NY", viewbox=VIEWBOX)
    (req,) = seen
    assert req.headers["user-agent"] == "PIT-Radar/1.0 (Long Island Coalition for the Homeless outreach; ops@example.org)"
    p = req.url.params
    assert p["q"] == "Synthetic Library, Patchogue, NY"
    assert (p["format"], p["countrycodes"], p["bounded"], p["addressdetails"], p["limit"]) == ("jsonv2", "us", "1", "1", "3")
    assert p["viewbox"] == "-73.75000,41.30000,-71.85000,40.55000"
    assert out == [{"lat": 40.766, "lon": -73.013, "category": "amenity", "type": "library",
                    "name": "Synthetic Library",
                    "address": {"county": "Suffolk County", "state": "New York", "country_code": "us",
                                "village": "Patchogue"}}]
    assert "display_name" not in out[0] and "road" not in out[0]["address"]


async def test_disabled_without_contact_email():
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as router:
            route = router.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=[RESULT]))
            for email in ("", "   ", "not-an-email"):
                c = NominatimClient(http, base_url=BASE, contact_email=email)
                assert not c.enabled
                assert await c.search("x", viewbox=VIEWBOX) == []
            assert not route.called


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="oops"),
    httpx.Response(200, text="<html>not json"),
    httpx.Response(200, json={"error": "bad"}),
    httpx.Response(403, text="blocked"),
])
async def test_errors_return_empty(response):
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(f"{BASE}/search").mock(return_value=response)
            c = NominatimClient(http, base_url=BASE, contact_email="ops@example.org")
            results, ok = await c.search_with_status("q", viewbox=VIEWBOX)
    assert results == [] and ok is False
    assert c.last_error


async def test_network_error_returns_empty():
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(f"{BASE}/search").mock(side_effect=httpx.ConnectTimeout("slow"))
            c = NominatimClient(http, base_url=BASE, contact_email="ops@example.org")
            assert await c.search("q", viewbox=VIEWBOX) == []
            assert "network error" in c.last_error


async def test_bad_rows_are_skipped():
    rows = [{"lat": "x", "lon": "1"}, "junk", {"lat": "40.1", "lon": "-73.1"}]
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(f"{BASE}/search").mock(return_value=httpx.Response(200, json=rows))
            c = NominatimClient(http, base_url=BASE, contact_email="ops@example.org")
            out = await c.search("q", viewbox=VIEWBOX)
    assert out == [{"lat": 40.1, "lon": -73.1, "address": {}}]


async def test_throttle_spaces_requests(monkeypatch):
    monkeypatch.setattr(nom, "POLICY_MIN_INTERVAL_S", 0.15)
    stamps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        stamps.append(time.monotonic())
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(f"{BASE}/search").mock(side_effect=handler)
            a = NominatimClient(http, base_url=BASE, contact_email="ops@example.org", min_interval_s=0.01)
            b = NominatimClient(http, base_url=BASE, contact_email="ops@example.org", min_interval_s=0.01)
            await asyncio.gather(a.search("1", viewbox=VIEWBOX), b.search("2", viewbox=VIEWBOX),
                                 a.search("3", viewbox=VIEWBOX))
    assert len(stamps) == 3
    gaps = [b_ - a_ for a_, b_ in itertools.pairwise(stamps)]
    assert all(g >= 0.14 for g in gaps), gaps          # policy floor applies across clients in the process


async def test_429_pauses_further_requests(monkeypatch):
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            route = router.get(f"{BASE}/search").mock(return_value=httpx.Response(429))
            c = NominatimClient(http, base_url=BASE, contact_email="ops@example.org")
            assert await c.search("q", viewbox=VIEWBOX) == []
            assert await c.search("q2", viewbox=VIEWBOX) == []
            assert route.call_count == 1                    # second call skipped during the back-off
            assert "throttl" in c.last_error
