"""api_usage accounting and daily budget totals. STUB - storage builder."""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session


def record_usage(db: Session, provider: str, *, now: dt.datetime, requests: int = 0, credits: float = 0,
                 input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0,
                 cache_write_tokens: int = 0, cost_usd: float = 0, errors: int = 0) -> None:
    """Upsert-add into api_usage for the LOCAL day (APP_TIMEZONE). Caller commits."""
    raise NotImplementedError


def spent_today(db: Session, *, now: dt.datetime) -> dict[str, dict[str, float]]:
    """{provider: {requests, credits, cost_usd, input_tokens, output_tokens}} for the local day."""
    raise NotImplementedError


def usage_summary(db: Session, *, now: dt.datetime) -> dict[str, dict[str, dict[str, float]]]:
    """{'today': {...per provider}, 'month': {...per provider}} for /api/stats."""
    raise NotImplementedError
