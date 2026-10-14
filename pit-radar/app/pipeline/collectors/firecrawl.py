"""Firecrawl collector (docs/04 §4). STUB - collectors builder."""
from __future__ import annotations

import datetime as dt
from typing import Callable, Optional

import httpx

from app.pipeline.collectors.base import Budget
from app.pipeline.sources import SourcesConfig
from app.pipeline.types import CollectResult, PlannedQuery, ProviderHealth


class FirecrawlCollector:
    name = "firecrawl"

    def __init__(self, *, api_key: str, base_url: str, sources: SourcesConfig, http: httpx.AsyncClient,
                 prefilter_title: Optional[Callable[[str], bool]] = None, backfill: bool = False,
                 now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)):
        """prefilter_title(title+description) decides which undated web results get a /v2/scrape for their date."""
        raise NotImplementedError

    async def run(self, queries: list[PlannedQuery], budget: Budget) -> CollectResult:
        raise NotImplementedError

    async def health(self) -> ProviderHealth:
        raise NotImplementedError
