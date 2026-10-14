"""Shared data types for the scan pipeline. THIS FILE IS THE CONTRACT between pipeline modules.

Flow (docs/02 §4):  planner -> collectors -> normalize -> dedupe -> datefilter -> prefilter
  -> classifier -> verifier -> geocode/geofence -> decision -> privacy -> DB -> hotspots -> events

Rules that apply to every type here:
  * datetimes are timezone-aware UTC
  * no author/user fields anywhere (privacy rule R2); no media URLs (R1)
  * `text` fields carry already-scrubbed text ([handle]/[email]/[phone]/[link] replacements)
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Provider = Literal["socialcrawl", "exa", "firecrawl"]
Platform = Literal["reddit", "x", "tiktok", "instagram", "threads", "bluesky", "facebook", "youtube", "news", "web"]
SourceScope = Literal["li_scoped", "regional", "general"]
DateSource = Literal["platform", "provider", "page_metadata", "relative"]
SignalType = Literal["encampment", "individual_unsheltered", "vehicle_dwelling", "self_report"]
Status = Literal["ai_verified", "confirmed", "dismissed", "needs_human_review", "unlocated", "self_report"]


def _iso(d: dt.datetime | None) -> str | None:
    return None if d is None else d.astimezone(dt.timezone.utc).isoformat()


def _parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
@dataclass
class PlannedQuery:
    """One provider request the planner wants made (docs/04 §6)."""

    provider: Provider
    capability: str                 # socialcrawl: keyword_search | subreddit_new | subreddit_search | post_comments
                                    #              | hashtag_posts | news_search;  exa: search;  firecrawl: search
    query: str                      # query text ('' for subreddit_new / post_comments)
    platform: Optional[str] = None  # SocialCrawl platform slug (reddit, twitter, ...); None for exa/firecrawl
    family: str = "region"          # region | place | hot | subreddit | comments | backfill
    place_id: Optional[int] = None  # gazetteer place this query is about
    place_name: Optional[str] = None
    subreddit: Optional[str] = None
    params: dict[str, Any] = field(default_factory=dict)  # capability-specific (e.g. {"post_id": ...} for comments)
    source_scope: SourceScope = "general"                 # scope implied by the query target (LI subreddit -> li_scoped)
    priority: int = 0               # higher runs first when budgets bind
    est_cost: float = 1.0           # SocialCrawl credits, or 1 request for exa/firecrawl

    def key(self) -> str:
        return f"{self.provider}|{self.capability}|{self.platform}|{self.subreddit}|{self.query}|{sorted(self.params.items())}"


# ---------------------------------------------------------------------------
# Collected items
# ---------------------------------------------------------------------------
@dataclass
class ItemContext:
    query: str
    source_scope: SourceScope = "general"
    query_place_id: Optional[int] = None
    query_place_name: Optional[str] = None
    subreddit: Optional[str] = None
    hashtags: list[str] = field(default_factory=list)
    platform_place_tag: Optional[str] = None   # platform geotag NAME only, never coordinates
    is_comment: bool = False
    parent_title: Optional[str] = None
    outlet_domain: Optional[str] = None        # registrable host of the URL, e.g. newsday.com


@dataclass
class PrefilterResult:
    keep: bool
    score: float = 0.0
    matched_signal_terms: list[str] = field(default_factory=list)
    matched_places: list[str] = field(default_factory=list)       # gazetteer names found in text
    drop_reason: Optional[str] = None                             # no_signal_term | no_li_place | exclusion:<kind>


@dataclass
class NormalizedItem:
    """Common shape every collector produces (schemas/normalized-item.schema.json)."""

    item_hash: str                  # sha256(canonical_url)
    content_hash: str               # sha256(normalized text)
    provider: Provider
    platform: Platform
    url: str                        # canonical public permalink; never a profile URL
    platform_post_id: str
    published_at: dt.datetime
    date_source: DateSource
    collected_at: dt.datetime
    text: str                       # scrubbed; <= 6000 chars
    context: ItemContext
    title: str = ""
    lang: Optional[str] = None
    media_present: bool = False
    prefilter: Optional[PrefilterResult] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["published_at"] = _iso(self.published_at)
        d["collected_at"] = _iso(self.collected_at)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NormalizedItem":
        d = dict(d)
        d["published_at"] = _parse_iso(d["published_at"])
        d["collected_at"] = _parse_iso(d["collected_at"])
        d["context"] = ItemContext(**d["context"])
        if d.get("prefilter"):
            d["prefilter"] = PrefilterResult(**d["prefilter"])
        return cls(**d)


@dataclass
class QueryLogEntry:
    """One provider request, for the query_log table (no post content)."""

    provider: Provider
    endpoint: str
    query_text: str
    status: Literal["ok", "empty", "error", "deferred"]
    place_id: Optional[int] = None
    http_status: Optional[int] = None
    items: int = 0
    credits: float = 0.0
    cost_usd: float = 0.0
    latency_ms: Optional[int] = None
    error_type: Optional[str] = None
    request_id: Optional[str] = None
    at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


@dataclass
class ProviderHealth:
    provider: str                   # socialcrawl | exa | firecrawl | anthropic | nominatim | geo
    status: Literal["ok", "degraded", "unhealthy", "unknown", "disabled"]
    message: str = ""
    credits_remaining: Optional[float] = None
    checked_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["checked_at"] = _iso(self.checked_at)
        return d


@dataclass
class CollectResult:
    """What a collector returns for one scan."""

    items: list[NormalizedItem] = field(default_factory=list)
    logs: list[QueryLogEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)       # human-readable, no secrets, no post text
    health: Optional[ProviderHealth] = None
    deferred: list[PlannedQuery] = field(default_factory=list)  # skipped for budget; planner retries next scan
    credits_used: float = 0.0
    requests: int = 0
    cost_usd: float = 0.0
    discovered_endpoints: Optional[dict[str, Any]] = None  # SocialCrawl: resolved endpoint map to persist


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VerdictLocation(_Strict):
    mentioned: bool
    location_text: str
    location_kind: Literal["named_place", "intersection", "street", "business", "town_only", "none"]
    is_private_residence: bool


class VerdictFlags(_Strict):
    possible_minors: bool
    medical_concern: bool
    weather_exposure: bool
    safety_concern: bool


class VerdictQuote(_Strict):
    text: str
    supports: Literal["situation", "location", "recency", "count"]


class Verdict(_Strict):
    """Mirrors schemas/classification.schema.json exactly."""

    relevance: Literal["relevant", "not_relevant", "uncertain"]
    signal_type: Literal["encampment", "individual_unsheltered", "vehicle_dwelling", "self_report", "none"]
    not_relevant_reason: Literal[
        "none", "general_discussion_or_opinion", "policy_news_or_statistics", "fundraiser_event_or_promotion",
        "shelter_or_housed_situation", "animal", "outside_nassau_suffolk", "past_event_not_current",
        "metaphor_joke_or_unrelated", "insufficient_information",
    ]
    in_long_island_counties: Literal["yes", "no", "unclear"]
    area_basis: str
    time_reference: Literal["current", "ongoing", "past", "unclear"]
    location: VerdictLocation
    people_count_estimate: int
    flags: VerdictFlags
    evidence_quotes: list[VerdictQuote]
    neutral_summary: str
    decision_basis: str
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""                 # model that actually served (resp.model)
    request_id: Optional[str] = None


@dataclass
class ClassifierResult:
    kind: Literal["ok", "needs_human_review", "error", "retry_later", "provider_down"]
    verdict: Optional[Verdict] = None
    reason: str = ""                # e.g. refusal:<category>, invalid_json, max_tokens, bad_request
    model: str = ""
    usage: LLMUsage = field(default_factory=LLMUsage)
    retry_after_s: Optional[float] = None


# ---------------------------------------------------------------------------
# Verification, geography, decision
# ---------------------------------------------------------------------------
@dataclass
class CheckResult:
    """One verifier check (V1..V8) for the 'Why this was kept' checklist."""

    code: str                       # V1..V8
    passed: bool
    detail: str = ""                # short, no post text beyond what is already stored


@dataclass
class StoredQuote:
    text: str                       # possibly masked copy (docs/05 V2)
    supports: str
    masked: bool = False


@dataclass
class VerificationResult:
    quotes: list[StoredQuote]       # survivors of V1/V2 (masked copies), <= 2
    evidence_redacted: bool         # a quote was dropped for privacy (V2)
    location_ok: bool               # V3: location_text is a verbatim span and non-empty
    time_ok: bool                   # V5 passed
    time_needs_review: bool         # V5: unclear and older than 7 days
    time_past: bool                 # V5: past -> discard
    summary: str                    # V6-cleaned neutral_summary (template if it failed)
    decision_basis: str             # V6-cleaned
    people_count: Optional[int]     # V7: -1 -> None
    relevance: str                  # V7-coerced
    signal_type: str                # V7-coerced ('none' if not relevant)
    is_private_residence: bool      # V8
    checks: list[CheckResult] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)  # reasons that force needs_human_review


@dataclass
class GeoResult:
    """Final, snapped location (docs/06 §3-4). Coordinates are never a raw street-address geocode."""

    lat: Optional[float]
    lon: Optional[float]
    county: Optional[Literal["Nassau", "Suffolk"]]
    precision: Optional[Literal["public_place", "hamlet", "town"]]
    place_id: Optional[int] = None
    place_label: Optional[str] = None      # e.g. "Ronkonkoma LIRR station"
    town: Optional[str] = None             # town/city or hamlet name for lists
    source: Literal["gazetteer", "nominatim", "hamlet_fallback", "none"] = "none"
    in_geofence: bool = False              # point-in-polygon passed (V4)
    outside_reason: Optional[str] = None   # e.g. "point in Queens", "nominatim county mismatch"


@dataclass
class Decision:
    """Outcome of the decision table (docs/05 §6)."""

    store: bool
    status: Optional[Status] = None
    drop_reason: Optional[str] = None      # not_relevant:<reason> | low_confidence | past_event | outside_area | ...
    review_reason: Optional[str] = None    # why needs_human_review, if it is
