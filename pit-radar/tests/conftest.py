"""Shared test fixtures. Test fixtures are test-only inputs and are never loaded by the running app
(docs/10). Area-specific fixtures live in each test module or under tests/fixtures/<area>/."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

# Environment for every test, set before app modules read settings.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-0123456789abcdef0123456789")
os.environ.setdefault("COOKIE_SECURE", "false")
for _k in ("SOCIALCRAWL_API_KEY", "EXA_API_KEY", "FIRECRAWL_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(_k, None)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = dt.datetime(2026, 10, 14, 16, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Every test gets its own data dir and SQLite file; settings cache is reset."""
    monkeypatch.setenv("PIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from app.config import reset_settings_cache
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def now() -> dt.datetime:
    return NOW


@pytest.fixture
def engine(tmp_path):
    from app.db import Base, reset_engine
    import app.models  # noqa: F401
    eng = reset_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def make_user(db):
    """make_user(name, role, password) -> User (password hashed with the real argon2id helper)."""
    def _make(name: str = "alex", role: str = "admin", password: str = "correct horse battery staple"):
        from app.auth import hash_password
        from app.models import User
        try:
            pw_hash = hash_password(password)
        except NotImplementedError:
            pytest.skip("waiting for app.auth.hash_password")
        u = User(name=name, role=role, password_hash=pw_hash, active=True)
        db.add(u)
        db.commit()
        return u
    return _make
