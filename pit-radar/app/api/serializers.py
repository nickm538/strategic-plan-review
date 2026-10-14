"""JSON shapes shared by the REST API and SSE events (docs/07 §2 payload shapes). STUB - api builder.

The runner calls these to publish signal.new / hotspot.updated, so SSE payloads are identical to REST payloads.
"""
from __future__ import annotations

from typing import Any

from app.models import Hotspot, Signal


def signal_to_dict(signal: Signal, *, role: str) -> dict[str, Any]:
    """docs/07 Signal shape plus: review_reason, checks (V1-V8 list for 'Why this was kept'), town,
    hotspot_id, reviewed_at, status_label. R11: lat/lon included only for role in (reviewer, admin) AND
    precision == 'public_place'; otherwise lat/lon are null. Self-reports never carry coordinates (R7)."""
    raise NotImplementedError


def hotspot_to_dict(hotspot: Hotspot) -> dict[str, Any]:
    """docs/07 Hotspot shape plus town, needs_review_30d, notes, updated_at."""
    raise NotImplementedError


def min_role_for_signal(signal: Signal) -> str:
    """'viewer' for ai_verified/confirmed; 'reviewer' for everything else (needs_human_review, unlocated,
    self_report, dismissed). Used for SSE audience filtering and list endpoints."""
    raise NotImplementedError
