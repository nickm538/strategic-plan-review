"""api_usage accounting and daily budget totals.

Rows are keyed by the LOCAL day (APP_TIMEZONE), so budgets reset at local midnight. The upsert is portable
(SQLite and Postgres): select the row; if it exists, add with an atomic `UPDATE ... SET x = x + :n`; if not,
INSERT inside a SAVEPOINT and, if a concurrent writer inserted the same key first, fall back to the UPDATE.
Everything runs inside the caller's transaction; the caller commits.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ApiUsage
from app.runtime_settings import local_today

logger = logging.getLogger(__name__)

_INT_FIELDS = ("requests", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "errors")
_NUM_FIELDS = ("credits", "cost_usd")
_ALL_FIELDS = ("requests", "credits", "cost_usd", "input_tokens", "output_tokens", "cache_read_tokens",
               "cache_write_tokens", "errors")
_MAX_PROVIDER = 16


def _as_int(v: Any) -> int:
    if isinstance(v, bool) or v is None:
        return 0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0
    return int(f) if math.isfinite(f) and f > 0 else 0


def _as_num(v: Any) -> float:
    if isinstance(v, bool) or v is None:
        return 0.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) and f > 0 else 0.0


def _num(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _local_day(now: dt.datetime) -> dt.date:
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    return local_today(get_settings(), now)


def _add_update(db: Session, day: dt.date, provider: str, inc: dict[str, Any]) -> int:
    values = {name: getattr(ApiUsage, name) + amount for name, amount in inc.items()}
    res = db.execute(
        update(ApiUsage)
        .where(ApiUsage.day == day, ApiUsage.provider == provider)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    cached = db.identity_map.get(db.identity_key(ApiUsage, (day, provider)))
    if cached is not None:
        db.expire(cached)
    n = getattr(res, "rowcount", 0)
    return int(n) if isinstance(n, int) else 0


def record_usage(db: Session, provider: str, *, now: dt.datetime, requests: int = 0, credits: float = 0,
                 input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0,
                 cache_write_tokens: int = 0, cost_usd: float = 0, errors: int = 0) -> None:
    """Upsert-add into api_usage for the LOCAL day (APP_TIMEZONE). Caller commits.
    Negative, NaN or non-numeric amounts are ignored (counted as 0)."""
    provider = (provider or "").strip().lower()[:_MAX_PROVIDER]
    if not provider:
        raise ValueError("provider is required")
    inc: dict[str, Any] = {}
    for name, value in (("requests", requests), ("input_tokens", input_tokens), ("output_tokens", output_tokens),
                        ("cache_read_tokens", cache_read_tokens), ("cache_write_tokens", cache_write_tokens),
                        ("errors", errors)):
        n = _as_int(value)
        if n:
            inc[name] = n
    for name, value in (("credits", credits), ("cost_usd", cost_usd)):
        f = _as_num(value)
        if f:
            inc[name] = Decimal(str(f))      # exact NUMERIC arithmetic on Postgres; fine on SQLite
    if not inc:
        return
    day = _local_day(now)

    existing: Optional[ApiUsage] = db.get(ApiUsage, (day, provider))
    if existing is not None and _add_update(db, day, provider, inc):
        return
    row = ApiUsage(day=day, provider=provider,
                   **{name: inc.get(name, 0) for name in _INT_FIELDS},
                   **{name: inc.get(name, Decimal(0)) for name in _NUM_FIELDS})
    try:
        with db.begin_nested():
            db.add(row)
    except IntegrityError:
        # Another writer created the row between our SELECT and INSERT: add to it instead.
        if row in db:
            db.expunge(row)
        if not _add_update(db, day, provider, inc):
            raise


def _totals(db: Session, *where: Any) -> dict[str, dict[str, float]]:
    cols = [func.coalesce(func.sum(getattr(ApiUsage, name)), 0) for name in _ALL_FIELDS]
    stmt = select(ApiUsage.provider, *cols).where(*where).group_by(ApiUsage.provider).order_by(ApiUsage.provider)
    out: dict[str, dict[str, float]] = {}
    for row in db.execute(stmt):
        provider = row[0]
        vals = {name: _num(v) for name, v in zip(_ALL_FIELDS, row[1:], strict=True)}
        for name in _INT_FIELDS:
            vals[name] = float(int(vals[name]))
        vals["credits"] = round(vals["credits"], 3)
        vals["cost_usd"] = round(vals["cost_usd"], 6)
        out[provider] = vals
    return out


def spent_today(db: Session, *, now: dt.datetime) -> dict[str, dict[str, float]]:
    """{provider: {requests, credits, cost_usd, input_tokens, output_tokens}} for the local day.
    (Also carries cache_read_tokens, cache_write_tokens and errors.)"""
    day = _local_day(now)
    return _totals(db, ApiUsage.day == day)


def usage_summary(db: Session, *, now: dt.datetime) -> dict[str, dict[str, dict[str, float]]]:
    """{'today': {...per provider}, 'month': {...per provider}} for /api/stats. 'month' is the local calendar
    month to date."""
    day = _local_day(now)
    first = day.replace(day=1)
    return {
        "today": _totals(db, ApiUsage.day == day),
        "month": _totals(db, ApiUsage.day >= first, ApiUsage.day <= day),
    }
