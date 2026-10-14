"""Password hashing and password policy (app/auth.py)."""
from __future__ import annotations

import pytest
from argon2 import PasswordHasher

from app import auth


def test_hash_is_argon2id_and_verifies():
    h = auth.hash_password("correct horse battery staple")
    assert h.startswith("$argon2id$")
    assert "correct horse" not in h
    assert auth.verify_password(h, "correct horse battery staple") is True
    assert auth.verify_password(h, "correct horse battery stapl") is False
    assert auth.verify_password(h, "") is False


def test_hashes_are_salted():
    assert auth.hash_password("same password 123") != auth.hash_password("same password 123")


@pytest.mark.parametrize("bad_hash", ["", "garbage", "$argon2id$v=19$m=65536,t=3,p=4$broken", "$2b$12$notargon"])
def test_verify_returns_false_for_invalid_hash(bad_hash):
    assert auth.verify_password(bad_hash, "anything at all") is False


def test_verify_rejects_non_string_password():
    h = auth.hash_password("correct horse battery staple")
    assert auth.verify_password(h, None) is False  # type: ignore[arg-type]


def test_needs_rehash():
    assert auth.needs_rehash(auth.hash_password("a fresh password 1")) is False
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash("old weak params 1")
    assert auth.needs_rehash(weak) is True
    assert auth.verify_password(weak, "old weak params 1") is True   # still verifies
    assert auth.needs_rehash("not a hash") is False


def test_dummy_verify_runs():
    auth.dummy_verify("whatever")
    auth.dummy_verify("")


@pytest.mark.parametrize("pw", [
    "correct horse battery staple",
    "Tr0ub4dor&3xyz",
    "ALL CAPS WITH SPACES",
    "1234 5678 9012 3456",
])
def test_validate_accepts_reasonable_passwords(pw):
    assert auth.validate_new_password(pw, name="alex") is None


@pytest.mark.parametrize("pw,fragment", [
    ("short1!", "at least 12"),
    ("", "at least 12"),
    ("abcdefghijklmnop", "two kinds"),
    ("ABCDEFGHIJKLMNOP", "two kinds"),
    ("1234567890123", "two kinds"),
    ("aaaaaaaaaaaaaaaa", "repetitive"),
    ("abababab12abab", "repetitive"),
    ("x" * 300 + "1", "at most"),
])
def test_validate_rejects(pw, fragment):
    msg = auth.validate_new_password(pw)
    assert msg is not None and fragment in msg


def test_validate_rejects_user_name():
    assert "user name" in auth.validate_new_password("Outreach Lead 1", name="outreach lead 1")
    assert auth.validate_new_password("Outreach Lead 1", name="someone else") is None


def test_validate_works_without_name():
    assert auth.validate_new_password("correct horse battery staple") is None
