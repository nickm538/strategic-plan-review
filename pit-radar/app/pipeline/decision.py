"""Decision table (docs/05 §6) including V4 (county + geofence). STUB - reasoning builder."""
from __future__ import annotations

from typing import Optional

from app.pipeline.types import CheckResult, Decision, GeoResult, Verdict, VerificationResult


def check_v4(verdict: Verdict, geo: Optional[GeoResult]) -> CheckResult:
    raise NotImplementedError


def decide(verdict: Verdict, vr: VerificationResult, geo: Optional[GeoResult], *,
           min_confidence_map: float, min_confidence_review: float,
           self_report_town: Optional[str] = None, self_report_county: Optional[str] = None) -> Decision:
    """Rows of docs/05 §6, in order:
      not_relevant or confidence < min_review                 -> store=False, drop 'not_relevant:<reason>' / 'low_confidence'
      time past                                               -> store=False, drop 'past_event'
      in_long_island_counties == 'no'                         -> store=False, drop 'outside_area'
      self_report with town in area                           -> self_report
      geocoded point outside polygon while verdict says yes   -> needs_human_review ('geofence_mismatch')
      relevant, no verifiable location                        -> unlocated
      any vr.review_reasons / uncertain / conf < min_map / county unclear -> needs_human_review
      otherwise                                               -> ai_verified"""
    raise NotImplementedError
