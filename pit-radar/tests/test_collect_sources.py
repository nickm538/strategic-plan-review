"""config/sources.yaml loader: overrides, scope rules, excluded subreddits, Exa domains (docs/04)."""
from __future__ import annotations

import pytest

from app.pipeline.sources import SourcesConfig, deep_merge, host_matches, scope_max, subreddit_from_url, validate_overrides


@pytest.fixture
def sources() -> SourcesConfig:
    return SourcesConfig.load()


def test_real_file_loads_and_lists_enabled_subreddits(sources):
    subs = sources.enabled_subreddits()
    assert "longisland" in subs and "LIRR" in subs
    assert "SBU" not in subs and "Hofstra" not in subs          # disabled in the file
    assert {"queens", "brooklyn", "nyc", "longislandcity"} <= sources.excluded_subreddits()


def test_deep_merge_merges_dicts_and_replaces_lists():
    base = {"a": {"x": 1, "y": [1, 2]}, "b": [1], "c": "keep"}
    over = {"a": {"y": [9]}, "b": [7, 8]}
    assert deep_merge(base, over) == {"a": {"x": 1, "y": [9]}, "b": [7, 8], "c": "keep"}
    assert base == {"a": {"x": 1, "y": [1, 2]}, "b": [1], "c": "keep"}  # not mutated


def test_overrides_replace_subreddit_list_and_merge_capability_dicts():
    over = {
        "subreddits": [{"name": "LongIsland", "enabled": True}, {"name": "Queens", "enabled": True}],
        "socialcrawl": {"capabilities": {"keyword_search": {"platforms": {"tiktok": {"enabled": False}}}}},
    }
    s = SourcesConfig.load(overrides=over)
    # list replaced wholesale; an excluded name is never collected even if listed as enabled
    assert s.enabled_subreddits() == ["LongIsland"]
    plats = s.capability_platforms("keyword_search")
    assert "tiktok" not in plats and "twitter" in plats          # dict merged key by key
    assert s.socialcrawl_platform_map()["twitter"] == "x"       # untouched sibling keys survive


def test_invalid_overrides_are_ignored_not_fatal():
    s = SourcesConfig.load(overrides={"subreddits": "not-a-list"})
    assert "longisland" in s.enabled_subreddits()
    assert validate_overrides({"subreddits": "x"})
    assert validate_overrides({"bogus": {}})
    assert validate_overrides({"news_domains": {"li_scoped": [{"domain": "example.org"}]}}) == []


@pytest.mark.parametrize("url,sub,expected", [
    ("https://www.reddit.com/r/LongIsland/comments/abc/x", None, "li_scoped"),
    ("https://old.reddit.com/r/lirr/comments/abc/x", None, "li_scoped"),       # case-insensitive
    ("https://www.reddit.com/r/nyc/comments/abc", None, "general"),             # excluded, never li_scoped
    ("https://example.com/whatever", "Patchogue", "li_scoped"),                  # explicit subreddit
    ("https://www.newsday.com/long-island/x", None, "li_scoped"),
    ("https://longisland.news12.com/story", None, "li_scoped"),
    ("https://amp.newsday.com/story", None, "li_scoped"),                        # subdomain of li domain
    ("https://news12.com/story", None, "general"),                               # parent of an li subdomain
    ("https://patch.com/new-york/bayshore/x", None, "regional"),                 # patch.com stays regional
    ("https://www.pix11.com/news/x", None, "regional"),
    ("https://www.cbsnews.com/newyork/news/x", None, "regional"),
    ("https://www.cbsnews.com/news/national-story", None, "general"),           # only the /newyork section
    ("https://notnewsday.com/x", None, "general"),                               # suffix is not a subdomain
    ("", None, "general"),
])
def test_scope_for(sources, url, sub, expected):
    assert sources.scope_for(url=url, subreddit=sub) == expected


def test_disabled_subreddit_is_not_li_scoped(sources):
    assert sources.scope_for(url="https://www.reddit.com/r/SBU/comments/x", subreddit=None) == "general"


def test_exa_include_domains(sources):
    d = sources.exa_include_domains()
    assert d[0] == "newsday.com" and "patch.com" in d and "reddit.com" in d
    assert len(d) == len(set(d))


def test_helpers():
    assert host_matches("longisland.news12.com", "news12.com")
    assert not host_matches("news12.com", "longisland.news12.com")
    assert not host_matches("evilnewsday.com", "newsday.com")
    assert subreddit_from_url("https://np.reddit.com/r/Montauk/comments/x") == "Montauk"
    assert subreddit_from_url("https://example.com/r/Montauk") is None
    assert scope_max("general", "regional", None) == "regional"
    assert scope_max("li_scoped", "general") == "li_scoped"


def test_firecrawl_and_hashtag_config(sources):
    assert sources.firecrawl_scrape_max_per_scan() == 10
    assert sources.firecrawl_search()["tbs"] == "qdr:w"
    assert sources.enabled_hashtags() == []                     # all disabled by default
    s = SourcesConfig.load(overrides={"hashtags": [{"tag": "#synthetictag", "platforms": ["tiktok"], "enabled": True}]})
    assert s.enabled_hashtags("tiktok") == [("synthetictag", ["tiktok"])]
    assert s.enabled_hashtags("instagram") == []
