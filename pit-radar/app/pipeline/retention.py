"""Retention jobs (docs/09 §4, R6). Runs hourly from the scheduler; idempotent; returns counts only.

Order matters: expired signals are rolled into hotspot_weekly (aggregates kept indefinitely) in the same
transaction that deletes them, so a crash can never lose the counts or double-count them.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    AuditLog,
    GeocodeCache,
    Hotspot,
    ItemSeen,
    PendingItem,
    QueryLog,
    Review,
    ScanRun,
    Signal,
    UserSession,
)
from app.pipeline import hotspots as hotspot_mod

logger = logging.getLogger(__name__)

QUERY_LOG_DAYS = 30
ITEMS_SEEN_DAYS = 90
GEOCODE_CACHE_DAYS = 180
SCAN_RUNS_DAYS = 180
AUDIT_DAYS = 365
REVIEWS_DAYS = 365
_CHUNK = 500


def _aware(d: dt.datetime) -> dt.datetime:
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)


def _rowcount(result: Any) -> int:
    n = getattr(result, "rowcount", None)
    return int(n) if isinstance(n, int) and n > 0 else 0


def _delete_where(db: Session, model: Any, *criteria: Any) -> int:
    res = db.execute(delete(model).where(*criteria).execution_options(synchronize_session=False))
    return _rowcount(res)


def _expire_signals(db: Session, now: dt.datetime, settings: Settings) -> tuple[int, int]:
    """Roll expired signals into hotspot_weekly, delete them, and refresh the hotspots they belonged to.
    Returns (signals_deleted, hotspots_refreshed)."""
    expired = list(db.scalars(select(Signal).where(Signal.expires_at < now).order_by(Signal.id)))
    if not expired:
        return 0, 0
    hotspot_mod.rollup_weekly(db, expired)
    touched = sorted({s.hotspot_id for s in expired if s.hotspot_id is not None})
    ids = [s.id for s in expired]
    deleted = 0
    for i in range(0, len(ids), _CHUNK):
        deleted += _delete_where(db, Signal, Signal.id.in_(ids[i:i + _CHUNK]))
    for s in expired:
        if s in db:
            db.expunge(s)
    refreshed = 0
    for hid in touched:
        hs = db.get(Hotspot, hid)
        if hs is not None and hs.status != "archived":
            hotspot_mod.recompute(db, hs, now=now, half_life_days=float(settings.half_life_days))
            refreshed += 1
    db.flush()
    return deleted, refreshed


def run_retention(db: Session, *, now: dt.datetime, settings: Settings) -> dict[str, int]:
    """1 expired signals -> rollup_weekly then delete; 2 pending_items past purge_after; 3 query_log > 30 d,
    items_seen > 90 d, geocode_cache > 180 d, scan_runs > 180 d (and their query_log), expired/revoked sessions;
    4 archive stale hotspots; 5 audit_log and reviews > 1 year. Returns counts only. Caller commits."""
    now = _aware(now)
    counts: dict[str, int] = {}

    # 1. Signals past expires_at (R6), after rolling their counts into hotspot_weekly.
    counts["signals_deleted"], counts["hotspots_refreshed"] = _expire_signals(db, now, settings)

    # 2. Pending items: purge_after passed, or older than the pending TTL whatever purge_after says.
    ttl_cutoff = now - dt.timedelta(hours=int(settings.pending_item_ttl_hours))
    counts["pending_items_deleted"] = _delete_where(
        db, PendingItem, or_(PendingItem.purge_after < now, PendingItem.collected_at < ttl_cutoff))

    # 3. Operational tables.
    counts["query_log_deleted"] = _delete_where(
        db, QueryLog, QueryLog.at < now - dt.timedelta(days=QUERY_LOG_DAYS))
    counts["items_seen_deleted"] = _delete_where(
        db, ItemSeen, ItemSeen.first_seen < now - dt.timedelta(days=ITEMS_SEEN_DAYS))
    counts["geocode_cache_deleted"] = _delete_where(
        db, GeocodeCache, GeocodeCache.created_at < now - dt.timedelta(days=GEOCODE_CACHE_DAYS))
    old_scans = list(db.scalars(select(ScanRun.id).where(
        ScanRun.started_at < now - dt.timedelta(days=SCAN_RUNS_DAYS),
        ~exists().where(Signal.scan_id == ScanRun.id),
    )))
    scan_q = scan_runs = 0
    for i in range(0, len(old_scans), _CHUNK):
        chunk = old_scans[i:i + _CHUNK]
        scan_q += _delete_where(db, QueryLog, QueryLog.scan_id.in_(chunk))
        scan_runs += _delete_where(db, ScanRun, ScanRun.id.in_(chunk))
    counts["query_log_deleted"] += scan_q
    counts["scan_runs_deleted"] = scan_runs
    counts["sessions_deleted"] = _delete_where(
        db, UserSession, or_(UserSession.expires_at < now, UserSession.revoked.is_(True)))

    # 4. Hotspots with no signal in HOTSPOT_ARCHIVE_DAYS (notes and team cleared).
    counts["hotspots_archived"] = hotspot_mod.archive_stale(
        db, now=now, archive_days=int(settings.hotspot_archive_days))

    # 5. Audit log and reviews older than one year (R10).
    counts["audit_log_deleted"] = _delete_where(db, AuditLog, AuditLog.at < now - dt.timedelta(days=AUDIT_DAYS))
    counts["reviews_deleted"] = _delete_where(db, Review, Review.at < now - dt.timedelta(days=REVIEWS_DAYS))

    db.flush()
    total = sum(v for k, v in counts.items() if k != "hotspots_refreshed")
    if total:
        logger.info("retention: %s", ", ".join(f"{k}={v}" for k, v in counts.items() if v))
    else:
        logger.debug("retention: nothing to delete")
    return counts

