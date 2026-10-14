"""GET /api/stream: Server-Sent Events (docs/07 §5).

The principal is resolved with a short-lived DB session (not a request-scoped dependency), so a long-lived
stream never holds a database connection. Session expiry is re-checked every few minutes; an expired or revoked
session ends the stream and the browser reconnects to a 401.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from app.api.common import get_state
from app.auth import COOKIE_NAME, resolve_session
from app.db import SessionLocal, utcnow

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

PING_S = 20
RECHECK_SESSION_S = 300


def _resolve_role(token: Optional[str]) -> Optional[str]:
    db = SessionLocal()
    try:
        resolved = resolve_session(db, token, now=utcnow())
        return None if resolved is None else resolved[0].role
    finally:
        db.close()


def _last_event_id(request: Request) -> Optional[int]:
    raw = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@router.get("/stream")
async def stream(request: Request) -> EventSourceResponse:
    token = request.cookies.get(COOKIE_NAME)
    role = await asyncio.to_thread(_resolve_role, token)
    if role is None:
        raise HTTPException(401, "Not authenticated")
    bus = get_state(request).events
    last_id = _last_event_id(request)

    async def gen() -> AsyncIterator[dict[str, Any]]:
        checked = time.monotonic()
        yield {"event": "hello", "data": json.dumps({"role": role})}
        # Pump the subscription into a local queue: cancelling a queue read on timeout is safe, whereas
        # cancelling the subscription's __anext__ would close the subscriber generator.
        local: asyncio.Queue[Any] = asyncio.Queue(maxsize=500)
        done = object()

        async def pump() -> None:
            try:
                async for e in bus.subscribe(role, last_id=last_id):  # type: ignore[arg-type]
                    if local.full():
                        local.get_nowait()          # drop the oldest; the client can refetch
                    local.put_nowait(e)
            finally:
                if local.full():
                    local.get_nowait()
                local.put_nowait(done)

        task = asyncio.create_task(pump())
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(local.get(), timeout=PING_S)
                except asyncio.TimeoutError:
                    evt = None
                if evt is done:
                    return
                if await request.is_disconnected():
                    return
                if time.monotonic() - checked > RECHECK_SESSION_S:
                    checked = time.monotonic()
                    if await asyncio.to_thread(_resolve_role, token) is None:
                        yield {"event": "session.expired", "data": "{}"}
                        return
                if evt is not None:
                    yield {"id": str(evt.id), "event": evt.event, "data": json.dumps(evt.data, default=str)}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return EventSourceResponse(gen(), ping=PING_S, headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
