"""Decision table (docs/05 §6) including V4 (county + geofence).

Order of the rules (first match wins):
  1. relevance not_relevant (after V7)          -> drop 'not_relevant:<reason>'
  2. confidence < min_confidence_review         -> drop 'low_confidence'
  3. V5 past                                    -> drop 'past_event'
  4. in_long_island_counties == 'no'            -> drop 'outside_area'
  5. self_report (R7, never a pin):
       town + county (Nassau/Suffolk) known     -> 'self_report', unless a verifier review reason, relevance
                                                   'uncertain' or county 'unclear' sends it to review
       no town, verdict says in area            -> needs_human_review 'self_report_no_town'
       otherwise                                -> drop 'outside_area'
  6. verifier review reasons (no_verified_quote, time_unclear_old) -> needs_human_review (docs/05 §5: a
     relevant verdict with 0 verified quotes is never ai_verified; staff see the post link, A3)
  7. relevance 'uncertain'                      -> needs_human_review 'relevance_uncertain'
  8. geocode outside the polygon (a point outside, or a refused match reported via outside_reason)
                                                -> needs_human_review 'geofence_mismatch' (verdict yes) /
                                                   'county_unclear' (verdict unclear)
  9. no verifiable location (V3 failed, no geocode, or precision 'town') -> 'unlocated' (feed only)
 10. county 'unclear'                           -> needs_human_review 'county_unclear'
 11. confidence < min_confidence_map            -> needs_human_review 'low_confidence'
 12. otherwise                                  -> 'ai_verified' (mapped; precision public_place or hamlet,
                                                   point inside the Nassau/Suffolk polygon)
"""
from __future__ import annotations

import logging
from typing import Optional

from app.pipeline import textnorm
from app.pipeline.types import CheckResult, ClassifierResult, Decision, GeoResult, Verdict, VerificationResult

logger = logging.getLogger(__name__)

IN_AREA_COUNTIES = ("Nassau", "Suffolk")
MAPPABLE_PRECISIONS = ("public_place", "hamlet")

# Review reasons set here (the verifier adds no_verified_quote / time_unclear_old).
REVIEW_RELEVANCE_UNCERTAIN = "relevance_uncertain"
REVIEW_LOW_CONFIDENCE = "low_confidence"
REVIEW_COUNTY_UNCLEAR = "county_unclear"
REVIEW_GEOFENCE_MISMATCH = "geofence_mismatch"
REVIEW_SELF_REPORT_NO_TOWN = "self_report_no_town"

# Drop reasons (docs/03 drop_reasons / items_seen.decision vocabulary).
DROP_LOW_CONFIDENCE = "low_confidence"
DROP_PAST_EVENT = "past_event"
DROP_OUTSIDE_AREA = "outside_area"


def _has_point(geo: Optional[GeoResult]) -> bool:
    return geo is not None and geo.lat is not None and geo.lon is not None and geo.source != "none"


def _inside(geo: Optional[GeoResult]) -> bool:
    """Point-in-polygon passed AND the polygon named an in-scope county."""
    return _has_point(geo) and bool(geo.in_geofence) and geo.county in IN_AREA_COUNTIES  # type: ignore[union-attr]


def _geofence_failed(geo: Optional[GeoResult]) -> bool:
    """The geocoder resolved the place but it is not in Nassau/Suffolk: a point outside the polygon, or a
    result the geocoder refused and reported through outside_reason (no point)."""
    if geo is None:
        return False
    if _has_point(geo):
        return not _inside(geo)
    return bool((geo.outside_reason or "").strip())


def check_v4(verdict: Verdict, geo: Optional[GeoResult]) -> CheckResult:
    """V4: in_long_island_counties == 'yes' AND the geocoded point is inside the Nassau/Suffolk polygon."""
    area = verdict.in_long_island_counties
    if area == "no":
        return CheckResult("V4", False, "model: outside Nassau/Suffolk")
    if _geofence_failed(geo):
        why = textnorm.clean(geo.outside_reason or "")[:80]  # type: ignore[union-attr]  # geo module text, no post text
        return CheckResult("V4", False, "geocode outside the Nassau/Suffolk polygon" + (f" ({why})" if why else ""))
    if not _has_point(geo):
        return CheckResult("V4", False, f"county per model: {area}; no geocoded point")
    if area == "unclear":
        return CheckResult("V4", False, f"county unclear in post; point inside {geo.county} County")  # type: ignore[union-attr]
    return CheckResult("V4", True, f"inside {geo.county} County polygon")  # type: ignore[union-attr]


def _ensure_v4(vr: VerificationResult, verdict: Verdict, geo: Optional[GeoResult]) -> None:
    """Add the V4 check to vr.checks once, keeping V1..V8 order (idempotent)."""
    if any(c.code == "V4" for c in vr.checks):
        return
    v4 = check_v4(verdict, geo)
    for i, c in enumerate(vr.checks):
        if c.code > "V4":
            vr.checks.insert(i, v4)
            return
    vr.checks.append(v4)


def _drop(reason: str) -> Decision:
    return Decision(store=False, status=None, drop_reason=reason)


def _review(reason: str) -> Decision:
    return Decision(store=True, status="needs_human_review", review_reason=reason)


def decide(verdict: Verdict, vr: VerificationResult, geo: Optional[GeoResult], *,
           min_confidence_map: float, min_confidence_review: float,
           self_report_town: Optional[str] = None, self_report_county: Optional[str] = None) -> Decision:
    """Rows of docs/05 §6, in order:
      not_relevant or confidence < min_review                 -> store=False,
                                                                drop 'not_relevant:<reason>' / 'low_confidence'
      time past                                               -> store=False, drop 'past_event'
      in_long_island_counties == 'no'                         -> store=False, drop 'outside_area'
      self_report with town in area                           -> self_report
      geocoded point outside polygon while verdict says yes   -> needs_human_review ('geofence_mismatch')
      relevant, no verifiable location                        -> unlocated
      any vr.review_reasons / uncertain / conf < min_map / county unclear -> needs_human_review
      otherwise                                               -> ai_verified
    The exact precedence is in the module docstring. Also appends the V4 CheckResult to vr.checks (once)."""
    _ensure_v4(vr, verdict, geo)
    relevance = vr.relevance
    conf = float(verdict.confidence)
    area = verdict.in_long_island_counties

    if relevance == "not_relevant":
        reason = verdict.not_relevant_reason if verdict.not_relevant_reason != "none" else "unspecified"
        return _drop(f"not_relevant:{reason}")
    if conf < min_confidence_review:
        return _drop(DROP_LOW_CONFIDENCE)
    if vr.time_past:
        return _drop(DROP_PAST_EVENT)
    if area == "no":
        return _drop(DROP_OUTSIDE_AREA)

    if vr.signal_type == "self_report":
        town = textnorm.clean(self_report_town or "")
        if town and self_report_county in IN_AREA_COUNTIES:
            if vr.review_reasons:
                return _review(vr.review_reasons[0])
            if relevance == "uncertain":
                return _review(REVIEW_RELEVANCE_UNCERTAIN)
            if area == "unclear":
                return _review(REVIEW_COUNTY_UNCLEAR)
            return Decision(store=True, status="self_report")
        if area == "yes":
            return _review(REVIEW_SELF_REPORT_NO_TOWN)
        return _drop(DROP_OUTSIDE_AREA)

    if vr.review_reasons:
        return _review(vr.review_reasons[0])
    if relevance == "uncertain":
        return _review(REVIEW_RELEVANCE_UNCERTAIN)

    if vr.location_ok and _geofence_failed(geo):
        return _review(REVIEW_GEOFENCE_MISMATCH if area == "yes" else REVIEW_COUNTY_UNCLEAR)

    located = vr.location_ok and _inside(geo) and geo.precision in MAPPABLE_PRECISIONS  # type: ignore[union-attr]
    if not located:
        return Decision(store=True, status="unlocated")

    if area == "unclear":
        return _review(REVIEW_COUNTY_UNCLEAR)
    if conf < min_confidence_map:
        return _review(REVIEW_LOW_CONFIDENCE)
    return Decision(store=True, status="ai_verified")


def decide_without_verdict(result: ClassifierResult) -> Optional[Decision]:
    """For a classifier result that carries no verdict: 'needs_human_review' (refusal:<category>,
    invalid_json, max_tokens, ...) -> stored for staff with that reason and no AI summary (docs/05 §3).
    Other kinds (retry_later, provider_down, error) are not decided here -> None (keep the item queued)."""
    if result.kind == "needs_human_review" and result.verdict is None:
        return _review(result.reason or "classifier_review")
    return None
