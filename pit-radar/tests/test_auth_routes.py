"""Auth routes through a minimal FastAPI app: login/logout/me/password, lockout, cookies, CSRF, roles,
audit rows and rate limits (app/api/auth_routes.py, app/api/deps.py)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json

import pytest
from argon2 import PasswordHasher
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import auth, security
from app.api import auth_routes
from app.api.deps import request_now, require_csrf, require_role, writer
from app.config import get_settings, reset_settings_cache
from app.models import AuditLog, User, UserSession

PW = "correct horse battery staple"


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


class Clock:
    def __init__(self, t: dt.datetime) -> None:
        self.t = t

    def advance(self, **kw) -> None:
        self.t = self.t + dt.timedelta(**kw)


@pytest.fixture
def clock(now):
    return Clock(now)


@pytest.fixture
def make_app(engine, clock):
    """Build the minimal app after any env tweaks the test made."""
    built = []

    def _make() -> FastAPI:
        auth_routes.reset_login_state()
        app = FastAPI()
        security.install(app, get_settings())
        app.include_router(auth_routes.router)

        @app.get("/api/protected")
        def protected(p=Depends(require_role("viewer"))):
            return {"name": p.user.name, "role": p.role}

        @app.get("/api/admin-only")
        def admin_only(p=Depends(require_role("admin"))):
            return {"ok": True}

        @app.post("/api/reviewer-write")
        def reviewer_write(p=Depends(writer("reviewer"))):
            return {"ok": True}

        @app.delete("/api/csrf-only", dependencies=[Depends(require_csrf)])
        def csrf_only():
            return {"ok": True}

        app.dependency_overrides[request_now] = lambda: clock.t
        built.append(app)
        return app

    yield _make
    security.uninstall_log_redaction()


@pytest.fixture
def client(make_app):
    return TestClient(make_app())


def login(c: TestClient, name: str = "alex", password: str = PW):
    return c.post("/api/auth/login", json={"name": name, "password": password})


def audit_rows(db, action=None):
    db.expire_all()
    q = db.query(AuditLog)
    if action:
        q = q.filter(AuditLog.action == action)
    return q.order_by(AuditLog.id).all()


def fresh_user(db, user_id) -> User:
    db.expire_all()
    return db.get(User, user_id)


# --- login ------------------------------------------------------------------------------

def test_login_success_sets_cookie_and_returns_csrf(client, make_user, db, clock):
    u = make_user("alex", "reviewer")
    r = login(client)
    assert r.status_code == 200
    body = r.json()
    assert body["user"] == {"name": "alex", "role": "reviewer"}
    csrf = body["csrf_token"]
    assert isinstance(csrf, str) and len(csrf) == 64

    set_cookie = r.headers["set-cookie"]
    assert set_cookie.startswith("pr_session=")
    lowered = set_cookie.lower()
    assert "httponly" in lowered and "samesite=lax" in lowered and "path=/" in lowered
    assert "max-age=43200" in lowered
    assert "secure" not in lowered.replace("samesite", "")          # COOKIE_SECURE=false in tests

    token = client.cookies.get("pr_session")
    sess = db.get(UserSession, _sha(token))
    assert sess is not None and sess.user_id == u.id
    assert sess.csrf_hash == _sha(csrf)
    assert sess.expires_at == clock.t + dt.timedelta(hours=12)
    assert fresh_user(db, u.id).last_login_at == clock.t

    rows = audit_rows(db, "login")
    assert len(rows) == 1 and rows[0].user_id == u.id
    assert rows[0].ip_hash and len(rows[0].ip_hash) == 64 and "testclient" not in rows[0].ip_hash


def test_cookie_secure_and_hsts_when_enabled(make_app, make_user, monkeypatch):
    monkeypatch.setenv("COOKIE_SECURE", "true")
    reset_settings_cache()
    make_user("alex", "viewer")
    c = TestClient(make_app(), base_url="https://testserver")
    r = login(c)
    assert r.status_code == 200
    parts = [p.strip().lower() for p in r.headers["set-cookie"].split(";")]
    assert "secure" in parts and "httponly" in parts
    assert r.headers["strict-transport-security"].startswith("max-age=")
    assert c.get("/api/me").status_code == 200


def test_unknown_name_and_bad_password_are_generic(client, make_user, db):
    make_user("alex", "viewer")
    r1 = login(client, "nobody-here", PW)
    r2 = login(client, "alex", "wrong password entirely")
    for r in (r1, r2):
        assert r.status_code == 401
        assert r.json() == {"detail": "Invalid name or password"}
        assert "set-cookie" not in r.headers
    rows = audit_rows(db, "login_failed")
    assert [row.details["reason"] for row in rows] == ["unknown_user", "bad_password"]
    assert rows[0].details["name"] == "nobody-here" and rows[0].user_id is None
    dumped = json.dumps([row.details for row in rows])
    assert "wrong password" not in dumped and PW not in dumped


def test_attempted_name_is_truncated_in_audit(client, db):
    r = login(client, "n" * 79, PW)
    assert r.status_code == 401
    long = "m" * 200
    r = login(client, long, PW)
    assert r.status_code == 401
    rows = audit_rows(db, "login_failed")
    assert all(len(row.details["name"]) <= 80 for row in rows)


@pytest.mark.parametrize("body", [{}, {"name": "alex"}, {"password": PW}, {"name": "", "password": ""},
                                  {"name": "alex", "password": "x" * 5000}])
def test_incomplete_login_bodies_are_401(client, make_user, body):
    make_user("alex", "viewer")
    r = client.post("/api/auth/login", json=body)
    assert r.status_code == 401
    assert "x" * 100 not in r.text


def test_lockout_after_five_failures_then_expiry(client, make_user, db, clock):
    u = make_user("alex", "viewer")
    for _ in range(4):
        assert login(client, password="wrong password 123").status_code == 401
    r = login(client, password="wrong password 123")
    assert r.status_code == 429
    assert "15 minutes" in r.json()["detail"]
    assert r.headers["retry-after"] == "900"
    locked_until = fresh_user(db, u.id).locked_until
    assert locked_until == clock.t + dt.timedelta(minutes=15)

    r = login(client)                                   # correct password, still locked
    assert r.status_code == 429
    clock.advance(minutes=14)
    r = login(client)
    assert r.status_code == 429 and "1 minute." in r.json()["detail"]
    clock.advance(minutes=1)
    r = login(client)
    assert r.status_code == 200
    fresh = fresh_user(db, u.id)
    assert fresh.failed_login_count == 0 and fresh.locked_until is None and fresh.failed_login_window_start is None
    reasons = [row.details["reason"] for row in audit_rows(db, "login_failed")]
    assert reasons == ["bad_password"] * 5 + ["locked", "locked"]


def test_failures_spread_beyond_window_do_not_lock(client, make_user, clock):
    make_user("alex", "viewer")
    for _ in range(4):
        assert login(client, password="wrong password 123").status_code == 401
    clock.advance(minutes=16)
    for _ in range(4):
        assert login(client, password="wrong password 123").status_code == 401
    assert login(client).status_code == 200


def test_success_resets_failure_count(client, make_user, db):
    u = make_user("alex", "viewer")
    for _ in range(3):
        login(client, password="wrong password 123")
    assert login(client).status_code == 200
    assert fresh_user(db, u.id).failed_login_count == 0
    for _ in range(4):
        assert login(client, password="wrong password 123").status_code == 401


def test_unknown_names_lock_out_like_real_ones(client, clock):
    for _ in range(4):
        assert login(client, "ghost-user", "wrong password 123").status_code == 401
    r = login(client, "ghost-user", "wrong password 123")
    assert r.status_code == 429 and "15 minutes" in r.json()["detail"]
    assert login(client, "Ghost-User", "wrong password 123").status_code == 429
    clock.advance(minutes=15)
    assert login(client, "ghost-user", "wrong password 123").status_code == 401


def test_inactive_user_cannot_log_in_or_use_session(client, make_user, db):
    u = make_user("alex", "viewer")
    assert login(client).status_code == 200
    assert client.get("/api/protected").status_code == 200
    db.expire_all()
    db.get(User, u.id).active = False
    db.commit()
    assert client.get("/api/protected").status_code == 401
    r = login(client)
    assert r.status_code == 401 and r.json()["detail"] == "Invalid name or password"
    assert audit_rows(db, "login_failed")[-1].details["reason"] == "inactive"


def test_name_match_is_case_insensitive(client, make_user):
    make_user("Alex", "viewer")
    r = login(client, "alex")
    assert r.status_code == 200 and r.json()["user"]["name"] == "Alex"


def test_rehash_on_login(client, make_user, db):
    u = make_user("alex", "viewer")
    db.expire_all()
    row = db.get(User, u.id)
    row.password_hash = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(PW)
    db.commit()
    assert login(client).status_code == 200
    new_hash = fresh_user(db, u.id).password_hash
    assert "m=65536" in new_hash and auth.verify_password(new_hash, PW)


def test_login_revokes_session_already_in_browser(client, make_user, db):
    make_user("alex", "viewer")
    make_user("blair", "viewer")
    assert login(client).status_code == 200
    first = client.cookies.get("pr_session")
    assert login(client, "blair").status_code == 200
    db.expire_all()
    assert db.get(UserSession, _sha(first)).revoked is True
    assert client.get("/api/me").json()["name"] == "blair"


# --- sessions, roles and CSRF -----------------------------------------------------------

@pytest.mark.parametrize("cookie", [None, "garbage", "x" * 400])
def test_protected_route_requires_session(client, cookie):
    if cookie:
        client.cookies.set("pr_session", cookie)
    r = client.get("/api/protected")
    assert r.status_code == 401 and r.json() == {"detail": "Not authenticated"}
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/me/password", json={}).status_code == 401
    assert client.post("/api/reviewer-write").status_code == 401


def test_role_checks(make_app, make_user):
    app = make_app()
    make_user("vee", "viewer")
    make_user("ada", "admin")
    viewer, admin = TestClient(app), TestClient(app)
    assert login(viewer, "vee").status_code == 200
    assert login(admin, "ada").status_code == 200
    assert viewer.get("/api/protected").json() == {"name": "vee", "role": "viewer"}
    r = viewer.get("/api/admin-only")
    assert r.status_code == 403 and "permission" in r.json()["detail"]
    assert admin.get("/api/admin-only").status_code == 200


def test_csrf_required_on_writes(make_app, make_user):
    app = make_app()
    make_user("rae", "reviewer")
    make_user("vee", "viewer")
    reviewer, viewer = TestClient(app), TestClient(app)
    csrf = login(reviewer, "rae").json()["csrf_token"]
    vcsrf = login(viewer, "vee").json()["csrf_token"]

    r = reviewer.post("/api/reviewer-write")
    assert r.status_code == 403 and "CSRF" in r.json()["detail"]
    assert reviewer.post("/api/reviewer-write", headers={"X-CSRF-Token": "0" * 64}).status_code == 403
    assert reviewer.post("/api/reviewer-write", headers={"X-CSRF-Token": vcsrf}).status_code == 403
    assert reviewer.post("/api/reviewer-write", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert viewer.post("/api/reviewer-write", headers={"X-CSRF-Token": vcsrf}).status_code == 403   # role
    assert reviewer.delete("/api/csrf-only").status_code == 403
    assert reviewer.delete("/api/csrf-only", headers={"X-CSRF-Token": csrf}).status_code == 200


def test_me_returns_the_login_csrf_token(client, make_user):
    make_user("alex", "admin")
    csrf = login(client).json()["csrf_token"]
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json() == {"name": "alex", "role": "admin", "csrf_token": csrf}
    assert r.headers["cache-control"] == "no-store"


def test_session_expiry_is_fixed(client, make_user, clock):
    make_user("alex", "viewer")
    assert login(client).status_code == 200
    clock.advance(hours=6)
    assert client.get("/api/me").status_code == 200      # activity does not extend the session
    clock.advance(hours=5, minutes=59)
    assert client.get("/api/me").status_code == 200
    clock.advance(minutes=1)
    assert client.get("/api/me").status_code == 401


# --- logout ---------------------------------------------------------------------------

def test_logout_revokes_and_clears_cookie(client, make_user, db):
    u = make_user("alex", "viewer")
    csrf = login(client).json()["csrf_token"]
    token = client.cookies.get("pr_session")
    r = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json() == {"ok": True}
    sc = r.headers["set-cookie"].lower()
    assert sc.startswith("pr_session=") and ("max-age=0" in sc or "expires=" in sc)
    db.expire_all()
    assert db.get(UserSession, _sha(token)).revoked is True
    replay = TestClient(client.app)
    replay.cookies.set("pr_session", token)
    assert replay.get("/api/me").status_code == 401
    rows = audit_rows(db, "logout")
    assert len(rows) == 1 and rows[0].user_id == u.id


def test_logout_live_session_needs_csrf(client, make_user):
    make_user("alex", "viewer")
    login(client)
    r = client.post("/api/auth/logout")
    assert r.status_code == 403
    assert client.get("/api/me").status_code == 200


def test_logout_with_expired_session_still_ok(client, make_user, db, clock):
    make_user("alex", "viewer")
    login(client)
    token = client.cookies.get("pr_session")
    clock.advance(hours=13)
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    db.expire_all()
    assert db.get(UserSession, _sha(token)).revoked is True


def test_logout_without_session_ok(client):
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.set("pr_session", "garbage")
    assert client.post("/api/auth/logout").status_code == 200


# --- password change ------------------------------------------------------------------

def test_password_change_revokes_other_sessions(make_app, make_user, db, clock):
    app = make_app()
    u = make_user("alex", "viewer")
    a, b = TestClient(app), TestClient(app)
    csrf = login(a).json()["csrf_token"]
    assert login(b).status_code == 200
    new_pw = "a brand new passphrase 2026"
    r = a.post("/api/me/password", json={"current_password": PW, "new_password": new_pw},
               headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert a.get("/api/me").status_code == 200
    assert b.get("/api/me").status_code == 401
    assert fresh_user(db, u.id).password_changed_at == clock.t
    rows = audit_rows(db, "password_change")
    assert rows[-1].details == {"ok": True, "other_sessions_revoked": 1}
    assert new_pw not in json.dumps([row.details for row in audit_rows(db)])
    c = TestClient(app)
    assert login(c).status_code == 401
    assert login(c, password=new_pw).status_code == 200


def test_password_change_errors(client, make_user):
    make_user("alex", "viewer")
    csrf = login(client).json()["csrf_token"]
    h = {"X-CSRF-Token": csrf}
    body = {"current_password": PW, "new_password": "another good passphrase 1"}
    assert client.post("/api/me/password", json=body).status_code == 403          # no CSRF
    r = client.post("/api/me/password", json={**body, "current_password": "not my password 1"}, headers=h)
    assert r.status_code == 400 and r.json()["detail"] == "Current password is incorrect"
    r = client.post("/api/me/password", json={"current_password": PW, "new_password": "short"}, headers=h)
    assert r.status_code == 422 and "at least 12" in r.json()["detail"]
    r = client.post("/api/me/password", json={"current_password": PW, "new_password": PW}, headers=h)
    assert r.status_code == 422 and "different" in r.json()["detail"]
    r = client.post("/api/me/password", json={"current_password": PW, "new_password": "ALEX"}, headers=h)
    assert r.status_code == 422


def test_password_change_wrong_current_counts_toward_lockout(client, make_user):
    make_user("alex", "viewer")
    csrf = login(client).json()["csrf_token"]
    h = {"X-CSRF-Token": csrf}
    body = {"current_password": "not my password 1", "new_password": "another good passphrase 1"}
    for _ in range(4):
        assert client.post("/api/me/password", json=body, headers=h).status_code == 400
    assert client.post("/api/me/password", json=body, headers=h).status_code == 429
    ok = {"current_password": PW, "new_password": "another good passphrase 1"}
    assert client.post("/api/me/password", json=ok, headers=h).status_code == 429


# --- rate limits ------------------------------------------------------------------------

def test_login_ip_throttle_counts_failures_only(client, make_user):
    make_user("alex", "viewer")
    limiter = client.app.state.pr_rate_limiter
    for _ in range(security.LOGIN_FAILURES_PER_IP - 1):
        limiter.hit("login:testclient", security.LOGIN_FAILURES_PER_IP, security.LOGIN_WINDOW_S)
    assert login(client).status_code == 200                       # successes are not counted
    assert login(client, "someone-else", "wrong password 123").status_code == 401   # 20th failure
    r = login(client)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0 and "network" in r.json()["detail"]


def test_api_rate_limit_per_session(make_app, make_user):
    app = make_app()
    make_user("alex", "viewer")
    a, b = TestClient(app), TestClient(app)
    login(a)
    login(b)
    sid = _sha(a.cookies.get("pr_session"))
    limiter = app.state.pr_rate_limiter
    for _ in range(security.API_REQUESTS_PER_SESSION - 1):
        limiter.hit(f"api:{sid}", security.API_REQUESTS_PER_SESSION, security.API_WINDOW_S)
    assert a.get("/api/protected").status_code == 200             # 120th request
    r = a.get("/api/protected")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert b.get("/api/protected").status_code == 200             # other session unaffected
