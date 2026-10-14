"""Passwords, sessions, CSRF, lockout (docs/07 §1, docs/09 §1). STUB - auth builder."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy.orm import Session

from app.models import User, UserSession

COOKIE_NAME = "pr_session"
CSRF_HEADER = "X-CSRF-Token"


def hash_password(password: str) -> str:
    raise NotImplementedError


def verify_password(password_hash: str, password: str) -> bool:
    raise NotImplementedError


def validate_new_password(password: str) -> Optional[str]:
    """None if acceptable (>= 12 chars, not all one class, not the user name), else an error message."""
    raise NotImplementedError


def create_session(db: Session, user: User, *, now: dt.datetime, ttl_hours: int) -> tuple[str, str]:
    """Returns (session_token, csrf_token). Stores only sha256 hashes. Caller commits."""
    raise NotImplementedError


def resolve_session(db: Session, token: Optional[str], *, now: dt.datetime) -> Optional[tuple[User, UserSession]]:
    raise NotImplementedError


def check_csrf(session: UserSession, header_value: Optional[str]) -> bool:
    raise NotImplementedError


def revoke_session(db: Session, session: UserSession) -> None:
    raise NotImplementedError
