"""Firecrawl collector (docs/04 §4): search, relative news dates, scrape-for-date, budget, health."""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from app.pipeline.collectors import base as collectors_base
from app.pipeline.collectors.base import Budget
from app.pipeline.collectors.firecrawl import FirecrawlCollector, metadata_date
from app.pipeline.sources import SourcesConfig
from app.pipeline.types import PlannedQuery

BASE = "https://fc.test"
NOW = dt.datetime(2026, 10, 14, 16, 0, tzinfo=dt.timezone.utc)
SECRET = "fc-test-key-not-real"

SEARCH_OK = {
    "success": True,
    "data": {
        "web": [
            {"url": "https://www.liherald.com/stories/synthetic-camp-story", "title": "Homeless camp in Freeport woods",
             "description": "Residents report a homeless encampment behind the Freeport station."},
            {"url": "https://example.org/recipes/cake", "title": "Best cake recipe", "description": "Flour and sugar."},
            {"url": "https://example.org/undatable", "title": "Homeless encampment report", "description": "Encampment."},
        ],
        "news": [
            {"title": "Encampment cleared near Hicksville station", "snippet": "A homeless encampment was cleared.",
             "url": "https://www.newsday.com/long-island/synthetic-news", "date": "3 days ago", "position": 1},
            {"title": "No date news", "snippet": "x", "url": "https://www.pix11.com/news/x", "position": 2},
        ],
    },
    "creditsUsed": 2,
    "id": "fc-search-1",
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
        normalize.parse_datetime("3 days ago", now=NOW)
    except NotImplementedError:
        pytest.skip("waiting for app.pipeline.normalize")


def _promising(text: str) -> bool:
    return "homeless" in text.lower() or "encampment" in text.lower()


def _col(http, key: str = SECRET, **kw) -> FirecrawlCollector:
    return FirecrawlCollector(api_key=key, base_url=BASE, sources=SourcesConfig.load(), http=http,
                              prefilter_title=_promising, now=lambda: NOW, **kw)


def _q(query='"Freeport" homeless', **kw) -> PlannedQuery:
    return PlannedQuery(provider="firecrawl", capability="search", query=query, **kw)


def _scrape_cb(request: httpx.Request) -> httpx.Response:
    url = json.loads(request.content)["url"]
    if "liherald" in url:
        return httpx.Response(200, json={"success": True, "data": {
            "markdown": "# Homeless camp in Freeport woods\n\nResidents report tents behind the station.\n![photo](https://example.invalid/p.jpg)",
            "metadata": {"title": "Homeless camp", "article:published_time": ["2026-10-11T14:00:00-04:00"],
                         "author": "Synthetic Reporter", "sourceURL": url}}})
    return httpx.Response(200, json={"success": True, "data": {"markdown": "text", "metadata": {"title": "t"}}})


async def test_search_news_relative_date_and_scrape_for_date(normalize_ready):
    budget = Budget(per_scan={"firecrawl": 6})
    async with respx.mock() as router, httpx.AsyncClient() as http:
        search = router.post(f"{BASE}/v2/search").mock(return_value=httpx.Response(200, json=SEARCH_OK))
        scrape = router.post(f"{BASE}/v2/scrape").mock(side_effect=_scrape_cb)
        col = _col(http)
        res = await col.run([_q(place_id=11, place_name="Freeport")], budget)
    req = search.calls.last.request
    assert req.headers["authorization"] == f"Bearer {SECRET}"
    body = json.loads(req.content)
    assert body == {"query": '"Freeport" homeless', "limit": 10, "sources": ["web", "news"], "tbs": "qdr:w",
                    "location": "New York, United States"}
    # only the two promising undated web results were scraped; the cake recipe was not
    scraped = sorted(json.loads(c.request.content)["url"] for c in scrape.calls)
    assert scraped == ["https://example.org/undatable", "https://www.liherald.com/stories/synthetic-camp-story"]
    assert json.loads(scrape.calls[0].request.content)["formats"] == ["markdown"]
    by_url = {i.url: i for i in res.items}
    assert set(by_url) == {"https://www.newsday.com/long-island/synthetic-news",
                           "https://www.liherald.com/stories/synthetic-camp-story"}
    news = by_url["https://www.newsday.com/long-island/synthetic-news"]
    assert news.date_source == "relative" and news.published_at == NOW - dt.timedelta(days=3)
    assert news.platform == "news" and news.context.source_scope == "li_scoped" and news.context.query_place_id == 11
    page = by_url["https://www.liherald.com/stories/synthetic-camp-story"]
    assert page.date_source == "page_metadata"
    assert page.published_at == dt.datetime(2026, 10, 11, 18, 0, tzinfo=dt.timezone.utc)
    assert "tents behind the station" in page.text and "example.invalid" not in page.text
    assert "Synthetic Reporter" not in json.dumps(page.to_dict())
    # 1 search + 2 scrapes counted against the firecrawl request budget
    assert budget.spent_scan["firecrawl"] == 3 and res.requests == 3
    assert col.last_stats["no_date"] == 2               # undated news + scraped page without a date
    assert col.last_stats["no_date_not_promising"] == 1
    statuses = sorted((e.endpoint, e.status) for e in res.logs)
    assert statuses == [("/v2/scrape", "empty"), ("/v2/scrape", "ok"), ("/v2/search", "ok")]


async def test_scrape_respects_budget_and_backfill_tbs(normalize_ready):
    budget = Budget(per_scan={"firecrawl": 1})
    async with respx.mock(assert_all_called=False) as router, httpx.AsyncClient() as http:
        search = router.post(f"{BASE}/v2/search").mock(return_value=httpx.Response(200, json=SEARCH_OK))
        scrape = router.post(f"{BASE}/v2/scrape").mock(side_effect=_scrape_cb)
        col = _col(http, backfill=True)
        res = await col.run([_q()], budget)
    assert json.loads(search.calls.last.request.content)["tbs"] == "qdr:m"
    assert not scrape.called and col.last_stats["no_date_scrape_budget"] == 2
    assert [i.platform for i in res.items] == ["news"]


async def test_scrape_cap_from_sources(normalize_ready):
    sources = SourcesConfig.load(overrides={"firecrawl": {"scrape_for_date": {"max_per_scan": 1}}})
    async with respx.mock() as router, httpx.AsyncClient() as http:
        router.post(f"{BASE}/v2/search").mock(return_value=httpx.Response(200, json=SEARCH_OK))
        scrape = router.post(f"{BASE}/v2/scrape").mock(side_effect=_scrape_cb)
        col = FirecrawlCollector(api_key=SECRET, base_url=BASE, sources=sources, http=http,
                                 prefilter_title=_promising, now=lambda: NOW)
        await col.run([_q()], Budget(per_scan={"firecrawl": 10}))
    assert scrape.call_count == 1 and col.last_stats["no_date_scrape_cap"] == 1


async def test_auth_error_unhealthy_and_budget_refunded():
    budget = Budget(per_scan={"firecrawl": 6})
    async with respx.mock() as router, httpx.AsyncClient() as http:
        router.post(f"{BASE}/v2/search").mock(return_value=httpx.Response(401, json={"success": False, "error": "Unauthorized"}))
        res = await _col(http).run([_q("a"), _q("b")], budget)
    assert res.health.status == "unhealthy" and "FIRECRAWL_API_KEY" in res.health.message
    assert budget.spent_scan.get("firecrawl", 0) == 0 and res.items == []


async def test_budget_defers_search():
    async with respx.mock() as router, httpx.AsyncClient() as http:
        route = router.post(f"{BASE}/v2/search").mock(return_value=httpx.Response(200, json={"success": True, "data": {}}))
        res = await _col(http).run([_q("a", priority=3), _q("b", priority=1)], Budget(per_scan={"firecrawl": 1}))
    assert route.call_count == 1 and [q.query for q in res.deferred] == ["b"]
    assert res.logs[0].status in ("empty", "deferred")


@pytest.mark.parametrize("response,status,credits", [
    (httpx.Response(200, json={"success": True, "data": {"remainingCredits": 1000, "planCredits": 5000}}), "ok", 1000),
    (httpx.Response(200, json={"success": True, "data": {"remainingCredits": 0}}), "unhealthy", 0),
    (httpx.Response(401, json={"success": False, "error": "Unauthorized"}), "unhealthy", None),
    (httpx.Response(404, json={"success": False, "error": "Could not find credit usage information"}), "unknown", None),
])
async def test_health_credit_usage(response, status, credits):
    async with respx.mock() as router, httpx.AsyncClient() as http:
        router.get(f"{BASE}/v2/team/credit-usage").mock(return_value=response)
        h = await _col(http).health()
    assert h.status == status and h.credits_remaining == credits


async def test_no_key_disabled():
    async with httpx.AsyncClient() as http:
        col = _col(http, key="")
        assert (await col.run([_q()], Budget(per_scan={"firecrawl": 6}))).health.status == "disabled"
        assert (await col.health()).status == "disabled"


def test_metadata_date_key_order():
    assert metadata_date({"publishedTime": "2026-10-01"}) == "2026-10-01"
    assert metadata_date({"og:title": "x", "DATEPUBLISHED": "2026-10-02"}) == "2026-10-02"
    assert metadata_date({"article:published_time": ["", "2026-10-03"]}) == "2026-10-03"
    assert metadata_date({"title": "x"}) is None and metadata_date(None) is None
