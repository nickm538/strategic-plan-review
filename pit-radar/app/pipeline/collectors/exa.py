"""Exa collector (docs/04 §3). STUB - collectors builder."""
from __future__ import annotations

import datetime as dt
from typing import Callable

import httpx

from app.pipeline.collectors.base import Budget
from app.pipeline.sources import SourcesConfig
from app.pipeline.types import CollectResult, PlannedQuery, ProviderHealth


class ExaCollector:
    name = "exa"

    def __init__(self, *, api_key: str, base_url: str, sources: SourcesConfig, http: httpx.AsyncClient,
                 max_age_days: int, now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)):
        raise NotImplementedError

    async def run(self, queries: list[PlannedQuery], budget: Budget) -> CollectResult:
        raise NotImplementedError

    async def health(self) -> ProviderHealth:
        raise NotImplementedError
