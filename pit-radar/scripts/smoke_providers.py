"""Live smoke test of the data providers (docs/10 §2). Manual only: needs real keys and network.

Usage (from the pit-radar folder; keys come from the environment or pit-radar/.env):
    python scripts/smoke_providers.py --provider socialcrawl --limit 4
    python scripts/smoke_providers.py --provider all

What it prints (never post text, never author/user values, never keys):
  * SocialCrawl: credit balance, the endpoint discovery summary (resolved ids, cost, parameter roles, a
    sources.yaml snippet to pin them), then one call per resolved capability (up to --limit calls):
    HTTP status, envelope top-level keys, item FIELD NAMES, row/item counts, credits used.
  * Exa: one recency-limited search: response keys, result field names, how many have publishedDate, cost.
  * Firecrawl: one /v2/search with tbs and up to 3 /v2/scrape calls to check publish-date extraction.
Exit code: 0 ok, 1 a provider was skipped or failed, 2 a provider rejected its API key.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.pipeline.collectors._common import first_str  # noqa: E402
from app.pipeline.collectors._sc_adapters import extract_rows, unwrap  # noqa: E402
from app.pipeline.collectors.base import request_json  # noqa: E402
from app.pipeline.collectors.exa import ExaCollector  # noqa: E402
from app.pipeline.collectors.firecrawl import FirecrawlCollector, metadata_date  # noqa: E402
from app.pipeline.collectors.socialcrawl import AUTH_ERRORS, SocialCrawlCollector, error_type_of  # noqa: E402
from app.pipeline.sources import SourcesConfig  # noqa: E402
from app.pipeline.types import PlannedQuery  # noqa: E402

OK, FAILED, AUTH = 0, 1, 2
SMOKE_KEYWORD = 'homeless "Long Island"'
SMOKE_SEMANTIC = "homeless encampment in the woods on Long Island, New York"
MAX_FIELD_NAMES = 200


def field_names(obj: Any, prefix: str = "", *, depth: int = 0, max_depth: int = 6, out: Optional[set[str]] = None) -> set[str]:
    """Dotted key paths of a JSON value ('[]' marks list elements). Names only, never values."""
    out = set() if out is None else out
    if depth > max_depth or len(out) >= MAX_FIELD_NAMES:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            name = f"{prefix}.{k}" if prefix else str(k)
            out.add(name)
            field_names(v, name, depth=depth + 1, max_depth=max_depth, out=out)
    elif isinstance(obj, list):
        for v in obj[:5]:
            field_names(v, f"{prefix}[]", depth=depth + 1, max_depth=max_depth, out=out)
    return out


def _print_names(label: str, names: set[str]) -> None:
    shown = sorted(names)
    print(f"    {label} ({len(shown)}{'+' if len(shown) >= MAX_FIELD_NAMES else ''}):")
    for i in range(0, len(shown), 4):
        print("      " + "  ".join(shown[i:i + 4]))


def _sources() -> SourcesConfig:
    return SourcesConfig.load()


# ---------------------------------------------------------------------------
# SocialCrawl
# ---------------------------------------------------------------------------
async def smoke_socialcrawl(http: httpx.AsyncClient, limit: int) -> int:
    s = get_settings()
    print("\n== SocialCrawl ==")
    if not s.has_key("socialcrawl_api_key"):
        print("  skipped: SOCIALCRAWL_API_KEY is not set")
        return FAILED
    sources = _sources()
    col = SocialCrawlCollector(api_key=s.socialcrawl_api_key.get_secret_value(), base_url=s.socialcrawl_base_url,
                               sources=sources, http=http, max_in_flight=s.socialcrawl_max_in_flight)
    h = await col.health()
    print(f"  balance: {h.status} - {h.message}")
    if h.status == "unhealthy" and "API key" in h.message:
        return AUTH

    disc = await col.discover()
    caps = disc.get("capabilities") or {}
    print(f"  discovery: {sum(len(v) for v in caps.values())} resolved, {len(disc.get('unresolved') or [])} unresolved")
    for err in disc.get("errors") or []:
        print(f"    error: {err}")
        if "API key" in err:
            return AUTH
    for cap, plats in sorted(caps.items()):
        for slug, e in sorted(plats.items()):
            roles = {k: v for k, v in e.items() if k.endswith("_param") and v}
            vals = {k: e[k] for k in ("sort_value", "time_value") if e.get(k)}
            print(f"    {cap}/{slug}: {e['id']} ({e['credits']:g} cr{'' if e.get('credits_known') else ', cost not listed'})"
                  f" roles={roles} {vals or ''} params={[p['name'] for p in e.get('params') or []]}")
    for u in disc.get("unresolved") or []:
        print(f"    unresolved: {u}")
    if caps:
        print("  proposed pins for config/sources.yaml (review before pasting):")
        for cap, plats in sorted(caps.items()):
            for slug, e in sorted(plats.items()):
                print(f"    socialcrawl.capabilities.{cap}.platforms.{slug}.pinned_endpoint: {e['id']}")

    calls = 0
    status = OK
    total_credits = 0.0
    first_thread: Optional[str] = None
    order = [c for c in ("subreddit_new", "keyword_search", "subreddit_search", "news_search", "hashtag_posts",
                         "post_comments") if c in caps]
    for cap in order:
        for slug in sorted(caps[cap]):
            if calls >= limit:
                print(f"  stopping after {limit} live call(s) (--limit)")
                break
            subreddit = (sources.enabled_subreddits() or ["longisland"])[0]
            kwargs: dict[str, Any] = {}
            if cap in ("keyword_search", "news_search", "subreddit_search"):
                kwargs["query"] = SMOKE_KEYWORD if cap != "subreddit_search" else "homeless"
            if cap in ("subreddit_new", "subreddit_search"):
                kwargs["subreddit"] = subreddit
            if cap == "hashtag_posts":
                kwargs["query"] = "longisland"
            if cap == "post_comments":
                if not first_thread:
                    print(f"    {cap}/{slug}: skipped (no thread URL from an earlier call)")
                    continue
                kwargs["post_url"] = first_thread
            out, sent, reason = await col.probe(cap, slug, **kwargs)
            if out is None:
                print(f"    {cap}/{slug}: not called ({reason})")
                continue
            calls += 1
            body = out.json if isinstance(out.json, dict) else {}
            et = error_type_of(out) if not (out.status and 200 <= out.status < 300) else ""
            used = body.get("credits_used")
            total_credits += float(used or 0)
            print(f"    {cap}/{slug}: HTTP {out.status} {et} params={sorted(sent)} credits_used={used} "
                  f"credits_remaining={body.get('credits_remaining')} cached={body.get('cached')}")
            if et in AUTH_ERRORS:
                return AUTH
            if et:
                status = FAILED
                continue
            print(f"      top-level keys: {sorted(body)}")
            data = body.get("data")
            if isinstance(data, dict):
                print(f"      data keys: {sorted(data)}")
            rows = extract_rows(data)
            names: set[str] = set()
            for r in rows[:5]:
                field_names(unwrap(r), out=names)
            _print_names("item field names", names)
            q = PlannedQuery(provider="socialcrawl", capability=cap, query=kwargs.get("query", ""), platform=slug,
                             subreddit=kwargs.get("subreddit"),
                             params={"post_url": first_thread} if cap == "post_comments" and first_thread else {})
            stats: Counter = Counter()
            items = col.map_response(body, q, stats=stats)
            print(f"      rows={len(rows)} normalized_items={len(items)} drops={dict(stats)}")
            if not first_thread and slug == "reddit":
                first_thread = next((it.url for it in items if not it.context.is_comment), None)
    print(f"  live calls: {calls}, credits used: {total_credits:g}")
    return status


# ---------------------------------------------------------------------------
# Exa
# ---------------------------------------------------------------------------
async def smoke_exa(http: httpx.AsyncClient, limit: int) -> int:
    s = get_settings()
    print("\n== Exa ==")
    if not s.has_key("exa_api_key"):
        print("  skipped: EXA_API_KEY is not set")
        return FAILED
    col = ExaCollector(api_key=s.exa_api_key.get_secret_value(), base_url=s.exa_base_url, sources=_sources(),
                       http=http, max_age_days=s.ingest_max_age_days)
    q = PlannedQuery(provider="exa", capability="search", query=SMOKE_SEMANTIC, params={"num_results": max(1, min(limit, 10))})
    now = dt.datetime.now(dt.timezone.utc)
    out = await request_json(http, "POST", f"{s.exa_base_url.rstrip('/')}/search", headers=col._headers(),
                             json_body=col.build_body(q, now=now), timeout=45.0)
    print(f"  /search: HTTP {out.status} {out.error_type or ''}")
    if out.status in (401, 403):
        return AUTH
    body = out.json if isinstance(out.json, dict) else {}
    if not (out.status and 200 <= out.status < 300):
        return FAILED
    results = body.get("results") if isinstance(body.get("results"), list) else []
    print(f"  top-level keys: {sorted(body)}")
    names: set[str] = set()
    for r in results[:5]:
        field_names(r, out=names)
    _print_names("result field names", names)
    dated = sum(1 for r in results if isinstance(r, dict) and r.get("publishedDate"))
    stats: Counter = Counter()
    items = col.map_results(body, q, now=now, stats=stats)
    print(f"  results={len(results)} with_publishedDate={dated} normalized_items={len(items)} drops={dict(stats)}")
    print(f"  costDollars={body.get('costDollars')}")
    return OK


# ---------------------------------------------------------------------------
# Firecrawl
# ---------------------------------------------------------------------------
async def smoke_firecrawl(http: httpx.AsyncClient, limit: int) -> int:
    s = get_settings()
    print("\n== Firecrawl ==")
    if not s.has_key("firecrawl_api_key"):
        print("  skipped: FIRECRAWL_API_KEY is not set")
        return FAILED
    sources = _sources()
    col = FirecrawlCollector(api_key=s.firecrawl_api_key.get_secret_value(), base_url=s.firecrawl_base_url,
                             sources=sources, http=http)
    h = await col.health()
    print(f"  credit usage: {h.status} - {h.message}")
    if h.status == "unhealthy" and "API key" in h.message:
        return AUTH
    base = s.firecrawl_base_url.rstrip("/")
    q = PlannedQuery(provider="firecrawl", capability="search", query=SMOKE_KEYWORD, params={"limit": max(1, min(limit, 10))})
    out = await request_json(http, "POST", f"{base}/v2/search", headers=col._headers(), json_body=col.build_body(q),
                             timeout=60.0)
    print(f"  /v2/search: HTTP {out.status} {out.error_type or ''}")
    if out.status in (401, 403):
        return AUTH
    body = out.json if isinstance(out.json, dict) else {}
    if not (out.status and 200 <= out.status < 300):
        return FAILED
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    web = data.get("web") if isinstance(data.get("web"), list) else []
    news = data.get("news") if isinstance(data.get("news"), list) else []
    print(f"  top-level keys: {sorted(body)}  data keys: {sorted(data)}  creditsUsed={body.get('creditsUsed')}")
    for label, rows in (("web", web), ("news", news)):
        names: set[str] = set()
        for r in rows[:5]:
            field_names(r, out=names)
        _print_names(f"{label} field names", names)
    print(f"  web={len(web)} news={len(news)} news_with_date={sum(1 for r in news if isinstance(r, dict) and r.get('date'))}")
    urls = [first_str(r, ("url",)) for r in news + web if isinstance(r, dict)]
    news_urls = [u for u in urls if u and sources.is_news_url(u)] or [u for u in urls if u]
    for u in news_urls[:3]:
        so = await request_json(http, "POST", f"{base}/v2/scrape", headers=col._headers(),
                                json_body={"url": u, "formats": ["markdown"], "onlyMainContent": True},
                                timeout=60.0, max_attempts=2)
        sj = so.json if isinstance(so.json, dict) else {}
        sd = sj.get("data") if isinstance(sj.get("data"), dict) else {}
        found = metadata_date(sd.get("metadata"))
        host = u.split("/")[2] if "://" in u else u
        print(f"  /v2/scrape {host}: HTTP {so.status} publish date {'found: ' + found if found else 'NOT found'}")
    return OK


async def _amain(provider: str, limit: int) -> int:
    codes: list[int] = []
    async with httpx.AsyncClient(timeout=60.0, headers={"User-Agent": "pit-radar-smoke/1.0"}) as http:
        if provider in ("socialcrawl", "all"):
            codes.append(await smoke_socialcrawl(http, limit))
        if provider in ("exa", "all"):
            codes.append(await smoke_exa(http, limit))
        if provider in ("firecrawl", "all"):
            codes.append(await smoke_firecrawl(http, limit))
    if AUTH in codes:
        return AUTH
    return FAILED if FAILED in codes else OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="smoke_providers.py", description="Live smoke test of SocialCrawl, Exa, Firecrawl.")
    p.add_argument("--provider", choices=("socialcrawl", "exa", "firecrawl", "all"), default="all")
    p.add_argument("--limit", type=int, default=4,
                   help="max billed SocialCrawl calls, and results per Exa/Firecrawl search (default 4)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    code = asyncio.run(_amain(args.provider, max(1, args.limit)))
    print(f"\nexit code {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
