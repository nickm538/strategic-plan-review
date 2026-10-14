"""Create a PIT Radar user or reset a password (docs/09 §5). Prints nothing secret.

Usage (from the pit-radar folder, after `alembic upgrade head`):
    python scripts/create_user.py --name <name> --role admin            # prompts twice, no echo
    python scripts/create_user.py --name <name> --reset-password        # new password for an existing user
    printf '%s\\n' "$PW" | python scripts/create_user.py --name <name> --role viewer --password-stdin
"""
from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from app import auth  # noqa: E402
from app.audit import audit  # noqa: E402
from app.db import SessionLocal, get_engine, utcnow  # noqa: E402
from app.models import ROLES, User  # noqa: E402

REQUIRED_TABLES = ("users", "sessions", "audit_log")
# Letters (any script), digits, space and . _ @ ' - ; 1-80 characters, no leading/trailing space.
NAME_RE = re.compile(r"^(?! )[\w .@'\-]{1,80}(?<! )$")


class CliError(Exception):
    """A user-facing error: printed to stderr, exit code 1."""


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="create_user.py", description="Create a PIT Radar user or reset a password.")
    p.add_argument("--name", required=True, help="sign-in name (1-80 characters)")
    p.add_argument("--role", choices=ROLES, help="role for a new user: viewer, reviewer or admin")
    p.add_argument("--password-stdin", action="store_true", help="read the password from one line of stdin")
    p.add_argument("--reset-password", action="store_true", help="set a new password for an existing user")
    return p


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        line = sys.stdin.readline()
        password = line.rstrip("\r\n")
        if not password:
            raise CliError("No password received on stdin.")
        return password
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        raise CliError("Passwords do not match.")
    return first


def _check_tables() -> None:
    try:
        insp = inspect(get_engine())
        missing = [t for t in REQUIRED_TABLES if not insp.has_table(t)]
    except SQLAlchemyError as exc:
        raise CliError(f"Could not open the database ({type(exc).__name__}). Check DATABASE_URL.") from None
    if missing:
        raise CliError("Database tables are missing. Run `alembic upgrade head` first.")


def _run(args: argparse.Namespace) -> str:
    name = args.name.strip()
    if not NAME_RE.match(name):
        raise CliError("Name must be 1-80 characters: letters, digits, spaces and . _ @ ' - only.")

    _check_tables()
    db = SessionLocal()
    try:
        existing = auth.find_user_by_name(db, name)
        if existing is None:
            # find_user_by_name refuses ambiguous case-insensitive matches; treat any match as taken.
            existing = next((u for u in db.query(User).all() if u.name.casefold() == name.casefold()), None)

        if args.reset_password:
            if existing is None:
                raise CliError(f"No user named {name}.")
        elif existing is not None:
            raise CliError(f"A user named {existing.name} already exists. Use --reset-password to change the password.")

        password = _read_password(args.password_stdin)
        problem = auth.validate_new_password(password, name=name)
        if problem:
            raise CliError(problem)

        now = utcnow()
        if args.reset_password:
            assert existing is not None
            existing.password_hash = auth.hash_password(password)
            existing.password_changed_at = now
            existing.failed_login_count = 0
            existing.failed_login_window_start = None
            existing.locked_until = None
            revoked = auth.revoke_user_sessions(db, existing.id)
            audit(db, "password_change", user_id=existing.id, object_type="user", object_id=existing.id,
                  details={"via": "cli", "sessions_revoked": revoked}, at=now)
            db.commit()
            return f"Password reset for {existing.name}."

        user = User(name=name, role=args.role, password_hash=auth.hash_password(password), active=True,
                    created_at=now, password_changed_at=now, failed_login_count=0)
        db.add(user)
        db.flush()
        audit(db, "admin_change", object_type="user", object_id=user.id,
              details={"via": "cli", "op": "create_user", "role": args.role}, at=now)
        db.commit()
        return f"Created {args.role} {name}."
    except SQLAlchemyError as exc:
        db.rollback()
        raise CliError(f"Database error ({type(exc).__name__}).") from None
    finally:
        db.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.reset_password and not args.role:
        parser.error("--role is required when creating a user")
    try:
        message = _run(args)
    except CliError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Cancelled.", file=sys.stderr)
        return 130
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
