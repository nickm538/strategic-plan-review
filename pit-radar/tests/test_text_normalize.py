"""Normalization: canonical URLs, scrubbing, date parsing, item assembly (docs/04 §5, docs/10 §1)."""
from __future__ import annotations

import datetime as dt
import inspect

import pytest

from app.pipeline import normalize
from app.pipeline.normalize import (MAX_TEXT_CHARS, build_item, canonical_url, parse_datetime, registrable_domain,
                                    scrub_text)
from app.pipeline.textnorm import content_fingerprint, sha256_hex
from app.pipeline.types import ItemContext

UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# canonical_url / registrable_domain
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("HTTPS://Old.Reddit.com/r/longisland/comments/abc123/Tents_Behind_Mall/?utm_source=share&utm_medium=web#c1",
     "https://www.reddit.com/r/longisland/comments/abc123/Tents_Behind_Mall"),
    ("https://np.reddit.com/r/LongIsland/comments/xyz/", "https://www.reddit.com/r/LongIsland/comments/xyz"),
    ("https://m.reddit.com/r/longisland/", "https://www.reddit.com/r/longisland"),
    ("https://twitter.com/example/status/1?s=20&t=keep", "https://x.com/example/status/1?t=keep"),
    ("https://mobile.twitter.com/example/status/2", "https://x.com/example/status/2"),
    ("https://www.x.com/example/status/3?si=abc", "https://x.com/example/status/3"),
    ("http://WWW.Newsday.com:80/long-island/Story-Path?fbclid=1&id=5&ref=home&gclid=2",
     "http://www.newsday.com/long-island/Story-Path?id=5"),
    ("https://example.org:443/a?igshid=1&ref_src=twsrc&mc_cid=1&mc_eid=2&UTM_Campaign=x&b=2",
     "https://example.org/a?b=2"),
    ("https://example.org:8443/a/", "https://example.org:8443/a"),
    ("https://user:secret@example.org/a", "https://example.org/a"),
    ("www.liherald.com/stories/x/", "https://www.liherald.com/stories/x"),
    ("https://example.org", "https://example.org/"),
    ("https://example.org/?", "https://example.org/"),
])
def test_canonical_url(raw, expected):
    assert canonical_url(raw) == expected


def test_canonical_url_is_idempotent_and_tolerant():
    once = canonical_url("https://old.reddit.com/r/x/comments/1/t/?utm_source=a")
    assert canonical_url(once) == once
    assert canonical_url("") == ""
    assert canonical_url("not a url") == "not a url"
    assert canonical_url("mailto:x@example.org") == "mailto:x@example.org"


@pytest.mark.parametrize("raw,expected", [
    ("https://www.newsday.com/long-island/x", "newsday.com"),
    ("https://amp.newsday.com/x", "newsday.com"),
    ("https://longisland.news12.com/a", "longisland.news12.com"),
    ("https://old.reddit.com/r/longisland", "reddit.com"),
    ("https://twitter.com/a/status/1", "x.com"),
    ("https://www.bbc.co.uk/news", "bbc.co.uk"),
    ("", None),
    ("not a url", None),
])
def test_registrable_domain(raw, expected):
    assert registrable_domain(raw) == expected


# ---------------------------------------------------------------------------
# scrub_text
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("see https://example.org/a?b=c for more", "see [link] for more"),
    ("(link: https://example.org/a).", "(link: [link])."),
    ("wiki https://en.example.org/wiki/Foo_(bar) ok", "wiki [link] ok"),
    ("www.example.org/page, then", "[link], then"),
    ("posted at reddit.com/r/longisland/abc today", "posted at [link] today"),
    ("mail tips.person+x@example.co.uk now", "mail [email] now"),
    ("thanks @some_user and @someone.bsky.social!", "thanks [handle] and [handle]!"),
    ("ask u/Example_Name or /u/other-name", "ask [handle] or [handle]"),
    ("call (631) 555-0123 or 516.555.0199", "call [phone] or [phone]"),
    ("call +1 631 555 0100 or 1-516-555-0123", "call [phone] or [phone]"),
    ("text 6315550123", "text [phone]"),
])
def test_scrub_cases(raw, expected):
    assert scrub_text(raw) == expected


def test_scrub_leaves_times_years_dates_and_plain_words():
    text = "Seen at 10:30 pm on 10/14/2026, again in 2026 (since 2025-2026), about 12 people, r/longisland."
    assert scrub_text(text) == text


def test_scrub_order_urls_before_emails_and_handles():
    assert scrub_text("https://example.org/@someone") == "[link]"
    assert scrub_text("someone@example.org") == "[email]"
    assert scrub_text(scrub_text("@a and https://x.example.org/b")) == "[handle] and [link]"


# ---------------------------------------------------------------------------
# parse_datetime
# ---------------------------------------------------------------------------
NOW = dt.datetime(2026, 10, 14, 16, 0, tzinfo=UTC)


@pytest.mark.parametrize("value,expected", [
    (1760457600, dt.datetime(2025, 10, 14, 16, 0, tzinfo=UTC)),
    (1760457600.5, dt.datetime(2025, 10, 14, 16, 0, 0, 500000, tzinfo=UTC)),
    (1760457600000, dt.datetime(2025, 10, 14, 16, 0, tzinfo=UTC)),
    ("1760457600", dt.datetime(2025, 10, 14, 16, 0, tzinfo=UTC)),
    ("1760457600000", dt.datetime(2025, 10, 14, 16, 0, tzinfo=UTC)),
    ("2026-10-14T12:00:00Z", dt.datetime(2026, 10, 14, 12, 0, tzinfo=UTC)),
    ("2026-10-14T12:00:00.123456789Z", dt.datetime(2026, 10, 14, 12, 0, 0, 123456, tzinfo=UTC)),
    ("2026-10-14T08:00:00-04:00", dt.datetime(2026, 10, 14, 12, 0, tzinfo=UTC)),
    ("2026-10-14T12:00:00+0000", dt.datetime(2026, 10, 14, 12, 0, tzinfo=UTC)),
    ("2026-10-14", dt.datetime(2026, 10, 14, 0, 0, tzinfo=UTC)),
    ("Wed, 14 Oct 2026 08:00:00 -0400", dt.datetime(2026, 10, 14, 12, 0, tzinfo=UTC)),
    ("Wed, 14 Oct 2026 12:00:00 GMT", dt.datetime(2026, 10, 14, 12, 0, tzinfo=UTC)),
    ("Oct 12, 2026", dt.datetime(2026, 10, 12, 0, 0, tzinfo=UTC)),
    ("October 12, 2026 at 3:45 p.m. ET", dt.datetime(2026, 10, 12, 19, 45, tzinfo=UTC)),
    ("Sept. 30th, 2026", dt.datetime(2026, 9, 30, 0, 0, tzinfo=UTC)),
    (dt.datetime(2026, 10, 1, 9, 0), dt.datetime(2026, 10, 1, 9, 0, tzinfo=UTC)),
])
def test_parse_absolute(value, expected):
    got, source = parse_datetime(value, now=NOW)
    assert got == expected
    assert got.tzinfo is not None and got.utcoffset() == dt.timedelta(0)
    assert source == "platform"


@pytest.mark.parametrize("value,delta", [
    ("3 days ago", dt.timedelta(days=3)),
    ("an hour ago", dt.timedelta(hours=1)),
    ("a minute ago", dt.timedelta(minutes=1)),
    ("yesterday", dt.timedelta(days=1)),
    ("just now", dt.timedelta(0)),
    ("2d", dt.timedelta(days=2)),
    ("5h", dt.timedelta(hours=5)),
    ("3w", dt.timedelta(weeks=3)),
    ("10 min ago", dt.timedelta(minutes=10)),
    ("10 mins ago", dt.timedelta(minutes=10)),
    ("2 hrs ago", dt.timedelta(hours=2)),
    ("1 month ago", dt.timedelta(days=30)),
    ("a few minutes ago", dt.timedelta(minutes=3)),
    ("Streamed 2 days ago", dt.timedelta(days=2)),
    ("hace 3 horas", dt.timedelta(hours=3)),
    ("last week", dt.timedelta(days=7)),
])
def test_parse_relative(value, delta):
    got, source = parse_datetime(value, now=NOW)
    assert source == "relative"
    assert got == NOW - delta


@pytest.mark.parametrize("value", [None, "", "   ", "garbage", True, False, 0, -5, "2004-12-31T23:59:59Z",
                                   1000000000 - 10**8, "2026", {"a": 1}, float("nan"), "99999 years ago"])
def test_parse_rejects(value):
    assert parse_datetime(value, now=NOW) == (None, None)


def test_parse_naive_now_is_treated_as_utc():
    got, source = parse_datetime("1 hour ago", now=dt.datetime(2026, 10, 14, 16, 0))
    assert got == dt.datetime(2026, 10, 14, 15, 0, tzinfo=UTC) and source == "relative"


# ---------------------------------------------------------------------------
# build_item
# ---------------------------------------------------------------------------
def _ctx(**kw) -> ItemContext:
    base = dict(query='homeless "Long Island"', source_scope="general")
    base.update(kw)
    return ItemContext(**base)


def _build(**kw):
    args = dict(provider="socialcrawl", platform="reddit",
                url="https://old.reddit.com/r/longisland/comments/abc/tents/?utm_source=share",
                platform_post_id="t3_abc", published_at=NOW - dt.timedelta(hours=2), date_source="platform",
                collected_at=NOW, title="Tents behind the station",
                body="Saw tents behind the Hicksville station last night.", context=_ctx())
    args.update(kw)
    return build_item(**args)


def test_build_item_basic_fields():
    item = _build()
    assert item is not None
    assert item.url == "https://www.reddit.com/r/longisland/comments/abc/tents"
    assert item.item_hash == sha256_hex(item.url)
    assert item.text == "Tents behind the station\n\nSaw tents behind the Hicksville station last night."
    assert item.content_hash == content_fingerprint(item.text)
    assert item.context.outlet_domain == "reddit.com"
    assert item.platform_post_id == "t3_abc"
    assert item.published_at.tzinfo is not None and item.collected_at.tzinfo is not None
    assert item.title == "Tents behind the station"
    assert item.date_source == "platform"


def test_build_item_has_no_author_parameter():
    params = set(inspect.signature(build_item).parameters)
    assert not {p for p in params if "author" in p or "user" in p or "handle" in p}


def test_build_item_rejects_missing_date_and_empty_text():
    assert _build(published_at=None) is None
    assert _build(title="", body="   \n  ") is None
    assert _build(url="") is None
    assert _build(platform="myspace") is None


def test_build_item_skips_duplicate_title():
    item = _build(title="Tents behind the station", body="Tents behind the station - and more of them today")
    assert item.text == "Tents behind the station - and more of them today"
    only_title = _build(title="Tents behind the station", body="")
    assert only_title.text == "Tents behind the station"


def test_build_item_scrubs_text_title_and_parent_title():
    item = _build(title="Ask @someone", body="Email me at a@example.org or call 631-555-0123, see https://ex.org/x",
                  context=_ctx(is_comment=True, parent_title="Thread by u/somebody"))
    assert "@" not in item.text and "631" not in item.text and "https" not in item.text
    assert item.title == "Ask [handle]"
    assert "[email]" in item.text and "[phone]" in item.text and "[link]" in item.text
    assert item.context.parent_title == "Thread by [handle]"


def test_build_item_keeps_paragraphs_and_cleans_lines():
    item = _build(title="T", body="line one  here\r\n\r\n\r\n“quoted”  line\n")
    assert item.text == 'T\n\nline one here\n\n"quoted" line'


def test_build_item_truncates_at_word_boundary():
    body = ("word " * 2000).strip()          # 9,999 chars
    item = _build(title="", body=body)
    assert len(item.text) <= MAX_TEXT_CHARS
    assert item.text.endswith("word")
    assert not item.text.endswith(" ")


def test_build_item_post_id_fallback_and_naive_dates():
    item = _build(platform_post_id="", published_at=dt.datetime(2026, 10, 14, 10, 0),
                  collected_at=dt.datetime(2026, 10, 14, 16, 0))
    assert item.platform_post_id == sha256_hex(item.url)[:32]
    assert item.published_at == dt.datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    assert item.collected_at.tzinfo is not None


def test_build_item_keeps_explicit_outlet_domain_and_does_not_mutate_context():
    ctx = _ctx(outlet_domain="longisland.news12.com", hashtags=["LongIsland"])
    item = _build(provider="exa", platform="news", url="https://longisland.news12.com/story", context=ctx)
    assert item.context.outlet_domain == "longisland.news12.com"
    assert item.context is not ctx and item.context.hashtags is not ctx.hashtags
    plain = _build(provider="firecrawl", platform="web", url="https://www.newsday.com/long-island/x", context=_ctx())
    assert plain.context.outlet_domain == "newsday.com"


def test_round_trip_to_dict():
    from app.pipeline.types import NormalizedItem
    item = _build()
    again = NormalizedItem.from_dict(item.to_dict())
    assert again == item


def test_module_has_no_stubs_left():
    assert "NotImplementedError" not in inspect.getsource(normalize)
