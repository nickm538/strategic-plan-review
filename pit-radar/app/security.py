"""HTTP hardening (docs/09 §1-2): security headers and CSP, in-memory rate limits, log redaction and
production secret checks.

`install(app, settings)` wires everything at app creation (before the app starts):
- refuses to run in production without a strong SESSION_SECRET,
- redacts secrets from every log record,
- gives the app its own rate limiter,
- adds SecurityHeadersMiddleware.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

REDACTED = "[redacted]"

# Limits from docs/07 §1 and docs/09 §2.
LOGIN_FAILURES_PER_IP = 20
LOGIN_WINDOW_S = 15 * 60
API_REQUESTS_PER_SESSION = 120
API_WINDOW_S = 60
EXPORTS_PER_USER = 20
EXPORT_WINDOW_S = 60 * 60

HSTS_VALUE = "max-age=31536000; includeSubDomains"
PERMISSIONS_POLICY = "geolocation=(self), camera=(), microphone=()"

_HOST_RE = re.compile(r"^(\*\.)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*(:\d{1,5})?$")


# --- Content Security Policy and headers ---------------------------------------

def _https_origin(entry: str) -> Optional[str]:
    """'host', 'host:port' or 'https://host/path' -> 'https://host[:port]'. None for anything else
    (non-https schemes, paths only, characters that could break out of the CSP)."""
    e = (entry or "").strip()
    if not e:
        return None
    if "://" not in e:
        e = "https://" + e
    try:
        parts = urlsplit(e)
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not parts.netloc or "@" in parts.netloc:
        return None
    if not _HOST_RE.match(parts.netloc):
        return None
    return f"https://{parts.netloc.lower()}"


def tile_origins(settings: Settings) -> list[str]:
    """MAP_TILE_HOSTS as https origins, in order, de-duplicated; invalid entries are skipped."""
    out: list[str] = []
    for host in settings.tile_hosts:
        origin = _https_origin(host)
        if origin is None:
            log.warning("MAP_TILE_HOSTS entry skipped (not a valid https host)")
        elif origin not in out:
            out.append(origin)
    return out


def style_origin(settings: Settings) -> Optional[str]:
    """Origin of MAP_STYLE_URL when it is an absolute https URL (relative styles are same-origin)."""
    url = (settings.map_style_url or "").strip()
    if not url.lower().startswith("https://"):
        return None
    return _https_origin(url)


def build_csp(settings: Settings, *, extra: Optional[Mapping[str, Iterable[str]]] = None) -> str:
    """CSP from docs/09 §2 with the configured tile/style hosts. `extra` appends sources per directive."""
    tiles = tile_origins(settings)
    connect = ["'self'", *tiles]
    style = style_origin(settings)
    if style and style not in connect:
        connect.append(style)
    directives: list[tuple[str, list[str]]] = [
        ("default-src", ["'self'"]),
        ("script-src", ["'self'"]),
        ("style-src", ["'self'"]),
        ("img-src", ["'self'", "data:", "blob:", *tiles]),
        ("connect-src", connect),
        ("worker-src", ["'self'", "blob:"]),
        ("child-src", ["'self'", "blob:"]),
        ("font-src", ["'self'", "data:"]),
        ("object-src", ["'none'"]),
        ("manifest-src", ["'self'"]),
        ("frame-ancestors", ["'none'"]),
        ("base-uri", ["'none'"]),
        ("form-action", ["'self'"]),
    ]
    if extra:
        for name, sources in extra.items():
            target = next((srcs for d, srcs in directives if d == name), None)
            if target is None:
                target = []
                directives.append((name, target))
            for src in sources:
                if src and src not in target and not re.search(r"[;,\s]", src):
                    target.append(src)
    return "; ".join(f"{name} {' '.join(sources)}" for name, sources in directives)


def security_headers(settings: Settings, *, csp_extra: Optional[Mapping[str, Iterable[str]]] = None) -> list[tuple[str, str]]:
    headers = [
        ("Content-Security-Policy", build_csp(settings, extra=csp_extra)),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("Permissions-Policy", PERMISSIONS_POLICY),
        ("X-Robots-Tag", "noindex, nofollow"),
        ("X-Frame-Options", "DENY"),
        ("Cross-Origin-Opener-Policy", "same-origin"),
    ]
    if settings.cookie_secure:
        headers.append(("Strict-Transport-Security", HSTS_VALUE))
    return headers


class SecurityHeadersMiddleware:
    """Pure ASGI middleware. Adds the security headers to every HTTP response (a header a route already
    set, e.g. a route-specific CSP, is kept) and forces `Cache-Control: no-store` on /api/* responses."""

    def __init__(self, app: ASGIApp, settings: Optional[Settings] = None,
                 csp_extra: Optional[Mapping[str, Iterable[str]]] = None) -> None:
        self.app = app
        self._headers = security_headers(settings or get_settings(), csp_extra=csp_extra)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        is_api = path == "/api" or path.startswith("/api/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for name, value in self._headers:
                    if name not in headers:
                        headers.append(name, value)
                if is_api:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_headers)


# --- Rate limiting --------------------------------------------------------------

class RateLimiter:
    """In-memory sliding-window limiter (one process, one replica). Thread-safe: FastAPI runs sync
    dependencies in a threadpool. Denied hits are not recorded, so a client that waits gets through."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = 50_000) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._hits: dict[str, tuple[float, deque[float]]] = {}
        self._ops = 0

    def hit(self, key: str, limit: int, window_s: float) -> tuple[bool, int]:
        """Record one hit. Returns (allowed, retry_after_seconds); retry_after is 0 when allowed."""
        return self._check(key, limit, window_s, record=True)

    def peek(self, key: str, limit: int, window_s: float) -> tuple[bool, int]:
        """Like hit() without recording: would the next hit be allowed?"""
        return self._check(key, limit, window_s, record=False)

    def reset(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    def __len__(self) -> int:
        return len(self._hits)

    def _check(self, key: str, limit: int, window_s: float, *, record: bool) -> tuple[bool, int]:
        now = self._clock()
        with self._lock:
            self._maybe_sweep(now)
            entry = self._hits.get(key)
            dq: deque[float] = entry[1] if entry else deque()
            cutoff = now - window_s
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if limit <= 0 or len(dq) >= limit:
                oldest = dq[0] if dq else now
                retry = max(1, math.ceil(oldest + window_s - now))
                if dq:
                    self._hits[key] = (window_s, dq)
                return False, retry
            if record:
                dq.append(now)
                self._hits[key] = (window_s, dq)
            elif entry is not None and not dq:
                del self._hits[key]
            return True, 0

    def _maybe_sweep(self, now: float) -> None:
        self._ops += 1
        if self._ops % 1000 and len(self._hits) < self._max_keys:
            return
        for k, (window, dq) in list(self._hits.items()):
            while dq and dq[0] <= now - window:
                dq.popleft()
            if not dq:
                del self._hits[k]
        if len(self._hits) >= self._max_keys:   # still full: drop the least recently used tenth
            by_last = sorted(self._hits.items(), key=lambda kv: kv[1][1][-1])
            for k, _ in by_last[: max(1, len(by_last) // 10)]:
                del self._hits[k]


rate_limiter = RateLimiter()   # fallback when an app was not set up with install()


def limiter_for(request: Request) -> RateLimiter:
    """The app's limiter (set by install()), else the module-level one."""
    app = request.scope.get("app")
    state = getattr(app, "state", None)
    lim = getattr(state, "pr_rate_limiter", None) if state is not None else None
    return lim if isinstance(lim, RateLimiter) else rate_limiter


def client_ip(request: Request) -> Optional[str]:
    """Client address as seen by the app. Behind Railway's proxy this is the real client only when uvicorn
    runs with --proxy-headers --forwarded-allow-ips='*'."""
    client = request.client
    return client.host if client and client.host else None


def too_many(retry_after: int, detail: str) -> HTTPException:
    return HTTPException(status_code=429, detail=detail, headers={"Retry-After": str(max(1, int(retry_after)))})


def _minutes(seconds: int) -> int:
    return max(1, math.ceil(seconds / 60))


@dataclass
class LoginThrottle:
    """Returned by the rate_limit_login dependency; the login route calls record_failure() on a failed
    attempt."""
    limiter: RateLimiter
    key: str
    limit: int
    window_s: float

    def record_failure(self) -> None:
        self.limiter.hit(self.key, self.limit, self.window_s)


def rate_limit_login(limiter: Optional[RateLimiter] = None, *, limit: int = LOGIN_FAILURES_PER_IP,
                     window_s: float = LOGIN_WINDOW_S) -> Callable[..., LoginThrottle]:
    """Dependency factory for POST /api/auth/login: 429 (with Retry-After) once `limit` failed attempts
    from one IP fall inside the window (20 per 15 min). Only failures count (via
    LoginThrottle.record_failure), so many staff signing in from one office or venue network on PIT night
    are never blocked; per-name lockout (app.auth) is separate."""
    def dependency(request: Request) -> LoginThrottle:
        lim = limiter or limiter_for(request)
        key = f"login:{client_ip(request) or 'unknown'}"
        allowed, retry = lim.peek(key, limit, window_s)
        if not allowed:
            raise too_many(retry, f"Too many failed sign-in attempts from this network. "
                                  f"Try again in {_minutes(retry)} minutes.")
        return LoginThrottle(lim, key, limit, window_s)
    return dependency


def enforce_api_limit(request: Request, session_id: str, *, limiter: Optional[RateLimiter] = None,
                      limit: int = API_REQUESTS_PER_SESSION, window_s: float = API_WINDOW_S) -> None:
    """Count this request against the session's API budget (120/min); 429 when exhausted. Counted at most
    once per request and limiter, so current_principal and rate_limit_api() can both run it."""
    lim = limiter or limiter_for(request)
    key = f"api:{session_id}"
    counted = getattr(request.state, "pr_rate_counted", None)
    if counted is None:
        counted = set()
        request.state.pr_rate_counted = counted
    marker = (id(lim), key)
    if marker in counted:
        return
    counted.add(marker)
    allowed, retry = lim.hit(key, limit, window_s)
    if not allowed:
        raise too_many(retry, "Too many requests. Please wait a moment and try again.")


def rate_limit_api(limiter: Optional[RateLimiter] = None, *, limit: int = API_REQUESTS_PER_SESSION,
                   window_s: float = API_WINDOW_S) -> Callable[..., Any]:
    """Dependency factory: authenticated principal, limited to `limit` requests per window per session.
    current_principal already applies the default limit; use this for a custom limiter or limit."""
    from app.api.deps import current_principal  # deps imports this module

    def dependency(request: Request, principal=Depends(current_principal)):  # noqa: ANN001,ANN202
        enforce_api_limit(request, principal.session.id, limiter=limiter, limit=limit, window_s=window_s)
        return principal
    return dependency


def rate_limit_export(limiter: Optional[RateLimiter] = None, *, limit: int = EXPORTS_PER_USER,
                      window_s: float = EXPORT_WINDOW_S) -> Callable[..., Any]:
    """Dependency factory for export routes: 20 exports per hour per user, else 429 with Retry-After.
    Returns the principal."""
    from app.api.deps import current_principal  # deps imports this module

    def dependency(request: Request, principal=Depends(current_principal)):  # noqa: ANN001,ANN202
        lim = limiter or limiter_for(request)
        allowed, retry = lim.hit(f"export:{principal.user.id}", limit, window_s)
        if not allowed:
            raise too_many(retry, f"Export limit reached ({limit} per hour). Try again in {_minutes(retry)} minutes.")
        return principal
    return dependency


# --- Log redaction ---------------------------------------------------------------

_SECRET_FIELDS = ("socialcrawl_api_key", "exa_api_key", "firecrawl_api_key", "anthropic_api_key", "session_secret",
                  "bootstrap_admin_password")
_MIN_SECRET_LEN = 8   # shorter values would garble ordinary log text; real keys are far longer

_CREDENTIAL_NAMES = (r"x-api-key|x-csrf-token|api[_-]?key|apikey|authorization|access[_-]?token|refresh[_-]?token|"
                     r"csrf[_-]?token|session[_-]?secret|pr_session|password|passwd|secret")


def _kv_rule(skip_placeholders: bool) -> re.Pattern[str]:
    """key=value / key: value / "key": "value" for credential names. With skip_placeholders, values that
    start with '%' are left alone so a logging format string keeps its %s/%(name)s placeholders."""
    guard = r"|%" if skip_placeholders else ""
    return re.compile(
        r"""(?ix)\b(""" + _CREDENTIAL_NAMES + r""")
        (["']?\s*[:=]\s*["']?)
        (?!\[redacted\]|bearer\b|basic\b""" + guard + r""")
        [^\s"',;&}\]]+""")


_BEARER_RULE = (re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9\-._~+/]{8,}=*"), r"\1\2" + REDACTED)
_ANTHROPIC_RULE = (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"), REDACTED)
_RULES = [_BEARER_RULE, (_kv_rule(False), r"\1\2" + REDACTED), _ANTHROPIC_RULE]
_FORMAT_RULES = [_BEARER_RULE, (_kv_rule(True), r"\1\2" + REDACTED), _ANTHROPIC_RULE]


class SecretRedactor:
    """Replaces configured secret values and credential-looking header/field values with [redacted]."""

    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        values = sorted({v for v in secret_values if v and len(v) >= _MIN_SECRET_LEN}, key=len, reverse=True)
        self._secret_re = re.compile("|".join(re.escape(v) for v in values)) if values else None

    def redact(self, text: str, *, format_string: bool = False) -> str:
        """Redacted copy of `text`. format_string=True keeps %-placeholders intact (logging msg with args)."""
        if not text:
            return text
        if self._secret_re is not None:
            text = self._secret_re.sub(REDACTED, text)
        for rx, repl in (_FORMAT_RULES if format_string else _RULES):
            text = rx.sub(repl, text)
        return text


def _settings_secrets(settings: Settings) -> list[str]:
    out = []
    for name in _SECRET_FIELDS:
        val = getattr(settings, name, None)
        if val is not None:
            raw = val.get_secret_value()
            if raw:
                out.append(raw)
                if raw.strip() != raw:
                    out.append(raw.strip())
    return out


_installed_redactor: Optional[SecretRedactor] = None
_settings_redactor: tuple[Optional[Settings], Optional[SecretRedactor]] = (None, None)
_factory_lock = threading.Lock()
_original_factory: Optional[Callable[..., logging.LogRecord]] = None
_exc_formatter = logging.Formatter()


def _current_redactor() -> SecretRedactor:
    global _settings_redactor
    if _installed_redactor is not None:
        return _installed_redactor
    settings = get_settings()
    cached_settings, cached = _settings_redactor
    if cached is None or cached_settings is not settings:
        cached = SecretRedactor(_settings_secrets(settings))
        _settings_redactor = (settings, cached)
    return cached


def redact_text(text: str) -> str:
    """Redact secrets from a string (configured secret values, bearer tokens, key=value credentials)."""
    if not isinstance(text, str) or not text:
        return text
    return _current_redactor().redact(text)


def _redact_arg(arg: Any, redactor: SecretRedactor) -> Any:
    if isinstance(arg, str):
        return redactor.redact(arg)
    if arg is None or isinstance(arg, (bool, int, float)):
        return arg
    try:
        text = str(arg)
    except Exception:  # noqa: BLE001
        return arg
    red = redactor.redact(text)
    return red if red != text else arg


def _format(msg: Any, args: Any) -> str:
    """What LogRecord.getMessage() would produce for msg/args."""
    text = str(msg)
    return text % args if args else text


def _redact_message(record: logging.LogRecord, r: SecretRedactor) -> None:
    try:
        formatted: Optional[str] = record.getMessage()
    except Exception:  # noqa: BLE001 - a broken format string; logging reports it itself
        formatted = None
    if formatted is not None and r.redact(formatted) == formatted:
        return                                         # nothing sensitive: leave msg/args untouched
    if not record.args:
        record.msg = r.redact(record.msg) if isinstance(record.msg, str) else _redact_arg(record.msg, r)
        return
    # Redact the format string and each argument separately, so formatters that rely on the args
    # structure (e.g. uvicorn's access log) keep working.
    msg = r.redact(record.msg, format_string=True) if isinstance(record.msg, str) else _redact_arg(record.msg, r)
    if isinstance(record.args, tuple):
        args: Any = tuple(_redact_arg(a, r) for a in record.args)
    elif isinstance(record.args, Mapping):
        args = {k: _redact_arg(v, r) for k, v in record.args.items()}
    else:
        args = _redact_arg(record.args, r)
    if formatted is None:
        record.msg, record.args = msg, args
        return
    try:
        candidate = _format(msg, args)
        clean = r.redact(candidate) == candidate
    except Exception:  # noqa: BLE001
        clean = False
    if clean:
        record.msg, record.args = msg, args
    else:                                              # secret spans msg and args: keep the redacted text
        record.msg, record.args = r.redact(formatted), None


def redact_record(record: logging.LogRecord) -> None:
    """Redact a log record in place (message, args, exception and stack text). Idempotent."""
    if getattr(record, "_pr_redacted", False):
        return
    try:
        r = _current_redactor()
        _redact_message(record, r)
        if record.exc_info and not record.exc_text:
            record.exc_text = r.redact(_exc_formatter.formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = r.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = r.redact(record.stack_info)
    except Exception:  # noqa: BLE001 - logging must never fail because of redaction
        pass
    record._pr_redacted = True  # type: ignore[attr-defined]


class RedactingFilter(logging.Filter):
    """logging.Filter that redacts secrets in place and never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        redact_record(record)
        return True


_FILTER = RedactingFilter()


def _attach(obj: logging.Filterer) -> None:
    if _FILTER not in obj.filters:
        obj.addFilter(_FILTER)


def _all_handlers() -> list[logging.Handler]:
    handlers = list(logging.getLogger().handlers)
    for lg in list(logging.root.manager.loggerDict.values()):
        if isinstance(lg, logging.Logger):
            handlers.extend(lg.handlers)
    return handlers


def install_log_redaction(settings: Optional[Settings] = None) -> RedactingFilter:
    """Redact secrets from all logging. A filter on the root logger alone only sees records logged to
    the root logger itself (propagated records skip logger filters), so the filter is also attached to
    every existing handler (root, uvicorn, ...) and a log-record factory redacts records as they are
    created, which covers loggers and handlers configured later. Safe to call more than once."""
    global _installed_redactor, _original_factory
    s = settings or get_settings()
    _installed_redactor = SecretRedactor(_settings_secrets(s))
    _attach(logging.getLogger())
    for handler in _all_handlers():
        _attach(handler)
    with _factory_lock:
        if _original_factory is None:
            original = logging.getLogRecordFactory()
            _original_factory = original

            def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
                record = original(*args, **kwargs)
                redact_record(record)
                return record

            logging.setLogRecordFactory(factory)
    return _FILTER


def uninstall_log_redaction() -> None:
    """Undo install_log_redaction (tests)."""
    global _installed_redactor, _original_factory
    _installed_redactor = None
    logging.getLogger().removeFilter(_FILTER)
    for handler in _all_handlers():
        handler.removeFilter(_FILTER)
    with _factory_lock:
        if _original_factory is not None:
            logging.setLogRecordFactory(_original_factory)
            _original_factory = None


# --- Startup checks and wiring -------------------------------------------------------

MIN_SESSION_SECRET_LEN = 32


def assert_production_secrets(settings: Optional[Settings] = None) -> None:
    """Fail closed: in production, refuse to start without a SESSION_SECRET of 32+ characters."""
    s = settings or get_settings()
    if not s.is_production:
        return
    if len(s.session_secret.get_secret_value().strip()) < MIN_SESSION_SECRET_LEN:
        raise RuntimeError(
            "SESSION_SECRET must be set to at least 32 random characters in production. Generate one with: "
            "python -c \"import secrets; print(secrets.token_urlsafe(48))\" and set it in the service variables."
        )
    if not s.cookie_secure:
        log.warning("COOKIE_SECURE is false in production; session cookies may be sent over plain HTTP")


def install(app: Any, settings: Optional[Settings] = None, *,
            csp_extra: Optional[Mapping[str, Iterable[str]]] = None) -> None:
    """Wire security into a FastAPI app. Call once at app creation, before the app starts."""
    s = settings or get_settings()
    assert_production_secrets(s)
    install_log_redaction(s)
    app.state.pr_rate_limiter = RateLimiter()
    app.add_middleware(SecurityHeadersMiddleware, settings=s, csp_extra=csp_extra)
