"""Firecrawl collector (docs/04 §4).

Verified on Oct 14, 2026 against docs.firecrawl.dev (read through a docs fetch, not the API):
  POST {base}/v2/search, Authorization: Bearer <key>, body {query, limit, tbs, sources, location};
  response {success, data: {web: [{url, title, description, ...}], news: [{title, snippet, url, date, ...}]},
  creditsUsed}. GET {base}/v2/team/credit-usage -> {success, data: {remainingCredits, planCredits, ...}}.
  POST {base}/v2/scrape {url, formats: ["markdown"], onlyMainContent: true} -> data.markdown, data.metadata.
  [verify] the page-metadata key names that carry the publish date (publishedTime, article:published_time,
  datePublished, ...): several are tried in order.

Undated web results whose title + description pass `prefilter_title` get one /v2/scrape for their date
(each scrape is one request against the firecrawl budget, at most firecrawl.scrape_for_date.max_per_scan per
scan). No date means discard (F3). News dates may be relative ("3 days ago") -> date_source "relative".
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections import Counter
from typing import Any, Callable, Optional

import httpx

from app.pipeline.collectors._common import (
    as_float, dig, first_str, is_http_url, is_profile_url, log_entry, sort_by_priority,
)
from app.pipeline.collectors.base import Budget, HttpOutcome, request_json
from app.pipeline.sources import SourcesConfig, host_matches, host_of, scope_max
from app.pipeline.types import CollectResult, ItemContext, NormalizedItem, PlannedQuery, ProviderHealth

logger = logging.getLogger(__name__)

PROVIDER = "firecrawl"
SEARCH_TIMEOUT_S = 60.0
SCRAPE_TIMEOUT_S = 60.0
MAX_CONCURRENCY = 3
SCRAPE_BODY_CHARS = 12000
METADATA_DATE_KEYS = (
    "publishedTime", "article:published_time", "article_published_time", "datePublished", "date_published",
    "og:published_time", "og:article:published_time", "published_time", "publishDate", "publish_date",
    "pubdate", "parsely-pub-date", "sailthru.date", "dc.date", "dcterms.date", "DC.date.issued", "date",
)


def _meta_value(v: Any) -> Optional[str]:
    if isinstance(v, str) and v.strip():
        return v.strip()[:200]
    if isinstance(v, (list, tuple)):
        for x in v:
            if isinstance(x, str) and x.strip():
                return x.strip()[:200]
    return None


def metadata_date(metadata: Any) -> Optional[str]:
    """First publish-date string found in scraped page metadata (keys tried in METADATA_DATE_KEYS order)."""
    if not isinstance(metadata, dict):
        return None
    lowered = {str(k).lower(): v for k, v in metadata.items()}
    for k in METADATA_DATE_KEYS:
        v = _meta_value(metadata.get(k))
        if v is None:
            v = _meta_value(lowered.get(k.lower()))
        if v:
            return v
    return None


class FirecrawlCollector:
    name = "firecrawl"

    def __init__(self, *, api_key: str, base_url: str, sources: SourcesConfig, http: httpx.AsyncClient,
                 prefilter_title: Optional[Callable[[str], bool]] = None, backfill: bool = False,
                 now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)):
        """prefilter_title(title+description) decides which undated web results get a /v2/scrape for their date."""
        self._api_key = (api_key or "").strip()
        self._base = (base_url or str(sources.firecrawl().get("base_url") or "https://api.firecrawl.dev")).rstrip("/")
        self._sources = sources
        self._http = http
        self._prefilter_title = prefilter_title
        self._backfill = bool(backfill)
        self._now = now
        self._last_health: Optional[ProviderHealth] = None
        self.last_stats: dict[str, int] = {}

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json",
                "Accept": "application/json"}

    def build_body(self, q: PlannedQuery) -> dict[str, Any]:
        cfg = self._sources.firecrawl_search()
        try:
            limit = int(q.params.get("limit") or cfg.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        tbs = cfg.get("tbs_backfill") if self._backfill else cfg.get("tbs")
        sources = [str(s) for s in (cfg.get("sources") or ["web", "news"]) if str(s) in ("web", "news")]
        body: dict[str, Any] = {"query": q.query, "limit": max(1, min(100, limit)),
                                "sources": sources or ["web", "news"]}
        if tbs:
            body["tbs"] = str(tbs)
        if cfg.get("location"):
            body["location"] = str(cfg["location"])
        return body

    def _platform_for(self, url: str, *, from_news: bool) -> str:
        h = host_of(url) or ""
        if host_matches(h, "reddit.com"):
            return "reddit"
        if from_news or self._sources.is_news_url(url):
            return "news"
        return "web"

    def _passes_prefilter(self, title: str, description: str) -> bool:
        if self._prefilter_title is None:
            return True
        try:
            return bool(self._prefilter_title(f"{title}\n{description}".strip()))
        except Exception as exc:  # noqa: BLE001 - a broken callback must not stop the scan
            logger.warning("firecrawl: prefilter_title failed (%s); not scraping", type(exc).__name__)
            return False

    def _item(self, *, q: PlannedQuery, url: str, title: str, body: str, published_at: dt.datetime,
              date_source: str, platform: str, now: dt.datetime) -> Optional[NormalizedItem]:
        from app.pipeline.normalize import build_item
        from app.pipeline.sources import subreddit_from_url

        sub = subreddit_from_url(url) if platform == "reddit" else None
        ctx = ItemContext(
            query=(q.query or "")[:500],
            source_scope=scope_max(q.source_scope, self._sources.scope_for(url=url, subreddit=sub)),
            query_place_id=q.place_id, query_place_name=q.place_name, subreddit=sub,
        )
        return build_item(provider=PROVIDER, platform=platform, url=url, platform_post_id="",
                          published_at=published_at, date_source=date_source, collected_at=now,  # type: ignore[arg-type]
                          title=title, body=body, context=ctx, media_present=False)

    def _usable_url(self, url: str, platform: str, stats: Counter) -> bool:
        if not is_http_url(url):
            stats["no_url"] += 1
            return False
        if platform == "reddit":
            from app.pipeline.sources import subreddit_from_url
            if is_profile_url("reddit", url):
                stats["profile_url"] += 1
                return False
            sub = subreddit_from_url(url)
            if sub and self._sources.is_excluded_subreddit(sub):
                stats["excluded_subreddit"] += 1
                return False
        return True

    async def run(self, queries: list[PlannedQuery], budget: Budget) -> CollectResult:
        result = CollectResult()
        qs = sort_by_priority(q for q in queries if q.provider == PROVIDER)
        now = self._now()
        if not self._api_key:
            if qs:
                result.errors.append(f"FIRECRAWL_API_KEY is not set; {len(qs)} Firecrawl queries skipped")
            result.health = ProviderHealth(provider=PROVIDER, status="disabled",
                                           message="FIRECRAWL_API_KEY is not set", checked_at=now)
            self._last_health = result.health
            return result
        stats: Counter = Counter()
        counters: Counter = Counter()
        stop: list[str] = []
        sem = asyncio.Semaphore(MAX_CONCURRENCY)
        slots: list[tuple[list[NormalizedItem], list[Any], list[tuple[str, str, str, str]]]] = [([], [], []) for _ in qs]

        async def one(i: int, q: PlannedQuery) -> None:
            items_out, logs_out, undated = slots[i]
            try:
                async with sem:
                    if stop:
                        if stop[0] == "credits":
                            result.deferred.append(q)
                            logs_out.append(log_entry(q, provider=PROVIDER, endpoint="/v2/search",
                                                      status="deferred", error_type="out_of_credits",
                                                      at=self._now()))
                        else:
                            counters["skipped"] += 1
                        return
                    est = float(q.est_cost or 1.0)
                    if not budget.try_charge(PROVIDER, est):
                        result.deferred.append(q)
                        logs_out.append(log_entry(q, provider=PROVIDER, endpoint="/v2/search", status="deferred",
                                                  error_type="budget", at=self._now()))
                        return
                    out = await request_json(self._http, "POST", f"{self._base}/v2/search",
                                             headers=self._headers(), json_body=self.build_body(q),
                                             timeout=SEARCH_TIMEOUT_S)
                result.requests += 1
                self._handle_search(q, est, out, budget, result, stats, counters, stop, items_out, logs_out, undated)
            except Exception as exc:  # noqa: BLE001
                logger.warning("firecrawl: query failed unexpectedly (%s)", type(exc).__name__)
                counters["failures"] += 1
                logs_out.append(log_entry(q, provider=PROVIDER, endpoint="/v2/search", status="error",
                                          error_type=f"internal:{type(exc).__name__}", at=self._now()))

        await asyncio.gather(*(one(i, q) for i, q in enumerate(qs)))

        # Scrape-for-date pass over undated web results, in query priority order, bounded and budgeted.
        max_scrapes = self._sources.firecrawl_scrape_max_per_scan()
        seen_urls: set[str] = set()
        scrape_jobs: list[tuple[int, PlannedQuery, str, str, str, str]] = []
        for i, q in enumerate(qs):
            for url, title, desc, platform in slots[i][2]:
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                if len(scrape_jobs) >= max_scrapes:
                    stats["no_date_scrape_cap"] += 1
                    continue
                scrape_jobs.append((i, q, url, title, desc, platform))
        if scrape_jobs and not stop:
            await self._scrape_all(scrape_jobs, budget, result, stats, counters, stop, slots)
        elif scrape_jobs:
            stats["no_date_provider_stopped"] += len(scrape_jobs)

        for items, logs, _ in slots:
            result.items.extend(items)
            result.logs.extend(logs)
        if counters["skipped"]:
            result.errors.append(f"{counters['skipped']} Firecrawl queries skipped after the provider stopped "
                                 f"({stop[0]})")
        self.last_stats = dict(stats)
        if stats:
            logger.info("firecrawl: %d item(s) mapped, %d scrape(s); dropped %s", stats.get("mapped", 0),
                        counters.get("scrapes", 0), {k: v for k, v in stats.items() if k != "mapped"} or "none")
        result.health = self._health_from_run(stop, counters, len(qs))
        if result.health is not None:
            self._last_health = result.health
        return result

    def _handle_search(self, q: PlannedQuery, est: float, out: HttpOutcome, budget: Budget, result: CollectResult,
                       stats: Counter, counters: Counter, stop: list[str], items_out: list[NormalizedItem],
                       logs_out: list[Any], undated: list[tuple[str, str, str, str]]) -> None:
        from app.pipeline.normalize import parse_datetime

        now = self._now()
        body = out.json if isinstance(out.json, dict) else None
        ok = out.status is not None and 200 <= out.status < 300 and body is not None and body.get("success") is not False
        request_id = (body or {}).get("id") if ok else None
        if not ok:
            self._failure(q, est, out, budget, result, counters, stop, logs_out, endpoint="/v2/search")
            return
        counters["ok"] += 1
        data = body.get("data")
        web = data.get("web") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        news = data.get("news") if isinstance(data, dict) else []
        web = web if isinstance(web, list) else []
        news = news if isinstance(news, list) else []
        n_mapped = 0
        for r in news:
            if not isinstance(r, dict):
                continue
            url = first_str(r, ("url", "link"))
            platform = self._platform_for(url, from_news=True)
            if not self._usable_url(url, platform, stats):
                continue
            raw_date = first_str(r, ("date", "publishedDate", "published_at", "metadata.publishedTime"))
            published_at, src = parse_datetime(raw_date, now=now) if raw_date else (None, None)
            if published_at is None:
                stats["no_date"] += 1
                continue
            item = self._item(q=q, url=url, title=first_str(r, ("title",), max_len=2000),
                              body=first_str(r, ("snippet", "description", "markdown")),
                              published_at=published_at, date_source="relative" if src == "relative" else "provider",
                              platform=platform, now=now)
            if item is None:
                stats["empty_text"] += 1
                continue
            stats["mapped"] += 1
            n_mapped += 1
            items_out.append(item)
        for r in web:
            if not isinstance(r, dict):
                continue
            url = first_str(r, ("url", "link"))
            platform = self._platform_for(url, from_news=False)
            if not self._usable_url(url, platform, stats):
                continue
            title = first_str(r, ("title", "metadata.title"), max_len=2000)
            desc = first_str(r, ("description", "snippet", "metadata.description"))
            raw_date = first_str(r, ("date", "publishedDate", "published_at")) or metadata_date(r.get("metadata"))
            published_at, src = parse_datetime(raw_date, now=now) if raw_date else (None, None)
            if published_at is None:
                if self._passes_prefilter(title, desc):
                    undated.append((url, title, desc, platform))
                else:
                    stats["no_date_not_promising"] += 1
                continue
            item = self._item(q=q, url=url, title=title, body=desc, published_at=published_at,
                              date_source="relative" if src == "relative" else "provider", platform=platform, now=now)
            if item is None:
                stats["empty_text"] += 1
                continue
            stats["mapped"] += 1
            n_mapped += 1
            items_out.append(item)
        credits = as_float(body.get("creditsUsed")) or 0.0
        logs_out.append(log_entry(q, provider=PROVIDER, endpoint="/v2/search",
                                  status="ok" if (web or news) else "empty", http_status=out.status, items=n_mapped,
                                  credits=credits, latency_ms=out.latency_ms, request_id=request_id, at=now))

    def _failure(self, q: PlannedQuery, est: float, out: HttpOutcome, budget: Budget, result: CollectResult,
                 counters: Counter, stop: list[str], logs_out: list[Any], *, endpoint: str) -> None:
        budget.adjust(PROVIDER, est, 0.0)
        counters["failures"] += 1
        status, et = "error", out.error_type or (f"http_{out.status}" if out.status else "network")
        if out.status in (401, 403):
            if not stop:
                stop.append("auth")
            et = "auth"
            logger.warning("firecrawl: authentication failed; stopping Firecrawl for this scan")
            result.errors.append("Firecrawl rejected the API key; check FIRECRAWL_API_KEY")
        elif out.status == 402:
            if not stop:
                stop.append("credits")
            et = "out_of_credits"
            if endpoint == "/v2/search":
                status = "deferred"
                result.deferred.append(q)
            result.errors.append("Firecrawl reports insufficient credits")
        elif endpoint == "/v2/search" and (out.status is None or out.status >= 500 or out.status in (408, 429)):
            status = "deferred"
            result.deferred.append(q)
            result.errors.append(f"Firecrawl search: {et} after retries; query deferred")
        else:
            result.errors.append(f"Firecrawl {endpoint}: {et}")
        logs_out.append(log_entry(q, provider=PROVIDER, endpoint=endpoint, status=status, http_status=out.status,
                                  latency_ms=out.latency_ms, error_type=et, at=self._now()))

    async def _scrape_all(self, jobs: list[tuple[int, PlannedQuery, str, str, str, str]], budget: Budget,
                          result: CollectResult, stats: Counter, counters: Counter, stop: list[str],
                          slots: list[tuple[list[NormalizedItem], list[Any], list[tuple[str, str, str, str]]]]) -> None:
        from app.pipeline.normalize import parse_datetime

        sem = asyncio.Semaphore(MAX_CONCURRENCY)
        scfg = self._sources.firecrawl_scrape()
        formats = [str(f) for f in (scfg.get("formats") or ["markdown"])]
        only_main = bool(scfg.get("only_main_content", True))

        async def scrape(job: tuple[int, PlannedQuery, str, str, str, str]) -> None:
            i, q, url, title, desc, platform = job
            items_out, logs_out, _ = slots[i]
            try:
                async with sem:
                    if stop:
                        stats["no_date_provider_stopped"] += 1
                        return
                    if not budget.try_charge(PROVIDER, 1.0):
                        stats["no_date_scrape_budget"] += 1
                        return
                    out = await request_json(self._http, "POST", f"{self._base}/v2/scrape", headers=self._headers(),
                                             json_body={"url": url, "formats": formats, "onlyMainContent": only_main},
                                             timeout=SCRAPE_TIMEOUT_S, max_attempts=2)
                result.requests += 1
                counters["scrapes"] += 1
                now = self._now()
                body = out.json if isinstance(out.json, dict) else None
                if not (out.status is not None and 200 <= out.status < 300 and body is not None
                        and body.get("success") is not False):
                    self._failure(q, 1.0, out, budget, result, counters, stop, logs_out, endpoint="/v2/scrape")
                    stats["no_date_scrape_failed"] += 1
                    return
                data = body.get("data") if isinstance(body.get("data"), dict) else {}
                raw_date = metadata_date(data.get("metadata"))
                published_at, src = parse_datetime(raw_date, now=now) if raw_date else (None, None)
                credits = as_float(dig(body, "data.metadata.creditsUsed")) or as_float(body.get("creditsUsed")) or 1.0
                if published_at is None:
                    stats["no_date"] += 1
                    logs_out.append(log_entry(q, provider=PROVIDER, endpoint="/v2/scrape", status="empty",
                                              http_status=out.status, credits=credits, latency_ms=out.latency_ms,
                                              error_type="no_publish_date", at=now))
                    return
                markdown = data.get("markdown") if isinstance(data.get("markdown"), str) else ""
                body_text = markdown[:SCRAPE_BODY_CHARS] if markdown else desc
                item = self._item(q=q, url=url, title=title or first_str(data, ("metadata.title",), max_len=2000),
                                  body=body_text, published_at=published_at,
                                  date_source="relative" if src == "relative" else "page_metadata",
                                  platform=platform, now=now)
                logs_out.append(log_entry(q, provider=PROVIDER, endpoint="/v2/scrape", status="ok",
                                          http_status=out.status, items=1 if item else 0, credits=credits,
                                          latency_ms=out.latency_ms, at=now))
                if item is None:
                    stats["empty_text"] += 1
                    return
                stats["mapped"] += 1
                items_out.append(item)
            except Exception as exc:  # noqa: BLE001
                logger.warning("firecrawl: scrape failed unexpectedly (%s)", type(exc).__name__)
                stats["no_date_scrape_failed"] += 1

        await asyncio.gather(*(scrape(j) for j in jobs))

    def _health_from_run(self, stop: list[str], counters: Counter, n: int) -> Optional[ProviderHealth]:
        now = self._now()
        if stop and stop[0] == "auth":
            return ProviderHealth(provider=PROVIDER, status="unhealthy",
                                  message="Firecrawl rejected the API key; check FIRECRAWL_API_KEY", checked_at=now)
        if stop and stop[0] == "credits":
            return ProviderHealth(provider=PROVIDER, status="unhealthy",
                                  message="Firecrawl reports insufficient credits", credits_remaining=0.0,
                                  checked_at=now)
        ok, bad = counters["ok"], counters["failures"]
        if ok == 0 and bad == 0:
            return None if n == 0 else self._last_health
        if ok == 0:
            return ProviderHealth(provider=PROVIDER, status="degraded",
                                  message=f"all {bad} Firecrawl request(s) failed this scan", checked_at=now)
        return ProviderHealth(provider=PROVIDER, status="degraded" if bad > ok else "ok",
                              message=f"{ok} search(es) ok" + (f", {bad} failed" if bad else ""), checked_at=now)

    async def health(self) -> ProviderHealth:
        """Disabled without a key; else GET /v2/team/credit-usage (verified in the docs) -> remainingCredits."""
        now = self._now()
        if not self._api_key:
            return ProviderHealth(provider=PROVIDER, status="disabled", message="FIRECRAWL_API_KEY is not set",
                                  checked_at=now)
        try:
            out = await request_json(self._http, "GET", f"{self._base}/v2/team/credit-usage", headers=self._headers(),
                                     timeout=15.0, max_attempts=2)
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(provider=PROVIDER, status="unknown",
                                  message=f"credit check failed ({type(exc).__name__})", checked_at=now)
        body = out.json if isinstance(out.json, dict) else None
        if out.status in (401, 403):
            h = ProviderHealth(provider=PROVIDER, status="unhealthy",
                               message="Firecrawl rejected the API key; check FIRECRAWL_API_KEY", checked_at=now)
        elif out.status is not None and 200 <= out.status < 300 and body is not None and body.get("success") is not False:
            remaining = None
            for path in ("data.remainingCredits", "data.remaining_credits", "remainingCredits", "remaining_credits"):
                remaining = as_float(dig(body, path))
                if remaining is not None:
                    break
            if remaining is not None and remaining <= 0:
                h = ProviderHealth(provider=PROVIDER, status="unhealthy", message="Firecrawl is out of credits",
                                   credits_remaining=remaining, checked_at=now)
            else:
                msg = f"{remaining:g} credits remaining" if remaining is not None else "reachable"
                h = ProviderHealth(provider=PROVIDER, status="ok", message=msg, credits_remaining=remaining,
                                   checked_at=now)
        else:
            if self._last_health is not None:
                return self._last_health
            et = out.error_type or (f"http_{out.status}" if out.status else "network")
            h = ProviderHealth(provider=PROVIDER, status="unknown", message=f"credit check failed ({et})",
                               checked_at=now)
        self._last_health = h
        return h
