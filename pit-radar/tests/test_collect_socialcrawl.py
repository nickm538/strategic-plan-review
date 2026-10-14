"""SocialCrawl collector: discovery, call validation, mapping (no author fields), every error.type branch,
budget deferral, URL rewrites (docs/04 §2). All HTTP is mocked with respx."""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from app.pipeline.collectors import base as collectors_base
from app.pipeline.collectors.base import Budget
from app.pipeline.collectors.socialcrawl import SocialCrawlCollector
from app.pipeline.sources import SourcesConfig
from app.pipeline.types import PlannedQuery

BASE = "https://sc.test"
NOW = dt.datetime(2026, 10, 14, 16, 0, tzinfo=dt.timezone.utc)
FIX = Path(__file__).parent / "fixtures" / "collect"
RESP = json.loads((FIX / "sc_responses.json").read_text())
CATALOG = json.loads((FIX / "sc_catalog.json").read_text())
SECRET = "sc-test-key-not-real"
AUTHOR_KEYS = {"author", "username", "display_name", "avatar_url", "author_url", "post_author", "handle", "user"}


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """request_json's retry backoff sleeps; make it instant for this module only."""
    async def _sleep(_s: float) -> None:
        return None
    monkeypatch.setattr(collectors_base, "asyncio", SimpleNamespace(sleep=_sleep))


@pytest.fixture
def normalize_ready():
    from app.pipeline import normalize
    try:
        normalize.canonical_url("https://example.com")
        normalize.parse_datetime("2026-10-13T00:00:00Z", now=NOW)
    except NotImplementedError:
        pytest.skip("waiting for app.pipeline.normalize")


def _entry(eid: str, params: list[dict[str, Any]], credits: float = 1.0, **roles: Any) -> dict[str, Any]:
    e = {"id": eid, "path": f"/v1/{eid}", "method": "GET", "credits": credits, "params": params, "pinned": False,
         "query_param": None, "subreddit_param": None, "post_param": None, "hashtag_param": None,
         "sort_param": None, "sort_value": None, "time_param": None, "time_value": None, "cursor_param": None,
         "extra_params": {}}
    e.update(roles)
    return e


def _p(name: str, required: bool = False, enum: list[str] | None = None) -> dict[str, Any]:
    return {"name": name, "required": required, "type": "string", "description": "", "enum": enum}


RESOLVED = {"resolved_at": "2026-10-14T00:00:00+00:00", "unresolved": [], "capabilities": {
    "keyword_search": {
        "reddit": _entry("reddit/search", [_p("query", True), _p("sort", enum=["relevance", "new"]),
                                           _p("timeframe", enum=["day", "week"]), _p("after")],
                         query_param="query", sort_param="sort", sort_value="new", time_param="timeframe",
                         time_value="week", cursor_param="after"),
        "twitter": _entry("twitter/search/tweets", [_p("query", True)], query_param="query"),
    },
    "subreddit_new": {"reddit": _entry("reddit/subreddit", [_p("subreddit", True), _p("sort", enum=["new", "top"])],
                                       subreddit_param="subreddit", sort_param="sort", sort_value="new")},
    "post_comments": {"reddit": _entry("reddit/post/comments", [_p("url", True), _p("cursor")], credits=5.0,
                                       post_param="url", cursor_param="cursor")},
}}


def _q(capability="keyword_search", platform="reddit", query='homeless "Long Island"', **kw) -> PlannedQuery:
    return PlannedQuery(provider="socialcrawl", capability=capability, query=query, platform=platform, **kw)


def _unpinned_sources() -> SourcesConfig:
    """The shipped sources.yaml with every pinned_endpoint cleared, so discovery is exercised from scratch."""
    src = SourcesConfig.load()
    raw = copy.deepcopy(src.raw)
    for cap in raw["socialcrawl"]["capabilities"].values():
        for plat in cap.get("platforms", {}).values():
            plat["pinned_endpoint"] = None
    return SourcesConfig(raw=raw)


def _collector(http: httpx.AsyncClient, resolved: dict | None = None, *, sources: SourcesConfig | None = None,
               **kw) -> SocialCrawlCollector:
    return SocialCrawlCollector(api_key=SECRET, base_url=BASE, sources=sources or _unpinned_sources(), http=http,
                                resolved_endpoints=copy.deepcopy(RESOLVED if resolved is None else resolved),
                                now=lambda: NOW, **kw)


def _budget(cap: float = 100.0) -> Budget:
    return Budget(per_scan={"socialcrawl": cap}, per_day={"socialcrawl": 1000.0})


def _walk_keys(obj: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(str(k))
            keys |= _walk_keys(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            keys |= _walk_keys(v)
    return keys


def _err(etype: str, status: int) -> httpx.Response:
    return httpx.Response(status, json={"success": False, "error": {"type": etype, "message": "synthetic",
                                                                     "status": status}},
                          headers={"retry-after": "0"})


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _catalog_router(router: respx.MockRouter, *, endpoints: dict[str, dict], guides: dict[str, dict]) -> list:
    seen: list = []

    def endpoints_cb(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        assert request.headers["x-api-key"] == SECRET
        plat = request.url.params.get("platform")
        return httpx.Response(200, json=endpoints.get(plat, {"success": True, "data": {"endpoints": []}}))

    def guide_cb(request: httpx.Request) -> httpx.Response:
        eid = request.url.params.get("id")
        if eid in guides:
            return httpx.Response(200, json=guides[eid])
        return _err("ENDPOINT_NOT_FOUND", 404)

    router.get(f"{BASE}/v1/utility/endpoints").mock(side_effect=endpoints_cb)
    router.get(f"{BASE}/v1/utility/endpoint").mock(side_effect=guide_cb)
    return seen


async def test_discovery_parses_two_catalog_shapes():
    a, b = CATALOG["shape_a"], CATALOG["shape_b"]
    async with respx.mock(assert_all_called=False) as router, httpx.AsyncClient() as http:
        seen = _catalog_router(router, endpoints={"reddit": a["endpoints_reddit"], "twitter": b["endpoints_twitter"]},
                               guides={**a["guides"], **b["guides"]})
        col = _collector(http, resolved={})
        res = await col.discover()
    caps = res["capabilities"]
    assert caps["keyword_search"]["reddit"]["id"] == "reddit/search"
    rs = caps["keyword_search"]["reddit"]
    assert (rs["query_param"], rs["sort_value"], rs["time_value"], rs["cursor_param"]) == ("query", "new", "week", "after")
    assert rs["credits"] == 1.0 and rs["extra_params"] == {"judgments": "off"} and rs["pinned"] is False
    assert {p["name"] for p in rs["params"]} >= {"query", "sort", "timeframe", "after", "judgments"}
    tw = caps["keyword_search"]["twitter"]
    assert tw["id"] == "twitter/search/tweets" and (tw["sort_param"], tw["sort_value"]) == ("search_type", "Latest")
    assert caps["subreddit_new"]["reddit"]["id"] == "reddit/subreddit"
    assert caps["subreddit_search"]["reddit"]["id"] == "reddit/subreddit/search"
    pc = caps["post_comments"]["reddit"]
    assert pc["id"] == "reddit/post/comments" and pc["post_param"] == "url" and pc["credits"] == 5.0
    # platforms with no catalog entries are reported, never invented
    assert any(u.startswith("keyword_search/tiktok") for u in res["unresolved"])
    assert "news_search" not in caps
    # the discovery searches used the capability words and the platform filter
    assert {"search": "subreddit posts", "platform": "reddit"} in seen
    assert col.resolved_endpoints is res


async def test_discovery_prefers_pinned_endpoint():
    a = CATALOG["shape_a"]
    sources = SourcesConfig.load(overrides={"socialcrawl": {"capabilities": {"keyword_search": {"platforms": {
        "reddit": {"pinned_endpoint": "reddit/subreddit/search"}}}}}})
    async with respx.mock(assert_all_called=False) as router, httpx.AsyncClient() as http:
        _catalog_router(router, endpoints={"reddit": a["endpoints_reddit"]}, guides=a["guides"])
        col = SocialCrawlCollector(api_key=SECRET, base_url=BASE, sources=sources, http=http, now=lambda: NOW)
        res = await col.discover()
    entry = res["capabilities"]["keyword_search"]["reddit"]
    assert entry["id"] == "reddit/subreddit/search" and entry["pinned"] is True


async def test_discovery_auth_failure_keeps_previous_map():
    async with respx.mock(assert_all_called=False) as router, httpx.AsyncClient() as http:
        router.get(f"{BASE}/v1/utility/endpoints").mock(return_value=_err("INVALID_API_KEY", 401))
        router.get(f"{BASE}/v1/utility/endpoint").mock(return_value=_err("INVALID_API_KEY", 401))
        col = _collector(http)
        res = await col.discover()
    assert res["capabilities"] == RESOLVED["capabilities"] and res.get("stale") is True
    assert any("SOCIALCRAWL_API_KEY" in e for e in res["errors"])
    assert SECRET not in json.dumps(res)


async def test_discovery_without_key_resolves_nothing():
    async with httpx.AsyncClient() as http:
        col = SocialCrawlCollector(api_key="", base_url=BASE, sources=SourcesConfig.load(), http=http, now=lambda: NOW)
        res = await col.discover()
    assert res["capabilities"] == {} and res["unresolved"]


# ---------------------------------------------------------------------------
# Run: success and mapping
# ---------------------------------------------------------------------------
async def test_reddit_search_success_maps_items_without_author_fields(normalize_ready):
    budget = _budget()
    async with respx.mock() as router, httpx.AsyncClient() as http:
        route = router.get(f"{BASE}/v1/reddit/search").mock(return_value=httpx.Response(200, json=RESP["reddit_search_ok"]))
        col = _collector(http)
        res = await col.run([_q(place_id=7, place_name="Hicksville")], budget)
    sent = route.calls.last.request
    assert dict(sent.url.params) == {"query": 'homeless "Long Island"', "sort": "new", "timeframe": "week"}
    assert sent.headers["x-api-key"] == SECRET
    urls = sorted(i.url for i in res.items)
    # r/nyc is excluded; the undated row is dropped (F3)
    assert urls == ["https://www.reddit.com/r/LongIsland/comments/abc123/tents_behind_the_plaza",
                    "https://www.reddit.com/r/LongIsland/comments/jkl012/link_post"]
    first = next(i for i in res.items if "abc123" in i.url)
    assert first.provider == "socialcrawl" and first.platform == "reddit" and first.platform_post_id == "abc123"
    assert first.published_at == dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.timezone.utc)
    assert first.date_source == "platform" and first.media_present is True and first.lang == "en"
    assert first.context.subreddit == "LongIsland" and first.context.source_scope == "li_scoped"
    assert first.context.query_place_id == 7 and first.context.query == 'homeless "Long Island"'
    assert "Tents behind the plaza" in first.text and "tarp" in first.text
    epoch = next(i for i in res.items if "jkl012" in i.url)
    assert epoch.published_at == dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.timezone.utc)
    for item in res.items:
        d = item.to_dict()
        assert not (_walk_keys(d) & AUTHOR_KEYS)
        blob = json.dumps(d)
        assert "synthetic_user" not in blob and "Synthetic Person" not in blob and "example.invalid" not in blob
    assert col.last_stats.get("no_date") == 1 and col.last_stats.get("excluded_subreddit") == 1
    # credits: estimate 1, actual 1; balance read into health
    assert res.credits_used == 1.0 and budget.spent_scan["socialcrawl"] == 1.0 and res.requests == 1
    assert res.health.status == "ok" and res.health.credits_remaining == 2480
    log = res.logs[0]
    assert log.status == "ok" and log.items == 2 and log.credits == 1.0 and log.request_id == "req-synthetic-1"
    assert log.endpoint == "/v1/reddit/search" and log.place_id == 7
    assert SECRET not in json.dumps([vars(e) for e in res.logs], default=str)


async def test_subreddit_new_and_comments_flattened(normalize_ready):
    budget = _budget()
    thread = "https://www.reddit.com/r/LongIsland/comments/abc123/tents_behind_the_plaza"
    async with respx.mock() as router, httpx.AsyncClient() as http:
        sub = router.get(f"{BASE}/v1/reddit/subreddit").mock(return_value=httpx.Response(200, json=RESP["reddit_search_ok"]))
        com = router.get(f"{BASE}/v1/reddit/post/comments").mock(return_value=httpx.Response(200, json=RESP["reddit_comments_ok"]))
        col = _collector(http)
        res = await col.run([
            _q("subreddit_new", query="", subreddit="LongIsland", source_scope="li_scoped"),
            _q("post_comments", query="", subreddit="LongIsland", params={"post_url": thread, "post_id": "abc123"}),
        ], budget)
    assert dict(sub.calls.last.request.url.params) == {"subreddit": "LongIsland", "sort": "new"}
    assert dict(com.calls.last.request.url.params) == {"url": thread}
    comments = [i for i in res.items if i.context.is_comment]
    assert sorted(i.url for i in comments) == [thread + "/c1", thread + "/c2"]      # deleted comment dropped
    c1 = next(i for i in comments if i.url.endswith("/c1"))
    assert c1.context.parent_title == "Tents behind the plaza" and c1.context.source_scope == "li_scoped"
    c2 = next(i for i in comments if i.url.endswith("/c2"))
    assert c2.published_at == dt.datetime(2026, 10, 13, 23, 0, tzinfo=dt.timezone.utc)   # its own timestamp
    for item in comments:
        assert not (_walk_keys(item.to_dict()) & AUTHOR_KEYS)
        assert "synthetic_commenter" not in json.dumps(item.to_dict())
    assert budget.spent_scan["socialcrawl"] == 6.0 and res.credits_used == 6.0


async def test_x_url_rewritten_and_profile_only_url_dropped(normalize_ready):
    async with respx.mock() as router, httpx.AsyncClient() as http:
        router.get(f"{BASE}/v1/twitter/search/tweets").mock(return_value=httpx.Response(200, json=RESP["twitter_search_ok"]))
        col = _collector(http)
        res = await col.run([_q(platform="twitter", query="encampment Hicksville")], _budget())
    assert [i.url for i in res.items] == ["https://x.com/i/web/status/1845000000000000001"]
    item = res.items[0]
    assert item.platform == "x" and item.context.hashtags == ["longisland"]
    assert "synthetic_x_user" not in json.dumps(item.to_dict())
    assert col.last_stats.get("no_post_url") == 1


# ---------------------------------------------------------------------------
# Run: validation, budget, errors
# ---------------------------------------------------------------------------
async def test_invalid_call_is_skipped_before_sending():
    async with respx.mock(assert_all_called=False) as router, httpx.AsyncClient() as http:
        route = router.get(f"{BASE}/v1/reddit/search")
        col = _collector(http)
        res = await col.run([_q(query="")], _budget())
    assert not route.called
    assert res.logs[0].status == "error" and res.logs[0].error_type == "invalid_params"
    assert any("call skipped" in e for e in res.errors)


async def test_unknown_parameter_rejected_by_validation():
    resolved = copy.deepcopy(RESOLVED)
    resolved["capabilities"]["keyword_search"]["reddit"]["extra_params"] = {"bogus": "1"}
    async with respx.mock(assert_all_called=False) as router, httpx.AsyncClient() as http:
        route = router.get(f"{BASE}/v1/reddit/search")
        res = await _collector(http, resolved).run([_q()], _budget())
    assert not route.called and "unknown parameter" in res.errors[0]


async def test_unresolved_capability_is_reported_not_called():
    async with respx.mock(assert_all_called=False) as router, httpx.AsyncClient() as http:
        res = await _collector(http).run([_q(platform="tiktok")], _budget())
    assert not router.calls and res.logs[0].error_type == "unresolved_endpoint"


async def test_budget_cap_defers_remaining_queries(normalize_ready):
    budget = _budget(cap=1.0)
    qs = [_q(query="a", priority=5), _q(query="b", priority=1)]
    async with respx.mock() as router, httpx.AsyncClient() as http:
        route = router.get(f"{BASE}/v1/reddit/search").mock(return_value=httpx.Response(200, json=RESP["reddit_search_ok"]))
        res = await _collector(http, max_in_flight=1).run(qs, budget)
    assert route.call_count == 1 and route.calls.last.request.url.params["query"] == "a"
    assert [q.query for q in res.deferred] == ["b"]
    assert sorted(e.status for e in res.logs) == ["deferred", "ok"]


async def test_auth_error_stops_provider_and_marks_unhealthy():
    qs = [_q(query=f"q{i}") for i in range(3)]
    async with respx.mock() as router, httpx.AsyncClient() as http:
        route = router.get(f"{BASE}/v1/reddit/search").mock(return_value=_err("INVALID_API_KEY", 401))
        budget = _budget()
        res = await _collector(http, max_in_flight=1).run(qs, budget)
    assert route.call_count == 1
    assert res.health.status == "unhealthy" and "SOCIALCRAWL_API_KEY" in res.health.message
    assert res.deferred == [] and any("skipped" in e for e in res.errors)
    assert budget.spent_scan["socialcrawl"] == 0.0           # failed call refunded
    assert SECRET not in json.dumps(res.errors)


async def test_missing_api_key_error_type_same_as_invalid():
    async with respx.mock() as router, httpx.AsyncClient() as http:
        router.get(f"{BASE}/v1/reddit/search").mock(return_value=_err("MISSING_API_KEY", 401))
        res = await _collector(http).run([_q()], _budget())
    assert res.health.status == "unhealthy"


async def test_insufficient_credits_stops_and_defers():
    qs = [_q(query=f"q{i}") for i in range(3)]
    async with respx.mock() as router, httpx.AsyncClient() as http:
        route = router.get(f"{BASE}/v1/reddit/search").mock(return_value=_err("INSUFFICIENT_CREDITS", 402))
        res = await _collector(http, max_in_flight=1).run(qs, _budget())
    assert route.call_count == 1
    assert res.health.status == "unhealthy" and "out of credits" in res.health.message
    assert sorted(q.query for q in res.deferred) == ["q0", "q1", "q2"]


async def test_invalid_request_disables_endpoint_param_combo():
    async with respx.mock() as router, httpx.AsyncClient() as http:
        route = router.get(f"{BASE}/v1/reddit/search").mock(return_value=_err("INVALID_REQUEST", 400))
        col = _collector(http)
        res1 = await col.run([_q(query="a")], _budget())
        res2 = await col.run([_q(query="b")], _budget())
    assert route.call_count == 1
    assert res1.logs[0].error_type == "INVALID_REQUEST" and res1.deferred == []
    assert res2.logs[0].error_type == "disabled_until_rediscovery"
    # the disable travels with the endpoint map, so a new collector instance (next scan) honours it
    persisted = res1.discovered_endpoints
    assert persisted and list(persisted["disabled"].values()) == ["INVALID_REQUEST"]
    async with respx.mock(assert_all_called=False) as router, httpx.AsyncClient() as http:
        route = router.get(f"{BASE}/v1/reddit/search")
        res3 = await _collector(http, persisted).run([_q(query="c")], _budget())
    assert not route.called and res3.logs[0].error_type == "disabled_until_rediscovery"


async def test_endpoint_not_found_triggers_catalog_refresh():
    a = CATALOG["shape_a"]
    async with respx.mock(assert_all_called=False) as router, httpx.AsyncClient() as http:
        router.get(f"{BASE}/v1/reddit/search").mock(return_value=_err("ENDPOINT_NOT_FOUND", 404))
        _catalog_router(router, endpoints={"reddit": a["endpoints_reddit"]}, guides=a["guides"])
        col = _collector(http)
        res = await col.run([_q()], _budget())
    assert any("refresh" in e for e in res.errors)
    assert res.discovered_endpoints is not None
    assert res.discovered_endpoints["capabilities"]["keyword_search"]["reddit"]["id"] == "reddit/search"
    assert res.logs[0].error_type == "ENDPOINT_NOT_FOUND"


async def test_resource_not_found_is_empty_not_error():
    async with respx.mock() as router, httpx.AsyncClient() as http:
        router.get(f"{BASE}/v1/reddit/subreddit").mock(return_value=_err("RESOURCE_NOT_FOUND", 404))
        res = await _collector(http).run([_q("subreddit_new", query="", subreddit="Montauk")], _budget())
    assert res.logs[0].status == "empty" and res.logs[0].error_type is None
    assert res.errors == [] and res.health.status == "ok"


async def test_concurrency_limit_is_retried(normalize_ready):
    async with respx.mock() as router, httpx.AsyncClient() as http:
        route = router.get(f"{BASE}/v1/reddit/search").mock(side_effect=[
            _err("CONCURRENCY_LIMIT", 429), httpx.Response(200, json=RESP["reddit_search_ok"])])
        res = await _collector(http).run([_q()], _budget())
    assert route.call_count == 2 and len(res.items) == 2 and res.logs[0].status == "ok"


async def test_persistent_429_defers_the_query():
    async with respx.mock() as router, httpx.AsyncClient() as http:
        route = router.get(f"{BASE}/v1/reddit/search").mock(return_value=_err("CONCURRENCY_LIMIT", 429))
        budget = _budget()
        res = await _collector(http).run([_q()], budget)
    assert route.call_count == 3                        # request_json's retry budget
    assert len(res.deferred) == 1 and res.logs[0].status == "deferred"
    assert budget.spent_scan["socialcrawl"] == 0.0


async def test_network_error_defers():
    async with respx.mock() as router, httpx.AsyncClient() as http:
        router.get(f"{BASE}/v1/reddit/search").mock(side_effect=httpx.ConnectError("boom"))
        res = await _collector(http).run([_q()], _budget())
    assert len(res.deferred) == 1 and res.health.status == "degraded"


async def test_run_without_key_is_disabled():
    async with httpx.AsyncClient() as http:
        col = SocialCrawlCollector(api_key="", base_url=BASE, sources=SourcesConfig.load(), http=http, now=lambda: NOW)
        res = await col.run([_q()], _budget())
    assert res.health.status == "disabled" and res.items == [] and res.deferred == []


async def test_run_with_empty_map_discovers_first():
    a = CATALOG["shape_a"]
    async with respx.mock(assert_all_called=False) as router, httpx.AsyncClient() as http:
        _catalog_router(router, endpoints={"reddit": a["endpoints_reddit"]}, guides=a["guides"])
        res = await _collector(http, resolved={}).run([], _budget())
    assert res.discovered_endpoints and res.discovered_endpoints["capabilities"]["subreddit_new"]["reddit"]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("response,status,credits", [
    (httpx.Response(200, json={"success": True, "data": {"credits_remaining": 2481}, "credits_used": 0}), "ok", 2481),
    (httpx.Response(200, json={"success": True, "data": {"balance": 0}}), "unhealthy", 0),
    (_err("INVALID_API_KEY", 401), "unhealthy", None),
    (httpx.Response(500, json={"success": False, "error": {"type": "INTERNAL_ERROR"}}), "unknown", None),
])
async def test_health_reads_balance(response, status, credits):
    async with respx.mock() as router, httpx.AsyncClient() as http:
        router.get(f"{BASE}/v1/credits/balance").mock(return_value=response)
        h = await _collector(http).health()
    assert h.status == status and h.credits_remaining == credits
    assert SECRET not in h.message


async def test_health_without_key_is_disabled():
    async with httpx.AsyncClient() as http:
        col = SocialCrawlCollector(api_key="", base_url=BASE, sources=SourcesConfig.load(), http=http)
        assert (await col.health()).status == "disabled"
