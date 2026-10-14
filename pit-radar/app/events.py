"""In-process pub/sub feeding Server-Sent Events (docs/07 §5).

One EventBus per process (AppState.events). Publishers call `publish()` from the event loop or from worker
threads (asyncio.to_thread); delivery to subscribers always happens on the captured event loop.

Audience: every event carries `min_role` (lowest role that may see it) and an optional `max_role` (highest
role that may see it). `max_role` lets a publisher send one copy of an event to viewers and a different, fuller
copy to reviewers and admins without anyone receiving both (e.g. `signal.new`: viewers get a copy without
coordinates, R11). Unknown roles see nothing.

Replay: the last `replay_size` events are kept for Last-Event-ID replay. Ids increase monotonically and start
from the process start time in milliseconds, so ids issued after a restart are larger than the ids a client
saw before it (its Last-Event-ID still orders correctly against the new buffer).

Back-pressure: each subscriber has a bounded asyncio.Queue. When a slow client's queue is full, the OLDEST
queued event is dropped to make room; publishers never block.
"""
from __future__ import annotations

import asyncio
import copy
import itertools
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Optional

logger = logging.getLogger(__name__)

Role = Literal["viewer", "reviewer", "admin"]
ROLE_RANK = {"viewer": 0, "reviewer": 1, "admin": 2}

DEFAULT_REPLAY_SIZE = 200
DEFAULT_QUEUE_SIZE = 200

# Event names used by the pipeline (docs/07 §5, plus geo.status and maintenance.finished).
SIGNAL_NEW = "signal.new"
SIGNAL_UPDATED = "signal.updated"
HOTSPOT_UPDATED = "hotspot.updated"
SCAN_STARTED = "scan.started"
SCAN_FINISHED = "scan.finished"
PROVIDER_HEALTH = "provider.health"
BUDGET_WARNING = "budget.warning"
GEO_STATUS = "geo.status"
MAINTENANCE_FINISHED = "maintenance.finished"


@dataclass(frozen=True)
class Event:
    id: int                 # monotonically increasing per process
    event: str              # signal.new | signal.updated | hotspot.updated | scan.started | scan.finished | provider.health | budget.warning | geo.status
    data: dict[str, Any]
    min_role: Role = "viewer"
    max_role: Optional[Role] = None   # None = no upper bound (everyone at or above min_role)

    def visible_to(self, role: str) -> bool:
        rank = ROLE_RANK.get(role)
        if rank is None:
            return False
        if rank < ROLE_RANK.get(self.min_role, len(ROLE_RANK)):
            return False
        if self.max_role is not None and rank > ROLE_RANK.get(self.max_role, -1):
            return False
        return True


@dataclass(eq=False)
class _Subscriber:
    role: str
    queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop
    dropped: int = 0
    last_delivered: int = field(default=0)


class EventBus:
    """Keeps the last 200 events for Last-Event-ID replay; fan-out to subscribers via bounded asyncio queues
    (a slow client drops events rather than blocking publishers)."""

    def __init__(self, replay_size: int = DEFAULT_REPLAY_SIZE, *, queue_size: int = DEFAULT_QUEUE_SIZE,
                 start_id: Optional[int] = None):
        if replay_size < 1 or queue_size < 1:
            raise ValueError("replay_size and queue_size must be >= 1")
        self._replay: deque[Event] = deque(maxlen=replay_size)
        self._queue_size = queue_size
        first = int(start_id) if start_id is not None else int(time.time() * 1000)
        self._ids = itertools.count(max(1, first))
        self._lock = threading.Lock()            # guards ids, replay buffer and the subscriber set
        self._subs: set[_Subscriber] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.dropped_total = 0

    # ------------------------------------------------------------------ loop binding
    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Remember the event loop that owns the subscriber queues. Called by the scheduler at startup and
        implicitly by subscribe() / publish() on the loop; worker threads then deliver through it."""
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        self._loop = loop

    @property
    def last_id(self) -> Optional[int]:
        with self._lock:
            return self._replay[-1].id if self._replay else None

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    # ------------------------------------------------------------------ publish
    def publish(self, event: str, data: dict[str, Any], *, min_role: Role = "viewer",
                max_role: Optional[Role] = None) -> Event:
        """Thread-safe (may be called from worker threads via loop.call_soon_threadsafe).

        `data` is deep-copied, so later changes by the caller never alter what subscribers receive."""
        if min_role not in ROLE_RANK:
            raise ValueError(f"unknown min_role {min_role!r}")
        if max_role is not None and (max_role not in ROLE_RANK or ROLE_RANK[max_role] < ROLE_RANK[min_role]):
            raise ValueError(f"invalid max_role {max_role!r}")
        payload = copy.deepcopy(dict(data or {}))
        with self._lock:
            ev = Event(id=next(self._ids), event=str(event), data=payload, min_role=min_role, max_role=max_role)
            self._replay.append(ev)
            subs = [s for s in self._subs if ev.visible_to(s.role)]
        if not subs:
            return ev
        try:
            running: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        by_loop: dict[asyncio.AbstractEventLoop, list[_Subscriber]] = {}
        for s in subs:
            by_loop.setdefault(s.loop, []).append(s)
        for loop, targets in by_loop.items():
            if loop is running:
                self._deliver(ev, targets)
            else:
                try:
                    loop.call_soon_threadsafe(self._deliver, ev, targets)
                except RuntimeError:          # loop closed: its subscribers are gone
                    with self._lock:
                        for s in targets:
                            self._subs.discard(s)
        return ev

    def _deliver(self, ev: Event, targets: list[_Subscriber]) -> None:
        """Runs on the subscribers' loop. Drop-oldest when a queue is full."""
        for s in targets:
            q = s.queue
            while True:
                try:
                    q.put_nowait(ev)
                    break
                except asyncio.QueueFull:
                    try:
                        q.get_nowait()
                        s.dropped += 1
                        self.dropped_total += 1
                    except asyncio.QueueEmpty:  # pragma: no cover - raced with the consumer
                        continue
            if s.dropped and s.dropped % 50 == 1:
                logger.info("events: slow subscriber (%s) dropped %d event(s)", s.role, s.dropped)

    # ------------------------------------------------------------------ read side
    def replay_after(self, last_id: Optional[int], role: Role) -> list[Event]:
        """Buffered events visible to `role` with id > last_id, oldest first. None -> [] (a fresh client
        gets live events only). A last_id newer than anything buffered (an id from before a restart that
        happened within the same millisecond window, or a forged id) -> the whole visible buffer."""
        if last_id is None:
            return []
        with self._lock:
            buf = list(self._replay)
        if not buf:
            return []
        try:
            last = int(last_id)
        except (TypeError, ValueError):
            return []
        if last > buf[-1].id:
            return [e for e in buf if e.visible_to(role)]
        return [e for e in buf if e.id > last and e.visible_to(role)]

    async def subscribe(self, role: Role, *, last_id: Optional[int] = None) -> AsyncIterator[Event]:
        """Yields events visible to `role`. Unsubscribes on generator close.

        With `last_id`, the generator first yields the replay (replay_after(last_id, role)) and then live
        events, registering BEFORE taking the replay so nothing published in between is lost or duplicated."""
        loop = asyncio.get_running_loop()
        if self._loop is None or self._loop.is_closed():
            self._loop = loop
        sub = _Subscriber(role=str(role), queue=asyncio.Queue(maxsize=self._queue_size), loop=loop)
        with self._lock:
            self._subs.add(sub)
        try:
            high = 0
            if last_id is not None:
                for ev in self.replay_after(last_id, role):
                    high = max(high, ev.id)
                    yield ev
            while True:
                ev = await sub.queue.get()
                if ev.id <= high:
                    continue            # already sent as part of the replay
                yield ev
        finally:
            with self._lock:
                self._subs.discard(sub)


__all__ = [
    "Event", "EventBus", "ROLE_RANK", "Role",
    "SIGNAL_NEW", "SIGNAL_UPDATED", "HOTSPOT_UPDATED", "SCAN_STARTED", "SCAN_FINISHED", "PROVIDER_HEALTH",
    "BUDGET_WARNING", "GEO_STATUS", "MAINTENANCE_FINISHED",
]
