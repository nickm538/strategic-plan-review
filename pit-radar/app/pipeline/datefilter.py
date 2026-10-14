"""Date window filter (docs/04 §5, F3).

Each item is judged on its OWN published_at (a Reddit comment uses the comment's time, not the thread's;
collectors set that). Both window edges are inclusive.
"""
from __future__ import annotations

import datetime as dt
import logging

from app.pipeline.types import NormalizedItem

logger = logging.getLogger(__name__)

FUTURE_SKEW = dt.timedelta(hours=1)


def _utc(d: dt.datetime) -> dt.datetime:
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)


def date_filter(items: list[NormalizedItem], *, now: dt.datetime, max_age_days: int,
                future_skew: dt.timedelta = FUTURE_SKEW) -> tuple[list[NormalizedItem], dict[str, int]]:
    """Keep now - max_age_days <= published_at <= now + 1h. Returns (kept, {"too_old": n, "future": n}).
    An item with no published_at (should not exist after build_item) is dropped and counted as "no_date"."""
    now_utc = _utc(now)
    oldest = now_utc - dt.timedelta(days=max_age_days)
    newest = now_utc + future_skew
    kept: list[NormalizedItem] = []
    counts = {"too_old": 0, "future": 0}
    no_date = 0
    for it in items:
        published = getattr(it, "published_at", None)
        if not isinstance(published, dt.datetime):
            no_date += 1
            continue
        p = _utc(published)
        if p < oldest:
            counts["too_old"] += 1
        elif p > newest:
            counts["future"] += 1
        else:
            kept.append(it)
    if no_date:
        counts["no_date"] = no_date
    if counts["too_old"] or counts["future"] or no_date:
        logger.debug("datefilter: kept=%d too_old=%d future=%d no_date=%d",
                     len(kept), counts["too_old"], counts["future"], no_date)
    return kept, counts
