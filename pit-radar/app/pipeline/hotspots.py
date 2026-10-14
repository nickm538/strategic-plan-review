"""Hotspot assignment, scoring and weekly rollups (docs/03 §4). STUB - storage builder."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Hotspot, Signal

EAST_END_TOWNS = frozenset({"East Hampton", "Southampton", "Riverhead", "Southold", "Shelter Island"})
STATUS_W = {"confirmed": 1.5, "ai_verified": 1.0, "needs_human_review": 0.5}
TYPE_W = {"encampment": 1.5, "individual_unsheltered": 1.0, "vehicle_dwelling": 1.0}


def assign_signal(db: Session, signal: Signal, *, radius_m: float, east_end_radius_m: float, now: dt.datetime) -> Optional[Hotspot]:
    """Attach a mapped (or needs_human_review with coordinates) signal to a hotspot: same place_id, else
    nearest active hotspot within radius (East End towns use east_end_radius_m), else create one labelled from
    the signal's place_label/town. Sets signal.hotspot_id. Self-reports/unlocated/dismissed are never assigned."""
    raise NotImplementedError


def recompute(db: Session, hotspot: Hotspot, *, now: dt.datetime, half_life_days: float) -> None:
    """Recompute score, counts (7d/30d), distinct_sources_30d (provider+outlet), confirmed_30d,
    needs_review_30d, primary_type (>60% weighted share else 'mixed'), last_signal_at."""
    raise NotImplementedError


def recompute_all(db: Session, *, now: dt.datetime, half_life_days: float, archive_days: int) -> int:
    """Refresh every non-archived hotspot (score decays with time); archive those with no signal in
    archive_days (clear notes and assigned_team). Returns number updated."""
    raise NotImplementedError


def rollup_weekly(db: Session, signals: list[Signal]) -> None:
    """Add these signals' counts into hotspot_weekly (week_start = Monday, local) BEFORE they are deleted."""
    raise NotImplementedError
