"""Bounded Nominatim fallback geocoder (docs/06 §3 step 4).

Usage policy (operations.osmfoundation.org/policies/nominatim) [verify current terms at deploy]: an
identifying User-Agent with a contact address, at most 1 request per second, and caching of results
(the caller caches in geocode_cache). The client is disabled when no contact email is configured.
Logs never contain the query text (it comes from posts) or request headers.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
import weakref
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

USER_AGENT_TEMPLATE = "PIT-Radar/1.0 (Long Island Coalition for the Homeless outreach; {email})"
REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
POLICY_MIN_INTERVAL_S = 1.0       # usage policy: absolute maximum of 1 request per second
BACKOFF_ON_THROTTLE_S = 60.0      # pause after HTTP 429/503 before the next request
MAX_QUERY_CHARS = 300
MAX_RESULTS = 3

# Only these result keys are kept (allow-list; display names and extra tags are never read).
_RESULT_KEYS = ("lat", "lon", "category", "type", "addresstype", "name", "importance", "place_rank",
                "osm_type", "osm_id")
_ADDRESS_KEYS = ("county", "state", "country_code", "city", "town", "village", "hamlet", "suburb", "municipality")


class _Throttle:
    """Process-wide serialization of Nominatim requests (one per running event loop)."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.last_at = 0.0
        self.blocked_until = 0.0


_THROTTLES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Throttle]" = weakref.WeakKeyDictionary()


def _throttle() -> _Throttle:
    loop = asyncio.get_running_loop()
    t = _THROTTLES.get(loop)
    if t is None:
        t = _Throttle()
        _THROTTLES[loop] = t
    return t


def _clean_result(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    try:
        lat, lon = float(raw.get("lat")), float(raw.get("lon"))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    out: dict[str, Any] = {k: raw[k] for k in _RESULT_KEYS if k in raw and k not in ("lat", "lon")}
    out["lat"], out["lon"] = lat, lon
    addr = raw.get("address")
    if isinstance(addr, dict):
        out["address"] = {k: str(addr[k]) for k in _ADDRESS_KEYS if isinstance(addr.get(k), (str, int, float))}
    else:
        out["address"] = {}
    return out


class NominatimClient:
    def __init__(self, http: httpx.AsyncClient, *, base_url: str, contact_email: str, min_interval_s: float = 1.0):
        """User-Agent 'PIT-Radar/1.0 (Long Island Coalition for the Homeless outreach; <contact_email>)'.
        Global throttle: at most one request per min_interval_s across the process (asyncio.Lock)."""
        self._http = http
        self._base = (base_url or "").strip().rstrip("/")
        self._email = (contact_email or "").strip()
        try:
            interval = float(min_interval_s)
        except (TypeError, ValueError):
            interval = POLICY_MIN_INTERVAL_S
        self._min_interval = max(POLICY_MIN_INTERVAL_S, interval if math.isfinite(interval) else 0.0)
        self.last_error: Optional[str] = None      # short, for the health panel (no query text)
        self.requests_made = 0

    @property
    def enabled(self) -> bool:
        """False when no contact email is configured (policy requires identification)."""
        return bool(self._base) and "@" in self._email and " " not in self._email

    @property
    def user_agent(self) -> str:
        return USER_AGENT_TEMPLATE.format(email=self._email)

    async def search(self, q: str, *, viewbox: tuple[float, float, float, float]) -> list[dict[str, Any]]:
        """GET /search?q=..&format=jsonv2&countrycodes=us&viewbox=..&bounded=1&addressdetails=1&limit=3.
        Returns [] on any error. Never raises."""
        results, _ok = await self.search_with_status(q, viewbox=viewbox)
        return results

    async def search_with_status(self, q: str, *, viewbox: tuple[float, float, float, float]
                                 ) -> tuple[list[dict[str, Any]], bool]:
        """Like search(), plus whether the HTTP exchange succeeded (so callers cache only real answers)."""
        try:
            return await self._search(q, viewbox)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never raises
            self.last_error = f"unexpected {type(exc).__name__}"
            logger.warning("nominatim: unexpected error (%s)", type(exc).__name__)
            return [], False

    async def _search(self, q: str, viewbox: tuple[float, float, float, float]) -> tuple[list[dict[str, Any]], bool]:
        if not self.enabled:
            return [], False
        query = " ".join((q or "").split())[:MAX_QUERY_CHARS]
        if not query:
            return [], False
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in viewbox)
        except (TypeError, ValueError):
            return [], False
        params = {
            "q": query,
            "format": "jsonv2",
            "countrycodes": "us",
            "viewbox": f"{min_lon:.5f},{max_lat:.5f},{max_lon:.5f},{min_lat:.5f}",
            "bounded": "1",
            "addressdetails": "1",
            "limit": str(MAX_RESULTS),
        }
        headers = {"User-Agent": self.user_agent, "Accept": "application/json", "Accept-Language": "en"}
        thr = _throttle()
        async with thr.lock:
            now = time.monotonic()
            if now < thr.blocked_until:
                self.last_error = "throttled by Nominatim; backing off"
                return [], False
            wait = thr.last_at + self._min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._http.get(f"{self._base}/search", params=params, headers=headers,
                                            timeout=REQUEST_TIMEOUT)
            except httpx.HTTPError as exc:
                self.last_error = f"network error ({type(exc).__name__})"
                logger.warning("nominatim: request failed (%s)", type(exc).__name__)
                return [], False
            finally:
                thr.last_at = time.monotonic()
                self.requests_made += 1
            if resp.status_code in (429, 503):
                thr.blocked_until = time.monotonic() + BACKOFF_ON_THROTTLE_S
                self.last_error = f"HTTP {resp.status_code} (throttled)"
                logger.warning("nominatim: HTTP %s; pausing %.0fs", resp.status_code, BACKOFF_ON_THROTTLE_S)
                return [], False
        if resp.status_code != 200:
            self.last_error = f"HTTP {resp.status_code}"
            logger.warning("nominatim: HTTP %s", resp.status_code)
            return [], False
        try:
            data = resp.json()
        except ValueError:
            self.last_error = "invalid JSON"
            logger.warning("nominatim: invalid JSON response")
            return [], False
        if not isinstance(data, list):
            self.last_error = "unexpected response shape"
            return [], False
        self.last_error = None
        out = [r for r in (_clean_result(x) for x in data[:MAX_RESULTS]) if r is not None]
        return out, True
