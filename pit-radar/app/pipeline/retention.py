"""Retention jobs (docs/09 §4, R6). STUB - storage builder."""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from app.config import Settings


def run_retention(db: Session, *, now: dt.datetime, settings: Settings) -> dict[str, int]:
    """1 expired signals -> rollup_weekly then delete; 2 pending_items past purge_after; 3 query_log > 30 d,
    items_seen > 90 d, geocode_cache > 180 d, scan_runs > 180 d (and their query_log), expired/revoked sessions;
    4 archive stale hotspots; 5 audit_log and reviews > 1 year. Returns counts only. Caller commits."""
    raise NotImplementedError
