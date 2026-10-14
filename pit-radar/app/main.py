"""FastAPI application factory (docs/02 §1, docs/07, docs/09 §2).

One process serves the API, the static front end and the in-process scheduler. Start it with scripts/start.sh
(runs migrations first). `create_app(start_background=False)` builds the same app without background tasks,
for tests and scripts.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text
from starlette.types import Scope

from app import security
from app.auth import COOKIE_NAME, resolve_session
from app.config import Settings, get_settings
from app.db import SessionLocal, get_engine, session_scope, utcnow
from app.paths import APP_ROOT, STATIC_DIR, data_dir
from app.runtime_settings import KEY_GEO_STATUS, KEY_LEXICON_OVERRIDES, KEY_SOURCES_OVERRIDES, get_value
from app.state import AppState

logger = logging.getLogger("app")
USER_AGENT = "PIT-Radar/1.0 (Long Island Coalition for the Homeless outreach)"
REQUIRED_TABLES = ("users", "sessions", "signals", "hotspots", "scan_runs", "settings")


def _configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root.setLevel(level)
    # httpx logs full request URLs at INFO (query strings can carry search terms); keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # anthropic SDK transport


def _guard_data_path() -> None:
    """Refuse to run on test fixtures (docs/10: fixtures are never loaded by the running app)."""
    fixtures = (APP_ROOT / "tests" / "fixtures").resolve()
    d = data_dir().resolve()
    if d == fixtures or fixtures in d.parents:
        raise RuntimeError("PIT_DATA_DIR points inside tests/fixtures; refusing to start")


def _tables_ok() -> bool:
    try:
        names = set(inspect(get_engine()).get_table_names())
    except Exception as exc:  # noqa: BLE001
        logger.error("database unreachable (%s)", type(exc).__name__)
        return False
    missing = [t for t in REQUIRED_TABLES if t not in names]
    if missing:
        logger.error("database is missing tables %s; run `alembic upgrade head` (scripts/start.sh does this)", missing)
        return False
    return True


def _load_overrides() -> tuple[Any, Any, Any]:
    try:
        with session_scope() as db:
            return (get_value(db, KEY_LEXICON_OVERRIDES), get_value(db, KEY_SOURCES_OVERRIDES),
                    get_value(db, KEY_GEO_STATUS))
    except Exception as exc:  # noqa: BLE001 - defaults still work
        logger.warning("could not read admin overrides (%s); using file defaults", type(exc).__name__)
        return None, None, None


def build_state(settings: Settings) -> AppState:
    from app.events import EventBus
    from app.pipeline.lexicon import Lexicon
    from app.pipeline.sources import SourcesConfig

    lex_over, src_over, geo_status = _load_overrides()
    lexicon = Lexicon.load(overrides=lex_over if isinstance(lex_over, dict) and lex_over else None)
    if getattr(lexicon, "override_errors", None):
        logger.warning("lexicon overrides skipped: %d invalid entr(ies)", len(lexicon.override_errors))
    sources = SourcesConfig.load(overrides=src_over if isinstance(src_over, dict) and src_over else None)
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        headers={"User-Agent": USER_AGENT},
        limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
    )
    client = None
    if settings.has_key("anthropic_api_key"):
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value(), max_retries=3,
                                          timeout=180.0)
    state = AppState(settings=settings, lexicon=lexicon, sources=sources, events=EventBus(), http=http,
                     anthropic=client)
    if isinstance(geo_status, dict):
        state.geo_status = {**geo_status, "state": "loading" if geo_status.get("state") == "ready" else
                            geo_status.get("state", "unknown")}
    return state


class AppStaticFiles(StaticFiles):
    """Vendored libraries are immutable per release (long cache); app files revalidate every load."""

    def file_response(self, full_path: Any, stat_result: os.stat_result, scope: Scope, status_code: int = 200) -> Response:
        resp = super().file_response(full_path, stat_result, scope, status_code)
        path = scope.get("path", "")
        if "/vendor/" in path:
            resp.headers["Cache-Control"] = "public, max-age=604800"
        else:
            resp.headers["Cache-Control"] = "no-cache"
        return resp


def create_app(*, start_background: bool = True) -> FastAPI:
    settings = get_settings()
    _configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from app.scheduler import Scheduler

        _guard_data_path()
        db_ok = _tables_ok()
        state = build_state(settings)
        app.state.pit = state
        scheduler = Scheduler(state)
        app.state.scheduler = scheduler
        if start_background and db_ok:
            await scheduler.start()
        elif start_background:
            logger.error("background tasks not started: database schema missing")
        logger.info("PIT Radar started (env=%s, providers: socialcrawl=%s exa=%s firecrawl=%s anthropic=%s)",
                    settings.app_env, settings.has_key("socialcrawl_api_key"), settings.has_key("exa_api_key"),
                    settings.has_key("firecrawl_api_key"), settings.has_key("anthropic_api_key"))
        try:
            yield
        finally:
            await scheduler.stop()
            await state.http.aclose()
            if state.anthropic is not None:
                try:
                    await state.anthropic.close()
                except Exception:  # noqa: BLE001
                    pass

    app = FastAPI(title="PIT Radar", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    security.install(app, settings)
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    from app.api import admin, auth_routes, data, export, stream

    app.include_router(auth_routes.router)
    app.include_router(data.router)
    app.include_router(admin.router)
    app.include_router(export.router)
    app.include_router(stream.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz(request: Request) -> JSONResponse:
        db_state = "ok"
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            db_state = "error"
        state = getattr(request.app.state, "pit", None)
        sched = state.scheduler if state is not None else None
        body = {
            "ok": db_state == "ok",
            "db": db_state,
            "scheduler": "running" if sched is not None and sched.running else "stopped",
            "last_scan_at": sched.last_scan_at.isoformat() if sched is not None and sched.last_scan_at else None,
        }
        return JSONResponse(body, status_code=200 if db_state == "ok" else 503,
                            headers={"Cache-Control": "no-store"})

    def _page(name: str) -> Response:
        path = STATIC_DIR / name
        if not path.is_file():
            return JSONResponse({"detail": f"{name} is missing from the build"}, status_code=503)
        return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-cache"})

    def _logged_in(request: Request) -> bool:
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return False
        db = SessionLocal()
        try:
            return resolve_session(db, token, now=utcnow()) is not None
        except Exception:  # noqa: BLE001
            return False
        finally:
            db.close()

    @app.get("/", include_in_schema=False)
    def index(request: Request) -> Response:
        if not _logged_in(request):
            return RedirectResponse("/login", status_code=302)
        return _page("index.html")

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request) -> Response:
        if _logged_in(request):
            return RedirectResponse("/", status_code=302)
        return _page("login.html")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> Response:
        path = STATIC_DIR / "sw.js"
        if not path.is_file():
            return Response(status_code=404)
        return FileResponse(path, media_type="text/javascript",
                            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> Response:
        path = STATIC_DIR / "manifest.webmanifest"
        if not path.is_file():
            return Response(status_code=404)
        return FileResponse(path, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        path = STATIC_DIR / "icons" / "favicon.png"
        if not path.is_file():
            return Response(status_code=404)
        return FileResponse(path, media_type="image/png")

    if STATIC_DIR.is_dir():
        app.mount("/static", AppStaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app()
