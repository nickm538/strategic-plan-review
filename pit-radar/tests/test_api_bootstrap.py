"""First-admin bootstrap from environment variables (only while no users exist)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import reset_settings_cache
from app.models import AuditLog, User

PW = "a long first admin passphrase"


def _app(monkeypatch, name, password):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_NAME", name)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", password)
    reset_settings_cache()
    from app.main import create_app
    return create_app(start_background=False)


def test_creates_first_admin_and_can_sign_in(engine, db, monkeypatch):
    with TestClient(_app(monkeypatch, "Nick", PW)) as c:
        r = c.post("/api/auth/login", json={"name": "Nick", "password": PW})
        assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    assert db.query(User).count() == 1
    assert db.query(AuditLog).filter(AuditLog.action == "admin_change").count() == 1


def test_never_runs_when_users_exist(engine, db, monkeypatch, make_user):
    make_user("existing", "admin", "existing user passphrase")
    with TestClient(_app(monkeypatch, "Intruder", PW)):
        pass
    assert db.query(User).filter(User.name == "Intruder").count() == 0


def test_weak_password_is_rejected(engine, db, monkeypatch):
    with TestClient(_app(monkeypatch, "Nick", "short")):
        pass
    assert db.query(User).count() == 0
