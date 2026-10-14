"""api_usage accounting (local-day budgets). The day boundary is midnight America/New_York."""
from __future__ import annotations

import datetime as dt

import pytest

from app.models import ApiUsage
from app.pipeline import usage

UTC = dt.timezone.utc
# 2026-10-15 04:00 UTC is midnight in New York (EDT, UTC-4).
BEFORE_MIDNIGHT = dt.datetime(2026, 10, 15, 3, 59, 59, tzinfo=UTC)
AFTER_MIDNIGHT = dt.datetime(2026, 10, 15, 4, 0, 0, tzinfo=UTC)


def test_record_usage_adds_within_a_local_day(db):
    now = dt.datetime(2026, 10, 14, 16, 0, tzinfo=UTC)
    usage.record_usage(db, "anthropic", now=now, requests=1, input_tokens=800, output_tokens=1200,
                       cache_read_tokens=6000, cache_write_tokens=10, cost_usd=0.0321)
    usage.record_usage(db, "anthropic", now=now, requests=1, input_tokens=200, output_tokens=300, cost_usd=0.01)
    usage.record_usage(db, "socialcrawl", now=now, requests=3, credits=4.5, errors=1)
    db.commit()
    row = db.get(ApiUsage, (dt.date(2026, 10, 14), "anthropic"))
    assert row.requests == 2 and row.input_tokens == 1000 and row.output_tokens == 1500
    assert row.cache_read_tokens == 6000 and row.cache_write_tokens == 10
    assert float(row.cost_usd) == pytest.approx(0.0421)
    today = usage.spent_today(db, now=now)
    assert today["anthropic"]["requests"] == 2 and today["anthropic"]["cost_usd"] == pytest.approx(0.0421)
    assert today["socialcrawl"]["credits"] == pytest.approx(4.5) and today["socialcrawl"]["errors"] == 1
    for vals in today.values():
        assert {"requests", "credits", "cost_usd", "input_tokens", "output_tokens"} <= set(vals)


def test_local_day_boundary_at_midnight_new_york(db):
    usage.record_usage(db, "exa", now=BEFORE_MIDNIGHT, requests=1, cost_usd=0.005)
    usage.record_usage(db, "exa", now=AFTER_MIDNIGHT, requests=2, cost_usd=0.010)
    db.commit()
    days = {r.day: r.requests for r in db.query(ApiUsage).filter(ApiUsage.provider == "exa")}
    assert days == {dt.date(2026, 10, 14): 1, dt.date(2026, 10, 15): 2}
    assert usage.spent_today(db, now=BEFORE_MIDNIGHT)["exa"]["requests"] == 1
    assert usage.spent_today(db, now=AFTER_MIDNIGHT)["exa"]["requests"] == 2


def test_timezone_setting_controls_the_day(db, monkeypatch):
    from app.config import reset_settings_cache
    monkeypatch.setenv("APP_TIMEZONE", "UTC")
    reset_settings_cache()
    usage.record_usage(db, "exa", now=BEFORE_MIDNIGHT, requests=1)
    db.commit()
    assert db.query(ApiUsage).one().day == dt.date(2026, 10, 15)


def test_usage_summary_today_and_month(db):
    usage.record_usage(db, "firecrawl", now=dt.datetime(2026, 9, 30, 12, tzinfo=UTC), requests=5)   # last month
    usage.record_usage(db, "firecrawl", now=dt.datetime(2026, 10, 1, 12, tzinfo=UTC), requests=2)
    usage.record_usage(db, "firecrawl", now=dt.datetime(2026, 10, 14, 12, tzinfo=UTC), requests=3)
    usage.record_usage(db, "anthropic", now=dt.datetime(2026, 10, 2, 12, tzinfo=UTC), cost_usd=1.25)
    # 2026-10-01 02:00 UTC is still Sept 30 in New York -> last month.
    usage.record_usage(db, "firecrawl", now=dt.datetime(2026, 10, 1, 2, tzinfo=UTC), requests=100)
    db.commit()
    s = usage.usage_summary(db, now=dt.datetime(2026, 10, 14, 16, tzinfo=UTC))
    assert s["today"] == {"firecrawl": pytest.approx(s["today"]["firecrawl"])}
    assert s["today"]["firecrawl"]["requests"] == 3
    assert s["month"]["firecrawl"]["requests"] == 5
    assert s["month"]["anthropic"]["cost_usd"] == pytest.approx(1.25)
    assert "anthropic" not in s["today"]


def test_zero_negative_and_invalid_amounts_ignored(db):
    now = dt.datetime(2026, 10, 14, 16, tzinfo=UTC)
    usage.record_usage(db, "exa", now=now)
    assert db.query(ApiUsage).count() == 0
    usage.record_usage(db, "exa", now=now, requests=-3, cost_usd=float("nan"), credits=2)
    db.commit()
    row = db.query(ApiUsage).one()
    assert row.requests == 0 and float(row.cost_usd) == 0 and float(row.credits) == pytest.approx(2)
    with pytest.raises(ValueError):
        usage.record_usage(db, "", now=now, requests=1)


def test_concurrent_insert_falls_back_to_update(db, engine):
    """Another writer creates the row between our SELECT and INSERT: the savepoint absorbs the conflict."""
    from app.db import SessionLocal
    now = dt.datetime(2026, 10, 14, 16, tzinfo=UTC)
    other = SessionLocal()
    try:
        usage.record_usage(other, "exa", now=now, requests=1)
        other.commit()
    finally:
        other.close()

    real_get = db.get
    calls = {"n": 0}

    def stale_get(model, ident, **kw):
        calls["n"] += 1
        if model is ApiUsage and calls["n"] == 1:
            return None              # simulate a SELECT that ran before the other commit
        return real_get(model, ident, **kw)

    db.get = stale_get  # type: ignore[method-assign]
    usage.record_usage(db, "exa", now=now, requests=2)
    db.commit()
    db.get = real_get  # type: ignore[method-assign]
    assert db.query(ApiUsage).one().requests == 3


def test_repeated_calls_in_one_transaction_with_loaded_row(db):
    now = dt.datetime(2026, 10, 14, 16, tzinfo=UTC)
    usage.record_usage(db, "anthropic", now=now, requests=1, cost_usd=0.5)
    row = db.get(ApiUsage, (dt.date(2026, 10, 14), "anthropic"))
    assert row.requests == 1
    usage.record_usage(db, "anthropic", now=now, requests=1, cost_usd=0.25)
    usage.record_usage(db, "anthropic", now=now, requests=1, cost_usd=0.25)
    assert row.requests == 3                     # the identity-mapped row is refreshed, not stale
    assert float(row.cost_usd) == pytest.approx(1.0)
    db.commit()
