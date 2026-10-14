"""Retention jobs (docs/09 §4, R6, docs/10 §1). Synthetic rows only."""
from __future__ import annotations

import datetime as dt

import pytest

from app.config import get_settings
from app.models import (
    AuditLog,
    GeocodeCache,
    Hotspot,
    HotspotWeekly,
    ItemSeen,
    PendingItem,
    QueryLog,
    Review,
    ScanRun,
    Signal,
    User,
    UserSession,
)
from app.pipeline import hotspots as H
from app.pipeline.retention import run_retention

NOW = dt.datetime(2026, 10, 14, 16, 0, tzinfo=dt.timezone.utc)
DAY = dt.timedelta(days=1)
_n = {"i": 0}


def _signal(db, hotspot_id=None, *, status="ai_verified", collected_days_ago=1.0, signal_type="encampment",
            scan_id=1, retention_days=30) -> Signal:
    _n["i"] += 1
    collected = NOW - dt.timedelta(days=collected_days_ago)
    s = Signal(
        scan_id=scan_id, provider="socialcrawl", platform="reddit",
        url=f"https://www.reddit.com/r/longisland/comments/r{_n['i']}/x", post_id_hash=f"r{_n['i']:063d}",
        published_at=collected - dt.timedelta(hours=6), collected_at=collected, signal_type=signal_type,
        status=status, neutral_summary="Encampment reported (ongoing).", evidence_quotes=[], flags={}, checks={},
        time_reference="ongoing", ai_confidence=0.9, model="claude-opus-5-5", decision_basis="",
        lat=40.75, lon=-73.2, precision="public_place", county="Suffolk", town="Exampletown",
        hotspot_id=hotspot_id, expires_at=collected + dt.timedelta(days=retention_days),
    )
    db.add(s)
    db.flush()
    return s


@pytest.fixture
def base(db):
    db.add(ScanRun(id=1, trigger="schedule", status="ok", started_at=NOW - 40 * DAY))
    db.flush()
    hs = Hotspot(label="Example station", county="Suffolk", lat=40.75, lon=-73.2, primary_type="encampment",
                 first_seen_at=NOW - 40 * DAY, last_signal_at=NOW - DAY, status="active")
    db.add(hs)
    db.flush()
    return hs


def test_expired_signals_rolled_up_then_deleted(db, base):
    hs = base
    expired_confirmed = _signal(db, hs.id, status="confirmed", collected_days_ago=31)
    expired_verified = _signal(db, hs.id, status="ai_verified", collected_days_ago=32)
    _signal(db, hs.id, status="dismissed", collected_days_ago=33)
    _signal(db, None, status="unlocated", collected_days_ago=35)
    keep = _signal(db, hs.id, status="ai_verified", collected_days_ago=2)
    week_ids = {H.week_start_local(s.published_at, "America/New_York")
                for s in (expired_confirmed, expired_verified)}
    keep_id = keep.id
    db.commit()

    counts = run_retention(db, now=NOW, settings=get_settings())
    db.commit()
    assert counts["signals_deleted"] == 4
    remaining = [s.id for s in db.query(Signal)]
    assert remaining == [keep_id]
    weekly = db.query(HotspotWeekly).all()
    assert {w.week_start for w in weekly} == week_ids
    assert sum(w.n_signals for w in weekly) == 2           # dismissed and unlocated are not counted
    assert sum(w.n_confirmed for w in weekly) == 1
    assert all(w.hotspot_id == hs.id and w.signal_type == "encampment" for w in weekly)
    db.refresh(hs)
    assert hs.signals_30d == 1                              # hotspot refreshed from the remaining member


def test_retention_is_idempotent_and_returns_counts_only(db, base):
    _signal(db, base.id, collected_days_ago=40)
    db.add(PendingItem(item_hash="p" * 64, normalized={}, prefilter_score=1.0,
                       collected_at=NOW - 4 * DAY, published_at=NOW - 5 * DAY, purge_after=NOW - DAY))
    db.commit()
    first = run_retention(db, now=NOW, settings=get_settings())
    db.commit()
    assert all(isinstance(v, int) for v in first.values())
    assert first["signals_deleted"] == 1 and first["pending_items_deleted"] == 1
    weekly_before = [(w.week_start, w.n_signals) for w in db.query(HotspotWeekly)]
    second = run_retention(db, now=NOW, settings=get_settings())
    db.commit()
    assert all(v == 0 for k, v in second.items() if k != "hotspots_refreshed")
    assert [(w.week_start, w.n_signals) for w in db.query(HotspotWeekly)] == weekly_before


def test_pending_items_purged_at_72_hours(db):
    def pending(tag: str, hours_ago: float, purge_after=None):
        collected = NOW - dt.timedelta(hours=hours_ago)
        db.add(PendingItem(item_hash=tag * 64, normalized={"k": tag}, prefilter_score=0.5, collected_at=collected,
                           published_at=collected - DAY,
                           purge_after=purge_after or collected + dt.timedelta(hours=72)))
    pending("a", 73)                 # purge_after passed -> deleted
    pending("b", 71)                 # 1 h left -> kept
    pending("c", 80, purge_after=NOW + DAY)   # bad purge_after, but older than the 72 h TTL -> deleted
    db.commit()
    counts = run_retention(db, now=NOW, settings=get_settings())
    db.commit()
    assert counts["pending_items_deleted"] == 2
    assert [p.item_hash[0] for p in db.query(PendingItem)] == ["b"]


def test_operational_tables_trimmed(db):
    db.add_all([
        ScanRun(id=10, trigger="schedule", status="ok", started_at=NOW - 200 * DAY),   # old, no signals -> deleted
        ScanRun(id=11, trigger="schedule", status="ok", started_at=NOW - 200 * DAY),   # old, but a signal refers
        ScanRun(id=12, trigger="schedule", status="ok", started_at=NOW - 10 * DAY),
    ])
    db.flush()
    db.add_all([
        QueryLog(scan_id=10, provider="exa", endpoint="/search", query_text="q", status="ok", at=NOW - 200 * DAY),
        QueryLog(scan_id=12, provider="exa", endpoint="/search", query_text="q", status="ok", at=NOW - 31 * DAY),
        QueryLog(scan_id=12, provider="exa", endpoint="/search", query_text="q", status="ok", at=NOW - 29 * DAY),
        ItemSeen(item_hash="1" * 64, content_hash="1" * 64, provider="exa", first_seen=NOW - 91 * DAY,
                 decision="stored"),
        ItemSeen(item_hash="2" * 64, content_hash="2" * 64, provider="exa", first_seen=NOW - 89 * DAY,
                 decision="stored"),
        GeocodeCache(query_norm="old", result={"miss": True}, created_at=NOW - 181 * DAY),
        GeocodeCache(query_norm="new", result={"miss": True}, created_at=NOW - 179 * DAY),
    ])
    _signal(db, None, status="unlocated", collected_days_ago=1, scan_id=11, retention_days=90)
    u = User(name="retention-test", role="viewer", password_hash="not-a-real-hash")
    db.add(u)
    db.flush()
    db.add_all([
        UserSession(id="s1", user_id=u.id, csrf_hash="c", created_at=NOW - 2 * DAY, expires_at=NOW - DAY),
        UserSession(id="s2", user_id=u.id, csrf_hash="c", created_at=NOW, expires_at=NOW + DAY, revoked=True),
        UserSession(id="s3", user_id=u.id, csrf_hash="c", created_at=NOW, expires_at=NOW + DAY),
        AuditLog(at=NOW - 366 * DAY, user_id=u.id, action="login", details={}),
        AuditLog(at=NOW - 364 * DAY, user_id=u.id, action="login", details={}),
        Review(object_type="signal", object_id=1, user_id=u.id, action="confirm", at=NOW - 400 * DAY),
        Review(object_type="signal", object_id=2, user_id=u.id, action="confirm", at=NOW - 10 * DAY),
    ])
    db.commit()

    counts = run_retention(db, now=NOW, settings=get_settings())
    db.commit()
    assert counts["scan_runs_deleted"] == 1
    assert sorted(r.id for r in db.query(ScanRun)) == [11, 12]
    assert counts["query_log_deleted"] == 2            # the 31-day row and the old scan's row
    assert db.query(QueryLog).count() == 1
    assert counts["items_seen_deleted"] == 1 and db.query(ItemSeen).count() == 1
    assert counts["geocode_cache_deleted"] == 1 and [g.query_norm for g in db.query(GeocodeCache)] == ["new"]
    assert counts["sessions_deleted"] == 2 and [s.id for s in db.query(UserSession)] == ["s3"]
    assert counts["audit_log_deleted"] == 1 and db.query(AuditLog).count() == 1
    assert counts["reviews_deleted"] == 1 and db.query(Review).count() == 1


def test_retention_archives_stale_hotspots(db):
    stale = Hotspot(label="Stale", county="Nassau", lat=40.7, lon=-73.6, primary_type="mixed",
                    first_seen_at=NOW - 200 * DAY, last_signal_at=NOW - 91 * DAY, status="not_found",
                    notes="nothing found", assigned_team="Team 4")
    fresh = Hotspot(label="Fresh", county="Nassau", lat=40.71, lon=-73.61, primary_type="mixed",
                    first_seen_at=NOW - 20 * DAY, last_signal_at=NOW - 3 * DAY, status="active", notes="ok")
    db.add_all([stale, fresh])
    db.commit()
    counts = run_retention(db, now=NOW, settings=get_settings())
    db.commit()
    assert counts["hotspots_archived"] == 1
    db.refresh(stale)
    db.refresh(fresh)
    assert stale.status == "archived" and stale.notes is None and stale.assigned_team is None
    assert fresh.status == "active" and fresh.notes == "ok"
