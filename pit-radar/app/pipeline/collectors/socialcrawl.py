"""SocialCrawl collector with runtime endpoint discovery (docs/04 §2). STUB - collectors builder."""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable, Optional

import httpx

from app.pipeline.collectors.base import Budget
from app.pipeline.sources import SourcesConfig
from app.pipeline.types import CollectResult, PlannedQuery, ProviderHealth


class SocialCrawlCollector:
    name = "socialcrawl"

    def __init__(self, *, api_key: str, base_url: str, sources: SourcesConfig, http: httpx.AsyncClient,
                 resolved_endpoints: Optional[dict[str, Any]] = None, max_in_flight: int = 10,
                 now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)):
        raise NotImplementedError

    async def discover(self) -> dict[str, Any]:
        """Resolve each capability x platform in sources.yaml to an endpoint via the FREE catalog
        (/v1/utility/endpoints?search=, /v1/utility/endpoint?id=). Result shape (persisted by the runner):
        {"resolved_at": iso, "capabilities": {cap: {platform: {"id", "path", "method", "credits",
          "params": [{"name","required","type","description"}], "query_param", "sort_param", "sort_value",
          "subreddit_param", "post_param", "cursor_param", "pinned": bool}}}, "unresolved": [..]}"""
        raise NotImplementedError

    async def run(self, queries: list[PlannedQuery], budget: Budget) -> CollectResult:
        """Execute planned SocialCrawl queries (<= max_in_flight concurrent), branch on `success` then
        `error.type` (docs/04 §2.1), read credits_used/credits_remaining, map items with a table-driven,
        per-platform adapter to NormalizedItem via normalize.build_item (dropping ALL author fields)."""
        raise NotImplementedError

    async def health(self) -> ProviderHealth:
        """GET /v1/credits/balance (free)."""
        raise NotImplementedError
