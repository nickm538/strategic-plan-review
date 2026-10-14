"""Scan orchestration (docs/02 §4-5).

One scan, in order:
  scan_runs row (running) -> scan_lock (45 min TTL) -> effective settings + budgets -> provider selection
  (key set; an `unhealthy` provider is skipped for the rest of the local day unless the scan is manual) ->
  SocialCrawl endpoint map (discovery when missing) -> plan -> collect (providers concurrently; one failing
  provider never fails the scan) -> persist query logs / usage / health / deferred queries / rotation cursor ->
  dedupe -> seen filter -> date filter -> prefilter -> Reddit comment pass (same stages) -> pending_items
  -> classify pending (newest first, then highest prefilter score; MAX_CLASSIFICATIONS_PER_SCAN, LLM_CONCURRENCY,
  daily LLM dollar cap) -> verify -> geocode -> decide -> privacy.build_signal_values -> signals row ->
  hotspot assignment -> SSE -> release lock -> scan_runs row (status, stage_counts, drop_reasons, errors, usage).

Every item that leaves the pipeline is recorded in items_seen with the reason (drop reason or outcome), so it is
not collected and judged again. Items that pass the prefilter are recorded as `queued` and wait in pending_items
(<= PENDING_ITEM_TTL_HOURS) until classified.

Logging: counts, ids, item-hash prefixes and reasons only. Post text never reaches a log line.
All database work runs in short session_scope() blocks through asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import inspect
import logging
import math
import os
import socket
import time
import traceback
import uuid
import weakref
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional, TypeVar

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import events as ev
from app.db import session_scope, utcnow
from app.models import MAPPED_STATUSES, GeocodeCache, Hotspot, PendingItem, Place, QueryLog, ScanLock, ScanRun, Signal
from app.pipeline import classifier, datefilter, dedupe, planner, privacy, verifier
from app.pipeline import decision as decision_mod
from app.pipeline import hotspots as hotspots_mod
from app.pipeline import prefilter as prefilter_mod
from app.pipeline import usage as usage_mod
from app.pipeline.collectors.base import Budget, Collector
from app.pipeline.geo import geocode as geocode_mod
from app.pipeline.types import (
    ClassifierResult,
    CollectResult,
    Decision,
    GeoResult,
    LLMUsage,
    NormalizedItem,
    PlannedQuery,
    ProviderHealth,
    VerificationResult,
)
from app.runtime_settings import (
    KEY_HEALTH_PREFIX,
    KEY_ROTATION_CURSOR,
    KEY_SC_ENDPOINTS,
    Effective,
    effective,
    get_value,
    local_today,
    set_value,
)
from app.state import AppState

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
LOCK_TTL = dt.timedelta(minutes=45)
KEY_DEFERRED = "planner.deferred"                 # settings key: queries deferred for budget, retried next scan
COLLECTOR_PROVIDERS = ("socialcrawl", "exa", "firecrawl")
KEY_ATTRS = {"socialcrawl": "socialcrawl_api_key", "exa": "exa_api_key", "firecrawl": "firecrawl_api_key"}
KEY_ENV = {"socialcrawl": "SOCIALCRAWL_API_KEY", "exa": "EXA_API_KEY", "firecrawl": "FIRECRAWL_API_KEY"}

STAGE_KEYS = ("planned", "collected", "unique", "new", "in_window", "prefiltered", "queued", "classified",
              "relevant", "verified", "in_area", "stored", "mapped", "needs_review", "self_reports", "unlocated")

MAX_CLASSIFY_ATTEMPTS = 5            # retry_later / error this many times -> stored for staff with no verdict
LLM_CALL_ESTIMATE_USD = 0.05         # reserved before each Claude call against the daily dollar cap
BUDGET_WARN_SHARE = 0.8              # budget.warning at 80% of a daily cap
COLLECT_TIMEOUT_S = 15 * 60          # hard ceiling for one provider's collection in one scan
DISCOVERY_TIMEOUT_S = 5 * 60
CLASSIFY_DEADLINE_S = 35 * 60        # stop starting classifications after this (lock TTL is 45 min)
MAX_COMMENT_THREADS = 10
MAX_DEFERRED = 300
MAX_ERRORS = 100
MAX_ERROR_CHARS = 300
RETRY_STREAK_STOP = 3                # consecutive retry_later results -> stop classifying this scan
GEOCODE_CACHE_DAYS = 180
HEALTH_KEEP_FOR = dt.timedelta(hours=24)
IN_AREA_COUNTIES = ("Nassau", "Suffolk")

# items_seen.decision for stored signals, by status.
STATUS_DECISION = {
    "ai_verified": "stored",
    "confirmed": "stored",
    "needs_human_review": "needs_review",
    "self_report": "self_report",
    "unlocated": "unlocated",
}
STATUS_STAGE = {
    "ai_verified": "mapped",
    "confirmed": "mapped",
    "needs_human_review": "needs_review",
    "self_report": "self_reports",
    "unlocated": "unlocated",
}


class DiscoveryUnavailable(RuntimeError):
    """SocialCrawl endpoint discovery cannot run (no API key). The message is safe to show to an admin."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _in_session(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    with session_scope() as db:
        return fn(db, *args, **kwargs)


async def _db(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    return await asyncio.to_thread(_in_session, fn, *args, **kwargs)


def _optional(fn: Callable[..., Any], **extras: Any) -> dict[str, Any]:
    """The subset of optional keyword arguments `fn` accepts (extensions beyond a module's stub contract)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(extras)
    return {k: v for k, v in extras.items() if k in params}


def _aware(d: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if d is None:
        return None
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)


def _parse_dt(v: Any) -> Optional[dt.datetime]:
    if isinstance(v, dt.datetime):
        return _aware(v)
    if not isinstance(v, str) or not v:
        return None
    try:
        return _aware(dt.datetime.fromisoformat(v.replace("Z", "+00:00")))
    except ValueError:
        return None


def _num(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _drop_key(reason: Optional[str]) -> str:
    """'exclusion:animal' -> 'exclusion', 'not_relevant:animal' -> 'not_relevant' (docs/03 drop_reasons keys)."""
    r = (reason or "unknown").strip()
    return (r.split(":", 1)[0] or "unknown")[:64]


def _exc_frames(exc: BaseException) -> str:
    """Traceback frames without the exception message (messages can echo data)."""
    return "".join(traceback.format_tb(exc.__traceback__))[-4000:]


def _has_capabilities(resolved: Any) -> bool:
    caps = resolved.get("capabilities") if isinstance(resolved, dict) else None
    return isinstance(caps, dict) and any(isinstance(v, dict) and v for v in caps.values())


def has_collector_key(settings: Any) -> bool:
    """True when at least one collector API key is set."""
    return any(settings.has_key(attr) for attr in KEY_ATTRS.values())


_DISCOVERY_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _discovery_lock() -> asyncio.Lock:
    """One SocialCrawl discovery at a time per event loop (scan-time and scheduled discovery share it)."""
    loop = asyncio.get_running_loop()
    lock = _DISCOVERY_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _DISCOVERY_LOCKS[loop] = lock
    return lock


def _query_to_dict(q: PlannedQuery) -> dict[str, Any]:
    return dataclasses.asdict(q)


_QUERY_FIELDS = frozenset(f.name for f in dataclasses.fields(PlannedQuery))


def _query_from_dict(d: Any) -> Optional[PlannedQuery]:
    if not isinstance(d, dict) or d.get("provider") not in COLLECTOR_PROVIDERS:
        return None
    try:
        q = PlannedQuery(**{k: v for k, v in d.items() if k in _QUERY_FIELDS})
    except TypeError:
        return None
    if not isinstance(q.params, dict):
        q.params = {}
    return q


# ---------------------------------------------------------------------------
# Collectors (factory is module-level so tests can substitute fakes)
# ---------------------------------------------------------------------------
def make_collector(provider: str, *, state: AppState, resolved_endpoints: Optional[dict[str, Any]] = None,
                   max_age_days: int = 30) -> Collector:
    """Build the collector for one provider from settings. Callers check that the key is set first."""
    s = state.settings
    if provider == "socialcrawl":
        from app.pipeline.collectors.socialcrawl import SocialCrawlCollector
        return SocialCrawlCollector(api_key=s.socialcrawl_api_key.get_secret_value(), base_url=s.socialcrawl_base_url,
                                    sources=state.sources, http=state.http, resolved_endpoints=resolved_endpoints or {},
                                    max_in_flight=s.socialcrawl_max_in_flight)
    if provider == "exa":
        from app.pipeline.collectors.exa import ExaCollector
        return ExaCollector(api_key=s.exa_api_key.get_secret_value(), base_url=s.exa_base_url, sources=state.sources,
                            http=state.http, max_age_days=max_age_days)
    if provider == "firecrawl":
        from app.pipeline.collectors.firecrawl import FirecrawlCollector
        lexicon = state.lexicon

        def promising(text: str) -> bool:
            """Undated web results get a scrape-for-date only when the title/description has a signal term."""
            try:
                return bool(lexicon.signal_matches(text or ""))
            except Exception:  # noqa: BLE001 - a lexicon problem must not stop collection
                return False

        return FirecrawlCollector(api_key=s.firecrawl_api_key.get_secret_value(), base_url=s.firecrawl_base_url,
                                  sources=state.sources, http=state.http, prefilter_title=promising)
    raise ValueError(f"unknown collector provider {provider!r}")


def _make_nominatim(state: AppState) -> Any:
    """Bounded Nominatim client on the shared HTTP client, or None when not configured."""
    s = state.settings
    try:
        from app.pipeline.geo.nominatim import NominatimClient
        client = NominatimClient(state.http, base_url=s.nominatim_base_url, contact_email=s.nominatim_contact_email,
                                 min_interval_s=s.nominatim_min_interval_sec)
        return client if client.enabled else None
    except Exception as exc:  # noqa: BLE001 - geocoding falls back to the gazetteer
        logger.warning("runner: Nominatim client unavailable (%s)", type(exc).__name__)
        return None


# ---------------------------------------------------------------------------
# Database steps (sync; each runs inside one session via _db / _Scan.db)
# ---------------------------------------------------------------------------
def _create_scan_row(db: Session, trigger: str, mode: str, now: dt.datetime) -> int:
    row = ScanRun(started_at=now, trigger=trigger, mode=mode, status="running", stage_counts={}, drop_reasons={},
                  errors=[], usage={})
    db.add(row)
    db.flush()
    return int(row.id)


def _acquire_lock(holder: str, now: dt.datetime) -> bool:
    """Atomic: take the single lock row when it is free or expired; create it on first use."""
    expires = now + LOCK_TTL
    with session_scope() as db:
        res = db.execute(
            update(ScanLock)
            .where(ScanLock.id == 1,
                   or_(ScanLock.holder.is_(None), ScanLock.expires_at.is_(None), ScanLock.expires_at < now))
            .values(holder=holder, acquired_at=now, expires_at=expires)
            .execution_options(synchronize_session=False)
        )
        if (res.rowcount or 0) == 1:
            return True
        if db.get(ScanLock, 1) is not None:
            return False
    try:
        with session_scope() as db:
            db.add(ScanLock(id=1, holder=holder, acquired_at=now, expires_at=expires))
        return True
    except IntegrityError:
        return False


def _extend_lock(db: Session, holder: str, now: dt.datetime) -> None:
    db.execute(update(ScanLock).where(ScanLock.id == 1, ScanLock.holder == holder)
               .values(expires_at=now + LOCK_TTL).execution_options(synchronize_session=False))


def _release_lock(holder: str) -> None:
    with session_scope() as db:
        db.execute(update(ScanLock).where(ScanLock.id == 1, ScanLock.holder == holder)
                   .values(holder=None, expires_at=None).execution_options(synchronize_session=False))


def _finish_scan_row(db: Session, scan_id: int, *, status: str, finished_at: dt.datetime,
                     stage_counts: dict[str, int], drop_reasons: dict[str, int], errors: list[str],
                     usage: dict[str, Any]) -> None:
    row = db.get(ScanRun, scan_id)
    if row is None:
        return
    row.status = status
    row.finished_at = finished_at
    row.stage_counts = stage_counts
    row.drop_reasons = drop_reasons
    row.errors = errors
    row.usage = usage


def _effective(db: Session, now: dt.datetime) -> Effective:
    return effective(db, now)


def _load_inputs(db: Session, now: dt.datetime, hot_limit: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "resolved": get_value(db, KEY_SC_ENDPOINTS, {}) or {},
        "cursor": get_value(db, KEY_ROTATION_CURSOR, 0),
        "deferred": get_value(db, KEY_DEFERRED, []) or [],
        "health": {p: get_value(db, KEY_HEALTH_PREFIX + p) for p in (*COLLECTOR_PROVIDERS, "anthropic")},
        "spent": {},
        "spent_error": None,
        "hot_place_ids": [],
    }
    try:
        out["spent"] = usage_mod.spent_today(db, now=now) or {}
    except Exception as exc:  # noqa: BLE001 - budgets fall back to per-scan caps only
        out["spent_error"] = type(exc).__name__
    if hot_limit > 0:
        out["hot_place_ids"] = [int(pid) for pid in db.scalars(
            select(Hotspot.place_id)
            .where(Hotspot.status == "active", Hotspot.place_id.is_not(None))
            .order_by(Hotspot.score.desc(), Hotspot.id.asc())
            .limit(hot_limit)
        )]
    return out


def _existing_place_ids(db: Session, ids: set[int]) -> set[int]:
    ids = {int(i) for i in ids if i is not None}
    if not ids:
        return set()
    return set(db.scalars(select(Place.id).where(Place.id.in_(sorted(ids)))))


def _store_health(db: Session, healths: list[ProviderHealth], now: dt.datetime) -> list[dict[str, Any]]:
    """Persist health.<provider>; return the records whose status or message changed (to publish).
    A health check that can only say 'unknown' does not overwrite a definite result less than a day old."""
    changed: list[dict[str, Any]] = []
    for h in healths:
        if h is None:
            continue
        key = KEY_HEALTH_PREFIX + h.provider
        prev = get_value(db, key)
        new = h.to_dict()
        if h.status == "unknown" and isinstance(prev, dict) and prev.get("status") in ("ok", "degraded", "unhealthy"):
            prev_at = _parse_dt(prev.get("checked_at"))
            if prev_at is not None and now - prev_at < HEALTH_KEEP_FOR:
                continue
        set_value(db, key, new)
        if not isinstance(prev, dict) or prev.get("status") != new.get("status") \
                or prev.get("message") != new.get("message"):
            changed.append(new)
    return changed


def _persist_collect(db: Session, scan_id: int, provider: str, res: CollectResult,
                     now: dt.datetime) -> list[dict[str, Any]]:
    """Query logs, api_usage, endpoint map and provider health for one collector run."""
    places = _existing_place_ids(db, {lg.place_id for lg in res.logs if lg.place_id is not None})
    n_errors = 0
    for lg in res.logs:
        status = lg.status if lg.status in ("ok", "empty", "error", "deferred") else "error"
        n_errors += status == "error"
        db.add(QueryLog(
            scan_id=scan_id,
            provider=(lg.provider or provider)[:16],
            endpoint=str(lg.endpoint or "unknown")[:500],
            query_text=str(lg.query_text or "")[:1000],
            place_id=lg.place_id if lg.place_id in places else None,
            status=status,
            http_status=lg.http_status,
            items=int(lg.items or 0),
            credits=_num(lg.credits),
            cost_usd=_num(lg.cost_usd),
            latency_ms=lg.latency_ms,
            error_type=(str(lg.error_type)[:200] if lg.error_type else None),
            request_id=(str(lg.request_id)[:200] if lg.request_id else None),
            at=_aware(lg.at) or now,
        ))
    try:
        usage_mod.record_usage(db, provider, now=now, requests=int(res.requests or 0), credits=_num(res.credits_used),
                               cost_usd=_num(res.cost_usd), errors=n_errors)
    except Exception as exc:  # noqa: BLE001 - usage accounting must not lose the query logs
        logger.warning("runner: usage not recorded for %s (%s)", provider, type(exc).__name__)
    if provider == "socialcrawl" and isinstance(res.discovered_endpoints, dict) and res.discovered_endpoints:
        prev = get_value(db, KEY_SC_ENDPOINTS, {}) or {}
        if _has_capabilities(res.discovered_endpoints) or not _has_capabilities(prev):
            set_value(db, KEY_SC_ENDPOINTS, res.discovered_endpoints)
    return _store_health(db, [res.health] if res.health is not None else [], now)


def _save_endpoints(db: Session, resolved: dict[str, Any]) -> None:
    set_value(db, KEY_SC_ENDPOINTS, resolved)


def _save_plan_state(db: Session, cursor: Optional[int], deferred: list[dict[str, Any]]) -> None:
    if cursor is not None:
        set_value(db, KEY_ROTATION_CURSOR, int(cursor))
    set_value(db, KEY_DEFERRED, deferred)


def _filter_new(db: Session, items: list[NormalizedItem]) -> list[NormalizedItem]:
    """Drop items already in items_seen, or already waiting in pending_items."""
    new, _n_seen = dedupe.filter_seen(db, items)
    if not new:
        return []
    hashes = sorted({it.item_hash for it in new})
    pending: set[str] = set()
    for i in range(0, len(hashes), 500):
        pending.update(db.scalars(select(PendingItem.item_hash).where(PendingItem.item_hash.in_(hashes[i:i + 500]))))
    return [it for it in new if it.item_hash not in pending]


def _mark_seen_many(db: Session, pairs: list[tuple[NormalizedItem, str]]) -> None:
    for item, decision in pairs:
        dedupe.mark_seen(db, item, decision)


def _queue_items(db: Session, items: list[NormalizedItem], ttl_hours: int) -> int:
    """Upsert prefilter survivors into pending_items and record them as `queued` in items_seen."""
    added = 0
    for it in items:
        score = float(it.prefilter.score) if it.prefilter is not None else 0.0
        row = db.scalars(select(PendingItem).where(PendingItem.item_hash == it.item_hash)).first()
        if row is not None:
            row.prefilter_score = max(float(row.prefilter_score or 0.0), score)
        else:
            collected = _aware(it.collected_at) or utcnow()
            db.add(PendingItem(
                item_hash=it.item_hash,
                normalized=it.to_dict(),
                prefilter_score=score,
                collected_at=collected,
                published_at=_aware(it.published_at) or collected,
                attempts=0,
                purge_after=collected + dt.timedelta(hours=int(ttl_hours)),
            ))
            added += 1
        dedupe.mark_seen(db, it, "queued")
    return added


def _select_pending(db: Session, now: dt.datetime, limit: int) -> list[tuple[int, dict[str, Any], int]]:
    rows = db.execute(
        select(PendingItem.id, PendingItem.normalized, PendingItem.attempts)
        .where(PendingItem.purge_after > now)
        .order_by(PendingItem.published_at.desc(), PendingItem.prefilter_score.desc(), PendingItem.id.asc())
        .limit(limit)
    ).all()
    return [(int(r[0]), dict(r[1] or {}), int(r[2] or 0)) for r in rows]


def _delete_pending(db: Session, row_id: int) -> None:
    db.execute(delete(PendingItem).where(PendingItem.id == row_id))


def _bump_attempts(db: Session, row_id: int) -> int:
    row = db.get(PendingItem, row_id)
    if row is None:
        return 0
    row.attempts = int(row.attempts or 0) + 1
    return int(row.attempts)


def _finalize_drop(db: Session, row_id: int, item: NormalizedItem, decision: str) -> None:
    dedupe.mark_seen(db, item, decision)
    _delete_pending(db, row_id)


def _record_usage_row(db: Session, provider: str, now: dt.datetime, amounts: dict[str, Any]) -> None:
    usage_mod.record_usage(db, provider, now=now, **amounts)


def _cache_get(db: Session, key: str, now: dt.datetime) -> Optional[dict[str, Any]]:
    row = db.get(GeocodeCache, key)
    if row is None:
        return None
    created = _aware(row.created_at)
    if created is not None and created < now - dt.timedelta(days=GEOCODE_CACHE_DAYS):
        return None
    return dict(row.result) if isinstance(row.result, dict) else None


def _cache_put(db: Session, key: str, value: dict[str, Any], now: dt.datetime) -> None:
    row = db.get(GeocodeCache, key)
    if row is None:
        db.add(GeocodeCache(query_norm=key, result=value, created_at=now))
    else:
        row.result = value
        row.created_at = now


def _signal_payloads(sig: Signal, hs: Optional[Hotspot]) -> dict[str, Any]:
    """SSE payloads, serialized while the session is open. Viewers get a copy without coordinates (R11);
    reviewers and admins get the full copy. Unmapped statuses never go to viewers."""
    from app.api import serializers

    out: dict[str, Any] = {"full": None, "viewer": None, "hotspot": None, "error": None}
    try:
        min_role = serializers.min_role_for_signal(sig)
    except Exception:  # noqa: BLE001 - same rule as the serializer contract
        min_role = "viewer" if sig.status in MAPPED_STATUSES else "reviewer"
    if sig.status not in MAPPED_STATUSES:
        min_role = "reviewer"
    try:
        out["full"] = serializers.signal_to_dict(sig, role="reviewer")
        if min_role == "viewer":
            viewer = serializers.signal_to_dict(sig, role="viewer")
            viewer["lat"] = None
            viewer["lon"] = None
            out["viewer"] = viewer
        if hs is not None:
            out["hotspot"] = serializers.hotspot_to_dict(hs)
    except Exception as exc:  # noqa: BLE001 - SSE is best effort; the signal is stored either way
        out = {"full": None, "viewer": None, "hotspot": None, "error": type(exc).__name__}
    return out


def _persist_signal(db: Session, *, row_id: Optional[int], item: NormalizedItem, values: dict[str, Any],
                    settings: Any, now: dt.datetime) -> dict[str, Any]:
    """Insert the Signal (unless its post is already stored), assign it to a hotspot when it has coordinates,
    mark the item seen and remove it from the queue. Returns ids and SSE payloads."""
    dup = db.scalar(select(Signal.id).where(Signal.post_id_hash == values["post_id_hash"]).limit(1))
    if dup is not None:
        dedupe.mark_seen(db, item, "duplicate_signal")
        if row_id is not None:
            _delete_pending(db, row_id)
        return {"duplicate": True}
    values = dict(values)
    pid = values.get("place_id")
    if pid is not None and db.get(Place, pid) is None:
        values["place_id"] = None
    sig = Signal(**values)
    db.add(sig)
    db.flush()
    hs: Optional[Hotspot] = None
    if sig.lat is not None and sig.lon is not None:
        try:
            with db.begin_nested():
                hs = hotspots_mod.assign_signal(
                    db, sig, radius_m=float(settings.hotspot_radius_m),
                    east_end_radius_m=float(settings.hotspot_radius_east_end_m), now=now,
                    **_optional(hotspots_mod.assign_signal, half_life_days=float(settings.half_life_days)))
                if hs is not None:
                    hotspots_mod.recompute(db, hs, now=now, half_life_days=float(settings.half_life_days))
        except Exception as exc:  # noqa: BLE001 - keep the signal even if clustering fails
            logger.warning("runner: hotspot assignment failed for signal %s (%s)", sig.id, type(exc).__name__)
            hs = None
    dedupe.mark_seen(db, item, STATUS_DECISION.get(sig.status, "stored"))
    if row_id is not None:
        _delete_pending(db, row_id)
    db.flush()
    return {"duplicate": False, "signal_id": int(sig.id), "status": sig.status,
            "hotspot_id": int(hs.id) if hs is not None else None, "payloads": _signal_payloads(sig, hs)}


# ---------------------------------------------------------------------------
# Scan context
# ---------------------------------------------------------------------------
@dataclass
class _Scan:
    state: AppState
    scan_id: int
    trigger: str
    eff: Effective
    started_at: dt.datetime
    fixed_now: Optional[dt.datetime]
    lock_holder: str
    t0: float = field(default_factory=time.monotonic)
    counts: Counter = field(default_factory=Counter)
    drops: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    partial_reasons: list[str] = field(default_factory=list)
    usage: dict[str, dict[str, float]] = field(default_factory=dict)
    deferred: list[PlannedQuery] = field(default_factory=list)
    db_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    warned: set[str] = field(default_factory=set)
    llm_spent: float = 0.0
    llm_reserved: float = 0.0
    llm_cap: float = 0.0
    stop_reason: Optional[str] = None
    retry_streak: int = 0
    nominatim: Any = None
    sse_error_logged: bool = False

    def now(self) -> dt.datetime:
        return self.fixed_now or utcnow()

    @property
    def settings(self) -> Any:
        return self.state.settings

    def error(self, msg: str) -> None:
        msg = " ".join(str(msg).split())[:MAX_ERROR_CHARS]
        if msg and msg not in self.errors and len(self.errors) < MAX_ERRORS:
            self.errors.append(msg)

    def partial(self, reason: str) -> None:
        self.error(reason)
        if reason not in self.partial_reasons:
            self.partial_reasons.append(reason)

    def add_usage(self, provider: str, **amounts: float) -> None:
        bucket = self.usage.setdefault(provider, {})
        for k, v in amounts.items():
            bucket[k] = round(bucket.get(k, 0) + _num(v), 6)

    async def db(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        async with self.db_lock:
            return await asyncio.to_thread(_in_session, fn, *args, **kwargs)

    def publish(self, event: str, data: dict[str, Any], **kw: Any) -> None:
        try:
            self.state.events.publish(event, data, **kw)
        except Exception as exc:  # noqa: BLE001 - SSE must never break a scan
            logger.warning("runner: publish %s failed (%s)", event, type(exc).__name__)

    # --- LLM dollar budget -----------------------------------------------------------------------------
    def llm_has_room(self) -> bool:
        return self.llm_spent + self.llm_reserved + LLM_CALL_ESTIMATE_USD <= self.llm_cap + 1e-9

    def reserve_llm(self) -> bool:
        if not self.llm_has_room():
            return False
        self.llm_reserved += LLM_CALL_ESTIMATE_USD
        return True

    def release_llm(self) -> None:
        self.llm_reserved = max(0.0, self.llm_reserved - LLM_CALL_ESTIMATE_USD)

    def budget_warning(self, provider: str, used: float, cap: float, *, stopped: bool = False,
                       scope: str = "day") -> None:
        key = f"{provider}:{scope}:{'stop' if stopped else 'warn'}"
        if key in self.warned:
            return
        self.warned.add(key)
        self.publish(ev.BUDGET_WARNING, {"provider": provider, "used": round(used, 4), "cap": round(cap, 4),
                                         "scope": scope, "stopped": stopped, "mode": self.eff.mode})

    def llm_check_warning(self) -> None:
        if self.llm_cap > 0 and self.llm_spent >= BUDGET_WARN_SHARE * self.llm_cap:
            self.budget_warning("anthropic", self.llm_spent, self.llm_cap)

    def llm_budget_stop(self) -> None:
        if self.stop_reason is None:
            self.stop_reason = "daily LLM budget reached"
            self.partial(f"classification stopped: daily LLM budget reached (${self.llm_spent:.2f} of "
                         f"${self.llm_cap:.2f}); remaining items stay queued")
        self.budget_warning("anthropic", self.llm_spent, self.llm_cap, stopped=True)


@dataclass
class _ClassifyJob:
    model: str
    effort: str
    system_blocks: list[dict[str, Any]]
    schema: dict[str, Any]
    gazetteer: Any
    geofence: Any
    min_map: float
    min_review: float
    max_age_days: int


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
async def run_scan(state: AppState, *, trigger: Literal["schedule", "manual", "pit_mode"],
                   force_mode: Optional[Literal["standard", "pit"]] = None,
                   now: Optional[dt.datetime] = None) -> Optional[int]:
    """Lock -> plan -> collect (providers in parallel, errors isolated) -> normalize -> dedupe -> seen filter ->
    date filter -> prefilter -> (reddit comment pass) -> queue to pending_items -> classify pending (newest,
    then highest score; <= MAX_CLASSIFICATIONS_PER_SCAN, LLM_CONCURRENCY, daily $ cap) -> verify -> geocode ->
    decide -> privacy.build_signal_values -> persist -> hotspots -> SSE -> usage -> release lock.
    Records stage_counts / drop_reasons / errors on the scan_runs row. Returns scan_id, or None if skipped.

    `now` (tests) pins the scan's clock; production uses the wall clock."""
    if trigger not in ("schedule", "manual", "pit_mode"):
        raise ValueError(f"invalid trigger {trigger!r}")
    if force_mode not in (None, "standard", "pit"):
        raise ValueError(f"invalid mode {force_mode!r}")
    fixed = _aware(now)
    started = fixed or utcnow()
    eff = await _db(_effective, started)
    if force_mode is not None:
        eff = dataclasses.replace(eff, pit_mode=(force_mode == "pit"))
    scan_id = await _db(_create_scan_row, trigger, eff.mode, started)
    holder = f"{socket.gethostname()}:{os.getpid()}:scan{scan_id}:{uuid.uuid4().hex[:8]}"
    if not await asyncio.to_thread(_acquire_lock, holder, started):
        await _db(_finish_scan_row, scan_id, status="skipped_locked", finished_at=fixed or utcnow(),
                  stage_counts={k: 0 for k in STAGE_KEYS}, drop_reasons={},
                  errors=["another scan holds the scan lock"], usage={})
        logger.info("scan %d skipped: another scan holds the lock", scan_id)
        return None

    ctx = _Scan(state=state, scan_id=scan_id, trigger=trigger, eff=eff, started_at=started, fixed_now=fixed,
                lock_holder=holder)
    sched = state.scheduler
    sched.current_scan_id = scan_id
    logger.info("scan %d started (trigger=%s, mode=%s)", scan_id, trigger, eff.mode)
    ctx.publish(ev.SCAN_STARTED, {"scan_id": scan_id, "mode": eff.mode, "trigger": trigger,
                                  "started_at": started.isoformat()})
    status = "failed"
    try:
        status = await _run(ctx)
    except asyncio.CancelledError:
        ctx.error("scan cancelled (shutdown)")
        status = "failed"
        raise
    except Exception as exc:  # noqa: BLE001 - recorded on the scan row; the scheduler keeps going
        logger.error("scan %d failed (%s)\n%s", scan_id, type(exc).__name__, _exc_frames(exc))
        ctx.error(f"scan failed: {type(exc).__name__}")
        status = "failed"
    finally:
        await asyncio.shield(_finish(ctx, status))
    return scan_id


async def _finish(ctx: _Scan, status: str) -> None:
    state = ctx.state
    finished = ctx.now()
    duration_ms = int((time.monotonic() - ctx.t0) * 1000)
    stage_counts = {k: int(ctx.counts.get(k, 0)) for k in STAGE_KEYS}
    drop_reasons = {k: int(v) for k, v in sorted(ctx.drops.items()) if v}
    try:
        await _db(_finish_scan_row, ctx.scan_id, status=status, finished_at=finished, stage_counts=stage_counts,
                  drop_reasons=drop_reasons, errors=list(ctx.errors), usage=ctx.usage)
    except Exception as exc:  # noqa: BLE001
        logger.error("scan %d: could not record results (%s)", ctx.scan_id, type(exc).__name__)
    try:
        await asyncio.to_thread(_release_lock, ctx.lock_holder)
    except Exception as exc:  # noqa: BLE001 - the TTL frees it anyway
        logger.error("scan %d: could not release the scan lock (%s); it expires by TTL", ctx.scan_id,
                     type(exc).__name__)
    sched = state.scheduler
    if sched.current_scan_id == ctx.scan_id:
        sched.current_scan_id = None
    sched.last_scan_at = ctx.started_at
    sched.last_scan_status = status
    ctx.publish(ev.SCAN_FINISHED, {"scan_id": ctx.scan_id, "status": status, "mode": ctx.eff.mode,
                                   "stage_counts": stage_counts, "drop_reasons": drop_reasons,
                                   "duration_ms": duration_ms, "errors": len(ctx.errors)})
    logger.info("scan %d finished: %s in %d ms; %s; drops %s", ctx.scan_id, status, duration_ms,
                ", ".join(f"{k}={v}" for k, v in stage_counts.items() if v) or "no items",
                drop_reasons or "none")


async def run_discovery(state: AppState) -> dict:
    """SocialCrawl endpoint discovery; persist to settings (KEY_SC_ENDPOINTS).

    Raises DiscoveryUnavailable when SOCIALCRAWL_API_KEY is not set. Returns the persisted map (a previous
    good map is kept when a new discovery resolves nothing)."""
    if not state.settings.has_key(KEY_ATTRS["socialcrawl"]):
        raise DiscoveryUnavailable("SOCIALCRAWL_API_KEY is not set; SocialCrawl endpoint discovery needs it")
    prev = await _db(lambda db: get_value(db, KEY_SC_ENDPOINTS, {}) or {})
    col = make_collector("socialcrawl", state=state, resolved_endpoints=prev)
    async with _discovery_lock():
        found = await asyncio.wait_for(col.discover(), timeout=DISCOVERY_TIMEOUT_S)  # type: ignore[attr-defined]
    found = found if isinstance(found, dict) else {}
    if _has_capabilities(found) or not _has_capabilities(prev):
        await _db(_save_endpoints, found)
        result = found
    else:
        result = dict(prev)
        result["errors"] = list(found.get("errors") or []) + ["discovery resolved nothing; previous map kept"]
        logger.warning("socialcrawl discovery resolved nothing; keeping the previous endpoint map")
    state.scheduler.last_discovery_at = utcnow()
    n = sum(len(v) for v in (result.get("capabilities") or {}).values() if isinstance(v, dict))
    logger.info("socialcrawl discovery: %d endpoint(s) in the stored map", n)
    return result


async def record_health(state: AppState, healths: list[Optional[ProviderHealth]], *,
                        now: Optional[dt.datetime] = None) -> list[dict[str, Any]]:
    """Persist provider health (settings health.<provider>) and publish provider.health for changes."""
    hs = [h for h in healths if h is not None]
    if not hs:
        return []
    changed = await _db(_store_health, hs, now or utcnow())
    publish_health(state, changed)
    return changed


def publish_health(state: AppState, records: list[dict[str, Any]]) -> None:
    for rec in records:
        try:
            state.events.publish(ev.PROVIDER_HEALTH, {
                "provider": rec.get("provider"), "status": rec.get("status"), "message": rec.get("message", ""),
                "checked_at": rec.get("checked_at"), "credits_remaining": rec.get("credits_remaining"),
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("runner: provider.health publish failed (%s)", type(exc).__name__)


# ---------------------------------------------------------------------------
# The scan body
# ---------------------------------------------------------------------------
async def _run(ctx: _Scan) -> str:
    state, eff = ctx.state, ctx.eff
    settings = ctx.settings
    now = ctx.now()
    hot_cap = max(0, int(_num(eff.mode_value("hot_places_per_scan"))))
    inputs = await ctx.db(_load_inputs, now, hot_cap)
    if inputs.get("spent_error"):
        ctx.error(f"today's usage could not be read ({inputs['spent_error']}); daily caps not applied")
    spent: dict[str, dict[str, float]] = inputs["spent"]
    ctx.llm_spent = _num((spent.get("anthropic") or {}).get("cost_usd"))
    ctx.llm_cap = _num(eff.mode_value("daily_llm_budget_usd"))

    budget = Budget(
        per_scan={"socialcrawl": _num(eff.mode_value("socialcrawl_credits_per_scan")),
                  "exa": _num(eff.mode_value("exa_searches_per_scan")),
                  "firecrawl": _num(eff.mode_value("firecrawl_searches_per_scan"))},
        per_day={"socialcrawl": _num(eff.mode_value("socialcrawl_credits_per_day"))},
        spent_today={"socialcrawl": _num((spent.get("socialcrawl") or {}).get("credits")),
                     "exa": _num((spent.get("exa") or {}).get("requests")),
                     "firecrawl": _num((spent.get("firecrawl") or {}).get("requests"))},
    )

    # --- providers ---------------------------------------------------------------------------------
    enabled: list[str] = []
    skipped_unhealthy: list[str] = []
    disabled_health: list[ProviderHealth] = []
    today = local_today(settings, now)
    for p in COLLECTOR_PROVIDERS:
        if not settings.has_key(KEY_ATTRS[p]):
            disabled_health.append(ProviderHealth(provider=p, status="disabled", message=f"{KEY_ENV[p]} is not set",
                                                  checked_at=now))
            continue
        rec = inputs["health"].get(p)
        if ctx.trigger != "manual" and isinstance(rec, dict) and rec.get("status") == "unhealthy":
            at = _parse_dt(rec.get("checked_at"))
            if at is not None and local_today(settings, at) == today:
                skipped_unhealthy.append(p)
                ctx.partial(f"{p} skipped: {rec.get('message') or 'unhealthy'} (retried tomorrow, after a "
                            f"healthy check, or on a manual scan)")
                continue
        enabled.append(p)
    if disabled_health:
        publish_health(state, await ctx.db(_store_health, disabled_health, now))

    gaz, gf = state.gazetteer, state.geofence
    geo_ready = gaz is not None and gf is not None
    llm_possible = state.anthropic is not None and geo_ready and ctx.llm_has_room()
    if enabled and all(budget.remaining(p) <= 0 for p in enabled) and not llm_possible:
        for p in enabled:
            cap = budget.per_day.get(p, budget.per_scan.get(p, 0.0))
            ctx.budget_warning(p, budget.spent_today.get(p, 0.0), cap, stopped=True)
        ctx.error("all collector budgets and the LLM budget are used up; scan skipped")
        return "skipped_budget"

    # --- endpoint map + collectors -----------------------------------------------------------------
    resolved: dict[str, Any] = inputs["resolved"] if isinstance(inputs["resolved"], dict) else {}
    max_age = max(1, int(_num(eff.get("ingest_max_age_days")) or 30))
    collectors: dict[str, Collector] = {}
    for p in enabled:
        try:
            collectors[p] = make_collector(p, state=state, resolved_endpoints=resolved, max_age_days=max_age)
        except Exception as exc:  # noqa: BLE001 - one provider's setup problem never fails the scan
            ctx.partial(f"{p}: collector unavailable ({type(exc).__name__})")
    sc = collectors.get("socialcrawl")
    if sc is not None and not _has_capabilities(resolved):
        resolved = await _discover_in_scan(ctx, sc, resolved)

    # --- plan ----------------------------------------------------------------------------------------
    deferred_all = [q for q in (_query_from_dict(d) for d in inputs["deferred"]) if q is not None]
    for_plan = [q for q in deferred_all if q.provider in collectors]
    carry = [q for q in deferred_all if q.provider in skipped_unhealthy]
    plan_cursor: Optional[int] = None
    queries: list[PlannedQuery] = []
    if collectors:
        hot = []
        if gaz is not None:
            for pid in inputs["hot_place_ids"]:
                try:
                    rec = gaz.by_id(pid)
                except Exception:  # noqa: BLE001
                    rec = None
                if rec is not None:
                    hot.append(rec)
        try:
            cursor = int(_num(inputs["cursor"]))
            plan = planner.plan_scan(eff=eff, lexicon=state.lexicon, sources=state.sources, gazetteer=gaz,
                                     rotation_cursor=cursor, hot_places=hot, resolved_endpoints=resolved,
                                     deferred=for_plan,
                                     **_optional(planner.plan_scan, enabled_providers=list(collectors)))
            queries = list(plan.queries)
            plan_cursor = plan.new_cursor
            for note in plan.notes:
                logger.debug("scan %d plan: %s", ctx.scan_id, note)
        except Exception as exc:  # noqa: BLE001 - classification of queued items still runs
            logger.error("scan %d: planning failed (%s)\n%s", ctx.scan_id, type(exc).__name__, _exc_frames(exc))
            ctx.partial(f"planning failed ({type(exc).__name__}); nothing collected this scan")
            carry = deferred_all            # keep every deferred query for the next scan
    ctx.counts["planned"] += len(queries)

    # --- collect ---------------------------------------------------------------------------------------
    survivors: list[NormalizedItem] = []
    if queries:
        results = await _collect(ctx, collectors, queries, budget)
        items: list[NormalizedItem] = []
        for name, res in results.items():
            await _absorb(ctx, name, res, collectors.get(name))
            items.extend(res.items)
        kept_main = await _stages(ctx, items)
        survivors.extend(kept_main)

        # Reddit comment pass: comments on threads that passed the prefilter (sightings are often there).
        sc_health = results.get("socialcrawl").health if "socialcrawl" in results else None
        if sc is not None and kept_main and not (sc_health is not None and sc_health.status == "unhealthy"):
            current = getattr(sc, "resolved_endpoints", None) or resolved
            try:
                cqs = planner.plan_comment_queries(kept_main, resolved_endpoints=current,
                                                   max_threads=MAX_COMMENT_THREADS)
            except Exception as exc:  # noqa: BLE001
                cqs = []
                ctx.error(f"comment planning failed ({type(exc).__name__})")
            if cqs:
                ctx.counts["planned"] += len(cqs)
                cres = (await _collect(ctx, {"socialcrawl": sc}, cqs, budget)).get("socialcrawl", CollectResult())
                await _absorb(ctx, "socialcrawl", cres, sc, comment_pass=True)
                survivors.extend(await _stages(ctx, cres.items))

    # --- queue + plan state -----------------------------------------------------------------------------
    if survivors:
        ctx.counts["queued"] += await ctx.db(_queue_items, survivors, int(settings.pending_item_ttl_hours))
    deferred_out = _merge_deferred(carry + ctx.deferred)
    await ctx.db(_save_plan_state, plan_cursor, [_query_to_dict(q) for q in deferred_out])
    if deferred_out:
        logger.info("scan %d: %d query(ies) deferred to the next scan", ctx.scan_id, len(deferred_out))
    _collector_budget_warnings(ctx, budget, enabled)

    # --- classify ------------------------------------------------------------------------------------------
    await _classify_phase(ctx)
    return "partial" if ctx.partial_reasons else "ok"


async def _discover_in_scan(ctx: _Scan, sc: Collector, prev: dict[str, Any]) -> dict[str, Any]:
    try:
        async with _discovery_lock():
            found = await asyncio.wait_for(sc.discover(), timeout=DISCOVERY_TIMEOUT_S)  # type: ignore[attr-defined]
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        ctx.error(f"socialcrawl: endpoint discovery failed ({type(exc).__name__})")
        return prev
    found = found if isinstance(found, dict) else {}
    if _has_capabilities(found) or not _has_capabilities(prev):
        await ctx.db(_save_endpoints, found)
        ctx.state.scheduler.last_discovery_at = utcnow()
    if not _has_capabilities(found):
        ctx.error("socialcrawl: endpoint discovery resolved no endpoints; SocialCrawl queries skipped")
        return prev
    return found


async def _collect(ctx: _Scan, collectors: dict[str, Collector], queries: list[PlannedQuery],
                   budget: Budget) -> dict[str, CollectResult]:
    """Run each provider's share of the plan concurrently. A provider that raises or times out yields an
    empty result with a degraded health record; the others are unaffected."""
    names = [n for n in collectors if any(q.provider == n for q in queries)]

    async def one(name: str) -> CollectResult:
        qs = [q for q in queries if q.provider == name]
        return await asyncio.wait_for(collectors[name].run(qs, budget), timeout=COLLECT_TIMEOUT_S)

    outs = await asyncio.gather(*(one(n) for n in names), return_exceptions=True)
    results: dict[str, CollectResult] = {}
    for name, out in zip(names, outs):
        if isinstance(out, asyncio.CancelledError):
            raise out
        if isinstance(out, BaseException):
            kind = "timed out" if isinstance(out, (asyncio.TimeoutError, TimeoutError)) else type(out).__name__
            logger.error("scan %d: %s collector failed (%s)", ctx.scan_id, name, kind)
            if not isinstance(out, (asyncio.TimeoutError, TimeoutError)):
                logger.debug("collector frames:\n%s", _exc_frames(out))
            results[name] = CollectResult(
                errors=[f"collector failed ({kind})"],
                health=ProviderHealth(provider=name, status="degraded", message=f"collector failed ({kind})",
                                      checked_at=ctx.now()))
        elif isinstance(out, CollectResult):
            results[name] = out
        else:
            results[name] = CollectResult(errors=["collector returned an unexpected result"])
    return results


async def _absorb(ctx: _Scan, name: str, res: CollectResult, collector: Optional[Collector],
                  *, comment_pass: bool = False) -> None:
    """Persist one collector run (logs, usage, endpoint map, health) and fold it into the scan totals."""
    try:
        changed = await ctx.db(_persist_collect, ctx.scan_id, name, res, ctx.now())
        publish_health(ctx.state, changed)
    except Exception as exc:  # noqa: BLE001 - items are still processed
        logger.error("scan %d: could not persist %s results (%s)", ctx.scan_id, name, type(exc).__name__)
        ctx.error(f"{name}: query logs not saved ({type(exc).__name__})")
    ctx.add_usage(name, requests=res.requests, credits=res.credits_used, cost_usd=res.cost_usd)
    for e in res.errors:
        ctx.error(f"{name}: {e}")
    ctx.deferred.extend(q for q in res.deferred if isinstance(q, PlannedQuery))
    if res.health is not None and res.health.status in ("unhealthy", "degraded"):
        ctx.partial(f"{name}{' comments' if comment_pass else ''}: {res.health.status} - {res.health.message}")
    stats = getattr(collector, "last_stats", None) if collector is not None else None
    if isinstance(stats, dict):
        for k, v in stats.items():
            n = int(_num(v))
            if n <= 0 or k == "mapped":
                continue
            ctx.drops["no_date" if str(k).startswith("no_date") else "rejected_by_collector"] += n


def _merge_deferred(queries: list[PlannedQuery]) -> list[PlannedQuery]:
    seen: set[str] = set()
    out: list[PlannedQuery] = []
    for q in sorted(queries, key=lambda q: -int(q.priority or 0)):
        try:
            k = q.key()
        except Exception:  # noqa: BLE001
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(q)
        if len(out) >= MAX_DEFERRED:
            break
    return out


async def _stages(ctx: _Scan, items: list[NormalizedItem]) -> list[NormalizedItem]:
    """dedupe -> seen filter -> date filter -> prefilter. Drops are recorded in items_seen with the reason.
    Returns the prefilter survivors (each carrying its PrefilterResult)."""
    state, eff = ctx.state, ctx.eff
    ctx.counts["collected"] += len(items)
    if not items:
        return []
    unique, _removed = dedupe.dedupe_batch(items)
    ctx.counts["unique"] += len(unique)
    new = await ctx.db(_filter_new, unique)
    ctx.counts["new"] += len(new)
    if not new:
        return []

    now = ctx.now()
    max_age = max(1, int(_num(eff.get("ingest_max_age_days")) or 30))
    in_window, date_counts = datefilter.date_filter(new, now=now, max_age_days=max_age)
    ctx.counts["in_window"] += len(in_window)
    for k, v in (date_counts or {}).items():
        if v:
            ctx.drops[_drop_key(k)] += int(v)
    kept_ids = {id(it) for it in in_window}
    oldest = now - dt.timedelta(days=max_age)
    to_mark: list[tuple[NormalizedItem, str]] = []
    for it in new:
        if id(it) in kept_ids:
            continue
        p = _aware(it.published_at) if isinstance(it.published_at, dt.datetime) else None
        to_mark.append((it, "no_date" if p is None else ("too_old" if p < oldest else "future")))

    gaz = state.gazetteer
    kept: list[NormalizedItem] = []
    for it in in_window:
        try:
            pf = prefilter_mod.prefilter(it, lexicon=state.lexicon, gazetteer=gaz)
        except Exception as exc:  # noqa: BLE001 - not marked seen, so it is judged again next scan
            logger.warning("scan %d: prefilter failed for item %s (%s)", ctx.scan_id, it.item_hash[:12],
                           type(exc).__name__)
            ctx.drops["prefilter_error"] += 1
            continue
        it.prefilter = pf
        if pf.keep:
            kept.append(it)
            continue
        reason = pf.drop_reason or "prefilter"
        ctx.drops[_drop_key(reason)] += 1
        if reason == "no_li_place" and gaz is None:
            continue    # judged again once the gazetteer can recognise Long Island place names
        to_mark.append((it, reason))
    ctx.counts["prefiltered"] += len(kept)
    if to_mark:
        await ctx.db(_mark_seen_many, to_mark)
    return kept


def _collector_budget_warnings(ctx: _Scan, budget: Budget, enabled: list[str]) -> None:
    for p, cap in budget.per_day.items():
        if p in enabled and cap > 0:
            used = budget.spent_today.get(p, 0.0)
            if used >= cap - 1e-9:
                ctx.budget_warning(p, used, cap, stopped=True)
            elif used >= BUDGET_WARN_SHARE * cap:
                ctx.budget_warning(p, used, cap)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
async def _classify_phase(ctx: _Scan) -> None:
    state, eff, settings = ctx.state, ctx.eff, ctx.settings
    if state.anthropic is None:
        ctx.partial("classification skipped: ANTHROPIC_API_KEY is not set (items stay queued up to "
                    f"{int(settings.pending_item_ttl_hours)} h)")
        return
    gaz, gf = state.gazetteer, state.geofence
    if gaz is None or gf is None:
        ctx.partial("classification skipped: geography is not ready (items stay queued)")
        return
    limit = max(0, int(_num(eff.mode_value("max_classifications_per_scan"))))
    if limit == 0:
        ctx.error("classification disabled: MAX_CLASSIFICATIONS_PER_SCAN is 0")
        return
    ctx.llm_check_warning()
    if not ctx.llm_has_room():
        ctx.llm_budget_stop()
        return

    try:
        blocks = state.system_blocks
        if not blocks:
            blocks = classifier.build_system_blocks(gaz)
            state.system_blocks = blocks
        schema = classifier.load_schema()
    except Exception as exc:  # noqa: BLE001 - collection results stay queued
        logger.error("scan %d: classifier prompt/schema unavailable (%s)", ctx.scan_id, type(exc).__name__)
        ctx.partial(f"classification skipped: classifier prompt or schema unavailable ({type(exc).__name__})")
        return
    job = _ClassifyJob(
        model=str(eff.get("anthropic_model")),
        effort=str(eff.get("classifier_effort")),
        system_blocks=blocks,
        schema=schema,
        gazetteer=gaz,
        geofence=gf,
        min_map=_num(eff.get("min_confidence_map")),
        min_review=_num(eff.get("min_confidence_review")),
        max_age_days=max(1, int(_num(eff.get("ingest_max_age_days")) or 30)),
    )
    rows = await ctx.db(_select_pending, ctx.now(), limit)
    if not rows:
        return
    await ctx.db(_extend_lock, ctx.lock_holder, ctx.now())
    ctx.nominatim = _make_nominatim(state)
    nom_before = int(getattr(ctx.nominatim, "requests_made", 0) or 0)
    sem = asyncio.Semaphore(max(1, int(settings.llm_concurrency)))
    try:
        await asyncio.gather(*(_classify_row(ctx, job, sem, row) for row in rows))
    finally:
        nom_used = int(getattr(ctx.nominatim, "requests_made", 0) or 0) - nom_before
        if nom_used > 0:
            ctx.add_usage("nominatim", requests=nom_used)
            try:
                await ctx.db(_record_usage_row, "nominatim", ctx.now(), {"requests": nom_used})
            except Exception as exc:  # noqa: BLE001
                logger.warning("runner: nominatim usage not recorded (%s)", type(exc).__name__)
    logger.info("scan %d: classified %d of %d queued item(s) picked%s", ctx.scan_id, ctx.counts["classified"],
                len(rows), f" (stopped: {ctx.stop_reason})" if ctx.stop_reason else "")


async def _classify_row(ctx: _Scan, job: _ClassifyJob, sem: asyncio.Semaphore,
                        row: tuple[int, dict[str, Any], int]) -> None:
    row_id, raw, attempts = row
    async with sem:
        if ctx.stop_reason:
            return
        if time.monotonic() - ctx.t0 > CLASSIFY_DEADLINE_S:
            ctx.stop_reason = "scan time limit"
            ctx.error("classification paused: scan time limit reached; remaining items stay queued")
            return
        try:
            item = NormalizedItem.from_dict(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scan %d: queued row %d unreadable (%s); removed", ctx.scan_id, row_id, type(exc).__name__)
            ctx.error("a queued item could not be read and was removed")
            await ctx.db(_delete_pending, row_id)
            return
        try:
            await _classify_item(ctx, job, row_id, item, attempts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one item never stops the others
            logger.error("scan %d: item %s failed in %s\n%s", ctx.scan_id, item.item_hash[:12], type(exc).__name__,
                         _exc_frames(exc))
            ctx.error(f"an item could not be processed ({type(exc).__name__}); it stays queued")
            try:
                await ctx.db(_bump_attempts, row_id)
            except Exception:  # noqa: BLE001
                pass


async def _classify_item(ctx: _Scan, job: _ClassifyJob, row_id: int, item: NormalizedItem, attempts: int) -> None:
    now = ctx.now()
    published = _aware(item.published_at)
    if published is None or published < now - dt.timedelta(days=job.max_age_days):
        ctx.drops["too_old"] += 1
        await ctx.db(_finalize_drop, row_id, item, "too_old")
        return
    if attempts >= MAX_CLASSIFY_ATTEMPTS:
        await _store_without_verdict(ctx, row_id, item, _gave_up(job, "attempts_exhausted"))
        return
    if not ctx.reserve_llm():
        ctx.llm_budget_stop()
        return
    try:
        result = await classifier.classify(
            item, client=ctx.state.anthropic, model=job.model, effort=job.effort, system_blocks=job.system_blocks,
            schema=job.schema, now=now, **_optional(classifier.classify, lexicon=ctx.state.lexicon))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - classify() should never raise; treat like a retryable error
        logger.warning("scan %d: classify raised for item %s (%s)", ctx.scan_id, item.item_hash[:12],
                       type(exc).__name__)
        result = ClassifierResult(kind="error", reason=f"exception:{type(exc).__name__}", model=job.model)
    finally:
        ctx.release_llm()
    if not isinstance(result, ClassifierResult):
        result = ClassifierResult(kind="error", reason="unexpected_result", model=job.model)
    await _account_llm(ctx, result)

    kind = result.kind
    if kind == "ok" and result.verdict is not None:
        ctx.retry_streak = 0
        ctx.counts["classified"] += 1
        await _process_verdict(ctx, job, row_id, item, result)
    elif kind in ("ok", "needs_human_review"):
        ctx.retry_streak = 0
        ctx.counts["classified"] += 1
        await _store_without_verdict(ctx, row_id, item, result)
    elif kind == "provider_down":
        reason = result.reason or "unavailable"
        if ctx.stop_reason is None:
            ctx.stop_reason = f"Anthropic unavailable ({reason})"
            ctx.partial(f"classification stopped: Anthropic unavailable ({reason}); items stay queued")
            changed = await ctx.db(_store_health, [ProviderHealth(
                provider="anthropic", status="unhealthy", message=_anthropic_message(reason, job.model),
                checked_at=utcnow())], utcnow())
            publish_health(ctx.state, changed)
    else:   # retry_later | error
        if kind == "retry_later":
            ctx.retry_streak += 1
        n = await ctx.db(_bump_attempts, row_id)
        if n >= MAX_CLASSIFY_ATTEMPTS:
            await _store_without_verdict(ctx, row_id, item, _gave_up(job, result.reason or kind))
        if ctx.retry_streak >= RETRY_STREAK_STOP and ctx.stop_reason is None:
            ctx.stop_reason = "Anthropic rate limited or erroring"
            ctx.partial("classification paused: Anthropic kept asking to retry later; items stay queued")


def _gave_up(job: _ClassifyJob, reason: str) -> ClassifierResult:
    return ClassifierResult(kind="needs_human_review", reason=f"classification_failed:{reason}"[:120],
                            model=job.model, usage=LLMUsage(model=job.model))


def _anthropic_message(reason: str, model: str) -> str:
    return {
        "anthropic_auth": "Anthropic rejected the API key; check ANTHROPIC_API_KEY",
        "anthropic_forbidden": "Anthropic denied access for this key",
        "anthropic_model_not_found": f"model {model} is not available to this key; check ANTHROPIC_MODEL",
        "anthropic_billing": "Anthropic reported a billing problem",
    }.get(reason, f"Anthropic unavailable ({reason})")


async def _account_llm(ctx: _Scan, result: ClassifierResult) -> None:
    u = result.usage if isinstance(result.usage, LLMUsage) else LLMUsage()
    cost = max(0.0, _num(u.cost_usd))
    ctx.llm_spent += cost
    amounts = {
        "requests": 0 if result.reason in ("render_failed",) else 1,
        "input_tokens": int(u.input_tokens or 0), "output_tokens": int(u.output_tokens or 0),
        "cache_read_tokens": int(u.cache_read_tokens or 0), "cache_write_tokens": int(u.cache_write_tokens or 0),
        "cost_usd": cost, "errors": 0 if result.kind in ("ok", "needs_human_review") else 1,
    }
    ctx.add_usage("anthropic", **amounts)
    try:
        await ctx.db(_record_usage_row, "anthropic", ctx.now(), amounts)
    except Exception as exc:  # noqa: BLE001 - the in-memory total still enforces this scan's cap
        logger.warning("runner: anthropic usage not recorded (%s)", type(exc).__name__)
    ctx.llm_check_warning()


async def _process_verdict(ctx: _Scan, job: _ClassifyJob, row_id: int, item: NormalizedItem,
                           result: ClassifierResult) -> None:
    verdict = result.verdict
    assert verdict is not None
    state = ctx.state
    vr: VerificationResult = verifier.verify(item, verdict, lexicon=state.lexicon, now=ctx.now())
    relevant = vr.relevance != "not_relevant"
    if relevant:
        ctx.counts["relevant"] += 1
        if vr.quotes and not vr.time_past:
            ctx.counts["verified"] += 1

    geo: Optional[GeoResult] = None
    town: Optional[str] = None
    county: Optional[str] = None
    worth_locating = (relevant and _num(verdict.confidence) >= job.min_review and not vr.time_past
                      and verdict.in_long_island_counties != "no")
    if worth_locating:
        if vr.signal_type == "self_report":
            town, county = await geocode_mod.town_for_self_report(post_text=item.text, context=item.context,
                                                                  gazetteer=job.gazetteer)
        elif vr.location_ok:
            geo = await geocode_mod.geocode(
                location_text=verdict.location.location_text, location_kind=verdict.location.location_kind,
                is_private_residence=vr.is_private_residence, post_text=item.text, context=item.context,
                gazetteer=job.gazetteer, geofence=job.geofence, nominatim=ctx.nominatim,
                cache_get=_cache_getter(ctx), cache_put=_cache_putter(ctx))
    dec: Decision = decision_mod.decide(verdict, vr, geo, min_confidence_map=job.min_map,
                                        min_confidence_review=job.min_review, self_report_town=town,
                                        self_report_county=county)
    if (geo is not None and geo.in_geofence and geo.county in IN_AREA_COUNTIES) or \
            (vr.signal_type == "self_report" and county in IN_AREA_COUNTIES):
        ctx.counts["in_area"] += 1
    if not dec.store:
        reason = dec.drop_reason or "not_stored"
        ctx.drops[_drop_key(reason)] += 1
        await ctx.db(_finalize_drop, row_id, item, reason)
        return
    await _store(ctx, row_id, item, result, vr, geo, dec, town, county)


async def _store_without_verdict(ctx: _Scan, row_id: Optional[int], item: NormalizedItem,
                                 result: ClassifierResult) -> None:
    """Refusal / invalid output / gave up: stored for staff with the post link only (docs/05 §3)."""
    dec = decision_mod.decide_without_verdict(result) if hasattr(decision_mod, "decide_without_verdict") else None
    if dec is None:
        dec = Decision(store=True, status="needs_human_review", review_reason=result.reason or "classifier_review")
    await _store(ctx, row_id, item, result, None, None, dec, None, None)


async def _store(ctx: _Scan, row_id: Optional[int], item: NormalizedItem, result: ClassifierResult,
                 vr: Optional[VerificationResult], geo: Optional[GeoResult], dec: Decision,
                 town: Optional[str], county: Optional[str]) -> None:
    settings = ctx.settings
    now = ctx.now()
    violation = getattr(privacy, "PrivacyViolation", None)
    try:
        values = privacy.build_signal_values(
            item=item, result=result, vr=vr, geo=geo, decision=dec, scan_id=ctx.scan_id, now=now,
            retention_days=int(settings.retention_days), self_report_town=town, self_report_county=county,
            **_optional(privacy.build_signal_values, lexicon=ctx.state.lexicon))
    except Exception as exc:  # noqa: BLE001
        if violation is not None and isinstance(exc, violation):
            logger.info("scan %d: item %s not stored (privacy: %s)", ctx.scan_id, item.item_hash[:12],
                        str(exc)[:80])
            ctx.drops["privacy"] += 1
            await _drop_item(ctx, row_id, item, "privacy")
            return
        logger.error("scan %d: signal values for item %s failed (%s)", ctx.scan_id, item.item_hash[:12],
                     type(exc).__name__)
        ctx.error(f"a classified item could not be stored ({type(exc).__name__})")
        ctx.drops["store_error"] += 1
        await _drop_item(ctx, row_id, item, "store_error")
        return

    out = await ctx.db(_persist_signal, row_id=row_id, item=item, values=values, settings=settings, now=now)
    if out.get("duplicate"):
        ctx.drops["duplicate"] += 1
        return
    status = out.get("status")
    ctx.counts["stored"] += 1
    stage = STATUS_STAGE.get(status or "")
    if stage:
        ctx.counts[stage] += 1
    logger.info("scan %d: stored signal %s (%s) from item %s", ctx.scan_id, out.get("signal_id"), status,
                item.item_hash[:12])
    payloads = out.get("payloads") or {}
    if payloads.get("error"):
        if not ctx.sse_error_logged:
            ctx.sse_error_logged = True
            logger.warning("runner: SSE payload serialization failed (%s); live updates skipped",
                           payloads["error"])
        return
    if payloads.get("full") is not None:
        ctx.publish(ev.SIGNAL_NEW, payloads["full"], min_role="reviewer")
    if payloads.get("viewer") is not None:
        ctx.publish(ev.SIGNAL_NEW, payloads["viewer"], min_role="viewer", max_role="viewer")
    if payloads.get("hotspot") is not None:
        ctx.publish(ev.HOTSPOT_UPDATED, payloads["hotspot"], min_role="viewer")


async def _drop_item(ctx: _Scan, row_id: Optional[int], item: NormalizedItem, decision: str) -> None:
    if row_id is not None:
        await ctx.db(_finalize_drop, row_id, item, decision)
    else:
        await ctx.db(_mark_seen_many, [(item, decision)])


def _cache_getter(ctx: _Scan) -> Callable[[str], Any]:
    async def get(key: str) -> Optional[dict[str, Any]]:
        return await ctx.db(_cache_get, key, utcnow())
    return get


def _cache_putter(ctx: _Scan) -> Callable[[str, dict[str, Any]], Any]:
    async def put(key: str, value: dict[str, Any]) -> None:
        await ctx.db(_cache_put, key, value, utcnow())
    return put


__all__ = [
    "run_scan", "run_discovery", "record_health", "publish_health", "make_collector", "has_collector_key",
    "DiscoveryUnavailable", "STAGE_KEYS", "LOCK_TTL", "KEY_DEFERRED",
]
