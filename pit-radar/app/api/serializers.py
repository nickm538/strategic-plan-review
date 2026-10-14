"""JSON shapes shared by the REST API and SSE events (docs/07 §2 payload shapes).

The runner calls these to publish signal.new / hotspot.updated, so SSE payloads are identical to REST payloads.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from app.models import MAPPED_STATUSES, Hotspot, Signal

STATUS_LABELS = {
    "ai_verified": "AI-verified",
    "confirmed": "Staff-confirmed",
    "needs_human_review": "Needs review",
    "unlocated": "Unlocated",
    "self_report": "Self-report",
    "dismissed": "Dismissed",
}
CHECK_ORDER = ("V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8")
CHECK_LABELS = {
    "V1": "Quotes found in the post",
    "V2": "Quotes pass privacy rules",
    "V3": "Place found in the post",
    "V4": "Inside Nassau/Suffolk",
    "V5": "Current or ongoing",
    "V6": "Summary passes privacy rules",
    "V7": "Verdict is consistent",
    "V8": "Not a private residence",
}
FLAG_KEYS = ("possible_minors", "medical_concern", "weather_exposure", "safety_concern")


def iso(d: Optional[dt.datetime]) -> Optional[str]:
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _checks_list(raw: Any) -> list[dict[str, Any]]:
    """Stored checks are {code: {passed, detail}}; the API returns an ordered list with display labels."""
    out: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = ((c.get("code"), c) for c in raw if isinstance(c, dict))
    else:
        items = ()
    by_code = {str(k): v for k, v in items if k}
    for code in sorted(by_code, key=lambda c: (CHECK_ORDER.index(c) if c in CHECK_ORDER else 99, c)):
        v = by_code[code]
        passed = bool(v.get("passed")) if isinstance(v, dict) else bool(v)
        detail = str(v.get("detail", "")) if isinstance(v, dict) else ""
        out.append({"code": code, "label": CHECK_LABELS.get(code, code), "passed": passed, "detail": detail[:200]})
    return out


def _quotes(raw: Any) -> list[dict[str, Any]]:
    out = []
    for q in raw or []:
        if isinstance(q, dict) and isinstance(q.get("text"), str):
            out.append({"text": q["text"], "supports": q.get("supports", "situation"), "masked": bool(q.get("masked"))})
    return out[:2]


def _flags(raw: Any) -> dict[str, bool]:
    raw = raw if isinstance(raw, dict) else {}
    return {k: bool(raw.get(k, False)) for k in FLAG_KEYS}


def min_role_for_signal(signal: Signal) -> str:
    """'viewer' for ai_verified/confirmed; 'reviewer' for everything else (needs_human_review, unlocated,
    self_report, dismissed). Used for SSE audience filtering and list endpoints."""
    return "viewer" if signal.status in MAPPED_STATUSES else "reviewer"


def signal_to_dict(signal: Signal, *, role: str) -> dict[str, Any]:
    """docs/07 Signal shape plus: review_reason, checks (V1-V8 list for 'Why this was kept'), town,
    hotspot_id, reviewed_at, status_label. R11: lat/lon included only for role in (reviewer, admin) AND
    precision == 'public_place'; otherwise lat/lon are null. Self-reports never carry coordinates (R7)."""
    show_coords = (role in ("reviewer", "admin") and signal.precision == "public_place"
                   and signal.status != "self_report")
    return {
        "id": int(signal.id),
        "hotspot_id": signal.hotspot_id,
        "platform": signal.platform,
        "provider": signal.provider,
        "url": signal.url,
        "published_at": iso(signal.published_at),
        "collected_at": iso(signal.collected_at),
        "signal_type": signal.signal_type,
        "status": signal.status,
        "status_label": STATUS_LABELS.get(signal.status, signal.status),
        "review_reason": signal.review_reason,
        "ai_confidence": round(float(signal.ai_confidence or 0.0), 3),
        "model": signal.model,
        "neutral_summary": signal.neutral_summary,
        "evidence_quotes": _quotes(signal.evidence_quotes),
        "evidence_redacted": bool(signal.evidence_redacted),
        "location_text": signal.location_text,
        "precision": signal.precision,
        "place_label": signal.place_label,
        "town": signal.town,
        "county": signal.county,
        "time_reference": signal.time_reference,
        "people_count_est": signal.people_count_est,
        "flags": _flags(signal.flags),
        "decision_basis": signal.decision_basis,
        "checks": _checks_list(signal.checks),
        "reviewed_at": iso(signal.reviewed_at),
        "lat": float(signal.lat) if show_coords and signal.lat is not None else None,
        "lon": float(signal.lon) if show_coords and signal.lon is not None else None,
    }


def hotspot_to_dict(hotspot: Hotspot) -> dict[str, Any]:
    """docs/07 Hotspot shape plus town, needs_review_30d, notes, updated_at."""
    return {
        "id": int(hotspot.id),
        "place_id": hotspot.place_id,
        "label": hotspot.label,
        "county": hotspot.county,
        "town": hotspot.town,
        "lat": float(hotspot.lat),
        "lon": float(hotspot.lon),
        "primary_type": hotspot.primary_type,
        "score": round(float(hotspot.score or 0.0), 3),
        "signals_7d": int(hotspot.signals_7d or 0),
        "signals_30d": int(hotspot.signals_30d or 0),
        "distinct_sources_30d": int(hotspot.distinct_sources_30d or 0),
        "confirmed_30d": int(hotspot.confirmed_30d or 0),
        "needs_review_30d": int(hotspot.needs_review_30d or 0),
        "verified_30d": verified_count(hotspot),
        "review_only": verified_count(hotspot) == 0,
        "first_seen_at": iso(hotspot.first_seen_at),
        "last_signal_at": iso(hotspot.last_signal_at),
        "status": hotspot.status,
        "assigned_team": hotspot.assigned_team,
        "notes": hotspot.notes,
        "updated_at": iso(hotspot.updated_at),
    }


def verified_count(hotspot: Hotspot) -> int:
    """Member signals in 30 days that are AI-verified or staff-confirmed (not awaiting review)."""
    return max(0, int(hotspot.signals_30d or 0) - int(hotspot.needs_review_30d or 0))


def hotspot_events(hotspot: Hotspot) -> list[tuple[dict[str, Any], str, Optional[str]]]:
    """(payload, min_role, max_role) for hotspot.updated. A hotspot backed only by signals awaiting review is
    not on the viewers' map (docs/05 §6: not mapped until confirmed): reviewers get the full payload and viewers
    get a removal notice so a stale copy disappears."""
    full = hotspot_to_dict(hotspot)
    if verified_count(hotspot) > 0 and hotspot.status != "archived":
        return [(full, "viewer", None)]
    return [(full, "reviewer", None), ({"id": int(hotspot.id), "removed": True}, "viewer", "viewer")]
