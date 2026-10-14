"""SocialCrawl row -> NormalizedItem, table-driven per platform (docs/04 §2.4).

SocialCrawl answers in one unified schema [verified from socialcrawl.dev/docs/schema/*.md, Oct 14 2026]:
  Post:    id, url, content.text, content.media_urls, published_at (ISO 8601 UTC), ext.subreddit, ext.title,
           ext.selftext, ext.content_language, ext.location, ext.tags ... (plus author.*, which we never read)
  Comment: id, url, parent_id, post_id, text, published_at, replies[] (nested tree on Reddit), ext.post_title,
           ext.post_url, ext.subreddit ... (plus author.*, never read)
  News article (google_news): id, title, url, source, domain, snippet, published_at
List rows may arrive wrapped (`items[].post.*`, as the docs' planner examples show), so one wrapper level
is unwrapped. Every lookup below is an explicit allow-list: author/user fields, ext.author_*, ext.coauthors,
ext.usertags, ext.post_author, ext.quoted_post and media URLs are never read (R1/R2).
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from app.pipeline.collectors._common import (
    dig, first_str, first_value, hashtags_from, place_tag_name, rewrite_post_url, truthy_media,
)
from app.pipeline.types import ItemContext, NormalizedItem, PlannedQuery

_WRAPPERS = ("post", "comment", "article", "news", "item", "video", "tweet", "node")


@dataclass(frozen=True)
class Adapter:
    id_keys: tuple[str, ...]
    url_keys: tuple[str, ...]
    time_keys: tuple[str, ...]
    title_keys: tuple[str, ...]
    body_keys: tuple[str, ...]
    shortcode_keys: tuple[str, ...] = ()
    permalink_keys: tuple[str, ...] = ()
    subreddit_keys: tuple[str, ...] = ()
    hashtag_keys: tuple[str, ...] = ("hashtags", "ext.hashtags", "ext.tags", "tags", "challenges")
    place_keys: tuple[str, ...] = ("ext.location", "location", "place", "ext.place", "poi", "location_name")
    media_keys: tuple[str, ...] = ("content.media_urls", "media_urls", "ext.all_media_urls", "media", "images",
                                   "image_url", "video", "videos", "attachments", "content.thumbnail_url",
                                   "thumbnail_url", "content.duration_seconds", "ext.media_type", "is_video")
    lang_keys: tuple[str, ...] = ("ext.content_language", "lang", "language")


_TIME_COMMON = ("published_at", "created_utc", "created_at", "createTime", "create_time", "published",
                "publishedAt", "timestamp", "date", "taken_at", "indexed_at", "time", "ext.published_at_epoch")

POST_ADAPTERS: dict[str, Adapter] = {
    "reddit": Adapter(
        id_keys=("id", "post_id", "name"),
        url_keys=("url", "post_url", "link"),
        permalink_keys=("permalink", "ext.permalink"),
        time_keys=_TIME_COMMON,
        title_keys=("title", "ext.title"),
        body_keys=("content.text", "ext.selftext", "selftext", "body", "text"),
        subreddit_keys=("ext.subreddit", "subreddit", "subreddit_name", "community"),
        lang_keys=("ext.content_language", "lang", "language"),
    ),
    "x": Adapter(
        id_keys=("id", "tweet_id", "id_str", "rest_id", "post_id"),
        url_keys=("url", "link", "post_url"),
        time_keys=_TIME_COMMON,
        title_keys=(),
        body_keys=("content.text", "full_text", "text", "ext.text", "body"),
        place_keys=("ext.location", "place", "location", "ext.place"),
    ),
    "threads": Adapter(
        id_keys=("id", "code", "post_id", "pk"),
        url_keys=("url", "post_url", "link"),
        time_keys=_TIME_COMMON,
        title_keys=(),
        body_keys=("content.text", "caption", "text", "ext.caption", "body"),
    ),
    "bluesky": Adapter(
        id_keys=("id", "uri", "post_id", "cid"),
        url_keys=("url", "post_url", "link"),
        time_keys=("published_at", "created_at", "indexed_at", "createdAt", "indexedAt", "timestamp", "date"),
        title_keys=(),
        body_keys=("content.text", "text", "record.text", "body"),
    ),
    "tiktok": Adapter(
        id_keys=("id", "aweme_id", "video_id", "post_id"),
        url_keys=("url", "share_url", "post_url", "link"),
        time_keys=_TIME_COMMON,
        title_keys=(),
        body_keys=("content.text", "desc", "description", "caption", "text"),
        place_keys=("ext.location", "poi", "location", "poi_info", "ext.poi"),
    ),
    "youtube": Adapter(
        id_keys=("id", "video_id", "videoId"),
        url_keys=("url", "link", "post_url"),
        time_keys=_TIME_COMMON + ("ext.videoPublishedAt", "snippet.publishedAt"),
        title_keys=("title", "ext.title", "snippet.title"),
        body_keys=("content.text", "ext.description", "description", "snippet.description", "text"),
    ),
    "facebook": Adapter(
        id_keys=("id", "post_id", "story_id"),
        url_keys=("url", "post_url", "permalink_url", "link"),
        time_keys=_TIME_COMMON,
        title_keys=(),
        body_keys=("content.text", "message", "text", "caption", "description", "body"),
    ),
    "instagram": Adapter(
        id_keys=("id", "pk", "media_id", "post_id"),
        url_keys=("url", "post_url", "link"),
        shortcode_keys=("shortcode", "code", "ext.shortcode", "ext.code"),
        time_keys=_TIME_COMMON,
        title_keys=(),
        body_keys=("content.text", "caption", "ext.caption", "caption.text", "text"),
    ),
    "news": Adapter(
        id_keys=("id", "article_id"),
        url_keys=("url", "link"),
        time_keys=("published_at", "date", "publishedAt", "published", "timestamp", "time"),
        title_keys=("title", "headline"),
        body_keys=("snippet", "description", "summary", "content.text"),
        hashtag_keys=(),
        place_keys=(),
        media_keys=(),
        lang_keys=("lang", "language"),
    ),
}

COMMENT_ADAPTER = Adapter(
    id_keys=("id", "comment_id", "name"),
    url_keys=("url", "permalink_url", "link"),
    permalink_keys=("permalink", "ext.permalink"),
    time_keys=("published_at", "created_utc", "created_at", "timestamp", "date", "time", "ext.published_at_epoch"),
    title_keys=(),
    body_keys=("text", "body", "content.text", "comment", "message"),
    subreddit_keys=("ext.subreddit", "subreddit"),
    hashtag_keys=(),
    place_keys=(),
    media_keys=("media", "media_urls", "content.media_urls", "images", "attachments"),
)


def unwrap(row: Any) -> Any:
    """`{"post": {...}}` -> the inner object (one level; only when the outer row has no id itself)."""
    if not isinstance(row, dict) or "id" in row:
        return row
    for w in _WRAPPERS:
        inner = row.get(w)
        if isinstance(inner, dict) and inner:
            return inner
    return row


def extract_rows(data: Any) -> list[Any]:
    """data.items, or data itself when it is a list (docs/04 §2.1); tolerant of a few other list keys."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("items", "results", "posts", "comments", "articles", "news", "videos", "tweets", "data"):
            v = data.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict) and isinstance(v.get("items"), list):
                return v["items"]
    return []


def flatten_comments(rows: list[Any], *, max_depth: int = 8, max_total: int = 500) -> Iterator[tuple[dict[str, Any], int]]:
    """Each comment and its nested replies[] (Reddit returns a tree), depth-first, bounded."""
    count = 0
    stack: list[tuple[Any, int]] = [(r, 0) for r in reversed(rows)]
    while stack and count < max_total:
        raw, depth = stack.pop()
        row = unwrap(raw)
        if not isinstance(row, dict):
            continue
        count += 1
        yield row, depth
        replies = row.get("replies")
        if isinstance(replies, dict):
            replies = extract_rows(replies)
        if isinstance(replies, list) and depth < max_depth:
            stack.extend((c, depth + 1) for c in reversed(replies))


def _post_id(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    v = first_value(row, keys)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(int(v))
    return v.strip()[:200] if isinstance(v, str) else ""


def _reddit_comment_url(row: dict[str, Any], comment_id: str, thread_url: Optional[str]) -> Optional[str]:
    cid = comment_id[3:] if comment_id.startswith("t1_") else comment_id
    url = first_str(row, ("url", "permalink_url", "link"))
    permalink = first_str(row, ("permalink", "ext.permalink"))
    built = rewrite_post_url("reddit", url=url, permalink=permalink, post_id="")
    if built and cid and cid.lower() in built.lower():
        return built
    base = first_str(row, ("ext.post_url",)) or thread_url or ""
    base_url = rewrite_post_url("reddit", url=base, permalink="", post_id="")
    if base_url and cid:
        return base_url.rstrip("/") + "/" + cid
    return None


def map_row(raw: Any, *, slug: str, app_platform: str, query: PlannedQuery, now: dt.datetime,
            sources: Any, is_comment: bool, stats: Counter, thread_url: Optional[str] = None,
            parent_title: Optional[str] = None) -> Optional[NormalizedItem]:
    """One SocialCrawl row -> NormalizedItem via normalize.build_item, or None (reason counted in `stats`)."""
    from app.pipeline.normalize import build_item, parse_datetime
    from app.pipeline.sources import scope_max, subreddit_from_url

    row = unwrap(raw)
    if not isinstance(row, dict):
        stats["not_an_object"] += 1
        return None
    if dig(row, "flags.deleted") is True:
        stats["deleted"] += 1
        return None
    ad = COMMENT_ADAPTER if is_comment else POST_ADAPTERS.get(app_platform)
    if ad is None:
        stats["unsupported_platform"] += 1
        return None

    post_id = _post_id(row, ad.id_keys)
    raw_url = first_str(row, ad.url_keys)
    if is_comment:
        url = _reddit_comment_url(row, post_id, thread_url) if app_platform == "reddit" else (
            rewrite_post_url(app_platform, url=raw_url, post_id=post_id))
    else:
        url = rewrite_post_url(
            app_platform, url=raw_url, post_id=post_id,
            shortcode=first_str(row, ad.shortcode_keys) if ad.shortcode_keys else "",
            permalink=first_str(row, ad.permalink_keys) if ad.permalink_keys else "",
        )
    if not url:
        stats["no_post_url"] += 1
        return None

    published_at, date_source = None, None
    for key in ad.time_keys:
        val = dig(row, key)
        if val is None or val == "":
            continue
        published_at, date_source = parse_datetime(val, now=now)
        if published_at is not None:
            break
    if published_at is None:
        stats["no_date"] += 1
        return None

    title = first_str(row, ad.title_keys, max_len=2000) if ad.title_keys else ""
    body = first_str(row, ad.body_keys)
    subreddit = None
    if app_platform == "reddit":
        subreddit = first_str(row, ad.subreddit_keys) or query.subreddit or subreddit_from_url(url) or None
        if subreddit:
            subreddit = subreddit.removeprefix("r/").removeprefix("/r/").strip("/") or None
        if subreddit and sources.is_excluded_subreddit(subreddit):
            stats["excluded_subreddit"] += 1
            return None
    text_for_tags = f"{title}\n{body}"
    hashtags = hashtags_from(first_value(row, ad.hashtag_keys), text_for_tags) if ad.hashtag_keys else []
    place_tag = place_tag_name(first_value(row, ad.place_keys)) if ad.place_keys else None
    media = truthy_media(row, ad.media_keys) if ad.media_keys else False
    lang = first_str(row, ad.lang_keys, max_len=16) or None
    if is_comment and not parent_title:
        parent_title = first_str(row, ("ext.post_title",), max_len=1000) or None

    scope = scope_max(query.source_scope, sources.scope_for(url=url, subreddit=subreddit))
    ctx = ItemContext(
        query=_ctx_query(query), source_scope=scope, query_place_id=query.place_id,
        query_place_name=query.place_name, subreddit=subreddit, hashtags=hashtags,
        platform_place_tag=place_tag, is_comment=is_comment, parent_title=parent_title,
    )
    item = build_item(
        provider="socialcrawl", platform=app_platform, url=url, platform_post_id=post_id,
        published_at=published_at, date_source=date_source or "platform", collected_at=now,
        title=title, body=body, context=ctx, lang=lang, media_present=media,
    )
    if item is None:
        stats["empty_text"] += 1
        return None
    stats["mapped"] += 1
    return item


def _ctx_query(q: PlannedQuery) -> str:
    from app.pipeline.collectors._common import query_label
    return query_label(q)[:500]
