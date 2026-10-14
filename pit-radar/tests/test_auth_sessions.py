"""Sessions, CSRF derivation, lockout timing and audit rows (app/auth.py, app/audit.py)."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest

from app import auth
from app.audit import audit, ip_hash
from app.config import reset_settings_cache
from app.models import AuditLog, UserSession

SECRET = "test-session-secret-0123456789abcdef0123456789"


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


@pytest.fixture
def user(make_user):
    return make_user("alex", "reviewer")


# --- sessions --------------------------------------------------------------------

def test_create_session_stores_only_hashes(db, user, now):
    token, csrf = auth.create_session(db, user, now=now, ttl_hours=12)
    db.commit()
    rows = db.query(UserSession).all()
    assert len(rows) == 1
    s = rows[0]
    assert s.id == _sha(token) and s.id != token
    assert csrf == hmac.new(SECRET.encode(), token.encode(), hashlib.sha256).hexdigest()
    assert s.csrf_hash == _sha(csrf) and s.csrf_hash != csrf
    assert s.expires_at == now + dt.timedelta(hours=12)
    assert s.created_at == now and s.revoked is False
    assert len(token) >= 43
    assert auth.csrf_for_token(token) == csrf


def test_resolve_session_valid_and_fixed_expiry(db, user, now):
    token, _ = auth.create_session(db, user, now=now, ttl_hours=12)
    db.commit()
    got = auth.resolve_session(db, token, now=now + dt.timedelta(hours=11, minutes=59))
    assert got is not None and got[0].id == user.id and got[1].id == _sha(token)
    assert auth.resolve_session(db, token, now=now + dt.timedelta(hours=12)) is None


@pytest.mark.parametrize("token", [None, "", "not-a-real-token", "x" * 500])
def test_resolve_session_rejects_unknown(db, user, now, token):
    assert auth.resolve_session(db, token, now=now) is None


def test_resolve_session_rejects_revoked(db, user, now):
    token, _ = auth.create_session(db, user, now=now, ttl_hours=12)
    db.commit()
    sess = db.get(UserSession, _sha(token))
    auth.revoke_session(db, sess)
    db.commit()
    assert auth.resolve_session(db, token, now=now) is None


def test_resolve_session_rejects_inactive_user(db, user, now):
    token, _ = auth.create_session(db, user, now=now, ttl_hours=12)
    user.active = False
    db.commit()
    assert auth.resolve_session(db, token, now=now) is None


def test_rotating_session_secret_invalidates_sessions(db, user, now, monkeypatch):
    token, _ = auth.create_session(db, user, now=now, ttl_hours=12)
    db.commit()
    monkeypatch.setenv("SESSION_SECRET", "a-completely-different-secret-value-0123456789")
    reset_settings_cache()
    assert auth.resolve_session(db, token, now=now) is None


def test_check_csrf(db, user, now):
    token, csrf = auth.create_session(db, user, now=now, ttl_hours=12)
    db.commit()
    sess = db.get(UserSession, _sha(token))
    assert auth.check_csrf(sess, csrf) is True
    assert auth.check_csrf(sess, csrf.upper()) is False
    assert auth.check_csrf(sess, csrf[:-1] + ("0" if csrf[-1] != "0" else "1")) is False
    assert auth.check_csrf(sess, token) is False
    assert auth.check_csrf(sess, None) is False
    assert auth.check_csrf(sess, "") is False
    assert auth.check_csrf(sess, "a" * 1000) is False


def test_check_csrf_uses_compare_digest(db, user, now, monkeypatch):
    token, csrf = auth.create_session(db, user, now=now, ttl_hours=12)
    db.commit()
    sess = db.get(UserSession, _sha(token))
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(auth.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    assert auth.check_csrf(sess, csrf) is True
    assert calls


def test_revoke_user_sessions_keeps_one(db, user, now):
    t1, _ = auth.create_session(db, user, now=now, ttl_hours=12)
    t2, _ = auth.create_session(db, user, now=now, ttl_hours=12)
    t3, _ = auth.create_session(db, user, now=now, ttl_hours=12)
    db.commit()
    n = auth.revoke_user_sessions(db, user.id, except_session_id=_sha(t2))
    db.commit()
    assert n == 2
    assert auth.resolve_session(db, t1, now=now) is None
    assert auth.resolve_session(db, t2, now=now) is not None
    assert auth.resolve_session(db, t3, now=now) is None


def test_purge_sessions_removes_long_expired_only(db, user, now):
    old, _ = auth.create_session(db, user, now=now - dt.timedelta(days=3), ttl_hours=12)
    recent, _ = auth.create_session(db, user, now=now - dt.timedelta(hours=13), ttl_hours=12)
    live, _ = auth.create_session(db, user, now=now, ttl_hours=12)
    db.commit()
    assert auth.purge_sessions(db, now=now, user_id=user.id) == 1
    db.commit()
    ids = {s.id for s in db.query(UserSession).all()}
    assert ids == {_sha(recent), _sha(live)}


def test_find_user_by_name(db, make_user):
    u = make_user("Outreach.Lead", "viewer")
    assert auth.find_user_by_name(db, "Outreach.Lead").id == u.id
    assert auth.find_user_by_name(db, "outreach.lead").id == u.id
    assert auth.find_user_by_name(db, "  outreach.lead ").id == u.id
    assert auth.find_user_by_name(db, "nobody") is None
    assert auth.find_user_by_name(db, "") is None


def test_secret_fallback_outside_production(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "")
    reset_settings_cache()
    k1 = auth.secret_key()
    assert isinstance(k1, bytes) and len(k1) >= 32
    assert auth.secret_key() == k1


def test_secret_required_in_production(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "")
    monkeypatch.setenv("APP_ENV", "production")
    reset_settings_cache()
    with pytest.raises(RuntimeError):
        auth.secret_key()


# --- lockout timing (pure, injected now) ---------------------------------------------

def _state():
    return SimpleNamespace(failed_login_count=0, failed_login_window_start=None, locked_until=None)


def test_lockout_after_max_failures(now):
    st = _state()
    for i in range(4):
        assert auth.register_failed_login(st, now=now + dt.timedelta(minutes=i), max_failures=5, lockout_minutes=15) is False
    t5 = now + dt.timedelta(minutes=4)
    assert auth.register_failed_login(st, now=t5, max_failures=5, lockout_minutes=15) is True
    assert st.locked_until == t5 + dt.timedelta(minutes=15)
    assert st.failed_login_count == 0
    assert auth.locked_minutes_remaining(st, now=t5) == 15
    assert auth.locked_minutes_remaining(st, now=t5 + dt.timedelta(minutes=14, seconds=1)) == 1
    assert auth.locked_minutes_remaining(st, now=t5 + dt.timedelta(minutes=15)) == 0


def test_failures_outside_window_start_new_window(now):
    st = _state()
    for i in range(4):
        auth.register_failed_login(st, now=now, max_failures=5, lockout_minutes=15)
    later = now + dt.timedelta(minutes=15)
    assert auth.register_failed_login(st, now=later, max_failures=5, lockout_minutes=15) is False
    assert st.failed_login_count == 1 and st.failed_login_window_start == later


def test_success_resets_counters(db, make_user, now):
    u = make_user("sam", "viewer")
    for _ in range(3):
        auth.register_failed_login(u, now=now, max_failures=5, lockout_minutes=15)
    auth.register_successful_login(u, now=now)
    assert u.failed_login_count == 0 and u.failed_login_window_start is None and u.locked_until is None
    assert u.last_login_at == now


def test_expired_lock_is_cleared_on_next_failure(now):
    st = _state()
    st.locked_until = now - dt.timedelta(minutes=1)
    assert auth.register_failed_login(st, now=now, max_failures=5, lockout_minutes=15) is False
    assert st.locked_until is None and st.failed_login_count == 1


# --- audit ---------------------------------------------------------------------------

def test_ip_hash_daily_salt(now):
    assert ip_hash(None) is None and ip_hash("") is None
    h = ip_hash("203.0.113.7", now=now)
    salt = hmac.new(SECRET.encode(), now.date().isoformat().encode(), hashlib.sha256).hexdigest()
    assert h == hashlib.sha256(("203.0.113.7" + salt).encode()).hexdigest()
    assert "203.0.113.7" not in h and len(h) == 64
    assert ip_hash("203.0.113.7", now=now + dt.timedelta(hours=1)) == h
    assert ip_hash("203.0.113.7", now=now + dt.timedelta(days=1)) != h
    assert ip_hash("203.0.113.8", now=now) != h


def test_audit_row_and_sanitized_details(db, make_user, now, monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-test-key-not-real-1234567890")
    reset_settings_cache()
    u = make_user("alex", "admin")
    audit(db, "export", user_id=u.id, object_type="hotspots", object_id=42, ip="198.51.100.4", at=now, details={
        "filters": {"window": "7d", "county": "Nassau"},
        "password": "hunter2-hunter2",
        "csrf_token": "abc",
        "nested": {"api_key": "zzz", "note": "uses exa-test-key-not-real-1234567890 here"},
        "long": "y" * 2000,
        "when": now,
        "ids": {1, 2},
    })
    db.commit()
    row = db.query(AuditLog).one()
    assert row.action == "export" and row.user_id == u.id and row.object_type == "hotspots"
    assert row.object_id == "42" and row.at == now
    assert row.ip_hash == ip_hash("198.51.100.4", now=now)
    d = row.details
    assert d["filters"] == {"window": "7d", "county": "Nassau"}
    assert d["password"] == "[redacted]" and d["csrf_token"] == "[redacted]"
    assert d["nested"]["api_key"] == "[redacted]"
    assert "exa-test-key-not-real" not in json.dumps(d)
    assert len(d["long"]) <= 501
    assert d["when"] == now.isoformat() and sorted(d["ids"]) == [1, 2]
    dumped = json.dumps(d)
    assert "hunter2" not in dumped and "198.51.100.4" not in dumped


def test_audit_defaults(db):
    audit(db, "scan_trigger")
    db.commit()
    row = db.query(AuditLog).one()
    assert row.details == {} and row.ip_hash is None and row.user_id is None and row.at.tzinfo is not None
