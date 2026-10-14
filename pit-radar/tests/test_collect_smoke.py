"""scripts/smoke_providers.py: field-name listing never prints values; exit codes (mocked HTTP only)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
import respx

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "smoke_providers.py"


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location("smoke_providers_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop(spec.name, None)


def test_field_names_lists_paths_not_values(smoke):
    obj = {"post": {"id": "1", "content": {"text": "secret post text"}, "author": {"username": "synthetic_user"}},
           "items": [{"a": 1}, {"b": {"c": 2}}]}
    names = smoke.field_names(obj)
    assert {"post.content.text", "post.author.username", "items[].a", "items[].b.c"} <= names
    joined = " ".join(names)
    assert "secret post text" not in joined and "synthetic_user" not in joined


def test_missing_keys_exit_1(smoke, monkeypatch, capsys):
    for k in ("SOCIALCRAWL_API_KEY", "EXA_API_KEY", "FIRECRAWL_API_KEY"):
        monkeypatch.setenv(k, "")
    from app.config import reset_settings_cache
    reset_settings_cache()
    assert smoke.main(["--provider", "all"]) == 1
    out = capsys.readouterr().out
    assert "SOCIALCRAWL_API_KEY is not set" in out and "EXA_API_KEY is not set" in out


def test_socialcrawl_auth_error_exit_2(smoke, monkeypatch, capsys):
    monkeypatch.setenv("SOCIALCRAWL_API_KEY", "sc-smoke-key-not-real")
    monkeypatch.setenv("SOCIALCRAWL_BASE_URL", "https://sc-smoke.test")
    from app.config import reset_settings_cache
    reset_settings_cache()
    with respx.mock(assert_all_called=False) as router:
        router.get("https://sc-smoke.test/v1/credits/balance").mock(return_value=httpx.Response(
            401, json={"success": False, "error": {"type": "INVALID_API_KEY", "message": "bad key"}}))
        code = smoke.main(["--provider", "socialcrawl", "--limit", "1"])
    out = capsys.readouterr().out
    assert code == 2 and "sc-smoke-key-not-real" not in out


def test_exa_smoke_prints_names_and_counts(smoke, monkeypatch, capsys):
    monkeypatch.setenv("EXA_API_KEY", "exa-smoke-key-not-real")
    monkeypatch.setenv("EXA_BASE_URL", "https://exa-smoke.test")
    from app.config import reset_settings_cache
    reset_settings_cache()
    body = {"results": [{"url": "https://www.newsday.com/x", "title": "Synthetic title", "publishedDate": "2026-10-13",
                         "text": "synthetic body text that must not be printed", "author": "Synthetic Author"}],
            "costDollars": {"total": 0.005}}
    with respx.mock() as router:
        router.post("https://exa-smoke.test/search").mock(return_value=httpx.Response(200, json=body))
        code = smoke.main(["--provider", "exa", "--limit", "2"])
    out = capsys.readouterr().out
    assert code == 0
    assert "publishedDate" in out and "with_publishedDate=1" in out
    assert "synthetic body text" not in out and "Synthetic Author" not in out and "exa-smoke-key" not in out
