"""Bounded Nominatim fallback geocoder (docs/06 §3 step 4). STUB - geography builder."""
from __future__ import annotations

from typing import Any

import httpx


class NominatimClient:
    def __init__(self, http: httpx.AsyncClient, *, base_url: str, contact_email: str, min_interval_s: float = 1.0):
        """User-Agent 'PIT-Radar/1.0 (Long Island Coalition for the Homeless outreach; <contact_email>)'.
        Global throttle: at most one request per min_interval_s across the process (asyncio.Lock)."""
        raise NotImplementedError

    @property
    def enabled(self) -> bool:
        """False when no contact email is configured (policy requires identification)."""
        raise NotImplementedError

    async def search(self, q: str, *, viewbox: tuple[float, float, float, float]) -> list[dict[str, Any]]:
        """GET /search?q=..&format=jsonv2&countrycodes=us&viewbox=..&bounded=1&addressdetails=1&limit=3.
        Returns [] on any error. Never raises."""
        raise NotImplementedError
