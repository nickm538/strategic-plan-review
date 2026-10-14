"""Privacy choke point: the ONLY path from pipeline objects to `signals` rows (docs/09 §3, R1-R7).
STUB - storage builder."""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any, Optional

from app.pipeline.types import ClassifierResult, Decision, GeoResult, NormalizedItem, VerificationResult

if TYPE_CHECKING:
    from app.pipeline.lexicon import Lexicon

# Exactly the Signal columns the pipeline may write. Anything else is dropped (property-tested).
ALLOWED_SIGNAL_FIELDS = frozenset({
    "scan_id", "provider", "platform", "url", "post_id_hash", "published_at", "collected_at", "signal_type",
    "status", "relevance", "review_reason", "neutral_summary", "evidence_quotes", "evidence_redacted",
    "location_text", "location_kind", "place_id", "place_label", "town", "lat", "lon", "precision", "county",
    "people_count_est", "flags", "checks", "time_reference", "ai_confidence", "model", "decision_basis",
    "expires_at",
})


def hash_post_id(platform: str, platform_post_id: str) -> str:
    """sha256(platform + ':' + platform_post_id) hex."""
    raise NotImplementedError


def build_signal_values(*, item: NormalizedItem, result: ClassifierResult, vr: Optional[VerificationResult],
                        geo: Optional[GeoResult], decision: Decision, scan_id: int, now: dt.datetime,
                        retention_days: int, self_report_town: Optional[str] = None,
                        self_report_county: Optional[str] = None) -> dict[str, Any]:
    """Return a dict with ONLY ALLOWED_SIGNAL_FIELDS keys. Enforces: no author fields (R2), no media (R1),
    quotes <= 2 x 30 words with handles/emails/phones/URLs scrubbed (R4), coordinates only from GeoResult
    (already snapped, R5), self_report -> lat/lon None + town only (R7), expires_at = collected_at +
    retention_days (R6). For refusal/needs_human_review with no verdict: signal_type 'unknown',
    neutral_summary 'Post flagged for staff review; no AI summary.', confidence 0."""
    raise NotImplementedError


def check_note(text: str, lexicon: "Lexicon") -> Optional[str]:
    """Staff notes (docs/07 §3): None if OK, else a friendly error message naming the problem category
    ('Notes must describe the place and situation only - remove descriptions of people (age).')."""
    raise NotImplementedError
