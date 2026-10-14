"""Passwords, sessions, CSRF, lockout (docs/07 §1, docs/09 §1).

- Passwords: Argon2id via argon2-cffi (RFC 9106 low-memory profile, the library default).
- Sessions: the raw token lives only in the HttpOnly cookie. The DB stores sessions.id = sha256(token).
- CSRF: csrf_token = HMAC-SHA256(SESSION_SECRET, token) hex, so GET /api/me can re-derive it from the
  cookie; the DB stores csrf_hash = sha256(csrf_token). Because the CSRF hash is bound to the secret,
  rotating SESSION_SECRET invalidates every session (docs/09 §6 incident rule).
- Lockout: LOGIN_MAX_FAILURES failures inside LOGIN_LOCKOUT_MINUTES lock the name for
  LOGIN_LOCKOUT_MINUTES. Helpers take an injected `now` so timing is testable.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import math
import secrets
import threading
from typing import Any, Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User, UserSession

log = logging.getLogger(__name__)

COOKIE_NAME = "pr_session"
CSRF_HEADER = "X-CSRF-Token"

MIN_PASSWORD_LEN = 12
MAX_PASSWORD_LEN = 256          # argon2 handles more; this bounds request size and hashing work
MAX_TOKEN_LEN = 200             # longer cookie/header values are rejected without hashing
MIN_DISTINCT_CHARS = 5

# Argon2id with the library's RFC 9106 "low memory" defaults (t=3, m=64 MiB, p=4).
_hasher = PasswordHasher()
# Each hash/verify uses 64 MiB; bound concurrency so a burst of logins cannot exhaust memory.
_hash_slots = threading.BoundedSemaphore(4)
_dummy_hash: Optional[str] = None
_dummy_lock = threading.Lock()

_fallback_secret: Optional[bytes] = None
_fallback_lock = threading.Lock()


# --- Secret -------------------------------------------------------------------

def secret_key() -> bytes:
    """SESSION_SECRET as bytes. Outside production an unset secret falls back to a per-process
    random key (sessions then end on restart). Production refuses to run without one
    (app.security.assert_production_secrets)."""
    global _fallback_secret
    settings = get_settings()
    value = settings.session_secret.get_secret_value()
    if value:
        return value.encode("utf-8")
    if settings.is_production:
        raise RuntimeError("SESSION_SECRET is not set")
    with _fallback_lock:
        if _fallback_secret is None:
            log.warning("SESSION_SECRET is not set; using a temporary per-process key (non-production only)")
            _fallback_secret = secrets.token_bytes(32)
        return _fallback_secret


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# --- Passwords ----------------------------------------------------------------

def hash_password(password: str) -> str:
    with _hash_slots:
        return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True only if `password` matches. Mismatch, malformed or empty hashes return False."""
    if not password_hash or not isinstance(password, str):
        return False
    try:
        with _hash_slots:
            return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception:  # noqa: BLE001 - never let a corrupt hash crash a login
        log.warning("password verification failed with an unexpected error")
        return False


def needs_rehash(password_hash: str) -> bool:
    """True if the hash was made with different parameters than the current hasher's."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:  # noqa: BLE001 - malformed hash: verification already failed
        return False


def dummy_verify(password: str) -> None:
    """Run one verification against a throwaway hash so unknown names cost the same as known ones."""
    global _dummy_hash
    with _dummy_lock:
        if _dummy_hash is None:
            _dummy_hash = hash_password(secrets.token_urlsafe(24))
        h = _dummy_hash
    verify_password(h, password or "")


def _char_class(c: str) -> str:
    if c.isdigit():
        return "digit"
    if c.isalpha():
        if c.isupper():
            return "upper"
        if c.islower():
            return "lower"
        return "letter"
    return "other"   # spaces, punctuation, symbols


def validate_new_password(password: str, *, name: Optional[str] = None) -> Optional[str]:
    """None if acceptable (>= 12 chars, not all one class, not the user name), else an error message."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if len(password) > MAX_PASSWORD_LEN:
        return f"Password must be at most {MAX_PASSWORD_LEN} characters."
    if name and password.strip().casefold() == name.strip().casefold():
        return "Password must not be the same as the user name."
    if len(set(password)) < MIN_DISTINCT_CHARS:
        return "Password is too repetitive. Use a longer phrase with more different characters."
    if len({_char_class(c) for c in password}) < 2:
        return ("Password must mix at least two kinds of characters "
                "(lowercase, uppercase, digits, spaces or symbols).")
    return None


# --- Sessions and CSRF --------------------------------------------------------

def session_id_for(token: str) -> str:
    """sessions.id for a raw session token."""
    return _sha256_hex(token)


def csrf_for_token(token: str) -> str:
    """The CSRF token bound to a session token: HMAC-SHA256(SESSION_SECRET, token) hex."""
    return hmac.new(secret_key(), token.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session(db: Session, user: User, *, now: dt.datetime, ttl_hours: int) -> tuple[str, str]:
    """Returns (session_token, csrf_token). Stores only sha256 hashes. Caller commits."""
    token = secrets.token_urlsafe(32)
    csrf = csrf_for_token(token)
    db.add(UserSession(
        id=session_id_for(token),
        user_id=user.id,
        csrf_hash=_sha256_hex(csrf),
        created_at=now,
        expires_at=now + dt.timedelta(hours=ttl_hours),
        revoked=False,
    ))
    return token, csrf


def _valid_token_shape(token: Any) -> bool:
    return isinstance(token, str) and 0 < len(token) <= MAX_TOKEN_LEN


def resolve_session(db: Session, token: Optional[str], *, now: dt.datetime) -> Optional[tuple[User, UserSession]]:
    """(user, session) for a live session token; None if unknown, revoked, expired, issued under a
    different SESSION_SECRET, or the user is missing or inactive."""
    if not _valid_token_shape(token):
        return None
    sess = db.get(UserSession, session_id_for(token))
    if sess is None or sess.revoked or sess.expires_at <= now:
        return None
    if not hmac.compare_digest(_sha256_hex(csrf_for_token(token)), sess.csrf_hash):
        return None   # SESSION_SECRET was rotated since this session was issued
    user = db.get(User, sess.user_id)
    if user is None or not user.active:
        return None
    return user, sess


def check_csrf(session: UserSession, header_value: Optional[str]) -> bool:
    if not _valid_token_shape(header_value) or session is None or not session.csrf_hash:
        return False
    return hmac.compare_digest(_sha256_hex(header_value), session.csrf_hash)


def revoke_session(db: Session, session: UserSession) -> None:
    """Mark one session revoked. Caller commits."""
    session.revoked = True
    db.add(session)


def revoke_user_sessions(db: Session, user_id: int, *, except_session_id: Optional[str] = None) -> int:
    """Revoke every live session of a user (optionally keeping one). Returns the count. Caller commits.
    Call this when an admin resets a password, demotes or deactivates a user."""
    stmt = update(UserSession).where(UserSession.user_id == user_id, UserSession.revoked.is_(False))
    if except_session_id is not None:
        stmt = stmt.where(UserSession.id != except_session_id)
    res = db.execute(stmt.values(revoked=True).execution_options(synchronize_session="fetch"))
    return int(res.rowcount or 0)


def purge_sessions(db: Session, *, now: dt.datetime, user_id: Optional[int] = None,
                   grace: dt.timedelta = dt.timedelta(days=1)) -> int:
    """Delete sessions that expired more than `grace` ago (revoked or not). Caller commits."""
    stmt = delete(UserSession).where(UserSession.expires_at < now - grace)
    if user_id is not None:
        stmt = stmt.where(UserSession.user_id == user_id)
    res = db.execute(stmt.execution_options(synchronize_session=False))
    return int(res.rowcount or 0)


# --- Users and lockout --------------------------------------------------------

def find_user_by_name(db: Session, name: str) -> Optional[User]:
    """Exact name match, else a unique case-insensitive match."""
    name = (name or "").strip()
    if not name:
        return None
    rows = list(db.execute(
        select(User).where(or_(User.name == name, func.lower(User.name) == name.lower()))
    ).scalars())
    for u in rows:
        if u.name == name:
            return u
    folded = [u for u in rows if u.name.casefold() == name.casefold()]
    return folded[0] if len(folded) == 1 else None


def locked_minutes_remaining(user: Any, *, now: dt.datetime) -> int:
    """Whole minutes (rounded up) until the lock ends; 0 when not locked."""
    until = getattr(user, "locked_until", None)
    if until is None or until <= now:
        return 0
    return max(1, math.ceil((until - now).total_seconds() / 60))


def register_failed_login(user: Any, *, now: dt.datetime, max_failures: int, lockout_minutes: int) -> bool:
    """Count a failure inside the rolling window; lock when the count reaches max_failures.
    Returns True if this failure locked the account. Works on User rows and in-memory stand-ins."""
    window = dt.timedelta(minutes=lockout_minutes)
    if user.locked_until is not None and user.locked_until <= now:
        user.locked_until = None
    start = user.failed_login_window_start
    if start is None or now - start >= window:
        user.failed_login_window_start = now
        user.failed_login_count = 0
    user.failed_login_count = (user.failed_login_count or 0) + 1
    if user.failed_login_count >= max(1, max_failures):
        user.locked_until = now + window
        user.failed_login_count = 0
        user.failed_login_window_start = None
        return True
    return False


def register_successful_login(user: User, *, now: dt.datetime) -> None:
    user.failed_login_count = 0
    user.failed_login_window_start = None
    user.locked_until = None
    user.last_login_at = now
