"""Environment configuration (pydantic-settings).

Every variable in config/.env.example has a field here. Environment variables are the defaults;
admins can override the non-secret, operational ones at runtime (see app/runtime_settings.py).
Secrets are only ever read from the environment and are never logged or sent to the browser.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.paths import APP_ROOT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(APP_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Secrets -------------------------------------------------------------
    socialcrawl_api_key: SecretStr = SecretStr("")
    exa_api_key: SecretStr = SecretStr("")
    firecrawl_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")

    # --- App -----------------------------------------------------------------
    app_env: Literal["production", "development", "test"] = "production"
    app_base_url: str = ""
    app_timezone: str = "America/New_York"
    log_level: str = "INFO"
    database_url: str = ""          # empty -> sqlite file in data_dir()
    port: int = 8000

    # --- Auth ----------------------------------------------------------------
    session_ttl_hours: int = 12
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    cookie_secure: bool = True

    # --- Claude classifier ---------------------------------------------------
    anthropic_model: str = "claude-opus-5-5"
    classifier_effort: Literal["low", "medium", "high"] = "medium"
    llm_concurrency: int = Field(4, ge=1, le=16)
    min_confidence_map: float = Field(0.70, ge=0, le=1)
    min_confidence_review: float = Field(0.50, ge=0, le=1)

    # --- Scanning ------------------------------------------------------------
    scan_interval_min: int = Field(60, ge=5)
    pit_scan_interval_min: int = Field(15, ge=5)
    pit_mode_start: str = ""        # YYYY-MM-DD
    pit_mode_end: str = ""          # YYYY-MM-DD
    ingest_max_age_days: int = Field(30, ge=1, le=90)
    places_per_scan: int = 8
    pit_places_per_scan: int = 12
    hot_places_per_scan: int = 5
    max_classifications_per_scan: int = 60
    pit_max_classifications_per_scan: int = 120

    # --- Budgets -------------------------------------------------------------
    socialcrawl_credits_per_scan: float = 40
    pit_socialcrawl_credits_per_scan: float = 60
    socialcrawl_credits_per_day: float = 900
    pit_socialcrawl_credits_per_day: float = 4000
    exa_searches_per_scan: int = 6
    pit_exa_searches_per_scan: int = 10
    firecrawl_searches_per_scan: int = 6
    pit_firecrawl_searches_per_scan: int = 10
    daily_llm_budget_usd: float = 5
    pit_daily_llm_budget_usd: float = 20

    # --- Providers -----------------------------------------------------------
    socialcrawl_base_url: str = "https://www.socialcrawl.dev"
    socialcrawl_max_in_flight: int = Field(10, ge=1, le=50)
    exa_base_url: str = "https://api.exa.ai"
    firecrawl_base_url: str = "https://api.firecrawl.dev"

    # --- Geocoding -----------------------------------------------------------
    nominatim_base_url: str = "https://nominatim.openstreetmap.org"
    nominatim_contact_email: str = ""
    nominatim_min_interval_sec: float = 1.0

    # --- Retention and hotspots ---------------------------------------------
    retention_days: int = Field(30, ge=1, le=90)
    pending_item_ttl_hours: int = 72
    hotspot_radius_m: float = 400
    hotspot_radius_east_end_m: float = 800
    half_life_days: float = 7
    hotspot_archive_days: int = 90

    # --- Map -----------------------------------------------------------------
    map_style_url: str = ""         # empty -> built-in dark style over OpenFreeMap vector tiles
    map_tile_hosts: str = "tiles.openfreemap.org"

    # --- Derived helpers -----------------------------------------------------
    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        v = (v or "").strip()
        if v.startswith("postgres://"):
            v = "postgresql+psycopg://" + v[len("postgres://"):]
        elif v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def tile_hosts(self) -> list[str]:
        return [h.strip() for h in self.map_tile_hosts.split(",") if h.strip()]

    def has_key(self, name: str) -> bool:
        """True if the named secret (e.g. 'anthropic_api_key') is set."""
        val = getattr(self, name, None)
        return bool(val and val.get_secret_value().strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """For tests: re-read the environment on next get_settings()."""
    get_settings.cache_clear()
