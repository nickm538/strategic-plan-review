"""SocialCrawl collector with runtime endpoint discovery (docs/04 §2).

Envelope, auth header, error types and limits are [verified] (socialcrawl.dev/docs, Oct 14 2026):
  {success, platform, endpoint, data, pagination, credits_used, credits_remaining, request_id, cached}
  error.type: MISSING_API_KEY | INVALID_API_KEY (401), INVALID_REQUEST (400, not billed),
  INSUFFICIENT_CREDITS (402), ENDPOINT_NOT_FOUND / RESOURCE_NOT_FOUND (404), CONCURRENCY_LIMIT (429).
Endpoint ids and parameter names are resolved at runtime from the FREE catalog (/v1/utility/*) and every
call is validated against the cached parameter list before it is sent.

Privacy: rows are mapped with explicit allow-lists (_sc_adapters); author/user fields are never read. Logs
carry counts, ids and reasons only; never post text, never request headers.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from app.pipeline.collectors import _sc_catalog as cat
from app.pipeline.collectors._common import as_float, dig, log_entry, query_label, sort_by_priority
from app.pipeline.collectors._sc_adapters import extract_rows, flatten_comments, map_row
from app.pipeline.collectors.base import Budget, HttpOutcome, request_json
from app.pipeline.sources import SourcesConfig
from app.pipeline.types import CollectResult, NormalizedItem, PlannedQuery, ProviderHealth, QueryLogEntry

logger = logging.getLogger(__name__)

PROVIDER = "socialcrawl"
REQUEST_TIMEOUT_S = 30.0
CATALOG_TIMEOUT_S = 20.0
MAX_GUIDE_FETCHES = 4            # candidate guides inspected per capability x platform
AUTH_ERRORS = frozenset({"MISSING_API_KEY", "INVALID_API_KEY", "UNAUTHORIZED", "FORBIDDEN"})
CREDIT_ERRORS = frozenset({"INSUFFICIENT_CREDITS", "PAYMENT_REQUIRED"})
TRANSIENT_ERRORS = frozenset({"CONCURRENCY_LIMIT", "RATE_LIMITED", "RATE_LIMIT_EXCEEDED", "UPSTREAM_ERROR",
                              "UPSTREAM_TIMEOUT", "SERVICE_UNAVAILABLE", "INTERNAL_ERROR", "TIMEOUT",
                              "IDEMPOTENCY_IN_PROGRESS"})


def _utc_iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).isoformat()


def error_type_of(outcome: HttpOutcome) -> str:
    """error.type from the envelope, else derived from the HTTP status / transport error."""
    body = outcome.json if isinstance(outcome.json, dict) else None
    err = body.get("error") if body else None
    if isinstance(err, dict):
        for k in ("type", "code"):
            v = err.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip().upper()
    elif isinstance(err, str) and re.fullmatch(r"[A-Z][A-Z_]{2,60}", err.strip()):
        return err.strip()
    st = outcome.status
    if st is None:
        return (outcome.error_type or "network").upper()
    return {400: "INVALID_REQUEST", 401: "INVALID_API_KEY", 402: "INSUFFICIENT_CREDITS", 403: "FORBIDDEN",
            404: "RESOURCE_NOT_FOUND", 429: "CONCURRENCY_LIMIT"}.get(st, f"HTTP_{st}")


@dataclass
class _RunState:
    stopped: Optional[str] = None             # "auth" | "credits"
    endpoint_missing: bool = False
    successes: int = 0
    failures: int = 0
    skipped_stopped: int = 0
    stats: Counter = field(default_factory=Counter)


class SocialCrawlCollector:
    name = "socialcrawl"

    def __init__(self, *, api_key: str, base_url: str, sources: SourcesConfig, http: httpx.AsyncClient,
                 resolved_endpoints: Optional[dict[str, Any]] = None, max_in_flight: int = 10,
                 now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)):
        self._api_key = (api_key or "").strip()
        self._base = (base_url or sources.socialcrawl_base_url()).rstrip("/")
        self._sources = sources
        self._http = http
        self._now = now
        self._resolved: dict[str, Any] = resolved_endpoints if isinstance(resolved_endpoints, dict) else {}
        try:
            self._max_in_flight = max(1, min(50, int(max_in_flight or 10)))
        except (TypeError, ValueError):
            self._max_in_flight = 10
        # "<endpoint id>|<param names>" -> reason. Kept in the endpoint map under "disabled" so it survives a
        # new collector instance; a fresh discovery clears it ("until catalog refresh", docs/04 §2.1).
        prev_disabled = self._resolved.get("disabled") if isinstance(self._resolved, dict) else None
        self._disabled: dict[str, str] = {str(k): str(v) for k, v in (prev_disabled or {}).items()} \
            if isinstance(prev_disabled, dict) else {}
        self._credits_remaining: Optional[float] = None
        self._last_health: Optional[ProviderHealth] = None
        self.needs_rediscovery = False
        self.last_stats: dict[str, int] = {}

    # ------------------------------------------------------------------ helpers
    @property
    def resolved_endpoints(self) -> dict[str, Any]:
        return self._resolved

    @property
    def credits_remaining(self) -> Optional[float]:
        return self._credits_remaining

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key, "Accept": "application/json"}

    def _url(self, path: str) -> str:
        return self._base + (path if path.startswith("/") else "/" + path)

    def endpoint_for(self, capability: str, platform: Optional[str]) -> Optional[dict[str, Any]]:
        caps = self._resolved.get("capabilities") if isinstance(self._resolved, dict) else None
        entry = (caps or {}).get(capability, {}).get(platform or "") if isinstance(caps, dict) else None
        return entry if isinstance(entry, dict) and entry.get("path") else None

    def _note_credits(self, body: Any) -> None:
        if isinstance(body, dict):
            cr = as_float(body.get("credits_remaining"))
            if cr is not None:
                self._credits_remaining = cr

    async def _get(self, path: str, params: dict[str, Any], *, timeout: float = REQUEST_TIMEOUT_S,
                   max_attempts: int = 3) -> HttpOutcome:
        return await request_json(self._http, "GET", self._url(path), headers=self._headers(), params=params,
                                  timeout=timeout, max_attempts=max_attempts)

    # ---------------------------------------------------------------- discovery
    async def discover(self) -> dict[str, Any]:
        """Resolve each capability x platform in sources.yaml to an endpoint via the FREE catalog
        (/v1/utility/endpoints?search=, /v1/utility/endpoint?id=). Result shape (persisted by the runner):
        {"resolved_at": iso, "capabilities": {cap: {platform: {"id", "path", "method", "credits",
          "params": [{"name","required","type","description"}], "query_param", "sort_param", "sort_value",
          "subreddit_param", "post_param", "cursor_param", "pinned": bool}}}, "unresolved": [..]}"""
        now = self._now()
        pairs = [(c, p) for c in self._sources.socialcrawl_capabilities()
                 for p in self._sources.capability_platforms(c)]
        result: dict[str, Any] = {"resolved_at": _utc_iso(now), "capabilities": {}, "unresolved": [],
                                  "errors": []}
        if not self._api_key:
            result["unresolved"] = [f"{c}/{p}: SOCIALCRAWL_API_KEY not set" for c, p in pairs]
            result["errors"].append("SOCIALCRAWL_API_KEY is not set")
            self._last_health = ProviderHealth(provider=PROVIDER, status="disabled",
                                               message="SOCIALCRAWL_API_KEY is not set", checked_at=now)
            return result

        sem = asyncio.Semaphore(min(self._max_in_flight, 5))
        memo: dict[tuple, asyncio.Task] = {}
        auth_failed: list[str] = []

        async def fetch(path: str, params: dict[str, Any]) -> Optional[Any]:
            key = (path, tuple(sorted(params.items())))
            if key not in memo:
                async def _do() -> Optional[Any]:
                    if auth_failed:
                        return None
                    async with sem:
                        out = await self._get(path, params, timeout=CATALOG_TIMEOUT_S, max_attempts=2)
                    et = "" if _ok(out) else error_type_of(out)
                    if et in AUTH_ERRORS or out.status in (401, 403):
                        auth_failed.append(et or "INVALID_API_KEY")
                        return None
                    if not _ok(out):
                        if et and et not in ("RESOURCE_NOT_FOUND", "ENDPOINT_NOT_FOUND"):
                            result["errors"].append(f"catalog {path}: {et}")
                        return None
                    return out.json
                memo[key] = asyncio.ensure_future(_do())
            return await memo[key]

        async def guide(endpoint_id: str) -> Optional[dict[str, Any]]:
            body = await fetch("/v1/utility/endpoint", {"id": endpoint_id})
            return cat.parse_guide(body) if body is not None else None

        async def candidates(cap: str, slug: str) -> list[dict[str, Any]]:
            words = self._sources.socialcrawl_capability(cap).get("discovery_search") or []
            bodies = [await fetch("/v1/utility/endpoints", {"platform": slug})]
            for w in words:
                if isinstance(w, str) and w.strip():
                    bodies.append(await fetch("/v1/utility/endpoints", {"search": w.strip(), "platform": slug}))
            merged: dict[str, dict[str, Any]] = {}
            for b in bodies:
                if b is None:
                    continue
                for e in cat.parse_catalog(b, platform=slug):
                    prev = merged.get(e["id"])
                    if prev is None:
                        merged[e["id"]] = e
                    else:
                        for k in ("credits", "params", "description"):
                            if prev.get(k) in (None, "", []) and e.get(k) not in (None, "", []):
                                prev[k] = e[k]
            return list(merged.values())

        async def resolve(cap: str, slug: str) -> tuple[str, str, Optional[dict[str, Any]], Optional[str]]:
            pinned = self._sources.pinned_endpoint(cap, slug)
            note = None
            if pinned:
                g = await guide(pinned)
                if g is not None and g.get("params") is not None:
                    entry = self._build_entry(cap, g, pinned=True)
                    if entry:
                        return cap, slug, entry, None
                note = f"pinned endpoint {pinned} not usable; fell back to catalog search"
            ranked = cat.rank_candidates(await candidates(cap, slug), cap)
            for cand in ranked[:MAX_GUIDE_FETCHES]:
                g = await guide(cand["id"])
                merged = dict(cand)
                if g is not None:
                    merged.update({k: v for k, v in g.items() if v not in (None, "", [])})
                    if merged.get("credits") is None:
                        merged["credits"] = cand.get("credits")
                if merged.get("params") is None:
                    continue
                entry = self._build_entry(cap, merged, pinned=False)
                if entry:
                    if note:
                        entry["note"] = note
                    return cap, slug, entry, None
            reason = "no matching endpoint in catalog" if not ranked else "no candidate exposes the needed parameters"
            return cap, slug, None, (note + "; " + reason) if note else reason

        outcomes = await asyncio.gather(*(resolve(c, p) for c, p in pairs), return_exceptions=True)
        caps: dict[str, dict[str, Any]] = {}
        for (c, p), out in zip(pairs, outcomes, strict=True):
            if isinstance(out, BaseException):
                logger.warning("socialcrawl discovery: %s/%s failed (%s)", c, p, type(out).__name__)
                result["unresolved"].append(f"{c}/{p}: discovery error {type(out).__name__}")
                continue
            _, _, entry, reason = out
            if entry is not None:
                caps.setdefault(c, {})[p] = entry
            else:
                result["unresolved"].append(f"{c}/{p}: {reason}")

        if auth_failed:
            msg = "SocialCrawl rejected the API key; check SOCIALCRAWL_API_KEY"
            result["errors"].append(msg)
            self._last_health = ProviderHealth(provider=PROVIDER, status="unhealthy", message=msg, checked_at=now)
            prev = self._resolved.get("capabilities") if isinstance(self._resolved, dict) else None
            if prev:                     # keep the last good map rather than wiping it
                kept = {"resolved_at": self._resolved.get("resolved_at"), "capabilities": prev,
                        "unresolved": list(self._resolved.get("unresolved") or []), "errors": result["errors"],
                        "stale": True}
                logger.warning("socialcrawl discovery: auth failed; keeping the previous endpoint map")
                return kept
            result["unresolved"] = [f"{c}/{p}: authentication failed" for c, p in pairs]
            return result

        # A capability that failed this time only because of transient catalog errors keeps its old entry.
        prev_caps = (self._resolved.get("capabilities") or {}) if isinstance(self._resolved, dict) else {}
        if result["errors"] and prev_caps:
            for c, plats in prev_caps.items():
                for p, entry in (plats or {}).items():
                    if p not in caps.get(c, {}) and isinstance(entry, dict):
                        caps.setdefault(c, {})[p] = dict(entry, stale=True)
                        result["unresolved"] = [u for u in result["unresolved"] if not u.startswith(f"{c}/{p}:")]
        result["capabilities"] = caps
        self._resolved = result
        self._disabled.clear()
        self.needs_rediscovery = False
        n = sum(len(v) for v in caps.values())
        logger.info("socialcrawl discovery: %d endpoint(s) resolved, %d unresolved", n, len(result["unresolved"]))
        return result

    def _build_entry(self, capability: str, e: dict[str, Any], *, pinned: bool) -> Optional[dict[str, Any]]:
        params = e.get("params") or []
        prefer = self._sources.socialcrawl_capability(capability).get("prefer_params") or {}
        sort_prefs = [str(x) for x in (prefer.get("sort") or [])] or list(cat.DEFAULT_SORT_PREFS)
        if capability in ("subreddit_new", "subreddit_search", "post_comments"):
            sort_prefs = list(cat.DEFAULT_SORT_PREFS)
        time_prefs = ["week"] + [str(x) for x in (prefer.get("time") or []) if str(x) not in ("week", "day")]
        if "month" not in time_prefs:
            time_prefs.append("month")
        roles = cat.derive_roles(capability, params, sort_prefs=sort_prefs, time_prefs=time_prefs)
        if not cat.roles_satisfied(capability, roles):
            return None
        credits = e.get("credits")
        return {
            "id": e["id"], "path": e["path"], "method": e.get("method") or "GET",
            "credits": float(credits) if credits is not None else 1.0,
            "credits_known": credits is not None,
            "description": (e.get("description") or "")[:300],
            "params": [{"name": p["name"], "required": bool(p.get("required")), "type": p.get("type") or "",
                        "description": (p.get("description") or "")[:300], "enum": p.get("enum")}
                       for p in params],
            **roles,
            "pinned": pinned,
        }

    # ------------------------------------------------------------------- calls
    def build_call(self, q: PlannedQuery, entry: dict[str, Any]) -> tuple[dict[str, Any], Optional[str]]:
        """Request parameters for a planned query, using the endpoint's role->name map, validated against
        the cached parameter list. Returns (params, reason-if-invalid)."""
        call: dict[str, Any] = {}
        cap = q.capability
        if entry.get("query_param") and cap in ("keyword_search", "subreddit_search", "news_search"):
            if q.query:
                call[entry["query_param"]] = q.query
        if entry.get("hashtag_param") and cap == "hashtag_posts":
            tag = str(q.params.get("hashtag") or q.query or "").strip().lstrip("#")
            if tag:
                call[entry["hashtag_param"]] = tag
        if entry.get("subreddit_param") and q.subreddit:
            call[entry["subreddit_param"]] = q.subreddit
        if entry.get("post_param") and cap == "post_comments":
            pname = entry["post_param"]
            if pname.lower() in ("url", "post_url", "link"):
                val = q.params.get("post_url")
            else:
                val = q.params.get("post_id")
                if isinstance(val, str) and val.startswith("t3_"):
                    val = val[3:]
            if val:
                call[pname] = val
        if entry.get("sort_param") and entry.get("sort_value"):
            call[entry["sort_param"]] = entry["sort_value"]
        if entry.get("time_param") and entry.get("time_value"):
            call[entry["time_param"]] = entry["time_value"]
        for k, v in (entry.get("extra_params") or {}).items():
            call[k] = v
        if not cat.roles_satisfied(cap, entry):
            return call, "endpoint lacks a required role parameter"
        needed = {"keyword_search": "query_param", "subreddit_search": "subreddit_param",
                  "subreddit_new": "subreddit_param", "post_comments": "post_param",
                  "hashtag_posts": "hashtag_param", "news_search": "query_param"}.get(cap)
        if needed and entry.get(needed) not in call:
            return call, f"planned query has no value for {needed.removesuffix('_param')}"
        reason = cat.validate_call(entry.get("params") or [], call)
        return call, reason

    @staticmethod
    def _combo_key(entry: dict[str, Any], call: dict[str, Any]) -> str:
        return f"{entry.get('id')}|{','.join(sorted(call))}"

    async def run(self, queries: list[PlannedQuery], budget: Budget) -> CollectResult:
        """Execute planned SocialCrawl queries (<= max_in_flight concurrent), branch on `success` then
        `error.type` (docs/04 §2.1), read credits_used/credits_remaining, map items with a table-driven,
        per-platform adapter to NormalizedItem via normalize.build_item (dropping ALL author fields)."""
        result = CollectResult()
        qs = sort_by_priority(q for q in queries if q.provider == PROVIDER)
        now = self._now()
        if not self._api_key:
            if qs:
                result.errors.append(f"SOCIALCRAWL_API_KEY is not set; {len(qs)} SocialCrawl queries skipped")
            result.health = ProviderHealth(provider=PROVIDER, status="disabled",
                                           message="SOCIALCRAWL_API_KEY is not set", checked_at=now)
            self._last_health = result.health
            return result
        if not (self._resolved.get("capabilities") if isinstance(self._resolved, dict) else None):
            try:
                result.discovered_endpoints = await self.discover()
            except Exception as exc:  # noqa: BLE001 - discovery must never fail the scan
                logger.warning("socialcrawl: discovery failed (%s)", type(exc).__name__)
                result.errors.append(f"SocialCrawl endpoint discovery failed ({type(exc).__name__})")

        state = _RunState()
        sem = asyncio.Semaphore(self._max_in_flight)
        slots: list[tuple[list[NormalizedItem], list[QueryLogEntry]]] = [([], []) for _ in qs]

        async def one(i: int, q: PlannedQuery) -> None:
            try:
                await self._run_one(q, budget, sem, state, result, slots[i])
            except Exception as exc:  # noqa: BLE001 - one bad query never fails the provider
                logger.warning("socialcrawl: query failed unexpectedly (%s)", type(exc).__name__)
                state.failures += 1
                slots[i][1].append(log_entry(q, provider=PROVIDER, endpoint=q.capability, status="error",
                                             error_type=f"internal:{type(exc).__name__}", at=self._now()))

        await asyncio.gather(*(one(i, q) for i, q in enumerate(qs)))
        for items, logs in slots:
            result.items.extend(items)
            result.logs.extend(logs)
        if state.skipped_stopped:
            result.errors.append(f"{state.skipped_stopped} SocialCrawl queries skipped after the provider stopped "
                                 f"({state.stopped})")
        if state.endpoint_missing:
            result.errors.append("SocialCrawl reported ENDPOINT_NOT_FOUND; refreshing the endpoint catalog")
            self.needs_rediscovery = True
            try:
                result.discovered_endpoints = await self.discover()
            except Exception as exc:  # noqa: BLE001
                logger.warning("socialcrawl: re-discovery failed (%s)", type(exc).__name__)
        self.last_stats = dict(state.stats)
        if state.stats:
            dropped = {k: v for k, v in state.stats.items() if k != "mapped"}
            logger.info("socialcrawl: %d item(s) mapped from %d request(s); dropped %s",
                        state.stats.get("mapped", 0), result.requests, dropped or "none")
        result.health = self._health_from_run(state, len(qs))
        if result.health is not None:
            self._last_health = result.health
        return result

    async def _run_one(self, q: PlannedQuery, budget: Budget, sem: asyncio.Semaphore, state: _RunState,
                       result: CollectResult, slot: tuple[list[NormalizedItem], list[QueryLogEntry]]) -> None:
        items_out, logs_out = slot
        async with sem:
            if state.stopped:
                if state.stopped == "credits":
                    result.deferred.append(q)
                    logs_out.append(log_entry(q, provider=PROVIDER, endpoint=q.capability, status="deferred",
                                              error_type="INSUFFICIENT_CREDITS", at=self._now()))
                else:
                    state.skipped_stopped += 1
                return
            entry = self.endpoint_for(q.capability, q.platform)
            if entry is None:
                result.errors.append(f"SocialCrawl {q.capability} on {q.platform} is not resolved; query skipped")
                logs_out.append(log_entry(q, provider=PROVIDER, endpoint=q.capability, status="error",
                                          error_type="unresolved_endpoint", at=self._now()))
                return
            call, reason = self.build_call(q, entry)
            endpoint = entry.get("path") or entry.get("id") or q.capability
            if reason:
                logger.info("socialcrawl: skipped %s (%s)", entry.get("id"), reason)
                result.errors.append(f"SocialCrawl {entry.get('id')}: call skipped ({reason})")
                logs_out.append(log_entry(q, provider=PROVIDER, endpoint=endpoint, status="error",
                                          error_type="invalid_params", at=self._now()))
                return
            combo = self._combo_key(entry, call)
            if combo in self._disabled:
                logs_out.append(log_entry(q, provider=PROVIDER, endpoint=endpoint, status="error",
                                          error_type="disabled_until_rediscovery", at=self._now()))
                return
            est = float(entry.get("credits") if entry.get("credits") is not None else (q.est_cost or 1.0))
            if not budget.try_charge(PROVIDER, est):
                result.deferred.append(q)
                logs_out.append(log_entry(q, provider=PROVIDER, endpoint=endpoint, status="deferred",
                                          error_type="budget", at=self._now()))
                return
            outcome = await self._get(entry["path"], call)
        result.requests += 1
        self._handle(q, entry, call, est, outcome, budget, state, result, items_out, logs_out)

    def _handle(self, q: PlannedQuery, entry: dict[str, Any], call: dict[str, Any], est: float,
                outcome: HttpOutcome, budget: Budget, state: _RunState, result: CollectResult,
                items_out: list[NormalizedItem], logs_out: list[QueryLogEntry]) -> None:
        body = outcome.json if isinstance(outcome.json, dict) else None
        self._note_credits(body)
        endpoint = entry.get("path") or entry.get("id") or q.capability
        request_id = (body or {}).get("request_id") or outcome.headers.get("x-request-id") \
            or outcome.headers.get("request-id")
        credits_used = as_float((body or {}).get("credits_used"))
        now = self._now()

        if _ok(outcome):
            actual = credits_used if credits_used is not None else est
            budget.adjust(PROVIDER, est, actual)
            result.credits_used += actual
            state.successes += 1
            items = self.map_response(body, q, now=now, stats=state.stats)
            items_out.extend(items)
            rows = len(extract_rows(body.get("data")))
            logs_out.append(log_entry(q, provider=PROVIDER, endpoint=endpoint, status="ok" if rows else "empty",
                                      http_status=outcome.status, items=len(items), credits=actual,
                                      latency_ms=outcome.latency_ms, request_id=request_id, at=now))
            return

        # Failure: SocialCrawl refunds failed calls and empty results; charge only what it reports.
        actual = credits_used if credits_used is not None else 0.0
        budget.adjust(PROVIDER, est, actual)
        result.credits_used += actual
        et = error_type_of(outcome)
        status = "error"
        if et == "RESOURCE_NOT_FOUND":
            status = "empty"
            state.successes += 1
        elif et in AUTH_ERRORS or outcome.status in (401, 403):
            state.stopped = state.stopped or "auth"
            state.failures += 1
            logger.warning("socialcrawl: authentication failed (%s); stopping SocialCrawl for this scan", et)
            result.errors.append("SocialCrawl rejected the API key; check SOCIALCRAWL_API_KEY")
        elif et in CREDIT_ERRORS or outcome.status == 402:
            state.stopped = "credits"
            state.failures += 1
            result.deferred.append(q)
            status = "deferred"
            logger.warning("socialcrawl: out of credits; stopping SocialCrawl for this scan")
            result.errors.append("SocialCrawl is out of credits; queries deferred")
        elif et == "INVALID_REQUEST" or outcome.status == 400:
            self._disable(self._combo_key(entry, call), "INVALID_REQUEST", result)
            state.failures += 1
            logger.warning("socialcrawl: INVALID_REQUEST from %s; endpoint+params disabled until rediscovery",
                           entry.get("id"))
            result.errors.append(f"SocialCrawl rejected parameters for {entry.get('id')}; disabled until the "
                                 f"catalog is refreshed")
        elif et == "ENDPOINT_NOT_FOUND":
            self._disable(self._combo_key(entry, call), "ENDPOINT_NOT_FOUND", result)
            state.endpoint_missing = True
            state.failures += 1
            logger.warning("socialcrawl: ENDPOINT_NOT_FOUND for %s; refresh catalog", entry.get("id"))
        elif et in TRANSIENT_ERRORS or outcome.status is None or (outcome.status or 0) >= 500 \
                or outcome.status == 429:
            state.failures += 1
            result.deferred.append(q)
            status = "deferred"
            result.errors.append(f"SocialCrawl {entry.get('id')}: {et} after retries; query deferred")
        else:
            state.failures += 1
            result.errors.append(f"SocialCrawl {entry.get('id')}: {et}")
        logs_out.append(log_entry(q, provider=PROVIDER, endpoint=endpoint, status=status,
                                  http_status=outcome.status, credits=actual, latency_ms=outcome.latency_ms,
                                  error_type=None if status == "empty" else et, request_id=request_id, at=now))

    def _disable(self, combo: str, reason: str, result: CollectResult) -> None:
        """Stop sending this endpoint+parameter combination until the next discovery, and hand the updated
        endpoint map to the runner to persist."""
        self._disabled[combo] = reason
        if isinstance(self._resolved, dict) and self._resolved.get("capabilities"):
            self._resolved["disabled"] = dict(self._disabled)
            if result.discovered_endpoints is None:
                result.discovered_endpoints = self._resolved

    def map_response(self, body: Any, q: PlannedQuery, *, now: Optional[dt.datetime] = None,
                     stats: Optional[Counter] = None) -> list[NormalizedItem]:
        """Map a successful envelope's rows to NormalizedItems (comments are flattened, one item each)."""
        now = now or self._now()
        stats = stats if stats is not None else Counter()
        data = body.get("data") if isinstance(body, dict) else None
        rows = extract_rows(data)
        pmap = self._sources.socialcrawl_platform_map()
        slug = q.platform or ""
        app_platform = pmap.get(slug, slug)
        out: list[NormalizedItem] = []
        if q.capability == "post_comments":
            thread_url = q.params.get("post_url") if isinstance(q.params.get("post_url"), str) else None
            for row, _depth in flatten_comments(rows):
                item = map_row(row, slug=slug, app_platform=app_platform, query=q, now=now, sources=self._sources,
                               is_comment=True, stats=stats, thread_url=thread_url)
                if item is not None:
                    out.append(item)
            return out
        for row in rows:
            item = map_row(row, slug=slug, app_platform=app_platform, query=q, now=now, sources=self._sources,
                           is_comment=False, stats=stats)
            if item is not None:
                out.append(item)
        return out

    def _health_from_run(self, state: _RunState, n_queries: int) -> Optional[ProviderHealth]:
        now = self._now()
        if state.stopped == "auth":
            return ProviderHealth(provider=PROVIDER, status="unhealthy",
                                  message="SocialCrawl rejected the API key; check SOCIALCRAWL_API_KEY",
                                  credits_remaining=self._credits_remaining, checked_at=now)
        if state.stopped == "credits":
            return ProviderHealth(provider=PROVIDER, status="unhealthy", message="SocialCrawl is out of credits",
                                  credits_remaining=self._credits_remaining if self._credits_remaining is not None
                                  else 0.0, checked_at=now)
        if state.successes == 0 and state.failures == 0:
            return None if n_queries == 0 else self._last_health
        if state.successes == 0:
            return ProviderHealth(provider=PROVIDER, status="degraded",
                                  message=f"all {state.failures} SocialCrawl request(s) failed this scan",
                                  credits_remaining=self._credits_remaining, checked_at=now)
        msg = f"{state.successes} request(s) ok" + (f", {state.failures} failed" if state.failures else "")
        return ProviderHealth(provider=PROVIDER, status="degraded" if state.failures > state.successes else "ok",
                              message=msg, credits_remaining=self._credits_remaining, checked_at=now)

    # ------------------------------------------------------------------ health
    async def health(self) -> ProviderHealth:
        """GET /v1/credits/balance (free)."""
        now = self._now()
        if not self._api_key:
            return ProviderHealth(provider=PROVIDER, status="disabled", message="SOCIALCRAWL_API_KEY is not set",
                                  checked_at=now)
        try:
            out = await self._get("/v1/credits/balance", {}, timeout=15.0, max_attempts=2)
        except Exception as exc:  # noqa: BLE001 - health checks never raise
            return ProviderHealth(provider=PROVIDER, status="unknown",
                                  message=f"balance check failed ({type(exc).__name__})", checked_at=now)
        body = out.json if isinstance(out.json, dict) else None
        et = error_type_of(out) if not _ok(out) else ""
        if et in AUTH_ERRORS or out.status in (401, 403):
            h = ProviderHealth(provider=PROVIDER, status="unhealthy",
                               message="SocialCrawl rejected the API key; check SOCIALCRAWL_API_KEY", checked_at=now)
        elif not _ok(out):
            h = ProviderHealth(provider=PROVIDER, status="unknown", message=f"balance check failed ({et})",
                               credits_remaining=self._credits_remaining, checked_at=now)
        else:
            bal = None
            for path in ("data.credits_remaining", "data.balance", "data.credits", "data.remaining",
                         "data.available", "credits_remaining", "balance"):
                bal = as_float(dig(body, path))
                if bal is not None:
                    break
            if bal is not None:
                self._credits_remaining = bal
            if bal is not None and bal <= 0:
                h = ProviderHealth(provider=PROVIDER, status="unhealthy", message="SocialCrawl is out of credits",
                                   credits_remaining=bal, checked_at=now)
            else:
                msg = f"balance {bal:g} credits" if bal is not None else "reachable (balance not reported)"
                h = ProviderHealth(provider=PROVIDER, status="ok", message=msg, credits_remaining=bal,
                                   checked_at=now)
        self._last_health = h
        return h

    # ---------------------------------------------------------- smoke support
    async def probe(self, capability: str, platform: str, *, query: str = "", subreddit: Optional[str] = None,
                    post_url: Optional[str] = None) -> tuple[Optional[HttpOutcome], dict[str, Any], Optional[str]]:
        """One live call for scripts/smoke_providers.py: (outcome, params sent, reason if not sent)."""
        entry = self.endpoint_for(capability, platform)
        if entry is None:
            return None, {}, "not resolved"
        q = PlannedQuery(provider=PROVIDER, capability=capability, query=query, platform=platform,
                         subreddit=subreddit, params={"post_url": post_url} if post_url else {})
        call, reason = self.build_call(q, entry)
        if reason:
            return None, call, reason
        out = await self._get(entry["path"], call)
        self._note_credits(out.json)
        return out, call, None


def _ok(outcome: HttpOutcome) -> bool:
    """2xx with a JSON envelope that is not marked as a failure."""
    body = outcome.json if isinstance(outcome.json, dict) else None
    if body is None or outcome.status is None or not 200 <= outcome.status < 300:
        return False
    if body.get("success") is True:
        return True
    if body.get("success") is False:
        return False
    return not body.get("error")


__all__ = ["SocialCrawlCollector", "error_type_of", "query_label"]
