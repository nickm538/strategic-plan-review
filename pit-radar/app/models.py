"""ORM models: the reference DDL in docs/03 §2, plus a few operational columns noted inline.

All timestamps are timezone-aware UTC (UTCDateTime). JSON columns are JSONB on Postgres.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, BigIdType, JSONType, UTCDateTime, utcnow

ROLES = ("viewer", "reviewer", "admin")
SIGNAL_TYPES = ("encampment", "individual_unsheltered", "vehicle_dwelling", "self_report", "unknown")
SIGNAL_STATUSES = ("ai_verified", "confirmed", "dismissed", "needs_human_review", "unlocated", "self_report")
MAPPED_STATUSES = ("ai_verified", "confirmed")
HOTSPOT_TYPES = ("encampment", "individual_unsheltered", "vehicle_dwelling", "mixed")
HOTSPOT_STATUSES = ("active", "visited", "not_found", "resolved", "archived")
PLACE_KINDS = (
    "town", "city", "village", "cdp", "lirr_station", "park", "beach", "mall",
    "transit", "library", "hospital", "other_public",
)
COUNTIES = ("Nassau", "Suffolk")
PRECISIONS = ("public_place", "hamlet", "town")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)          # argon2id
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    last_login_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    # Operational (lockout, docs/07 §1): failures inside the current window, and lock expiry.
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_login_window_start: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    locked_until: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    password_changed_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    __table_args__ = (CheckConstraint(_in("role", ROLES), name="ck_users_role"),)


class UserSession(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)             # sha256(token) hex; raw token only in the cookie
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)         # sha256(csrf token)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ScanRun(Base):
    __tablename__ = "scan_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)            # schedule | manual | pit_mode
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="standard")  # standard | pit
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    stage_counts: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    drop_reasons: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    errors: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)  # per-provider credits/requests/tokens/$
    __table_args__ = (
        CheckConstraint(_in("trigger", ("schedule", "manual", "pit_mode")), name="ck_scan_trigger"),
        CheckConstraint(
            _in("status", ("running", "ok", "partial", "failed", "skipped_locked", "skipped_budget")),
            name="ck_scan_status",
        ),
    )


class QueryLog(Base):
    __tablename__ = "query_log"
    id: Mapped[int] = mapped_column(BigIdType, primary_key=True, autoincrement=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scan_runs.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    place_id: Mapped[Optional[int]] = mapped_column(ForeignKey("places.id"))
    status: Mapped[str] = mapped_column(String(16), nullable=False)             # ok | empty | error | deferred
    http_status: Mapped[Optional[int]] = mapped_column(Integer)
    items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    credits: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    error_type: Mapped[Optional[str]] = mapped_column(Text)
    request_id: Mapped[Optional[str]] = mapped_column(Text)                     # provider request id, for support
    at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow, index=True)


class ItemSeen(Base):
    __tablename__ = "items_seen"
    item_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    first_seen: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow, index=True)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (UniqueConstraint("content_hash", "provider", name="items_seen_content"),)


class PendingItem(Base):
    __tablename__ = "pending_items"
    id: Mapped[int] = mapped_column(BigIdType, primary_key=True, autoincrement=True)
    item_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    normalized: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)  # NormalizedItem.to_dict()
    prefilter_score: Mapped[float] = mapped_column(Float, nullable=False)
    collected_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    published_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    purge_after: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)


class Place(Base):
    __tablename__ = "places"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    aliases: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    county: Mapped[str] = mapped_column(String(16), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("places.id"))
    town: Mapped[Optional[str]] = mapped_column(Text)                           # town/city containing it
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_places_source"),
        CheckConstraint(_in("kind", PLACE_KINDS), name="ck_places_kind"),
        CheckConstraint(_in("county", COUNTIES), name="ck_places_county"),
    )


class Hotspot(Base):
    __tablename__ = "hotspots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    place_id: Mapped[Optional[int]] = mapped_column(ForeignKey("places.id"))
    label: Mapped[str] = mapped_column(Text, nullable=False)
    county: Mapped[str] = mapped_column(String(16), nullable=False)
    town: Mapped[Optional[str]] = mapped_column(Text)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    primary_type: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    signals_7d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signals_30d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    distinct_sources_30d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confirmed_30d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    needs_review_30d: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    last_signal_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    assigned_team: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow)
    __table_args__ = (
        CheckConstraint(_in("primary_type", HOTSPOT_TYPES), name="ck_hotspots_type"),
        CheckConstraint(_in("status", HOTSPOT_STATUSES), name="ck_hotspots_status"),
        CheckConstraint(_in("county", COUNTIES), name="ck_hotspots_county"),
        Index("hotspots_status_score", "status", "score"),
    )


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(BigIdType, primary_key=True, autoincrement=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scan_runs.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    post_id_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    published_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    collected_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    relevance: Mapped[Optional[str]] = mapped_column(String(16))                 # relevant | uncertain | NULL (no verdict)
    review_reason: Mapped[Optional[str]] = mapped_column(Text)                   # why needs_human_review (e.g. refusal:<cat>, low_confidence)
    neutral_summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_quotes: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False, default=list)
    evidence_redacted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    location_text: Mapped[Optional[str]] = mapped_column(Text)
    location_kind: Mapped[Optional[str]] = mapped_column(String(16))
    place_id: Mapped[Optional[int]] = mapped_column(ForeignKey("places.id"))
    place_label: Mapped[Optional[str]] = mapped_column(Text)
    town: Mapped[Optional[str]] = mapped_column(Text)                            # town/hamlet name (self-reports: the only location kept)
    lat: Mapped[Optional[float]] = mapped_column(Float)
    lon: Mapped[Optional[float]] = mapped_column(Float)
    precision: Mapped[Optional[str]] = mapped_column(String(16))
    county: Mapped[Optional[str]] = mapped_column(String(16))
    people_count_est: Mapped[Optional[int]] = mapped_column(Integer)
    flags: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    checks: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)  # verifier V1-V8 results for "Why this was kept"
    time_reference: Mapped[str] = mapped_column(String(16), nullable=False)
    ai_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    decision_basis: Mapped[str] = mapped_column(Text, nullable=False)
    hotspot_id: Mapped[Optional[int]] = mapped_column(ForeignKey("hotspots.id"))
    reviewed_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    expires_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    __table_args__ = (
        CheckConstraint(_in("signal_type", SIGNAL_TYPES), name="ck_signals_type"),
        CheckConstraint(_in("status", SIGNAL_STATUSES), name="ck_signals_status"),
        CheckConstraint(f"precision IS NULL OR {_in('precision', PRECISIONS)}", name="ck_signals_precision"),
        CheckConstraint(f"county IS NULL OR {_in('county', COUNTIES)}", name="ck_signals_county"),
        # R7: self-reports never carry coordinates.
        CheckConstraint("status <> 'self_report' OR (lat IS NULL AND lon IS NULL)", name="ck_signals_selfreport_nocoords"),
        Index("signals_published", "published_at"),
        Index("signals_status", "status"),
        Index("signals_hotspot", "hotspot_id"),
    )


class HotspotWeekly(Base):
    __tablename__ = "hotspot_weekly"
    hotspot_id: Mapped[int] = mapped_column(ForeignKey("hotspots.id"), primary_key=True)
    week_start: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    signal_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    n_signals: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_confirmed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(BigIdType, primary_key=True, autoincrement=True)
    object_type: Mapped[str] = mapped_column(String(16), nullable=False)
    object_id: Mapped[int] = mapped_column(BigIdType, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(32))
    note: Mapped[Optional[str]] = mapped_column(Text)
    at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow, index=True)
    __table_args__ = (CheckConstraint(_in("object_type", ("signal", "hotspot")), name="ck_reviews_object"),)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigIdType, primary_key=True, autoincrement=True)
    at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    object_type: Mapped[Optional[str]] = mapped_column(String(32))
    object_id: Mapped[Optional[str]] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    ip_hash: Mapped[Optional[str]] = mapped_column(String(64))


class ApiUsage(Base):
    __tablename__ = "api_usage"
    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)                # local day in APP_TIMEZONE
    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    credits: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(BigIdType, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigIdType, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(BigIdType, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(BigIdType, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(14, 6), nullable=False, default=0)
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class GeocodeCache(Base):
    __tablename__ = "geocode_cache"
    query_norm: Mapped[str] = mapped_column(Text, primary_key=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow, index=True)


class ScanLock(Base):
    __tablename__ = "scan_lock"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    holder: Mapped[Optional[str]] = mapped_column(Text)
    acquired_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    expires_at: Mapped[Optional[dt.datetime]] = mapped_column(UTCDateTime)
    __table_args__ = (CheckConstraint("id = 1", name="ck_scan_lock_single"),)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONType, nullable=False)
    updated_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow)
