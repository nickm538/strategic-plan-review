"""Provider payload -> NormalizedItem helpers (docs/04 §2.4, §5). STUB - text-pipeline builder."""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from app.pipeline.types import DateSource, ItemContext, NormalizedItem

MAX_TEXT_CHARS = 6000


def canonical_url(url: str) -> str:
    """Lowercase host; strip utm_*, fbclid, si, s, ref, gclid, igshid; drop fragment; old./np.reddit.com ->
    www.reddit.com; twitter.com/mobile.twitter.com -> x.com; remove trailing slash noise."""
    raise NotImplementedError


def registrable_domain(url: str) -> Optional[str]:
    """'https://www.newsday.com/x' -> 'newsday.com'; 'longisland.news12.com' kept as-is when listed in sources."""
    raise NotImplementedError


def scrub_text(text: str) -> str:
    """Replace @handles, emails, phone numbers and URLs with [handle]/[email]/[phone]/[link] (R2/R4)."""
    raise NotImplementedError


def parse_datetime(value: Any, *, now: dt.datetime) -> tuple[Optional[dt.datetime], Optional[DateSource]]:
    """Unix seconds/ms, ISO 8601 strings, or relative strings ('3 days ago', '5 hours ago', 'yesterday')
    -> (aware UTC datetime, date_source). Relative values return date_source='relative'. Unparseable -> (None, None)."""
    raise NotImplementedError


def build_item(
    *,
    provider: str,
    platform: str,
    url: str,
    platform_post_id: str,
    published_at: Optional[dt.datetime],
    date_source: DateSource,
    collected_at: dt.datetime,
    title: str,
    body: str,
    context: ItemContext,
    lang: Optional[str] = None,
    media_present: bool = False,
) -> Optional[NormalizedItem]:
    """Assemble a NormalizedItem: canonical URL, scrubbed title+body text (<= MAX_TEXT_CHARS), item_hash =
    sha256(canonical_url), content_hash = textnorm.content_fingerprint(text), context.outlet_domain filled.
    Returns None when published_at is None (F3) or text is empty. NEVER accepts author fields."""
    raise NotImplementedError
