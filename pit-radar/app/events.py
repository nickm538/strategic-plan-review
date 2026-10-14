"""In-process pub/sub feeding Server-Sent Events (docs/07 §5). STUB - runner builder."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal, Optional

Role = Literal["viewer", "reviewer", "admin"]
ROLE_RANK = {"viewer": 0, "reviewer": 1, "admin": 2}


@dataclass(frozen=True)
class Event:
    id: int                 # monotonically increasing per process
    event: str              # signal.new | signal.updated | hotspot.updated | scan.started | scan.finished | provider.health | budget.warning | geo.status
    data: dict[str, Any]
    min_role: Role = "viewer"


class EventBus:
    """Keeps the last 200 events for Last-Event-ID replay; fan-out to subscribers via bounded asyncio queues
    (a slow client drops events rather than blocking publishers)."""

    def __init__(self, replay_size: int = 200):
        raise NotImplementedError

    def publish(self, event: str, data: dict[str, Any], *, min_role: Role = "viewer") -> Event:
        """Thread-safe (may be called from worker threads via loop.call_soon_threadsafe)."""
        raise NotImplementedError

    def replay_after(self, last_id: Optional[int], role: Role) -> list[Event]:
        raise NotImplementedError

    async def subscribe(self, role: Role) -> AsyncIterator[Event]:
        """Yields events visible to `role`. Unsubscribes on generator close."""
        raise NotImplementedError
        yield  # pragma: no cover
