"""Process-wide application state, created in the FastAPI lifespan (app/main.py).

Holds loaded configuration and long-lived clients. One instance per process (one replica).
Fields that depend on optional inputs (geography, API keys) may be None; code must handle that
and surface the reason in the health panel instead of crashing.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # imports only for type checkers; avoids import cycles at runtime
    import httpx
    from anthropic import AsyncAnthropic

    from app.config import Settings
    from app.events import EventBus
    from app.pipeline.geo.gazetteer import Gazetteer
    from app.pipeline.geo.geofence import Geofence
    from app.pipeline.lexicon import Lexicon
    from app.pipeline.sources import SourcesConfig


@dataclass
class SchedulerStatus:
    running: bool = False
    current_scan_id: Optional[int] = None
    last_scan_at: Optional[dt.datetime] = None
    last_scan_status: Optional[str] = None
    next_scan_at: Optional[dt.datetime] = None
    last_retention_at: Optional[dt.datetime] = None
    last_discovery_at: Optional[dt.datetime] = None


@dataclass
class AppState:
    settings: "Settings"
    lexicon: "Lexicon"
    sources: "SourcesConfig"
    events: "EventBus"
    http: "httpx.AsyncClient"                      # shared client for providers + Nominatim (no default auth headers)
    anthropic: Optional["AsyncAnthropic"] = None   # None when ANTHROPIC_API_KEY is not set
    gazetteer: Optional["Gazetteer"] = None        # None until geography is built (app.pipeline.geo.build)
    geofence: Optional["Geofence"] = None
    system_blocks: Optional[list[dict[str, Any]]] = None   # cached classifier system prompt (built once gazetteer loads)
    scheduler: SchedulerStatus = field(default_factory=SchedulerStatus)
    geo_status: dict[str, Any] = field(default_factory=lambda: {"state": "unknown", "message": ""})
    started_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))

    @property
    def geo_ready(self) -> bool:
        return self.gazetteer is not None and self.geofence is not None
