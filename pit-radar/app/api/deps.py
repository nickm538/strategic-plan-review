"""FastAPI dependencies: current user, role checks, CSRF (docs/07).

Order of checks: no/invalid session -> 401; role too low -> 403; missing or wrong X-CSRF-Token on a
mutating request -> 403. Every authenticated request also counts against the per-session API rate limit
(120/min, docs/09 §2) and gets 429 with Retry-After when it is exhausted.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import COOKIE_NAME, CSRF_HEADER, check_csrf, resolve_session
from app.db import get_db, utcnow
from app.models import ROLES, User, UserSession
from app.security import client_ip, enforce_api_limit

ROLE_RANK = {role: rank for rank, role in enumerate(ROLES)}   # viewer < reviewer < admin
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass
class Principal:
    user: User
    session: UserSession

    @property
    def role(self) -> str:
        return self.user.role

    def has_role(self, min_role: str) -> bool:
        return role_at_least(self.user.role, min_role)


def role_at_least(role: Optional[str], min_role: str) -> bool:
    return ROLE_RANK.get(role or "", -1) >= ROLE_RANK[min_role]


def request_now() -> dt.datetime:
    """Current UTC time as a dependency, so tests can inject a clock via app.dependency_overrides."""
    return utcnow()


def get_client_ip(request: Request) -> Optional[str]:
    """Client IP (request.client.host; uvicorn --proxy-headers sets it from X-Forwarded-For)."""
    return client_ip(request)


def current_principal(request: Request, db: Session = Depends(get_db),
                      now: dt.datetime = Depends(request_now)) -> Principal:
    """Resolve the pr_session cookie -> Principal, else HTTP 401."""
    resolved = resolve_session(db, request.cookies.get(COOKIE_NAME), now=now)
    if resolved is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user, session = resolved
    principal = Principal(user=user, session=session)
    request.state.principal = principal
    enforce_api_limit(request, session.id)
    return principal


def require_role(min_role: str) -> Callable[..., Principal]:
    """Dependency factory: 403 unless principal role >= min_role (viewer < reviewer < admin)."""
    if min_role not in ROLE_RANK:
        raise ValueError(f"unknown role {min_role!r}")

    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not role_at_least(principal.user.role, min_role):
            raise HTTPException(status_code=403, detail="You do not have permission to do this")
        return principal

    dependency.__name__ = f"require_role_{min_role}"
    return dependency


def _csrf_or_403(request: Request, principal: Principal) -> None:
    if request.method.upper() in UNSAFE_METHODS and not check_csrf(principal.session, request.headers.get(CSRF_HEADER)):
        raise HTTPException(status_code=403, detail="Missing or invalid CSRF token")


def require_csrf(request: Request, principal: Principal = Depends(current_principal)) -> None:
    """For POST/PUT/PATCH/DELETE: X-CSRF-Token must match the session's token, else 403."""
    if not isinstance(principal, Principal):   # called directly rather than as a dependency
        principal = getattr(request.state, "principal", None)
        if not isinstance(principal, Principal):
            raise HTTPException(status_code=401, detail="Not authenticated")
    _csrf_or_403(request, principal)


def writer(min_role: str = "viewer") -> Callable[..., Principal]:
    """Dependency factory for mutating routes: session (401), role >= min_role (403) and a valid
    X-CSRF-Token (403). Returns the Principal."""
    role_dep = require_role(min_role)

    def dependency(request: Request, principal: Principal = Depends(role_dep)) -> Principal:
        _csrf_or_403(request, principal)
        return principal

    dependency.__name__ = f"writer_{min_role}"
    return dependency
