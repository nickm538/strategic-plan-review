"""Exa collector (docs/04 §3).

POST {base}/search with header x-api-key and
  {query, type, numResults, startPublishedDate, includeDomains?, contents: {text: {maxCharacters}}}.
Reads results[].url/title/publishedDate/text/id and costDollars.total. Results without publishedDate are
discarded (F3). [verify] request/response field names against docs.exa.ai at deploy (written from prior
knowledge of the API; the sandbox could not reach it). Author fields are never read (R2).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections import Counter
from typing import Any, Callable, Optional

import httpx

from app.pipeline.collectors._common import (
    as_float, dig, first_str, is_http_url, is_profile_url, iso_z, log_entry, sort_by_priority,
)
from app.pipeline.collectors.base import Budget, HttpOutcome, request_json
from app.pipeline.sources import SourcesConfig, host_matches, host_of, scope_max
from app.pipeline.types import CollectResult, ItemContext, NormalizedItem, PlannedQuery, ProviderHealth

logger = logging.getLogger(__name__)

PROVIDER = "exa"
REQUEST_TIMEOUT_S = 45.0
MAX_CONCURRENCY = 3


class ExaCollector:
    name = "exa"

    def __init__(self, *, api_key: str, base_url: str, sources: SourcesConfig, http: httpx.AsyncClient,
                 max_age_days: int, now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)):
        self._api_key = (api_key or "").strip()
        self._base = (base_url or str(sources.exa().get("base_url") or "https://api.exa.ai")).rstrip("/")
        self._sources = sources
        self._http = http
        self._now = now
        try:
            self._max_age_days = max(1, int(max_age_days))
        except (TypeError, ValueError):
            self._max_age_days = 30
        self._last_health: Optional[ProviderHealth] = None
        self.last_stats: dict[str, int] = {}

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key, "Content-Type": "application/json", "Accept": "application/json"}

    def build_body(self, q: PlannedQuery, *, now: dt.datetime) -> dict[str, Any]:
        """Request body for one planned query. The unrestricted semantic family (params.unrestricted) runs
        without includeDomains so local forums and blogs can surface."""
        cfg = self._sources.exa_search()
        try:
            num = int(q.params.get("num_results") or cfg.get("num_results") or 10)
        except (TypeError, ValueError):
            num = 10
        try:
            max_chars = int(cfg.get("text_max_characters") or 4000)
        except (TypeError, ValueError):
            max_chars = 4000
        body: dict[str, Any] = {
            "query": q.query,
            "type": str(cfg.get("type") or "auto"),
            "numResults": max(1, min(100, num)),
            "startPublishedDate": iso_z(now - dt.timedelta(days=self._max_age_days)),
            "contents": {"text": {"maxCharacters": max(200, min(20000, max_chars))}},
        }
        if not q.params.get("unrestricted"):
            domains = self._sources.exa_include_domains()
            if domains:
                body["includeDomains"] = domains
        return body

    def _platform_for(self, url: str) -> str:
        h = host_of(url) or ""
        if host_matches(h, "reddit.com"):
            return "reddit"
        if self._sources.is_news_url(url):
            return "news"
        return "web"

    def map_results(self, body: Any, q: PlannedQuery, *, now: dt.datetime, stats: Counter) -> list[NormalizedItem]:
        from app.pipeline.normalize import build_item, parse_datetime
        from app.pipeline.sources import subreddit_from_url

        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            data = dig(body, "data.results") if isinstance(body, dict) else None
            results = data if isinstance(data, list) else []
        out: list[NormalizedItem] = []
        for r in results:
            if not isinstance(r, dict):
                stats["not_an_object"] += 1
                continue
            url = first_str(r, ("url", "id"))
            if not is_http_url(url):
                stats["no_url"] += 1
                continue
            platform = self._platform_for(url)
            if platform == "reddit" and is_profile_url("reddit", url):
                stats["profile_url"] += 1
                continue
            raw_date = first_str(r, ("publishedDate", "published_date", "published"))
            published_at, _src = parse_datetime(raw_date, now=now) if raw_date else (None, None)
            if published_at is None:
                stats["no_date"] += 1
                continue
            title = first_str(r, ("title",), max_len=2000)
            text = first_str(r, ("text", "summary"))
            if not text:
                hl = r.get("highlights")
                if isinstance(hl, list):
                    text = " … ".join(h.strip() for h in hl if isinstance(h, str) and h.strip())
            sub = subreddit_from_url(url) if platform == "reddit" else None
            if sub and self._sources.is_excluded_subreddit(sub):
                stats["excluded_subreddit"] += 1
                continue
            ctx = ItemContext(
                query=(q.query or "")[:500],
                source_scope=scope_max(q.source_scope, self._sources.scope_for(url=url, subreddit=sub)),
                query_place_id=q.place_id, query_place_name=q.place_name, subreddit=sub,
            )
            item = build_item(
                provider=PROVIDER, platform=platform, url=url, platform_post_id=first_str(r, ("id",)) or "",
                published_at=published_at, date_source="provider", collected_at=now, title=title, body=text,
                context=ctx, media_present=bool(r.get("image")) if platform != "news" else False,
            )
            if item is None:
                stats["empty_text"] += 1
                continue
            stats["mapped"] += 1
            out.append(item)
        return out

    async def run(self, queries: list[PlannedQuery], budget: Budget) -> CollectResult:
        result = CollectResult()
        qs = sort_by_priority(q for q in queries if q.provider == PROVIDER)
        now = self._now()
        if not self._api_key:
            if qs:
                result.errors.append(f"EXA_API_KEY is not set; {len(qs)} Exa queries skipped")
            result.health = ProviderHealth(provider=PROVIDER, status="disabled", message="EXA_API_KEY is not set",
                                           checked_at=now)
            self._last_health = result.health
            return result
        stats: Counter = Counter()
        sem = asyncio.Semaphore(MAX_CONCURRENCY)
        stop: list[str] = []
        slots: list[tuple[list[NormalizedItem], list[Any]]] = [([], []) for _ in qs]
        counters = Counter()

        async def one(i: int, q: PlannedQuery) -> None:
            items_out, logs_out = slots[i]
            try:
                async with sem:
                    if stop:
                        if stop[0] == "credits":
                            result.deferred.append(q)
                            logs_out.append(log_entry(q, provider=PROVIDER, endpoint="/search", status="deferred",
                                                      error_type="out_of_credits", at=self._now()))
                        else:
                            counters["skipped"] += 1
                        return
                    est = float(q.est_cost or 1.0)
                    if not budget.try_charge(PROVIDER, est):
                        result.deferred.append(q)
                        logs_out.append(log_entry(q, provider=PROVIDER, endpoint="/search", status="deferred",
                                                  error_type="budget", at=self._now()))
                        return
                    t_now = self._now()
                    out = await request_json(self._http, "POST", f"{self._base}/search", headers=self._headers(),
                                             json_body=self.build_body(q, now=t_now), timeout=REQUEST_TIMEOUT_S)
                result.requests += 1
                self._handle(q, est, out, budget, result, stats, counters, stop, items_out, logs_out)
            except Exception as exc:  # noqa: BLE001 - one bad query never fails the provider
                logger.warning("exa: query failed unexpectedly (%s)", type(exc).__name__)
                counters["failures"] += 1
                logs_out.append(log_entry(q, provider=PROVIDER, endpoint="/search", status="error",
                                          error_type=f"internal:{type(exc).__name__}", at=self._now()))

        await asyncio.gather(*(one(i, q) for i, q in enumerate(qs)))
        for items, logs in slots:
            result.items.extend(items)
            result.logs.extend(logs)
        if counters["skipped"]:
            result.errors.append(f"{counters['skipped']} Exa queries skipped after the provider stopped ({stop[0]})")
        self.last_stats = dict(stats)
        if stats:
            logger.info("exa: %d item(s) mapped; dropped %s", stats.get("mapped", 0),
                        {k: v for k, v in stats.items() if k != "mapped"} or "none")
        result.health = self._health_from_run(stop, counters, len(qs))
        if result.health is not None:
            self._last_health = result.health
        return result

    def _handle(self, q: PlannedQuery, est: float, out: HttpOutcome, budget: Budget, result: CollectResult,
                stats: Counter, counters: Counter, stop: list[str], items_out: list[NormalizedItem],
                logs_out: list[Any]) -> None:
        now = self._now()
        body = out.json if isinstance(out.json, dict) else None
        request_id = (body or {}).get("requestId") or out.headers.get("x-request-id") or out.headers.get("request-id")
        if out.status is not None and 200 <= out.status < 300 and body is not None:
            items = self.map_results(body, q, now=now, stats=stats)
            items_out.extend(items)
            cost = as_float(dig(body, "costDollars.total"))
            if cost is None:
                cost = as_float(body.get("costDollars")) or 0.0
            result.cost_usd += cost
            counters["ok"] += 1
            n_results = len(body.get("results") or []) if isinstance(body.get("results"), list) else 0
            logs_out.append(log_entry(q, provider=PROVIDER, endpoint="/search", status="ok" if n_results else "empty",
                                      http_status=out.status, items=len(items), credits=est, cost_usd=cost,
                                      latency_ms=out.latency_ms, request_id=request_id, at=now))
            return
        budget.adjust(PROVIDER, est, 0.0)
        counters["failures"] += 1
        status, et = "error", out.error_type or (f"http_{out.status}" if out.status else "network")
        if out.status in (401, 403):
            if not stop:
                stop.append("auth")
            et = "auth"
            logger.warning("exa: authentication failed; stopping Exa for this scan")
            result.errors.append("Exa rejected the API key; check EXA_API_KEY")
        elif out.status == 402:
            if not stop:
                stop.append("credits")
            et = "out_of_credits"
            status = "deferred"
            result.deferred.append(q)
            result.errors.append("Exa reports insufficient credits; queries deferred")
        elif out.status is None or out.status >= 500 or out.status == 429:
            status = "deferred"
            result.deferred.append(q)
            result.errors.append(f"Exa search: {et} after retries; query deferred")
        else:
            result.errors.append(f"Exa search: {et}")
        logs_out.append(log_entry(q, provider=PROVIDER, endpoint="/search", status=status, http_status=out.status,
                                  latency_ms=out.latency_ms, error_type=et, request_id=request_id, at=now))

    def _health_from_run(self, stop: list[str], counters: Counter, n: int) -> Optional[ProviderHealth]:
        now = self._now()
        if stop and stop[0] == "auth":
            return ProviderHealth(provider=PROVIDER, status="unhealthy",
                                  message="Exa rejected the API key; check EXA_API_KEY", checked_at=now)
        if stop and stop[0] == "credits":
            return ProviderHealth(provider=PROVIDER, status="unhealthy", message="Exa reports insufficient credits",
                                  checked_at=now)
        ok, bad = counters["ok"], counters["failures"]
        if ok == 0 and bad == 0:
            return None if n == 0 else self._last_health
        if ok == 0:
            return ProviderHealth(provider=PROVIDER, status="degraded",
                                  message=f"all {bad} Exa request(s) failed this scan", checked_at=now)
        return ProviderHealth(provider=PROVIDER, status="degraded" if bad > ok else "ok",
                              message=f"{ok} request(s) ok" + (f", {bad} failed" if bad else ""), checked_at=now)

    async def health(self) -> ProviderHealth:
        """Disabled without a key. Exa has no free status/credit endpoint known to this app [verify], so the
        result of the last scan is reported, or 'unknown' before the first scan."""
        now = self._now()
        if not self._api_key:
            return ProviderHealth(provider=PROVIDER, status="disabled", message="EXA_API_KEY is not set",
                                  checked_at=now)
        if self._last_health is not None:
            return self._last_health
        return ProviderHealth(provider=PROVIDER, status="unknown",
                              message="key set; no free status endpoint, checked on each scan", checked_at=now)
