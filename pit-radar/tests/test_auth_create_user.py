"""scripts/create_user.py: create users, reset passwords, never echo the password."""
from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

from app import auth
from app.models import AuditLog, User, UserSession

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "create_user.py"
PW = "a long staff passphrase 42"


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("pit_create_user", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def prompts(cli, monkeypatch):
    """Queue answers for getpass; records the prompts shown."""
    answers: list[str] = []
    shown: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        shown.append(prompt)
        return answers.pop(0)

    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    return answers, shown


def _out(capsys) -> tuple[str, str]:
    c = capsys.readouterr()
    return c.out, c.err


def test_create_admin_with_prompts(cli, prompts, engine, db, capsys):
    answers, shown = prompts
    answers += [PW, PW]
    assert cli.main(["--name", "alex", "--role", "admin"]) == 0
    out, err = _out(capsys)
    assert out == "Created admin alex.\n" and err == ""
    assert PW not in out + err
    assert len(shown) == 2
    u = db.query(User).filter_by(name="alex").one()
    assert u.role == "admin" and u.active is True
    assert u.password_hash.startswith("$argon2id$") and auth.verify_password(u.password_hash, PW)
    assert u.password_changed_at is not None
    row = db.query(AuditLog).one()
    assert row.action == "admin_change" and row.object_type == "user" and row.object_id == str(u.id)
    assert row.details == {"via": "cli", "op": "create_user", "role": "admin"}


def test_mismatched_prompts(cli, prompts, engine, db, capsys):
    answers, _ = prompts
    answers += [PW, PW + "x"]
    assert cli.main(["--name", "alex", "--role", "viewer"]) == 1
    out, err = _out(capsys)
    assert "do not match" in err and out == ""
    assert PW not in out + err
    assert db.query(User).count() == 0


def test_weak_password_rejected(cli, prompts, engine, db, capsys):
    answers, _ = prompts
    answers += ["short", "short"]
    assert cli.main(["--name", "alex", "--role", "viewer"]) == 1
    out, err = _out(capsys)
    assert "at least 12" in err and "short" not in err.replace("at least", "")
    assert db.query(User).count() == 0


def test_password_equal_to_name_rejected(cli, monkeypatch, engine, db, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("Outreach Lead 7\n"))
    assert cli.main(["--name", "Outreach Lead 7", "--role", "viewer", "--password-stdin"]) == 1
    assert "user name" in _out(capsys)[1]


def test_password_stdin(cli, monkeypatch, engine, db, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(PW + "\nignored second line\n"))
    assert cli.main(["--name", "rae", "--role", "reviewer", "--password-stdin"]) == 0
    out, err = _out(capsys)
    assert out == "Created reviewer rae.\n" and PW not in out + err
    u = db.query(User).filter_by(name="rae").one()
    assert auth.verify_password(u.password_hash, PW)


def test_password_stdin_empty(cli, monkeypatch, engine, db, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(["--name", "rae", "--role", "reviewer", "--password-stdin"]) == 1
    assert "No password" in _out(capsys)[1]


def test_duplicate_name_case_insensitive(cli, prompts, engine, db, capsys):
    answers, _ = prompts
    answers += [PW, PW, PW, PW]
    assert cli.main(["--name", "Alex", "--role", "viewer"]) == 0
    _out(capsys)
    assert cli.main(["--name", "alex", "--role", "admin"]) == 1
    assert "already exists" in _out(capsys)[1]
    assert db.query(User).count() == 1
    assert len(answers) == 2          # no password prompt for a name that is taken


def test_reset_password(cli, prompts, engine, db, capsys, now):
    answers, _ = prompts
    answers += [PW, PW]
    assert cli.main(["--name", "alex", "--role", "admin"]) == 0
    _out(capsys)
    u = db.query(User).filter_by(name="alex").one()
    token, _ = auth.create_session(db, u, now=now, ttl_hours=12)
    u.failed_login_count = 3
    db.commit()

    new_pw = "replacement passphrase 99"
    answers += [new_pw, new_pw]
    assert cli.main(["--name", "ALEX", "--reset-password"]) == 0
    out, err = _out(capsys)
    assert out == "Password reset for alex.\n" and new_pw not in out + err
    db.expire_all()
    u = db.query(User).filter_by(name="alex").one()
    assert auth.verify_password(u.password_hash, new_pw) and not auth.verify_password(u.password_hash, PW)
    assert u.failed_login_count == 0 and u.locked_until is None
    sess = db.query(UserSession).one()
    assert sess.revoked is True
    row = db.query(AuditLog).filter_by(action="password_change").one()
    assert row.details == {"via": "cli", "sessions_revoked": 1}


def test_reset_unknown_user(cli, prompts, engine, db, capsys):
    assert cli.main(["--name", "ghost", "--reset-password"]) == 1
    assert "No user named ghost" in _out(capsys)[1]


def test_role_required_for_new_user(cli, engine):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--name", "alex"])
    assert exc.value.code == 2


@pytest.mark.parametrize("name", ["", "   ", "x" * 81, "bad\nname", "semi;colon", "<script>"])
def test_invalid_names(cli, prompts, engine, db, capsys, name):
    assert cli.main(["--name", name, "--role", "viewer"]) == 1
    assert "Name must be" in _out(capsys)[1]


def test_missing_tables_message(cli, prompts, tmp_path, capsys):
    from app.db import reset_engine
    eng = reset_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    try:
        assert cli.main(["--name", "alex", "--role", "admin"]) == 1
        out, err = _out(capsys)
        assert "alembic upgrade head" in err and out == ""
    finally:
        eng.dispose()


def test_end_to_end_after_alembic(tmp_path):
    """Real CLI process against a database created by `alembic upgrade head`."""
    import os
    import subprocess

    root = SCRIPT.parent.parent
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'e2e.db'}", "APP_ENV": "test",
           "PIT_DATA_DIR": str(tmp_path / "data")}
    mig = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, env=env,
                         capture_output=True, text=True, timeout=120)
    assert mig.returncode == 0, mig.stderr[-2000:]
    run = subprocess.run([sys.executable, "scripts/create_user.py", "--name", "first.admin", "--role", "admin",
                          "--password-stdin"], cwd=root, env=env, input=PW + "\n",
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr[-2000:]
    assert run.stdout == "Created admin first.admin.\n"
    assert PW not in run.stdout + run.stderr
