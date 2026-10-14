"""Audit log helper (R10). STUB - auth builder."""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session


def ip_hash(ip: Optional[str]) -> Optional[str]:
    """sha256(ip + daily salt); salt derived from SESSION_SECRET and the UTC date. Never store raw IPs."""
    raise NotImplementedError


def audit(db: Session, action: str, *, user_id: Optional[int] = None, object_type: Optional[str] = None,
          object_id: Optional[str | int] = None, details: Optional[dict[str, Any]] = None, ip: Optional[str] = None) -> None:
    """Add an audit_log row (caller commits). action: login | login_failed | logout | export | review |
    admin_change | scan_trigger | password_change | geo_rebuild."""
    raise NotImplementedError
