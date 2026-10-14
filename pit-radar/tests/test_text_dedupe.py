"""De-duplication within a batch and against items_seen (docs/04 §5, docs/03 items_seen)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from app.models import ItemSeen
from app.pipeline.dedupe import dedupe_batch, filter_seen, mark_seen
from app.pipeline.normalize import build_item
from app.pipeline.types import ItemContext


def _item(now, *, url, body="Tents behind the Ronkonkoma station again.", provider="socialcrawl", platform="reddit",
          hours_ago=1, scope="general", place=None):
    return build_item(provider=provider, platform=platform, url=url, platform_post_id="", collected_at=now,
                      published_at=now - dt.timedelta(hours=hours_ago), date_source="platform", title="",
                      body=body, context=ItemContext(query="q", source_scope=scope, query_place_name=place))


def test_batch_collapses_same_url_keeping_earliest(now):
    a = _item(now, url="https://old.reddit.com/r/longisland/comments/1/x/?utm_source=a", hours_ago=1)
    b = _item(now, url="https://www.reddit.com/r/longisland/comments/1/x", hours_ago=5, body="different text here")
    c = _item(now, url="https://www.reddit.com/r/longisland/comments/2/y", body="something else entirely")
    out, removed = dedupe_batch([a, b, c])
    assert removed == 1
    assert len(out) == 2
    assert out[0].published_at == b.published_at          # earliest copy kept, in first-seen position
    assert out[1] is c


def test_batch_collapses_same_content_across_urls(now):
    story = "County officials said an encampment near the Bay Shore station was cleared Tuesday."
    a = _item(now, url="https://www.newsday.com/a", body=story, provider="exa", platform="news", hours_ago=2)
    b = _item(now, url="https://www.syndicated.example.org/copy", body=story + " ", provider="firecrawl",
              platform="news", hours_ago=6)
    out, removed = dedupe_batch([a, b])
    assert removed == 1 and len(out) == 1
    assert out[0].url == b.url                              # earliest published copy wins


def test_batch_merges_li_scope_and_query_place(now):
    general = _item(now, url="https://www.reddit.com/r/x/comments/9", hours_ago=1)
    scoped = _item(now, url="https://www.reddit.com/r/x/comments/9/", hours_ago=3, scope="li_scoped", place="Ronkonkoma")
    out, removed = dedupe_batch([general, scoped])
    assert removed == 1
    assert out[0].context.source_scope == "li_scoped"
    assert out[0].context.query_place_name == "Ronkonkoma"
    # inputs are not mutated
    assert general.context.source_scope == "general"

    later_scoped = _item(now, url="https://www.reddit.com/r/x/comments/8", hours_ago=1, scope="li_scoped")
    earlier_general = _item(now, url="https://www.reddit.com/r/x/comments/8", hours_ago=4, place="Islip")
    out2, _ = dedupe_batch([later_scoped, earlier_general])
    assert out2[0].published_at == earlier_general.published_at
    assert out2[0].context.source_scope == "li_scoped"
    assert out2[0].context.query_place_name == "Islip"


def test_batch_empty():
    assert dedupe_batch([]) == ([], 0)


def test_filter_seen_and_mark_seen(db, now):
    a = _item(now, url="https://www.reddit.com/r/longisland/comments/a")
    b = _item(now, url="https://www.reddit.com/r/longisland/comments/b", body="People living in the woods near the sump.")
    c = _item(now, url="https://www.reddit.com/r/longisland/comments/c", body="Car with someone sleeping in it nightly.")
    mark_seen(db, a, "stored")
    db.commit()

    new, n_seen = filter_seen(db, [a, b, c])
    assert n_seen == 1 and new == [b, c]

    # same content under another URL from the same provider is "seen"; from another provider it is not
    a_copy = _item(now, url="https://www.reddit.com/r/longisland/comments/a_copy")
    a_other = _item(now, url="https://www.newsday.com/a_copy", provider="exa", platform="news")
    new2, n2 = filter_seen(db, [a_copy, a_other])
    assert n2 == 1 and new2 == [a_other]
    assert filter_seen(db, []) == ([], 0)


def test_mark_seen_upserts_decision(db, now):
    a = _item(now, url="https://www.reddit.com/r/longisland/comments/a")
    mark_seen(db, a, "pending")
    db.commit()
    first = db.get(ItemSeen, a.item_hash).first_seen
    mark_seen(db, a, "not_relevant:animal")
    db.commit()
    row = db.get(ItemSeen, a.item_hash)
    assert row.decision == "not_relevant:animal"
    assert row.first_seen == first
    assert row.provider == "socialcrawl" and row.content_hash == a.content_hash
    assert db.scalar(select(func.count()).select_from(ItemSeen)) == 1


def test_mark_seen_handles_content_unique_constraint(db, now):
    a = _item(now, url="https://www.reddit.com/r/longisland/comments/a")
    a_copy = _item(now, url="https://www.reddit.com/r/longisland/comments/a_copy")   # same text, same provider
    a_news = _item(now, url="https://www.newsday.com/x", provider="exa", platform="news")
    mark_seen(db, a, "stored")
    mark_seen(db, a_copy, "duplicate")        # pending in the same session: no IntegrityError at flush
    mark_seen(db, a_news, "stored")
    db.commit()
    rows = db.scalars(select(ItemSeen)).all()
    assert len(rows) == 2
    by_provider = {r.provider: r for r in rows}
    assert by_provider["socialcrawl"].decision == "duplicate"
    assert by_provider["exa"].decision == "stored"

    mark_seen(db, a_copy, "stored again")     # committed row found by (content_hash, provider)
    db.commit()
    assert db.get(ItemSeen, a.item_hash).decision == "stored again"


def test_mark_seen_stores_no_content(db, now):
    a = _item(now, url="https://www.reddit.com/r/longisland/comments/a")
    mark_seen(db, a, "stored")
    db.commit()
    row = db.get(ItemSeen, a.item_hash)
    values = {c.name: getattr(row, c.name) for c in ItemSeen.__table__.columns}
    assert set(values) == {"item_hash", "content_hash", "provider", "first_seen", "decision"}
    assert all("Ronkonkoma" not in str(v) for v in values.values())
    assert values["first_seen"].tzinfo is not None
