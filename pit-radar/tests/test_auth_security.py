"""Security headers + CSP, rate limiter, log redaction and production secret checks (app/security.py)."""
from __future__ import annotations

import io
import logging

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app import security
from app.config import Settings, get_settings, reset_settings_cache

DEFAULT_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob: https://tiles.openfreemap.org https://orthos.its.ny.gov; "
    "connect-src 'self' https://tiles.openfreemap.org https://orthos.its.ny.gov; "
    "worker-src 'self' blob:; child-src 'self' blob:; font-src 'self' data:; object-src 'none'; "
    "manifest-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def _headers_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.add_middleware(security.SecurityHeadersMiddleware, settings=settings)

    @app.get("/api/thing")
    def api_thing():
        return {"ok": True}

    @app.get("/page")
    def page():
        return PlainTextResponse("hello", headers={"Cache-Control": "max-age=60"})

    @app.get("/custom-csp")
    def custom():
        return PlainTextResponse("x", headers={"Content-Security-Policy": "default-src 'none'"})

    return app


@pytest.fixture(autouse=True)
def _cleanup_logging():
    yield
    security.uninstall_log_redaction()


# --- headers and CSP -------------------------------------------------------------------

def test_default_csp_matches_spec():
    assert security.build_csp(get_settings()) == DEFAULT_CSP


def test_csp_includes_configured_tile_and_style_hosts(monkeypatch):
    monkeypatch.setenv("MAP_TILE_HOSTS", "tiles.example.org, https://cdn.example.net/path/x, javascript:alert(1), "
                                         "bad host;script-src *, http://plain.example.com")
    monkeypatch.setenv("MAP_STYLE_URL", "https://styles.example.com/dark.json")
    reset_settings_cache()
    csp = security.build_csp(get_settings())
    directives = dict(d.split(" ", 1) for d in csp.split("; "))
    assert directives["img-src"] == "'self' data: blob: https://tiles.example.org https://cdn.example.net"
    assert directives["connect-src"] == ("'self' https://tiles.example.org https://cdn.example.net "
                                         "https://styles.example.com")
    assert "javascript" not in csp and "plain.example.com" not in csp and "script-src *" not in csp
    assert directives["script-src"] == "'self'"


def test_csp_extra_sources():
    csp = security.build_csp(get_settings(), extra={"script-src": ["'wasm-unsafe-eval'", "bad; x"],
                                                    "media-src": ["'none'"]})
    assert "script-src 'self' 'wasm-unsafe-eval';" in csp
    assert csp.endswith("media-src 'none'") and "bad" not in csp


def test_security_headers_on_every_response():
    c = TestClient(_headers_app(get_settings()))
    for path in ("/api/thing", "/page", "/missing"):
        r = c.get(path)
        h = r.headers
        assert h["content-security-policy"] == DEFAULT_CSP
        assert h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "no-referrer"
        assert h["permissions-policy"] == "geolocation=(self), camera=(), microphone=()"
        assert h["x-robots-tag"] == "noindex, nofollow"
        assert h["x-frame-options"] == "DENY"
        assert h["cross-origin-opener-policy"] == "same-origin"
        assert "strict-transport-security" not in h           # COOKIE_SECURE=false in tests


def test_no_store_on_api_only():
    c = TestClient(_headers_app(get_settings()))
    assert c.get("/api/thing").headers["cache-control"] == "no-store"
    assert c.get("/api/missing").headers["cache-control"] == "no-store"
    assert c.get("/page").headers["cache-control"] == "max-age=60"


def test_route_specific_csp_is_kept():
    c = TestClient(_headers_app(get_settings()))
    assert c.get("/custom-csp").headers["content-security-policy"] == "default-src 'none'"


def test_hsts_only_when_cookie_secure(monkeypatch):
    monkeypatch.setenv("COOKIE_SECURE", "true")
    reset_settings_cache()
    c = TestClient(_headers_app(get_settings()), base_url="https://testserver")
    assert c.get("/page").headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_install_adds_middleware_and_limiter():
    app = FastAPI()
    security.install(app, get_settings())

    @app.get("/x")
    def x():
        return {"ok": True}

    r = TestClient(app).get("/x")
    assert r.headers["x-frame-options"] == "DENY"
    assert isinstance(app.state.pr_rate_limiter, security.RateLimiter)
    assert app.state.pr_rate_limiter is not security.rate_limiter


# --- rate limiter ---------------------------------------------------------------------

class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_rate_limiter_sliding_window():
    clk = FakeClock()
    rl = security.RateLimiter(clock=clk)
    for i in range(3):
        assert rl.hit("k", 3, 60) == (True, 0)
        clk.t += 10
    allowed, retry = rl.hit("k", 3, 60)          # hits at 1000, 1010, 1020; now 1030
    assert allowed is False and retry == 30
    clk.t = 1060.5                                # first hit slid out
    assert rl.hit("k", 3, 60) == (True, 0)
    assert rl.hit("k", 3, 60)[0] is False
    assert rl.hit("other", 3, 60) == (True, 0)    # independent keys


def test_rate_limiter_peek_and_reset():
    clk = FakeClock()
    rl = security.RateLimiter(clock=clk)
    for _ in range(5):
        assert rl.peek("k", 2, 60) == (True, 0)  # peeking never records
    rl.hit("k", 2, 60)
    rl.hit("k", 2, 60)
    assert rl.peek("k", 2, 60)[0] is False
    rl.reset("k")
    assert rl.peek("k", 2, 60) == (True, 0)
    rl.hit("a", 1, 60)
    rl.reset()
    assert len(rl) == 0


def test_rate_limiter_denied_hits_are_not_recorded():
    clk = FakeClock()
    rl = security.RateLimiter(clock=clk)
    rl.hit("k", 1, 10)
    for _ in range(50):
        clk.t += 0.1
        assert rl.hit("k", 1, 10)[0] is False
    clk.t = 1010.01
    assert rl.hit("k", 1, 10)[0] is True


def test_rate_limiter_sweeps_and_bounds_keys():
    clk = FakeClock()
    rl = security.RateLimiter(clock=clk, max_keys=100)
    for i in range(500):
        rl.hit(f"k{i}", 5, 1000)
        clk.t += 0.01
    assert len(rl) <= 100
    clk.t += 5000
    rl.hit("fresh", 5, 10)
    for _ in range(1000):
        rl.peek("fresh", 5, 10)
    assert len(rl) <= 2


def _principal_app(limiter: security.RateLimiter):
    """App with a fake principal so the export/API limit dependencies can be tested in isolation."""
    from types import SimpleNamespace

    from app.api.deps import current_principal

    app = FastAPI()
    users = {"1": SimpleNamespace(user=SimpleNamespace(id=1), session=SimpleNamespace(id="s1")),
             "2": SimpleNamespace(user=SimpleNamespace(id=2), session=SimpleNamespace(id="s2"))}

    def fake_principal(request: security.Request):
        return users[request.headers.get("x-user", "1")]

    app.dependency_overrides[current_principal] = fake_principal

    @app.get("/api/export")
    def export(p=Depends(security.rate_limit_export(limiter))):
        return {"user": p.user.id}

    @app.get("/api/limited")
    def limited(p=Depends(security.rate_limit_api(limiter, limit=3, window_s=60))):
        return {"ok": True}

    return app


def test_rate_limit_export_per_user():
    limiter = security.RateLimiter(clock=FakeClock())
    c = TestClient(_principal_app(limiter))
    for _ in range(security.EXPORTS_PER_USER):
        assert c.get("/api/export").status_code == 200
    r = c.get("/api/export")
    assert r.status_code == 429
    assert r.headers["retry-after"] == "3600"
    assert "20 per hour" in r.json()["detail"]
    assert c.get("/api/export", headers={"x-user": "2"}).status_code == 200


def test_rate_limit_api_custom_limit():
    limiter = security.RateLimiter(clock=FakeClock())
    c = TestClient(_principal_app(limiter))
    assert [c.get("/api/limited").status_code for _ in range(4)] == [200, 200, 200, 429]


# --- log redaction ----------------------------------------------------------------------

SECRETS = {
    "EXA_API_KEY": "exa-0000-test-not-a-real-key-1111",
    "ANTHROPIC_API_KEY": "sk-ant-api03-TESTONLYnotarealkey000000",
    "FIRECRAWL_API_KEY": "fc-testonly000000000000000000",
    "SOCIALCRAWL_API_KEY": "sc_testonly_0000000000000000",
}


@pytest.fixture
def secret_settings(monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    return get_settings()


def _assert_clean(text: str) -> None:
    for v in SECRETS.values():
        assert v not in text
    assert "test-session-secret-0123456789abcdef" not in text


def test_redaction_of_configured_secrets_and_headers(secret_settings, caplog):
    security.install_log_redaction(secret_settings)
    lg = logging.getLogger("app.pipeline.collectors.fake")
    with caplog.at_level(logging.INFO):
        lg.info("calling exa with key %s", SECRETS["EXA_API_KEY"])
        lg.info("headers: {'x-api-key': 'abc123secretvalue', 'Authorization': 'Bearer tok_ABCDEFGH12345678'}")
        lg.info("anthropic key=" + SECRETS["ANTHROPIC_API_KEY"])
        lg.warning("firecrawl %(k)s failed", {"k": SECRETS["FIRECRAWL_API_KEY"]})
        lg.info("session secret is %s", "test-session-secret-0123456789abcdef0123456789")
        lg.info("cookie pr_session=abcdefghijklmnop; other=1")
        try:
            raise ValueError("provider rejected " + SECRETS["SOCIALCRAWL_API_KEY"])
        except ValueError:
            lg.exception("socialcrawl call failed")
    text = caplog.text
    _assert_clean(text)
    assert "abc123secretvalue" not in text and "tok_ABCDEFGH12345678" not in text
    assert "abcdefghijklmnop" not in text
    assert text.count("[redacted]") >= 7
    assert "calling exa with key [redacted]" in text
    assert "socialcrawl call failed" in text and "ValueError" in text


def test_redaction_reaches_handlers_added_later(secret_settings):
    security.install_log_redaction(secret_settings)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
    lg = logging.getLogger("uvicorn.test_later")
    lg.addHandler(handler)
    lg.propagate = False
    lg.setLevel(logging.INFO)
    try:
        lg.info("key in url /v1/search?api_key=%s", SECRETS["EXA_API_KEY"])
        lg.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/api/x?token=abc", "1.1", 200)
    finally:
        lg.removeHandler(handler)
    out = stream.getvalue()
    _assert_clean(out)
    assert "/v1/search?api_key=[redacted]" in out
    assert '"GET /api/x?token=abc HTTP/1.1" 200' in out          # structure of access-log args preserved


def test_redact_text_helper(secret_settings):
    assert security.redact_text("x " + SECRETS["EXA_API_KEY"]) == "x [redacted]"
    assert security.redact_text('{"password": "hunter2hunter2"}') == '{"password": "[redacted]"}'
    assert security.redact_text("password_change done") == "password_change done"
    assert security.redact_text("") == ""


def test_short_secret_values_are_not_used_as_patterns(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "abc")
    reset_settings_cache()
    assert security.redact_text("abc def") == "abc def"


def test_uninstall_restores_factory(secret_settings):
    before = logging.getLogRecordFactory()
    security.install_log_redaction(secret_settings)
    security.install_log_redaction(secret_settings)      # idempotent
    assert logging.getLogRecordFactory() is not before
    security.uninstall_log_redaction()
    assert logging.getLogRecordFactory() is before
    assert security._FILTER not in logging.getLogger().filters


# --- production checks ------------------------------------------------------------------

def _prod(monkeypatch, secret: str) -> Settings:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", secret)
    reset_settings_cache()
    return get_settings()


@pytest.mark.parametrize("secret", ["", "short", "x" * 31, " " * 40])
def test_production_requires_strong_session_secret(monkeypatch, secret):
    s = _prod(monkeypatch, secret)
    with pytest.raises(RuntimeError) as exc:
        security.assert_production_secrets(s)
    assert "SESSION_SECRET" in str(exc.value)
    if secret.strip():
        assert secret not in str(exc.value)
    with pytest.raises(RuntimeError):
        security.install(FastAPI(), s)


def test_production_accepts_strong_secret(monkeypatch):
    security.assert_production_secrets(_prod(monkeypatch, "s" * 48))


def test_development_allows_missing_secret(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SESSION_SECRET", "")
    reset_settings_cache()
    security.assert_production_secrets(get_settings())


def test_redaction_when_secret_spans_format_and_args(secret_settings, caplog):
    security.install_log_redaction(secret_settings)
    lg = logging.getLogger("app.fake")
    with caplog.at_level(logging.INFO):
        lg.info("password: %s", "hunter2hunter2")
        lg.info("login ok for %s", "alex")                       # clean records keep msg/args untouched
    assert "hunter2hunter2" not in caplog.text
    assert "password: [redacted]" in caplog.text
    clean = [r for r in caplog.records if "login ok" in r.getMessage()][0]
    assert clean.msg == "login ok for %s" and clean.args == ("alex",)


def test_redaction_never_breaks_logging(secret_settings, caplog):
    security.install_log_redaction(secret_settings)
    lg = logging.getLogger("app.fake")

    class Boom:
        def __str__(self) -> str:
            raise RuntimeError("no str")

    broken = logging.LogRecord("app.fake", logging.INFO, __file__, 1, "mismatched %s %s",
                               (SECRETS["EXA_API_KEY"],), None)
    security.redact_record(broken)                               # must not raise on a bad format string
    assert SECRETS["EXA_API_KEY"] not in repr(broken.args)
    odd = logging.LogRecord("app.fake", logging.INFO, __file__, 1, "object %s", (Boom(),), None)
    security.redact_record(odd)                                  # unprintable argument: left as is
    assert isinstance(odd.args[0], Boom)
    with caplog.at_level(logging.INFO):
        lg.info(SECRETS["EXA_API_KEY"])                          # secret as the message itself
        lg.info({"api_key": SECRETS["EXA_API_KEY"]})             # non-string message
    _assert_clean(caplog.text)
