"""In-process scheduler (docs/02 §1, §4; docs/09 §4).

Background asyncio tasks, one process, one replica:
  geography  load the built geography, or build it (network on Railway) with backoff; then sync places and
             build the classifier system prompt
  scans      first scan ~60 s after geography is ready, then every SCAN_INTERVAL_MIN (PIT interval in PIT mode),
             re-reading runtime settings every tick; skipped while paused, before geography, or with no keys
  maintenance hourly retention + hotspot decay refresh
  discovery  SocialCrawl endpoint discovery at startup (if missing or stale) and daily
  health     provider health every 15 minutes

Every loop catches and logs exceptions and keeps going. Log lines carry counts and reasons only.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any, Awaitable, Callable, Optional

from app import events as ev
from app.db import session_scope, utcnow
from app.paths import REPO_DATA_DIR, geo_dir
from app.runtime_settings import KEY_GEO_STATUS, KEY_SC_ENDPOINTS, effective, get_value, set_value
from app.state import AppState

logger = logging.getLogger(__name__)

FIRST_SCAN_DELAY_S = 60
TICK_S = 30                                     # how often the scan loop re-checks settings
MAINTENANCE_EVERY_S = 3600
HEALTH_EVERY_S = 15 * 60
DISCOVERY_EVERY = dt.timedelta(hours=24)
GEO_BACKOFF_S = (60, 5 * 60, 15 * 60, 60 * 60)  # then every hour
ALIASES_PATH = REPO_DATA_DIR / "aliases.yaml"


class Scheduler:
    """asyncio tasks: geography bootstrap (load or build), scan loop (interval from runtime settings: standard vs
    PIT mode; respects SCANS_PAUSED and provider keys), hourly retention + hotspot decay refresh, daily SocialCrawl
    discovery, provider health checks every 15 min. All loops survive exceptions and log them."""

    def __init__(self, state: AppState):
        self.state = state
        self._tasks: list[asyncio.Task[Any]] = []
        self._scan_task: Optional[asyncio.Task[Any]] = None
        self._geo_task: Optional[asyncio.Task[Any]] = None
        self._geo_ready_evt = asyncio.Event()
        self._wake = asyncio.Event()              # set to re-evaluate the scan schedule now
        self._stopping = False
        self._next_due: Optional[dt.datetime] = None
        self.skip_reason: str = ""

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._stopping = False
        self.state.scheduler.running = True
        self._geo_task = self._spawn("geography", self._geography_bootstrap)
        self._tasks += [
            self._spawn("scan-loop", self._scan_loop),
            self._spawn("maintenance", self._maintenance_loop),
            self._spawn("discovery", self._discovery_loop),
            self._spawn("health", self._health_loop),
        ]
        logger.info("scheduler started")

    async def stop(self) -> None:
        self._stopping = True
        self.state.scheduler.running = False
        tasks = [t for t in self._tasks + [self._geo_task, self._scan_task] if t is not None and not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("scheduler stopped")

    # ------------------------------------------------------------------ public actions
    async def trigger_scan(self, *, mode: str | None = None) -> bool:
        """Manual scan from the admin API. False if a scan is already running."""
        if self._scan_running():
            return False
        if mode not in (None, "standard", "pit"):
            raise ValueError("mode must be standard or pit")
        self._scan_task = asyncio.create_task(self._run_scan("manual", mode), name="pit-radar-manual-scan")
        return True

    async def rebuild_geography(self) -> bool:
        """Rebuild geography from the sources now. False if a build is already in progress."""
        if self.state.geo_status.get("state") == "building":
            return False
        if self._geo_task is not None and not self._geo_task.done():
            self._geo_task.cancel()
            await asyncio.gather(self._geo_task, return_exceptions=True)
        self._geo_task = self._spawn("geography-rebuild", lambda: self._geography_bootstrap(force_build=True))
        return True

    def status(self) -> dict[str, Any]:
        s = self.state.scheduler
        return {
            "running": s.running,
            "current_scan_id": s.current_scan_id,
            "last_scan_at": s.last_scan_at.isoformat() if s.last_scan_at else None,
            "last_scan_status": s.last_scan_status,
            "next_scan_at": s.next_scan_at.isoformat() if s.next_scan_at else None,
            "last_retention_at": s.last_retention_at.isoformat() if s.last_retention_at else None,
            "last_discovery_at": s.last_discovery_at.isoformat() if s.last_discovery_at else None,
            "skip_reason": self.skip_reason,
        }

    # ------------------------------------------------------------------ helpers
    def _spawn(self, name: str, fn: Callable[[], Awaitable[Any]]) -> asyncio.Task[Any]:
        return asyncio.create_task(self._guard(name, fn), name=f"pit-radar-{name}")

    async def _guard(self, name: str, fn: Callable[[], Awaitable[Any]]) -> None:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - background loops must never take the app down
            logger.exception("scheduler task %s crashed (%s)", name, type(exc).__name__)

    def _scan_running(self) -> bool:
        return self._scan_task is not None and not self._scan_task.done()

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake early when the schedule should be re-evaluated."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=max(0.0, seconds))
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    def _set_geo_status(self, state: str, message: str, **extra: Any) -> None:
        status = {"state": state, "message": message, "updated_at": utcnow().isoformat(), **extra}
        self.state.geo_status = status
        try:
            with session_scope() as db:
                set_value(db, KEY_GEO_STATUS, status)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scheduler: could not persist geography status (%s)", type(exc).__name__)
        try:
            self.state.events.publish(ev.GEO_STATUS, status, min_role="viewer")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ geography
    async def _geography_bootstrap(self, force_build: bool = False) -> None:
        from app.pipeline.geo.build import GeoBuildError, build_geography, geo_files_present, read_manifest

        out = geo_dir()
        attempt = 0
        while not self._stopping:
            try:
                if force_build or not geo_files_present(out):
                    self._set_geo_status("building", "Downloading Census and OpenStreetMap data and building the "
                                                     "Nassau/Suffolk gazetteer. This takes a few minutes.")
                    manifest = await build_geography(self.state.http, out, aliases_path=ALIASES_PATH,
                                                     contact_email=self.state.settings.nominatim_contact_email)
                    logger.info("geography built: %s", (manifest or {}).get("counts"))
                    force_build = False
                await self._load_geography(out, read_manifest(out) or {})
                return
            except asyncio.CancelledError:
                raise
            except GeoBuildError as exc:
                msg = f"Geography build failed: {exc}"
            except Exception as exc:  # noqa: BLE001
                msg = f"Geography unavailable ({type(exc).__name__})"
                logger.exception("geography bootstrap error")
            delay = GEO_BACKOFF_S[min(attempt, len(GEO_BACKOFF_S) - 1)]
            attempt += 1
            # Keep serving a previously loaded geography if a rebuild failed.
            state = "degraded" if self.state.geo_ready else "failed"
            self._set_geo_status(state, f"{msg}. Retrying in {delay // 60} min.")
            logger.warning("geography: %s; retry in %ds", msg, delay)
            await asyncio.sleep(delay)

    async def _load_geography(self, out: Any, manifest: dict[str, Any]) -> None:
        from app.pipeline import classifier
        from app.pipeline.geo.build import load_geography
        from app.pipeline.geo.gazetteer import sync_places

        gaz, gf = await asyncio.to_thread(load_geography, out, aliases_path=ALIASES_PATH)

        def _sync() -> int:
            with session_scope() as db:
                return sync_places(db, gaz)

        n = await asyncio.to_thread(_sync)
        try:
            blocks = await asyncio.to_thread(classifier.build_system_blocks, gaz)
        except Exception as exc:  # noqa: BLE001 - the runner rebuilds it on demand
            logger.warning("geography: classifier prompt not built (%s)", type(exc).__name__)
            blocks = None
        self.state.gazetteer, self.state.geofence, self.state.system_blocks = gaz, gf, blocks
        warnings = list(manifest.get("warnings") or [])
        state = "degraded" if warnings else "ready"
        msg = f"{n} places loaded." + (f" Warnings: {'; '.join(map(str, warnings))[:300]}" if warnings else "")
        self._set_geo_status(state, msg, counts=manifest.get("counts"), built_at=manifest.get("built_at"))
        self._geo_ready_evt.set()
        self._wake.set()
        logger.info("geography ready: %d places", n)

    # ------------------------------------------------------------------ scans
    def _keys_reason(self) -> str:
        from app.pipeline.runner import has_collector_key

        if not has_collector_key(self.state.settings):
            return "No data-provider API key is set (SOCIALCRAWL_API_KEY, EXA_API_KEY or FIRECRAWL_API_KEY)."
        return ""

    async def _scan_loop(self) -> None:
        sched = self.state.scheduler
        await self._geo_ready_evt.wait()
        self._next_due = utcnow() + dt.timedelta(seconds=FIRST_SCAN_DELAY_S)
        while not self._stopping:
            try:
                eff = await asyncio.to_thread(_effective_now)
                interval = int(eff.get("pit_scan_interval_min") if eff.pit_mode else eff.get("scan_interval_min"))
                now = utcnow()
                reason = ""
                if eff.scans_paused:
                    reason = "Scans are paused by an admin."
                elif not self.state.geo_ready:
                    reason = "Waiting for geography."
                else:
                    reason = self._keys_reason()
                self.skip_reason = reason
                sched.next_scan_at = None if reason else self._next_due
                if not reason and self._next_due is not None and now >= self._next_due and not self._scan_running():
                    trigger = "pit_mode" if eff.pit_mode else "schedule"
                    self._scan_task = asyncio.create_task(self._run_scan(trigger, None), name="pit-radar-scan")
                    self._next_due = now + dt.timedelta(minutes=interval)
                    sched.next_scan_at = self._next_due
                elif self._next_due is not None and self._next_due - now > dt.timedelta(minutes=interval):
                    # Interval shortened (e.g. PIT mode switched on): pull the next scan forward.
                    self._next_due = now + dt.timedelta(minutes=interval)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("scan loop tick failed")
            await self._sleep(TICK_S)

    async def _run_scan(self, trigger: str, mode: Optional[str]) -> None:
        from app.pipeline.runner import run_scan

        try:
            await run_scan(self.state, trigger=trigger, force_mode=mode)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - run_scan records its own failures; this is a last resort
            logger.exception("scan task failed")

    # ------------------------------------------------------------------ maintenance
    async def _maintenance_loop(self) -> None:
        await asyncio.sleep(5)
        while not self._stopping:
            try:
                counts = await asyncio.to_thread(_maintenance_once)
                self.state.scheduler.last_retention_at = utcnow()
                self.state.events.publish(ev.MAINTENANCE_FINISHED, counts, min_role="admin")
                logger.info("maintenance: %s", counts)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("maintenance failed")
            await asyncio.sleep(MAINTENANCE_EVERY_S)

    # ------------------------------------------------------------------ discovery
    async def _discovery_loop(self) -> None:
        from app.pipeline.runner import DiscoveryUnavailable, run_discovery

        await asyncio.sleep(10)
        while not self._stopping:
            try:
                if self.state.settings.has_key("socialcrawl_api_key"):
                    resolved = await asyncio.to_thread(_get_setting, KEY_SC_ENDPOINTS)
                    if _stale(resolved):
                        await run_discovery(self.state)
            except asyncio.CancelledError:
                raise
            except DiscoveryUnavailable:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("discovery failed")
            await asyncio.sleep(3600)  # re-check hourly; discovery itself only runs when the map is stale

    # ------------------------------------------------------------------ health
    async def _health_loop(self) -> None:
        await asyncio.sleep(15)
        while not self._stopping:
            try:
                await self.check_health()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("health check failed")
            await asyncio.sleep(HEALTH_EVERY_S)

    async def check_health(self) -> None:
        from app.pipeline.runner import KEY_ATTRS, make_collector, record_health
        from app.pipeline.types import ProviderHealth

        st = self.state
        out: list[ProviderHealth] = []
        for provider, attr in KEY_ATTRS.items():
            if not st.settings.has_key(attr):
                out.append(ProviderHealth(provider=provider, status="disabled", message="API key not set"))
                continue
            try:
                resolved = await asyncio.to_thread(_get_setting, KEY_SC_ENDPOINTS) if provider == "socialcrawl" else None
                col = make_collector(provider, state=st, resolved_endpoints=resolved or {})
                out.append(await asyncio.wait_for(col.health(), timeout=30))
            except Exception as exc:  # noqa: BLE001
                out.append(ProviderHealth(provider=provider, status="unknown",
                                          message=f"health check failed ({type(exc).__name__})"))
        out.append(await self._anthropic_health())
        nominatim_ok = bool(st.settings.nominatim_contact_email.strip())
        out.append(ProviderHealth(provider="nominatim", status="ok" if nominatim_ok else "disabled",
                                  message="" if nominatim_ok else
                                  "NOMINATIM_CONTACT_EMAIL not set; street-level geocoding is off"))
        await record_health(st, out)

    async def _anthropic_health(self):
        from app.pipeline.types import ProviderHealth

        st = self.state
        if st.anthropic is None:
            return ProviderHealth(provider="anthropic", status="disabled", message="ANTHROPIC_API_KEY not set")
        model = await asyncio.to_thread(lambda: str(_effective_now().get("anthropic_model")))
        try:
            import anthropic

            await asyncio.wait_for(st.anthropic.models.retrieve(model), timeout=30)
            return ProviderHealth(provider="anthropic", status="ok", message=f"model {model} available")
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            return ProviderHealth(provider="anthropic", status="unhealthy", message="API key rejected")
        except anthropic.NotFoundError:
            return ProviderHealth(provider="anthropic", status="unhealthy",
                                  message=f"model {model} not available to this key")
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(provider="anthropic", status="degraded",
                                  message=f"health check failed ({type(exc).__name__})")


# ---------------------------------------------------------------------- sync helpers (run in threads)
def _effective_now():
    with session_scope() as db:
        return effective(db)


def _get_setting(key: str) -> Any:
    with session_scope() as db:
        return get_value(db, key)


def _stale(resolved: Any) -> bool:
    if not isinstance(resolved, dict) or not resolved.get("capabilities"):
        return True
    at = resolved.get("resolved_at")
    try:
        when = dt.datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return utcnow() - when > DISCOVERY_EVERY


def _maintenance_once() -> dict[str, Any]:
    from app.config import get_settings
    from app.pipeline import hotspots, retention

    s = get_settings()
    now = utcnow()
    with session_scope() as db:
        counts = dict(retention.run_retention(db, now=now, settings=s))
    with session_scope() as db:
        counts["hotspots_refreshed"] = hotspots.recompute_all(db, now=now, half_life_days=s.half_life_days,
                                                              archive_days=s.hotspot_archive_days)
    return counts
