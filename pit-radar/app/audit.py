"""Audit log helper (R10).

Rows record who did what, when, to which object. Raw IP addresses are never stored: ip_hash is
sha256(ip + daily salt), where the salt is HMAC(SESSION_SECRET, UTC date), so the same address can be
correlated within a day but not across days. Details never contain passwords, tokens or secrets.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import re
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.auth import secret_key
from app.db import utcnow
from app.models import AuditLog
from app.security import redact_text

log = logging.getLogger(__name__)

AUDIT_ACTIONS = frozenset({
    "login", "login_failed", "logout", "export", "review", "admin_change",
    "scan_trigger", "password_change", "geo_rebuild",
})

# Detail keys whose values are always replaced. Matched case-insensitively anywhere in the key.
_SENSITIVE_KEY = re.compile(r"pass(word|wd|phrase)?|token|secret|api[_-]?key|apikey|authorization|cookie|csrf",
                            re.IGNORECASE)
_REDACTED = "[redacted]"
_MAX_STR = 500
_MAX_ITEMS = 50
_MAX_DEPTH = 4


def _daily_salt(day: dt.date) -> str:
    return hmac.new(secret_key(), day.isoformat().encode("ascii"), hashlib.sha256).hexdigest()


def ip_hash(ip: Optional[str], *, now: Optional[dt.datetime] = None) -> Optional[str]:
    """sha256(ip + daily salt); salt derived from SESSION_SECRET and the UTC date. Never store raw IPs."""
    if not ip:
        return None
    day = (now or utcnow()).astimezone(dt.timezone.utc).date()
    return hashlib.sha256((str(ip) + _daily_salt(day)).encode("utf-8")).hexdigest()


def _scrub_text(value: str) -> str:
    value = redact_text(value)
    return value if len(value) <= _MAX_STR else value[:_MAX_STR] + "…"


def _sanitize(value: Any, depth: int = 0) -> Any:
    """JSON-safe copy with sensitive keys redacted, long strings truncated and nesting bounded."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if depth >= _MAX_DEPTH:
        return _REDACTED
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _MAX_ITEMS:
                break
            key = str(k)[:64]
            out[key] = _REDACTED if _SENSITIVE_KEY.search(key) else _sanitize(v, depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize(v, depth + 1) for v in list(value)[:_MAX_ITEMS]]
    return _scrub_text(str(value))


def audit(db: Session, action: str, *, user_id: Optional[int] = None, object_type: Optional[str] = None,
          object_id: Optional[str | int] = None, details: Optional[dict[str, Any]] = None, ip: Optional[str] = None,
          at: Optional[dt.datetime] = None) -> None:
    """Add an audit_log row (caller commits). action: login | login_failed | logout | export | review |
    admin_change | scan_trigger | password_change | geo_rebuild."""
    if action not in AUDIT_ACTIONS:
        log.warning("audit: non-standard action %r", action[:32])
    when = at or utcnow()
    db.add(AuditLog(
        at=when,
        user_id=user_id,
        action=action[:32],
        object_type=object_type[:32] if object_type else None,
        object_id=None if object_id is None else str(object_id)[:200],
        details=_sanitize(details or {}),
        ip_hash=ip_hash(ip, now=when),
    ))
