"""Pure collector helpers: catalog parsing, ranking, parameter roles, validation, URL rewrites."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline.collectors import _sc_catalog as cat
from app.pipeline.collectors._common import (
    hashtags_from, is_profile_url, iso_z, place_tag_name, rewrite_post_url, truthy_media,
)
from app.pipeline.collectors._sc_adapters import extract_rows, flatten_comments, unwrap

FIX = json.loads((Path(__file__).parent / "fixtures" / "collect" / "sc_catalog.json").read_text())


@pytest.mark.parametrize("value,expected", [
    (1, 1.0), ("5", 5.0), ("1-34 (metered)", 1.0), ("5-9 (metered)", 5.0), ({"min": 2, "max": 9}, 2.0),
    ("advanced", 5.0), ("premium", 10.0), ("Free (0 credits)", 0.0), (None, None), (True, None), ("n/a", None),
])
def test_parse_credits(value, expected):
    assert cat.parse_credits(value) == expected


def test_parse_catalog_shape_a():
    entries = cat.parse_catalog(FIX["shape_a"]["endpoints_reddit"], platform="reddit")
    ids = [e["id"] for e in entries]
    assert "reddit/search" in ids and "reddit/post/comments" in ids
    search = next(e for e in entries if e["id"] == "reddit/search")
    assert search["path"] == "/v1/reddit/search" and search["credits"] == 1.0
    assert [p["name"] for p in search["params"]] == ["query", "sort", "timeframe", "after"]
    assert search["params"][0]["required"] is True and search["params"][1]["required"] is False


def test_parse_catalog_shape_b_and_guide():
    entries = cat.parse_catalog(FIX["shape_b"]["endpoints_twitter"], platform="twitter")
    assert {e["id"] for e in entries} == {"twitter/profile", "twitter/ai-search", "twitter/search/users",
                                          "twitter/search/tweets"}
    assert next(e for e in entries if e["id"] == "twitter/ai-search")["credits"] == 5.0
    g = cat.parse_guide(FIX["shape_b"]["guides"]["twitter/search/tweets"])
    assert g["id"] == "twitter/search/tweets" and g["path"] == "/v1/twitter/search/tweets" and g["credits"] == 1.0
    names = {p["name"]: p for p in g["params"]}
    assert names["query"]["required"] and names["search_type"]["enum"] == ["Top", "Latest"]


def test_parse_catalog_filters_other_platforms_and_accepts_bare_list():
    body = {"data": [{"path": "/v1/tiktok/search"}, {"path": "/v1/reddit/search", "credits": 1}]}
    assert [e["id"] for e in cat.parse_catalog(body, platform="reddit")] == ["reddit/search"]


def test_rank_candidates_prefers_cheapest_specific_match():
    entries = cat.parse_catalog(FIX["shape_a"]["endpoints_reddit"], platform="reddit")
    assert cat.rank_candidates(entries, "keyword_search")[0]["id"] == "reddit/search"
    assert cat.rank_candidates(entries, "subreddit_new")[0]["id"] == "reddit/subreddit"
    assert cat.rank_candidates(entries, "subreddit_search")[0]["id"] == "reddit/subreddit/search"
    assert cat.rank_candidates(entries, "post_comments")[0]["id"] == "reddit/post/comments"
    tw = cat.parse_catalog(FIX["shape_b"]["endpoints_twitter"], platform="twitter")
    assert [e["id"] for e in cat.rank_candidates(tw, "keyword_search")] == ["twitter/search/tweets"]


def test_keyword_search_skips_account_search_by_description():
    entries = [{"id": "instagram/search", "method": "GET", "credits": 1.0,
                "description": "Search Instagram accounts, hashtags, and places"},
               {"id": "instagram/search/reels", "method": "GET", "credits": 1.0, "description": "Search Instagram reels"}]
    assert [e["id"] for e in cat.rank_candidates(entries, "keyword_search")] == ["instagram/search/reels"]


def test_derive_roles_reddit_search():
    g = cat.parse_guide(FIX["shape_a"]["guides"]["reddit/search"])
    roles = cat.derive_roles("keyword_search", g["params"], sort_prefs=["latest", "recent", "new", "date"])
    assert roles["query_param"] == "query"
    assert (roles["sort_param"], roles["sort_value"]) == ("sort", "new")
    assert (roles["time_param"], roles["time_value"]) == ("timeframe", "week")
    assert roles["cursor_param"] == "after"
    assert roles["extra_params"] == {"judgments": "off"}
    assert cat.roles_satisfied("keyword_search", roles)


def test_derive_roles_other_capabilities():
    comments = cat.parse_guide(FIX["shape_a"]["guides"]["reddit/post/comments"])
    r = cat.derive_roles("post_comments", comments["params"])
    assert r["post_param"] == "url" and r["cursor_param"] == "cursor" and r["query_param"] is None
    sub = cat.parse_guide(FIX["shape_a"]["guides"]["reddit/subreddit"])
    r = cat.derive_roles("subreddit_new", sub["params"])
    assert r["subreddit_param"] == "subreddit" and r["sort_value"] == "new"
    tw = cat.parse_guide(FIX["shape_b"]["guides"]["twitter/search/tweets"])
    r = cat.derive_roles("keyword_search", tw["params"], sort_prefs=["latest", "recent", "new", "date"])
    assert (r["sort_param"], r["sort_value"]) == ("search_type", "Latest")
    assert not cat.roles_satisfied("post_comments", cat.derive_roles("post_comments", sub["params"]))


def test_choose_value_from_description_when_no_enum():
    params = [{"name": "q", "required": True, "enum": None, "description": ""},
              {"name": "sort", "required": False, "enum": None, "description": "One of top, latest or relevance"}]
    r = cat.derive_roles("keyword_search", params, sort_prefs=["latest", "new"])
    assert r["query_param"] == "q" and r["sort_value"] == "latest"


def test_validate_call():
    spec = [{"name": "query", "required": True, "enum": None}, {"name": "sort", "required": False, "enum": ["new", "top"]}]
    assert cat.validate_call(spec, {"query": "x", "sort": "new"}) is None
    assert "unknown" in cat.validate_call(spec, {"query": "x", "bogus": 1})
    assert "missing" in cat.validate_call(spec, {"sort": "new"})
    assert "not allowed" in cat.validate_call(spec, {"query": "x", "sort": "hot"})


@pytest.mark.parametrize("platform,kwargs,expected", [
    ("x", {"url": "", "post_id": "1845000000000000001"}, "https://x.com/i/web/status/1845000000000000001"),
    ("x", {"url": "https://twitter.com/someone/status/1845000000000000002?s=20", "post_id": "x"},
     "https://x.com/i/web/status/1845000000000000002"),
    ("x", {"url": "https://x.com/someone", "post_id": "abc"}, None),
    ("instagram", {"url": "https://www.instagram.com/reel/AbC123xyz/?igsh=1", "post_id": "999"},
     "https://www.instagram.com/p/AbC123xyz/"),
    ("instagram", {"url": "https://www.instagram.com/someone/", "post_id": "999"}, None),
    ("youtube", {"url": "https://www.youtube.com/shorts/abcdefghijk", "post_id": ""},
     "https://www.youtube.com/watch?v=abcdefghijk"),
    ("youtube", {"url": "https://www.youtube.com/@channel", "post_id": "abcdefghijk"},
     "https://www.youtube.com/watch?v=abcdefghijk"),
    ("tiktok", {"url": "https://www.tiktok.com/@someone/video/7234567890123456789?lang=en", "post_id": "1"},
     "https://www.tiktok.com/@someone/video/7234567890123456789"),
    ("tiktok", {"url": "https://www.tiktok.com/@someone", "post_id": "1"}, None),
    ("threads", {"url": "https://www.threads.net/@someone/post/Cabc123?x=1", "post_id": ""},
     "https://www.threads.net/@someone/post/Cabc123"),
    ("bluesky", {"url": "", "post_id": "at://did:plc:abc123/app.bsky.feed.post/3kxyz"},
     "https://bsky.app/profile/did:plc:abc123/post/3kxyz"),
    ("bluesky", {"url": "", "post_id": "3kxyz"}, None),
    ("facebook", {"url": "https://www.facebook.com/somepage/posts/12345?ref=x", "post_id": ""},
     "https://www.facebook.com/somepage/posts/12345"),
    ("facebook", {"url": "https://www.facebook.com/profile.php?id=1", "post_id": ""}, None),
    ("reddit", {"url": "https://example.com/article", "post_id": "t3_abc12", "permalink": ""},
     "https://www.reddit.com/comments/abc12"),
    ("reddit", {"url": "", "post_id": "", "permalink": "/r/LongIsland/comments/abc12/title/"},
     "https://www.reddit.com/r/LongIsland/comments/abc12/title/"),
    ("reddit", {"url": "https://www.reddit.com/user/someone", "post_id": "", "permalink": ""}, None),
])
def test_rewrite_post_url(platform, kwargs, expected):
    assert rewrite_post_url(platform, **kwargs) == expected


def test_profile_url_detection():
    assert is_profile_url("x", "https://x.com/someone")
    assert not is_profile_url("x", "https://x.com/i/web/status/1")
    assert is_profile_url("reddit", "https://www.reddit.com/u/someone")
    assert not is_profile_url("news", "https://www.newsday.com/story")


def test_small_helpers():
    assert hashtags_from([{"title": "LongIsland"}, "#help", "help"]) == ["LongIsland", "help"]
    assert hashtags_from(None, "tents again #LongIsland #lirr") == ["LongIsland", "lirr"]
    assert place_tag_name({"name": "Hicksville", "lat": 40.7, "lng": -73.5}) == "Hicksville"
    assert place_tag_name("40.76, -73.52") is None
    assert truthy_media({"content": {"media_urls": ["x"]}}, ["content.media_urls"])
    assert not truthy_media({"content": {"media_urls": []}, "is_video": False}, ["content.media_urls", "is_video"])
    import datetime as dt
    assert iso_z(dt.datetime(2026, 9, 14, 16, 0, tzinfo=dt.timezone.utc)) == "2026-09-14T16:00:00.000Z"


def test_rows_unwrap_and_comment_flattening():
    assert extract_rows({"items": [1, 2]}) == [1, 2]
    assert extract_rows([3]) == [3]
    assert extract_rows({"nothing": 1}) == []
    assert unwrap({"post": {"id": "a"}}) == {"id": "a"}
    assert unwrap({"id": "b", "post": {"id": "a"}}) == {"id": "b", "post": {"id": "a"}}
    tree = [{"id": "1", "replies": [{"id": "2", "replies": [{"id": "3"}]}]}, {"comment": {"id": "4"}}]
    assert [(r["id"], d) for r, d in flatten_comments(tree)] == [("1", 0), ("2", 1), ("3", 2), ("4", 0)]
    assert len(list(flatten_comments(tree, max_total=2))) == 2
