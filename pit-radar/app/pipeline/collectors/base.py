"""Collector protocol, budget accounting and a shared HTTP helper (docs/04, docs/02 §5).

Collectors are DB-free: they take planned queries and an httpx.AsyncClient and return a CollectResult.
The runner persists logs, usage and items.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

import httpx

from app.pipeline.types import CollectResult, PlannedQuery, ProviderHealth


class Collector(Protocol):
    name: str

    async def run(self, queries: list[PlannedQuery], budget: "Budget") -> CollectResult: ...

    async def health(self) -> ProviderHealth: ...


@dataclass
class Budget:
    """Per-scan and per-day caps for one scan (docs/04 §6). Unit: SocialCrawl credits, else requests.

    `spent_today` is loaded from api_usage at scan start. Collectors call `try_charge` BEFORE a request
    with the estimated cost, then `adjust` with the actual cost from the response (refunds included).
    """

    per_scan: dict[str, float]                     # provider -> cap for this scan
    per_day: dict[str, float] = field(default_factory=dict)   # provider -> daily cap (missing = no daily cap)
    spent_today: dict[str, float] = field(default_factory=dict)
    spent_scan: dict[str, float] = field(default_factory=dict)
    exhausted: set[str] = field(default_factory=set)

    def remaining(self, provider: str) -> float:
        scan_left = self.per_scan.get(provider, 0.0) - self.spent_scan.get(provider, 0.0)
        if provider in self.per_day:
            day_left = self.per_day[provider] - self.spent_today.get(provider, 0.0)
            return max(0.0, min(scan_left, day_left))
        return max(0.0, scan_left)

    def try_charge(self, provider: str, amount: float) -> bool:
        if provider in self.exhausted or amount > self.remaining(provider) + 1e-9:
            self.exhausted.add(provider)
            return False
        self.spent_scan[provider] = self.spent_scan.get(provider, 0.0) + amount
        self.spent_today[provider] = self.spent_today.get(provider, 0.0) + amount
        return True

    def adjust(self, provider: str, estimated: float, actual: float) -> None:
        delta = actual - estimated
        self.spent_scan[provider] = max(0.0, self.spent_scan.get(provider, 0.0) + delta)
        self.spent_today[provider] = max(0.0, self.spent_today.get(provider, 0.0) + delta)


class ProviderAuthError(Exception):
    """401/403: bad or missing key. Stop this provider for the scan and mark it unhealthy."""


class ProviderCreditsError(Exception):
    """402 / out of credits. Stop this provider and mark it unhealthy."""


@dataclass
class HttpOutcome:
    status: Optional[int]
    json: Any = None
    text: str = ""
    latency_ms: int = 0
    error_type: Optional[str] = None     # timeout | connect | http_<code> | invalid_json
    retry_after: Optional[float] = None
    headers: dict[str, str] = field(default_factory=dict)


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    timeout: float = 30.0,
    max_attempts: int = 3,
    retry_statuses: tuple[int, ...] = (408, 425, 429, 500, 502, 503, 504),
) -> HttpOutcome:
    """HTTP request with exponential backoff + jitter on 408/425/429/5xx/timeouts (honors Retry-After).

    Never raises for HTTP errors; returns an HttpOutcome. Never logs headers (they carry API keys).
    """
    attempt = 0
    last = HttpOutcome(status=None, error_type="not_attempted")
    while attempt < max_attempts:
        attempt += 1
        t0 = time.monotonic()
        try:
            resp = await client.request(method, url, headers=headers, params=params, json=json_body, timeout=timeout)
        except httpx.TimeoutException:
            last = HttpOutcome(status=None, error_type="timeout", latency_ms=int((time.monotonic() - t0) * 1000))
        except httpx.HTTPError as e:
            last = HttpOutcome(status=None, error_type=f"connect:{type(e).__name__}",
                               latency_ms=int((time.monotonic() - t0) * 1000))
        else:
            latency = int((time.monotonic() - t0) * 1000)
            ra = _retry_after(resp.headers.get("retry-after"))
            body: Any = None
            err = None
            try:
                body = resp.json() if resp.content else None
            except ValueError:
                err = "invalid_json"
            if resp.status_code >= 400:
                err = f"http_{resp.status_code}"
            last = HttpOutcome(status=resp.status_code, json=body, text="" if body is not None else resp.text[:500],
                               latency_ms=latency, error_type=err, retry_after=ra,
                               headers={k.lower(): v for k, v in resp.headers.items() if k.lower() in ("x-request-id", "request-id", "retry-after")})
            if resp.status_code not in retry_statuses:
                return last
        if attempt < max_attempts:
            delay = last.retry_after if last.retry_after is not None else min(20.0, 1.5 * 2 ** (attempt - 1))
            await asyncio.sleep(delay + random.uniform(0, 0.5))
    return last


def _retry_after(v: str | None) -> Optional[float]:
    if not v:
        return None
    try:
        return max(0.0, min(60.0, float(v)))
    except ValueError:
        return None
