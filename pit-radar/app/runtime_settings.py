"""Runtime-overridable settings (docs/03 `settings` table, PUT /api/admin/settings).

Environment variables (app.config.Settings) are the defaults. A row in `settings` with one of the
allow-listed keys below overrides the default without a redeploy. Secrets are never overridable.

Also hosts small JSON key/value helpers used by the pipeline for operational state
(provider health, SocialCrawl endpoint map, planner rotation cursor, geography build status).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import utcnow
from app.models import Setting

# key -> (Settings attribute, parser/validator). Keys are the env-var names admins see.
def _float_range(lo: float, hi: float) -> Callable[[Any], float]:
    def f(v: Any) -> float:
        x = float(v)
        if not lo <= x <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
        return x
    return f


def _int_range(lo: int, hi: int) -> Callable[[Any], int]:
    def f(v: Any) -> int:
        if isinstance(v, bool):
            raise ValueError("must be an integer")
        x = int(v)
        if x != float(v) or not lo <= x <= hi:
            raise ValueError(f"must be an integer between {lo} and {hi}")
        return x
    return f


def _date_or_empty(v: Any) -> str:
    s = str(v or "").strip()
    if s:
        dt.date.fromisoformat(s)
    return s


def _effort(v: Any) -> str:
    s = str(v)
    if s not in ("low", "medium", "high"):
        raise ValueError("must be low, medium or high")
    return s


def _model(v: Any) -> str:
    s = str(v).strip()
    if not s.startswith("claude-") or len(s) > 64:
        raise ValueError("must be a Claude model id like claude-opus-5-5")
    return s


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if str(v).lower() in ("1", "true", "yes", "on"):
        return True
    if str(v).lower() in ("0", "false", "no", "off"):
        return False
    raise ValueError("must be true or false")


OVERRIDABLE: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "ANTHROPIC_MODEL": ("anthropic_model", _model),
    "CLASSIFIER_EFFORT": ("classifier_effort", _effort),
    "MIN_CONFIDENCE_MAP": ("min_confidence_map", _float_range(0, 1)),
    "MIN_CONFIDENCE_REVIEW": ("min_confidence_review", _float_range(0, 1)),
    "SCAN_INTERVAL_MIN": ("scan_interval_min", _int_range(5, 1440)),
    "PIT_SCAN_INTERVAL_MIN": ("pit_scan_interval_min", _int_range(5, 1440)),
    "PIT_MODE_START": ("pit_mode_start", _date_or_empty),
    "PIT_MODE_END": ("pit_mode_end", _date_or_empty),
    "INGEST_MAX_AGE_DAYS": ("ingest_max_age_days", _int_range(1, 90)),
    "PLACES_PER_SCAN": ("places_per_scan", _int_range(0, 100)),
    "PIT_PLACES_PER_SCAN": ("pit_places_per_scan", _int_range(0, 100)),
    "HOT_PLACES_PER_SCAN": ("hot_places_per_scan", _int_range(0, 50)),
    "MAX_CLASSIFICATIONS_PER_SCAN": ("max_classifications_per_scan", _int_range(0, 1000)),
    "PIT_MAX_CLASSIFICATIONS_PER_SCAN": ("pit_max_classifications_per_scan", _int_range(0, 1000)),
    "SOCIALCRAWL_CREDITS_PER_SCAN": ("socialcrawl_credits_per_scan", _float_range(0, 10000)),
    "PIT_SOCIALCRAWL_CREDITS_PER_SCAN": ("pit_socialcrawl_credits_per_scan", _float_range(0, 10000)),
    "SOCIALCRAWL_CREDITS_PER_DAY": ("socialcrawl_credits_per_day", _float_range(0, 100000)),
    "PIT_SOCIALCRAWL_CREDITS_PER_DAY": ("pit_socialcrawl_credits_per_day", _float_range(0, 100000)),
    "EXA_SEARCHES_PER_SCAN": ("exa_searches_per_scan", _int_range(0, 200)),
    "PIT_EXA_SEARCHES_PER_SCAN": ("pit_exa_searches_per_scan", _int_range(0, 200)),
    "FIRECRAWL_SEARCHES_PER_SCAN": ("firecrawl_searches_per_scan", _int_range(0, 200)),
    "PIT_FIRECRAWL_SEARCHES_PER_SCAN": ("pit_firecrawl_searches_per_scan", _int_range(0, 200)),
    "DAILY_LLM_BUDGET_USD": ("daily_llm_budget_usd", _float_range(0, 1000)),
    "PIT_DAILY_LLM_BUDGET_USD": ("pit_daily_llm_budget_usd", _float_range(0, 1000)),
}

# Operational switches that are not env vars.
SCANS_PAUSED_KEY = "SCANS_PAUSED"          # bool: admin pause
PIT_MODE_MANUAL_KEY = "PIT_MODE_MANUAL"    # bool: admin forces PIT mode on

# Operational state keys (JSON values written by the app, updated_by NULL).
KEY_SC_ENDPOINTS = "socialcrawl.resolved_endpoints"
KEY_ROTATION_CURSOR = "planner.rotation_cursor"
KEY_GEO_STATUS = "geo.status"
KEY_LEXICON_OVERRIDES = "lexicon.overrides"
KEY_SOURCES_OVERRIDES = "sources.overrides"
KEY_HEALTH_PREFIX = "health."              # health.socialcrawl, health.exa, ...


def get_value(db: Session, key: str, default: Any = None) -> Any:
    row = db.get(Setting, key)
    return default if row is None else row.value


def set_value(db: Session, key: str, value: Any, user_id: int | None = None) -> None:
    """Upsert a settings row. Caller commits."""
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value, updated_by=user_id, updated_at=utcnow()))
    else:
        row.value = value
        row.updated_by = user_id
        row.updated_at = utcnow()


def validate_override(key: str, value: Any) -> Any:
    """Parse and validate one admin override. Raises KeyError (unknown key) or ValueError."""
    if key in (SCANS_PAUSED_KEY, PIT_MODE_MANUAL_KEY):
        return _bool(value)
    if key not in OVERRIDABLE:
        raise KeyError(key)
    return OVERRIDABLE[key][1](value)


@dataclass(frozen=True)
class Effective:
    """Settings after applying admin overrides, plus the resolved scan mode."""

    base: Settings
    values: dict[str, Any]          # attribute name -> effective value
    overrides: dict[str, Any]       # env-name -> override value (only overridden keys)
    pit_mode: bool
    scans_paused: bool

    def get(self, attr: str) -> Any:
        return self.values.get(attr, getattr(self.base, attr))

    def mode_value(self, attr: str) -> Any:
        """Value for the current mode: `pit_<attr>` while PIT mode is on, if such a setting exists."""
        if self.pit_mode and hasattr(self.base, f"pit_{attr}"):
            return self.get(f"pit_{attr}")
        return self.get(attr)

    @property
    def mode(self) -> str:
        return "pit" if self.pit_mode else "standard"


def local_today(settings: Settings | None = None, now: dt.datetime | None = None) -> dt.date:
    s = settings or get_settings()
    now = now or utcnow()
    return now.astimezone(ZoneInfo(s.app_timezone)).date()


def effective(db: Session, now: dt.datetime | None = None) -> Effective:
    base = get_settings()
    rows = {r.key: r.value for r in db.query(Setting).filter(
        Setting.key.in_(list(OVERRIDABLE) + [SCANS_PAUSED_KEY, PIT_MODE_MANUAL_KEY])
    )}
    values: dict[str, Any] = {}
    overrides: dict[str, Any] = {}
    for env_key, (attr, parse) in OVERRIDABLE.items():
        if env_key in rows:
            try:
                values[attr] = parse(rows[env_key])
                overrides[env_key] = values[attr]
            except (ValueError, TypeError):
                continue  # ignore a corrupt override; env default stays in force
    manual = bool(rows.get(PIT_MODE_MANUAL_KEY, False))
    paused = bool(rows.get(SCANS_PAUSED_KEY, False))
    start = values.get("pit_mode_start", base.pit_mode_start)
    end = values.get("pit_mode_end", base.pit_mode_end)
    today = local_today(base, now)
    in_window = False
    try:
        if start and end:
            in_window = dt.date.fromisoformat(start) <= today <= dt.date.fromisoformat(end)
    except ValueError:
        in_window = False
    return Effective(base=base, values=values, overrides=overrides,
                     pit_mode=manual or in_window, scans_paused=paused)
