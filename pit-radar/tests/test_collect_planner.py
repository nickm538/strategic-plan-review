"""Query planner (docs/04 §6): rotation coverage, caps, deferred first, unresolved capabilities skipped,
determinism, template groups, comment queries."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any, Optional

import pytest
import yaml

from app.config import Settings
from app.paths import CONFIG_DIR
from app.pipeline.geo.gazetteer import PlaceRec
from app.pipeline.planner import (
    PRIO_DEFERRED, PRIO_HOT, PRIO_REGION, PRIO_ROTATION, PRIO_SUBREDDIT_NEW, fill_template, plan_comment_queries,
    plan_scan, station_name, template_group,
)
from app.pipeline.sources import SourcesConfig
from app.pipeline.types import ItemContext, NormalizedItem, PlannedQuery, PrefilterResult
from app.runtime_settings import Effective

TEMPLATES = yaml.safe_load((CONFIG_DIR / "lexicon.yaml").read_text())["query_templates"]
LEX = SimpleNamespace(query_templates=TEMPLATES)
KW_PLATFORMS = ["reddit", "twitter", "threads", "bluesky", "tiktok", "youtube", "facebook"]


def _entry(eid: str, credits: float = 1.0) -> dict[str, Any]:
    return {"id": eid, "path": f"/v1/{eid}", "credits": credits}


def _resolved(kw: Optional[list[str]] = None, **extra: Any) -> dict[str, Any]:
    caps = {
        "keyword_search": {p: _entry(f"{p}/search") for p in (KW_PLATFORMS if kw is None else kw)},
        "subreddit_new": {"reddit": _entry("reddit/subreddit")},
        "subreddit_search": {"reddit": _entry("reddit/subreddit/search")},
        "post_comments": {"reddit": _entry("reddit/post/comments", 5.0)},
        "news_search": {"google_news": _entry("google_news/search")},
    }
    caps.update(extra)
    return {"resolved_at": "2026-10-14T00:00:00+00:00", "capabilities": caps, "unresolved": []}


def _eff(pit: bool = False, **values: Any) -> Effective:
    return Effective(base=Settings(), values=values, overrides={}, pit_mode=pit, scans_paused=False)


def _places(n: int) -> list[PlaceRec]:
    kinds = ["cdp", "village", "lirr_station", "town", "park"]
    return [PlaceRec(name=f"Placename{i}" + (" Station" if kinds[i % 5] == "lirr_station" else ""),
                     kind=kinds[i % 5], county="Suffolk" if i % 2 else "Nassau", lat=40.8, lon=-73.1,
                     source="synthetic", source_id=str(i), id=1000 + i) for i in range(n)]


def _gaz(places: list[PlaceRec]):
    return SimpleNamespace(rotation_places=lambda: list(places))


@pytest.fixture
def sources() -> SourcesConfig:
    return SourcesConfig.load()


def _plan(sources, *, eff=None, gaz=None, cursor=0, hot=None, resolved=None, deferred=None, **kw):
    return plan_scan(eff=eff or _eff(), lexicon=LEX, sources=sources, gazetteer=gaz, rotation_cursor=cursor,
                     hot_places=hot or [], resolved_endpoints=_resolved() if resolved is None else resolved,
                     deferred=deferred, **kw)


def test_rotation_covers_every_place_and_wraps(sources):
    places = _places(53)
    gaz = _gaz(places)
    cursor, seen, wrapped = 0, set(), False
    for _ in range(40):
        r = _plan(sources, gaz=gaz, cursor=cursor)
        rot = {q.place_id for q in r.queries if q.family == "place"}
        assert 0 < len(rot) <= 8                     # PLACES_PER_SCAN default
        seen |= rot
        assert 0 <= r.new_cursor < len(places)
        wrapped = wrapped or r.new_cursor < cursor
        cursor = r.new_cursor
    assert seen == {p.id for p in places}
    assert wrapped


def test_caps_are_respected(sources):
    places = _places(40)
    r = _plan(sources, gaz=_gaz(places), hot=places[:9], eff=_eff())
    sc = [q for q in r.queries if q.provider == "socialcrawl"]
    assert sum(q.est_cost for q in sc) <= 40                       # SOCIALCRAWL_CREDITS_PER_SCAN
    assert len([q for q in r.queries if q.provider == "exa"]) <= 6  # EXA_SEARCHES_PER_SCAN
    fc = [q for q in r.queries if q.provider == "firecrawl"]
    assert len(fc) <= 3                                             # half of 6 left for scrape-for-date
    hot_ids = {q.place_id for q in r.queries if q.family == "hot"}
    assert len(hot_ids) <= 5                                        # HOT_PLACES_PER_SCAN
    assert len({q.place_id for q in r.queries if q.family == "place"}) <= 8
    # every enabled subreddit is always planned
    assert {q.subreddit for q in sc if q.capability == "subreddit_new"} == set(sources.enabled_subreddits())


def test_pit_mode_uses_pit_values(sources):
    places = _places(60)
    std = _plan(sources, gaz=_gaz(places), eff=_eff())
    pit = _plan(sources, gaz=_gaz(places), eff=_eff(pit=True))
    n_std = len({q.place_id for q in std.queries if q.family == "place"})
    n_pit = len({q.place_id for q in pit.queries if q.family == "place"})
    assert n_pit > n_std and n_pit <= 12
    assert len([q for q in pit.queries if q.provider == "exa"]) <= 10
    assert sum(q.est_cost for q in pit.queries if q.provider == "socialcrawl") <= 60


def test_tight_budget_shrinks_rotation_without_skipping_places(sources):
    places = _places(30)
    r = _plan(sources, gaz=_gaz(places), eff=_eff(socialcrawl_credits_per_scan=35.0))
    planned = sorted({q.place_id for q in r.queries if q.family == "place"})
    n = len(planned)
    assert 0 < n < 8
    assert planned == [p.id for p in places[:n]]
    assert r.new_cursor == n                               # resumes exactly after the last planned place
    assert any("wait for the next scan" in note for note in r.notes)
    assert sum(q.est_cost for q in r.queries if q.provider == "socialcrawl") <= 35 - 8.75   # comment reserve kept
    assert any("kept for the comment pass" in note for note in r.notes)


def test_exhausted_socialcrawl_budget_lets_exa_firecrawl_carry_rotation(sources):
    places = _places(30)
    r = _plan(sources, gaz=_gaz(places), eff=_eff(socialcrawl_credits_per_scan=12.0))
    rot = [q for q in r.queries if q.family == "place"]
    assert rot and {q.provider for q in rot} <= {"exa", "firecrawl"}
    planned = sorted({q.place_id for q in rot})
    assert planned == [p.id for p in places[:len(planned)]] and r.new_cursor == len(planned)
    assert any("Exa/Firecrawl only" in note for note in r.notes)


def test_deferred_come_first_and_are_deduplicated(sources):
    d1 = PlannedQuery(provider="socialcrawl", capability="keyword_search", query="carry me", platform="twitter",
                      family="place", place_id=5, priority=PRIO_ROTATION)
    d2 = PlannedQuery(provider="exa", capability="search", query="carry exa", family="hot", priority=PRIO_HOT)
    d_unresolved = PlannedQuery(provider="socialcrawl", capability="keyword_search", query="x", platform="tiktok")
    r = _plan(sources, gaz=_gaz(_places(10)), deferred=[d1, d2, d_unresolved],
              resolved=_resolved(kw=["reddit", "twitter"]))
    # deferred first, among themselves in their original priority order (hot before rotation)
    assert [q.query for q in r.queries[:2]] == ["carry exa", "carry me"]
    assert all(q.priority == PRIO_DEFERRED for q in r.queries[:2])
    assert r.queries[1].family == "place" and r.queries[1].place_id == 5
    assert all(q.query != "x" for q in r.queries)
    assert any("no longer resolved" in n for n in r.notes)
    prios = [q.priority for q in r.queries]
    assert prios == sorted(prios, reverse=True)
    assert r.queries[2].priority == PRIO_SUBREDDIT_NEW


def test_unresolved_capabilities_are_skipped_and_noted(sources):
    r = _plan(sources, gaz=_gaz(_places(10)), resolved=_resolved(kw=["reddit", "twitter"]))
    plats = {q.platform for q in r.queries if q.capability == "keyword_search"}
    assert plats <= {"reddit", "twitter"} and plats
    assert any("keyword_search on tiktok unavailable" in n for n in r.notes)
    empty = _plan(sources, gaz=_gaz(_places(10)), resolved={})
    assert not [q for q in empty.queries if q.provider == "socialcrawl"]
    assert [q for q in empty.queries if q.provider in ("exa", "firecrawl")]   # other providers still planned
    assert {q.place_id for q in empty.queries if q.family == "place"}         # rotation still advances


def test_region_queries_rotate_templates_across_platforms(sources):
    r = _plan(sources)
    region = [q for q in r.queries if q.family == "region" and q.capability == "keyword_search"]
    assert [q.platform for q in region] == KW_PLATFORMS
    assert len({q.query for q in region}) == len(region)      # different template on each platform
    assert all(q.query in TEMPLATES["region_wide"]["keyword"] for q in region)
    covered: set[tuple[str, str]] = set()
    for cursor in range(60):
        for q in _plan(sources, cursor=cursor).queries:
            if q.family == "region" and q.capability == "keyword_search":
                covered.add((q.platform, q.query))
    assert len(covered) >= 0.9 * len(KW_PLATFORMS) * len(TEMPLATES["region_wide"]["keyword"])
    exa = [q for q in r.queries if q.provider == "exa"]
    assert len(exa) == 2 and {bool(q.params.get("unrestricted")) for q in exa} == {False, True}
    assert any(q.capability == "news_search" and q.platform == "google_news" for q in r.queries)
    assert all(q.priority == PRIO_REGION for q in r.queries if q.family == "region")


def test_no_gazetteer_skips_places_but_plans_region(sources):
    r = _plan(sources, gaz=None, hot=_places(3), cursor=41)
    assert not [q for q in r.queries if q.family in ("hot", "place")]
    assert [q for q in r.queries if q.family == "region"]
    assert r.new_cursor == 42 and any("no gazetteer" in n for n in r.notes)


def test_gazetteer_stub_is_tolerated(sources):
    def boom():
        raise NotImplementedError
    r = _plan(sources, gaz=SimpleNamespace(rotation_places=boom))
    assert not [q for q in r.queries if q.family == "place"]


def test_hot_places_planned_with_place_ids(sources):
    places = _places(20)
    hot = [places[2], places[2], places[7]]               # duplicates collapse
    r = _plan(sources, gaz=_gaz(places), hot=hot)
    hq = [q for q in r.queries if q.family == "hot"]
    assert {q.place_id for q in hq} == {places[2].id, places[7].id}
    assert all(q.priority == PRIO_HOT and q.place_name for q in hq)
    assert len({q.platform for q in hq if q.provider == "socialcrawl" and q.place_id == places[2].id}) == 2


def test_output_is_deterministic(sources):
    places = _places(30)
    a = _plan(sources, gaz=_gaz(places), hot=places[:3], cursor=17)
    b = _plan(sources, gaz=_gaz(places), hot=places[:3], cursor=17)
    assert [q.key() for q in a.queries] == [q.key() for q in b.queries]
    assert a.new_cursor == b.new_cursor and a.notes == b.notes


def test_enabled_providers_limit(sources):
    r = _plan(sources, gaz=_gaz(_places(10)), enabled_providers={"exa"})
    assert {q.provider for q in r.queries} == {"exa"}
    assert len(r.queries) <= 6


def test_template_helpers():
    assert template_group("cdp") == "town_village_cdp" and template_group("city") == "town_village_cdp"
    assert template_group("lirr_station") == "lirr_station" and template_group("beach") == "landmark"
    assert station_name("Hicksville Station") == "Hicksville" and station_name("Ronkonkoma") == "Ronkonkoma"
    assert fill_template('"{place_station} LIRR" homeless', "Bay Shore Station") == '"Bay Shore LIRR" homeless'
    assert fill_template('"{place}" homeless {x}', "Bay Shore") == '"Bay Shore" homeless {x}'


def test_station_place_queries_use_station_templates(sources):
    places = [p for p in _places(15) if p.kind == "lirr_station"]
    r = _plan(sources, gaz=_gaz(places))
    qs = [q.query for q in r.queries if q.family == "place" and q.provider == "socialcrawl"]
    assert qs and all("Station station" not in q for q in qs)
    assert all(("station" in q.lower()) or ("lirr" in q.lower()) for q in qs)


# ---------------------------------------------------------------------------
# plan_comment_queries
# ---------------------------------------------------------------------------
def _item(url: str, *, hours_ago: int, platform: str = "reddit", is_comment: bool = False,
          keep: Optional[bool] = True) -> NormalizedItem:
    now = dt.datetime(2026, 10, 14, 16, 0, tzinfo=dt.timezone.utc)
    return NormalizedItem(
        item_hash=url, content_hash=url, provider="socialcrawl", platform=platform, url=url,
        platform_post_id=url.rsplit("/", 1)[-1], published_at=now - dt.timedelta(hours=hours_ago),
        date_source="platform", collected_at=now, text="synthetic",
        context=ItemContext(query="q", source_scope="li_scoped", subreddit="LongIsland", is_comment=is_comment,
                            query_place_id=9),
        prefilter=None if keep is None else PrefilterResult(keep=keep),
    )


def test_plan_comment_queries_newest_threads_only():
    items = [
        _item("https://www.reddit.com/r/LongIsland/comments/old", hours_ago=30),
        _item("https://www.reddit.com/r/LongIsland/comments/new", hours_ago=1),
        _item("https://www.reddit.com/r/LongIsland/comments/mid", hours_ago=5, keep=None),
        _item("https://www.reddit.com/r/LongIsland/comments/drop", hours_ago=2, keep=False),
        _item("https://www.reddit.com/r/LongIsland/comments/new/c1", hours_ago=0, is_comment=True),
        _item("https://x.com/i/web/status/1", hours_ago=0, platform="x"),
    ]
    qs = plan_comment_queries(items, resolved_endpoints=_resolved(), max_threads=2)
    assert [q.params["post_url"] for q in qs] == ["https://www.reddit.com/r/LongIsland/comments/new",
                                                  "https://www.reddit.com/r/LongIsland/comments/mid"]
    q = qs[0]
    assert q.capability == "post_comments" and q.platform == "reddit" and q.family == "comments"
    assert q.est_cost == 5.0 and q.subreddit == "LongIsland" and q.place_id == 9 and q.source_scope == "li_scoped"
    assert plan_comment_queries(items, resolved_endpoints={}) == []
