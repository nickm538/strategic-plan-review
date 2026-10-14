"""Helpers shared by the API routers: app state access, time windows, cursors, bbox, notice text."""
from __future__ import annotations

import base64
import datetime as dt
import json
from typing import Any, Optional

from fastapi import HTTPException, Request

from app.state import AppState

# R9: shown on every screen and every export.
PURPOSE_NOTICE = "For outreach and PIT planning only. Not for enforcement. Do not share outside LICH outreach."

WINDOWS: dict[str, dt.timedelta] = {
    "24h": dt.timedelta(hours=24),
    "72h": dt.timedelta(hours=72),
    "7d": dt.timedelta(days=7),
    "30d": dt.timedelta(days=30),
}
SIGNAL_TYPES = ("encampment", "individual_unsheltered", "vehicle_dwelling")
COUNTIES = ("Nassau", "Suffolk")
DEFAULT_SELFREPORT_TEMPLATE = (
    "Hi, I'm with Long Island outreach. If you need a place to stay tonight, you can call Long Island 2-1-1 "
    "(dial 211) any time. You can also call [County DSS emergency housing number - verify] or reach our "
    "outreach team at [LICH outreach contact - verify]. You're not alone, and help is available."
)
KEY_SELFREPORT_TEMPLATE = "SELFREPORT_REPLY_TEMPLATE"


def get_state(request: Request) -> AppState:
    state = getattr(request.app.state, "pit", None)
    if state is None:
        raise HTTPException(503, "The app is still starting. Try again in a moment.")
    return state


def window_start(window: Optional[str], now: dt.datetime, default: str = "7d") -> dt.datetime:
    key = window or default
    if key not in WINDOWS:
        raise HTTPException(422, f"window must be one of {', '.join(WINDOWS)}")
    return now - WINDOWS[key]


def check_choice(value: Optional[str], allowed: tuple[str, ...], name: str) -> Optional[str]:
    if value is None or value == "":
        return None
    if value not in allowed:
        raise HTTPException(422, f"{name} must be one of {', '.join(allowed)}")
    return value


def encode_cursor(published_at: dt.datetime, row_id: int) -> str:
    raw = json.dumps([published_at.astimezone(dt.timezone.utc).isoformat(), int(row_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: Optional[str]) -> Optional[tuple[dt.datetime, int]]:
    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        ts, rid = json.loads(base64.urlsafe_b64decode(cursor + pad).decode())
        when = dt.datetime.fromisoformat(ts)
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        return when, int(rid)
    except Exception:  # noqa: BLE001 - any malformed cursor is a client error
        raise HTTPException(400, "Invalid cursor") from None


def parse_bbox(bbox: Optional[str]) -> Optional[tuple[float, float, float, float]]:
    if not bbox:
        return None
    try:
        parts = [float(x) for x in bbox.split(",")]
    except ValueError:
        raise HTTPException(422, "bbox must be minLon,minLat,maxLon,maxLat") from None
    if len(parts) != 4 or not (-180 <= parts[0] <= parts[2] <= 180 and -90 <= parts[1] <= parts[3] <= 90):
        raise HTTPException(422, "bbox must be minLon,minLat,maxLon,maxLat")
    return parts[0], parts[1], parts[2], parts[3]


def iso(d: Optional[dt.datetime]) -> Optional[str]:
    from app.api.serializers import iso as _iso
    return _iso(d)


def body_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(422, "Expected a JSON object")
    return payload
