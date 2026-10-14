"""Date window filter boundaries (docs/04 §5, docs/10 §1)."""
from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from app.pipeline.datefilter import date_filter
from app.pipeline.normalize import build_item
from app.pipeline.types import ItemContext

UTC = dt.timezone.utc


def _item(now: dt.datetime, published_at: dt.datetime, n: int = 0):
    return build_item(provider="socialcrawl", platform="reddit", url=f"https://www.reddit.com/r/longisland/comments/{n}",
                      platform_post_id=str(n), published_at=published_at, date_source="platform", collected_at=now,
                      title="", body=f"tents behind the station {n}", context=ItemContext(query="q"))


@pytest.mark.parametrize("offset,kept,bucket", [
    (-dt.timedelta(days=30), True, None),                              # exactly max age: kept
    (-dt.timedelta(days=30, seconds=1), False, "too_old"),
    (-dt.timedelta(days=29, hours=23), True, None),
    (dt.timedelta(minutes=30), True, None),                            # +30 min: kept (clock skew)
    (dt.timedelta(hours=1), True, None),                               # exactly +1h: kept
    (dt.timedelta(hours=1, seconds=1), False, "future"),
    (dt.timedelta(hours=2), False, "future"),                          # +2h: dropped
    (dt.timedelta(0), True, None),
])
def test_boundaries(now, offset, kept, bucket):
    item = _item(now, now + offset)
    out, counts = date_filter([item], now=now, max_age_days=30)
    assert (out == [item]) is kept
    assert set(counts) == {"too_old", "future"}
    if bucket:
        assert counts[bucket] == 1
    else:
        assert counts == {"too_old": 0, "future": 0}


def test_mixed_batch_counts_and_order(now):
    items = [_item(now, now - dt.timedelta(days=d), i) for i, d in enumerate([1, 45, 3, 31, 0])]
    items.append(_item(now, now + dt.timedelta(hours=5), 9))
    out, counts = date_filter(items, now=now, max_age_days=30)
    assert [i.platform_post_id for i in out] == ["0", "2", "4"]
    assert counts == {"too_old": 2, "future": 1}


def test_custom_max_age(now):
    item = _item(now, now - dt.timedelta(days=8))
    assert date_filter([item], now=now, max_age_days=7)[0] == []
    assert date_filter([item], now=now, max_age_days=8)[0] == [item]


def test_comment_uses_its_own_timestamp(now):
    """A comment item carries its own published_at; an old thread does not make it old."""
    comment = _item(now, now - dt.timedelta(hours=3), 1)
    comment = dataclasses.replace(comment, context=dataclasses.replace(
        comment.context, is_comment=True, parent_title="Thread from two months ago"))
    out, _ = date_filter([comment], now=now, max_age_days=30)
    assert out == [comment]


def test_missing_date_is_dropped_and_counted(now):
    item = _item(now, now)
    broken = dataclasses.replace(item, published_at=None)
    out, counts = date_filter([broken, item], now=now, max_age_days=30)
    assert out == [item]
    assert counts["no_date"] == 1


def test_non_utc_now_is_handled():
    eastern = dt.timezone(dt.timedelta(hours=-4))
    now_local = dt.datetime(2026, 10, 14, 12, 0, tzinfo=eastern)          # 16:00 UTC
    item = _item(now_local, dt.datetime(2026, 10, 14, 17, 0, tzinfo=UTC))  # +1h exactly
    assert date_filter([item], now=now_local, max_age_days=30)[0] == [item]
