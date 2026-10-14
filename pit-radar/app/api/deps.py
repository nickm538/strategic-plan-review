"""FastAPI dependencies: current user, role checks, CSRF (docs/07). STUB - auth builder."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from app.models import User, UserSession


@dataclass
class Principal:
    user: User
    session: UserSession

    @property
    def role(self) -> str:
        return self.user.role


def current_principal(request: Request) -> Principal:
    """Resolve the pr_session cookie -> Principal, else HTTP 401."""
    raise NotImplementedError


def require_role(min_role: str):
    """Dependency factory: 403 unless principal role >= min_role (viewer < reviewer < admin)."""
    raise NotImplementedError


def require_csrf(request: Request) -> None:
    """For POST/PUT/PATCH/DELETE: X-CSRF-Token must match the session's token, else 403."""
    raise NotImplementedError
