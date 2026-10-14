"""In-process scheduler (docs/02 §1, §4; docs/09 §4). STUB - runner builder."""
from __future__ import annotations

from app.state import AppState


class Scheduler:
    """asyncio tasks: geography bootstrap (load or build), scan loop (interval from runtime settings: standard vs
    PIT mode; respects SCANS_PAUSED and provider keys), hourly retention + hotspot decay refresh, daily SocialCrawl
    discovery, provider health checks every 15 min. All loops survive exceptions and log them."""

    def __init__(self, state: AppState):
        raise NotImplementedError

    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    async def trigger_scan(self, *, mode: str | None = None) -> bool:
        """Manual scan from the admin API. False if a scan is already running."""
        raise NotImplementedError

    async def rebuild_geography(self) -> bool:
        raise NotImplementedError
