"""Exa collector (docs/04 §3): request shape, dated results kept, undated discarded, cost, errors, budget."""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from app.pipeline.collectors import base as collectors_base
from app.pipeline.collectors.base import Budget
from app.pipeline.collectors.exa import ExaCollector
from app.pipeline.sources import SourcesConfig
from app.pipeline.types import PlannedQuery

BASE = "https://exa.test"
NOW = dt.datetime(2026, 10, 14, 16, 0, tzinfo=dt.timezone.utc)
SECRET = "exa-test-key-not-real"

EXA_OK = {
    "requestId": "exa-req-1",
    "resolvedSearchType": "neural",
    "results": [
        {"id": "https://www.newsday.com/long-island/synthetic-encampment-story", "url": "https://www.newsday.com/long-island/synthetic-encampment-story",
         "title": "Encampment found in wooded area near train station", "publishedDate": "2026-10-12T09:30:00.000Z",
         "author": "Synthetic Reporter", "text": "Officials said a homeless encampment was found in the woods near the station."},
        {"id": "https://example.org/blog/undated", "url": "https://example.org/blog/undated", "title": "Undated page",
         "publishedDate": None, "author": "Someone", "text": "No date here."},
        {"url": "https://www.reddit.com/r/LongIsland/comments/zz9/tents/", "title": "Tents by the lot",
         "publishedDate": "2026-10-13", "text": "tents behind the shopping center", "image": "https://example.invalid/i.jpg"},
        {"url": "https://example.org/forum/thread-1", "title": "Forum thread", "publishedDate": "2026-10-10T00:00:00Z",
         "highlights": ["someone sleeping in a car in the parking lot"]},
    ],
    "costDollars": {"total": 0.012, "search": {"neural": 0.005}},
}


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    async def _sleep(_s: float) -> None:
        return None
    monkeypatch.setattr(collectors_base, "asyncio", SimpleNamespace(sleep=_sleep))


@pytest.fixture
def normalize_ready():
    from app.pipeline import normalize
    try:
        normalize.parse_datetime("2026-10-13", now=NOW)
    except NotImplementedError:
        pytest.skip("waiting for app.pipeline.normalize")


def _col(http: httpx.AsyncClient, key: str = SECRET) -> ExaCollector:
    return ExaCollector(api_key=key, base_url=BASE, sources=SourcesConfig.load(), http=http, max_age_days=30,
                        now=lambda: NOW)


def _q(query="homeless encampment in the woods on Long Island, New York", **kw) -> PlannedQuery:
    return PlannedQuery(provider="exa", capability="search", query=query, **kw)


def _budget(cap: float = 6) -> Budget:
    return Budget(per_scan={"exa": cap})


async def test_exa_success_maps_dated_results_and_discards_undated(normalize_ready):
    budget = _budget()
    async with respx.mock() as router, httpx.AsyncClient() as http:
        route = router.post(f"{BASE}/search").mock(return_value=httpx.Response(200, json=EXA_OK))
        col = _col(http)
        res = await col.run([_q(place_id=3, place_name="Bay Shore")], budget)
    req = route.calls.last.request
    assert req.headers["x-api-key"] == SECRET
    body = json.loads(req.content)
    assert body["query"].startswith("homeless encampment") and body["type"] == "auto" and body["numResults"] == 10
    assert body["startPublishedDate"] == "2026-09-14T16:00:00.000Z"
    assert body["contents"] == {"text": {"maxCharacters": 4000}}
    assert "newsday.com" in body["includeDomains"] and "reddit.com" in body["includeDomains"]

    by_url = {i.url: i for i in res.items}
    assert set(by_url) == {"https://www.newsday.com/long-island/synthetic-encampment-story",
                           "https://www.reddit.com/r/LongIsland/comments/zz9/tents",
                           "https://example.org/forum/thread-1"}
    news = by_url["https://www.newsday.com/long-island/synthetic-encampment-story"]
    assert news.platform == "news" and news.provider == "exa" and news.date_source == "provider"
    assert news.context.source_scope == "li_scoped" and news.context.query_place_id == 3
    assert news.published_at == dt.datetime(2026, 10, 12, 9, 30, tzinfo=dt.timezone.utc)
    red = by_url["https://www.reddit.com/r/LongIsland/comments/zz9/tents"]
    assert red.platform == "reddit" and red.context.subreddit == "LongIsland" and red.context.source_scope == "li_scoped"
    web = by_url["https://example.org/forum/thread-1"]
    assert web.platform == "web" and "sleeping in a car" in web.text
    for item in res.items:
        assert "Synthetic Reporter" not in json.dumps(item.to_dict())
    assert col.last_stats["no_date"] == 1
    assert res.cost_usd == pytest.approx(0.012) and res.requests == 1
    assert res.logs[0].cost_usd == pytest.approx(0.012) and res.logs[0].items == 3
    assert budget.spent_scan["exa"] == 1 and res.health.status == "ok"


async def test_unrestricted_family_omits_include_domains():
    async with respx.mock() as router, httpx.AsyncClient() as http:
        route = router.post(f"{BASE}/search").mock(return_value=httpx.Response(200, json={"results": []}))
        res = await _col(http).run([_q(params={"unrestricted": True})], _budget())
    assert "includeDomains" not in json.loads(route.calls.last.request.content)
    assert res.logs[0].status == "empty"


async def test_budget_defers_extra_exa_queries():
    async with respx.mock() as router, httpx.AsyncClient() as http:
        route = router.post(f"{BASE}/search").mock(return_value=httpx.Response(200, json={"results": []}))
        res = await _col(http).run([_q("a", priority=2), _q("b", priority=1)], _budget(cap=1))
    assert route.call_count == 1 and [q.query for q in res.deferred] == ["b"]


async def test_auth_error_marks_unhealthy_and_stops():
    async with respx.mock() as router, httpx.AsyncClient() as http:
        route = router.post(f"{BASE}/search").mock(return_value=httpx.Response(401, json={"error": "Invalid API key"}))
        res = await ExaCollector(api_key=SECRET, base_url=BASE, sources=SourcesConfig.load(), http=http,
                                 max_age_days=30, now=lambda: NOW).run([_q("a"), _q("b"), _q("c")], _budget())
    assert route.call_count <= 3
    assert res.health.status == "unhealthy" and "EXA_API_KEY" in res.health.message
    assert SECRET not in json.dumps(res.errors)


async def test_server_error_defers_after_retries():
    async with respx.mock() as router, httpx.AsyncClient() as http:
        route = router.post(f"{BASE}/search").mock(return_value=httpx.Response(503, json={"error": "busy"}))
        budget = _budget()
        res = await _col(http).run([_q()], budget)
    assert route.call_count == 3 and len(res.deferred) == 1 and budget.spent_scan["exa"] == 0


async def test_no_key_disabled_and_health():
    async with httpx.AsyncClient() as http:
        col = _col(http, key="")
        res = await col.run([_q()], _budget())
        assert res.health.status == "disabled" and res.items == []
        assert (await col.health()).status == "disabled"
        assert (await _col(http).health()).status == "unknown"
