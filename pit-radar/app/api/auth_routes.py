"""Auth routes (docs/07 §1 and §7): login, logout, current user, password change.

Login failures are generic ("Invalid name or password") whether the name exists or not; unknown names
cost one Argon2 verification like real ones and lock out the same way, so neither timing nor the
lockout response reveals which names exist.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import threading
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app import auth
from app.api.deps import Principal, current_principal, get_client_ip, request_now, writer
from app.audit import audit
from app.config import Settings, get_settings
from app.db import get_db
from app.models import UserSession
from app.security import LoginThrottle, rate_limit_login, too_many

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["auth"])

GENERIC_LOGIN_ERROR = "Invalid name or password"
MAX_NAME_LEN = 80


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: Optional[str] = None
    password: Optional[str] = None


class PasswordChangeBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    current_password: Optional[str] = None
    new_password: Optional[str] = None


# --- Lockout bookkeeping for names that do not exist ------------------------------------

@dataclass
class _LockState:
    failed_login_count: int = 0
    failed_login_window_start: Optional[dt.datetime] = None
    locked_until: Optional[dt.datetime] = None


class _UnknownNameLockouts:
    """In-memory lockout state for unknown names (same rules as real users), bounded in size."""

    def __init__(self, max_entries: int = 10_000) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, _LockState] = {}
        self._max = max_entries

    @staticmethod
    def _key(name: str) -> str:
        return name.casefold()[:MAX_NAME_LEN]

    def locked_seconds(self, name: str, *, now: dt.datetime) -> float:
        with self._lock:
            st = self._states.get(self._key(name))
            if st is None or st.locked_until is None or st.locked_until <= now:
                return 0.0
            return (st.locked_until - now).total_seconds()

    def fail(self, name: str, *, now: dt.datetime, max_failures: int, lockout_minutes: int) -> bool:
        with self._lock:
            key = self._key(name)
            st = self._states.get(key)
            if st is None:
                if len(self._states) >= self._max:
                    self._prune(now, dt.timedelta(minutes=lockout_minutes))
                st = self._states[key] = _LockState()
            return auth.register_failed_login(st, now=now, max_failures=max_failures,
                                              lockout_minutes=lockout_minutes)

    def _prune(self, now: dt.datetime, window: dt.timedelta) -> None:
        for k, st in list(self._states.items()):
            locked = st.locked_until is not None and st.locked_until > now
            active = st.failed_login_window_start is not None and now - st.failed_login_window_start < window
            if not locked and not active:
                del self._states[k]
        if len(self._states) >= self._max:
            self._states.clear()

    def reset(self) -> None:
        with self._lock:
            self._states.clear()


_unknown_names = _UnknownNameLockouts()


def reset_login_state() -> None:
    """Forget in-memory lockouts for unknown names (tests)."""
    _unknown_names.reset()


# --- Helpers -----------------------------------------------------------------------------

def _lock_error(seconds: float) -> HTTPException:
    minutes = max(1, math.ceil(seconds / 60))
    unit = "minute" if minutes == 1 else "minutes"
    return too_many(math.ceil(seconds), f"Too many failed sign-in attempts. Try again in {minutes} {unit}.")


def _user_lock_seconds(user: Any, now: dt.datetime) -> float:
    if user.locked_until is None or user.locked_until <= now:
        return 0.0
    return (user.locked_until - now).total_seconds()


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    ttl_s = int(settings.session_ttl_hours) * 3600
    response.set_cookie(auth.COOKIE_NAME, token, max_age=ttl_s, expires=ttl_s, path="/",
                        secure=settings.cookie_secure, httponly=True, samesite="lax")


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(auth.COOKIE_NAME, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")


def _token_shape_ok(token: Optional[str]) -> bool:
    return isinstance(token, str) and 0 < len(token) <= auth.MAX_TOKEN_LEN


# --- Routes ------------------------------------------------------------------------------

@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response, db: Session = Depends(get_db),
          now: dt.datetime = Depends(request_now),
          throttle: LoginThrottle = Depends(rate_limit_login())) -> dict[str, Any]:
    """Sign in with name + password. Sets the pr_session cookie and returns the CSRF token."""
    settings = get_settings()
    ip = get_client_ip(request)
    name = body.name.strip() if isinstance(body.name, str) else ""
    password = body.password if isinstance(body.password, str) else ""
    attempted = name[:MAX_NAME_LEN]

    def fail(reason: str, *, user_id: Optional[int] = None, lock_seconds: float = 0.0) -> HTTPException:
        throttle.record_failure()
        audit(db, "login_failed", user_id=user_id, details={"name": attempted, "reason": reason}, ip=ip, at=now)
        db.commit()
        log.info("login failed (reason=%s)", reason)
        return _lock_error(lock_seconds) if lock_seconds > 0 else HTTPException(401, GENERIC_LOGIN_ERROR)

    if not name or not password or len(name) > MAX_NAME_LEN or len(password) > auth.MAX_PASSWORD_LEN:
        raise fail("invalid_input")

    user = auth.find_user_by_name(db, name)
    if user is None:
        locked_s = _unknown_names.locked_seconds(name, now=now)
        if locked_s:
            raise fail("locked", lock_seconds=locked_s)
        auth.dummy_verify(password)
        locked = _unknown_names.fail(name, now=now, max_failures=settings.login_max_failures,
                                     lockout_minutes=settings.login_lockout_minutes)
        raise fail("unknown_user", lock_seconds=settings.login_lockout_minutes * 60 if locked else 0.0)

    locked_s = _user_lock_seconds(user, now)
    if locked_s:
        raise fail("locked", user_id=user.id, lock_seconds=locked_s)

    if not auth.verify_password(user.password_hash, password):
        locked = auth.register_failed_login(user, now=now, max_failures=settings.login_max_failures,
                                            lockout_minutes=settings.login_lockout_minutes)
        raise fail("bad_password", user_id=user.id, lock_seconds=_user_lock_seconds(user, now) if locked else 0.0)

    if not user.active:
        raise fail("inactive", user_id=user.id)

    # Session fixation: retire any live session this browser already carried.
    old_token = request.cookies.get(auth.COOKIE_NAME)
    if _token_shape_ok(old_token):
        old = db.get(UserSession, auth.session_id_for(old_token))
        if old is not None and not old.revoked and old.expires_at > now:
            auth.revoke_session(db, old)

    auth.register_successful_login(user, now=now)
    if auth.needs_rehash(user.password_hash):
        user.password_hash = auth.hash_password(password)
    auth.purge_sessions(db, now=now, user_id=user.id)
    token, csrf = auth.create_session(db, user, now=now, ttl_hours=settings.session_ttl_hours)
    audit(db, "login", user_id=user.id, ip=ip, at=now)
    db.commit()
    log.info("login ok (user_id=%s)", user.id)

    _set_session_cookie(response, token, settings)
    return {"user": {"name": user.name, "role": user.role}, "csrf_token": csrf}


@router.post("/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db),
           now: dt.datetime = Depends(request_now)) -> dict[str, Any]:
    """Revoke the session and clear the cookie. A live session needs the CSRF header; an expired,
    revoked or missing session still gets 200 and a cleared cookie."""
    settings = get_settings()
    token = request.cookies.get(auth.COOKIE_NAME)
    ip = get_client_ip(request)
    resolved = auth.resolve_session(db, token, now=now)
    if resolved is not None:
        user, sess = resolved
        if not auth.check_csrf(sess, request.headers.get(auth.CSRF_HEADER)):
            raise HTTPException(status_code=403, detail="Missing or invalid CSRF token")
        auth.revoke_session(db, sess)
        audit(db, "logout", user_id=user.id, ip=ip, at=now)
        db.commit()
    elif _token_shape_ok(token):
        sess = db.get(UserSession, auth.session_id_for(token))
        if sess is not None and not sess.revoked:
            auth.revoke_session(db, sess)
            audit(db, "logout", user_id=sess.user_id, details={"session": "expired_or_invalid"}, ip=ip, at=now)
            db.commit()
    _clear_session_cookie(response, settings)
    return {"ok": True}


@router.get("/me")
def me(request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Current user and the session's CSRF token (re-derived from the cookie)."""
    token = request.cookies.get(auth.COOKIE_NAME) or ""
    return {"name": principal.user.name, "role": principal.user.role, "csrf_token": auth.csrf_for_token(token)}


@router.post("/me/password")
def change_password(body: PasswordChangeBody, request: Request, db: Session = Depends(get_db),
                    now: dt.datetime = Depends(request_now),
                    principal: Principal = Depends(writer("viewer"))) -> dict[str, Any]:
    """Change the signed-in user's password. Other sessions of this user are revoked."""
    settings = get_settings()
    user = principal.user
    ip = get_client_ip(request)
    current = body.current_password if isinstance(body.current_password, str) else ""
    new = body.new_password if isinstance(body.new_password, str) else ""

    locked_s = _user_lock_seconds(user, now)
    if locked_s:
        raise _lock_error(locked_s)

    if not current or len(current) > auth.MAX_PASSWORD_LEN or not auth.verify_password(user.password_hash, current):
        locked = auth.register_failed_login(user, now=now, max_failures=settings.login_max_failures,
                                            lockout_minutes=settings.login_lockout_minutes)
        audit(db, "password_change", user_id=user.id, details={"ok": False, "reason": "current_password_incorrect"},
              ip=ip, at=now)
        db.commit()
        if locked:
            raise _lock_error(_user_lock_seconds(user, now))
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    problem = auth.validate_new_password(new, name=user.name)
    if problem is None and new == current:
        problem = "New password must be different from the current password."
    if problem:
        raise HTTPException(status_code=422, detail=problem)

    user.password_hash = auth.hash_password(new)
    user.password_changed_at = now
    user.failed_login_count = 0
    user.failed_login_window_start = None
    revoked = auth.revoke_user_sessions(db, user.id, except_session_id=principal.session.id)
    audit(db, "password_change", user_id=user.id, details={"ok": True, "other_sessions_revoked": revoked},
          ip=ip, at=now)
    db.commit()
    return {"ok": True}
