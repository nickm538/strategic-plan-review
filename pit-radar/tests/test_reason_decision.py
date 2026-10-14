"""Decision table (docs/05 §6) and V4, row by row, plus the confidence thresholds (docs/10 §1)."""
from __future__ import annotations

import pytest

from app.pipeline import verifier as ver
from app.pipeline.decision import check_v4, decide, decide_without_verdict
from app.pipeline.types import ClassifierResult, GeoResult
from tests.test_reason_support import DEFAULT_POST, NOW, FakeLexicon, make_item, make_verdict

LEX = FakeLexicon()
MAP, REVIEW = 0.70, 0.50

# Synthetic GeoResults. The decision table reads only presence of a point, precision, county, in_geofence
# and outside_reason, so the coordinates are deliberately not real places (1.0 / -1.0).
STATION = GeoResult(lat=1.0, lon=-1.0, county="Suffolk", precision="public_place", place_id=11,
                    place_label="Ronkonkoma LIRR station", town="Ronkonkoma", source="gazetteer", in_geofence=True)
HAMLET = GeoResult(lat=1.0, lon=-1.0, county="Nassau", precision="hamlet", place_id=12, place_label="Merrick",
                   town="Merrick", source="hamlet_fallback", in_geofence=True)
TOWN = GeoResult(lat=1.0, lon=-1.0, county="Nassau", precision="town", place_label="Hempstead (town)",
                 town="Hempstead", source="gazetteer", in_geofence=True)
OUTSIDE = GeoResult(lat=1.0, lon=-1.0, county=None, precision="public_place", source="nominatim",
                    in_geofence=False, outside_reason="point in Queens")
REFUSED = GeoResult(lat=None, lon=None, county=None, precision=None, source="none",
                    outside_reason="nominatim county mismatch")
NONE = GeoResult(lat=None, lon=None, county=None, precision=None, source="none")


def go(geo=STATION, *, text=DEFAULT_POST, age_hours=3.0, sr_town=None, sr_county=None, **overrides):
    verdict = make_verdict(**overrides)
    vr = ver.verify(make_item(text, age_hours=age_hours), verdict, lexicon=LEX, now=NOW)
    d = decide(verdict, vr, geo, min_confidence_map=MAP, min_confidence_review=REVIEW,
               self_report_town=sr_town, self_report_county=sr_county)
    return d, vr


def status(d):
    return d.status if d.store else f"drop:{d.drop_reason}"


# --- rows that discard ------------------------------------------------------------------------------
@pytest.mark.parametrize("reason", ["fundraiser_event_or_promotion", "policy_news_or_statistics", "animal",
                                    "outside_nassau_suffolk", "past_event_not_current"])
def test_not_relevant_dropped_with_reason(reason):
    d, _ = go(relevance="not_relevant", signal_type="none", not_relevant_reason=reason, evidence_quotes=[])
    assert (d.store, d.status, d.drop_reason) == (False, None, f"not_relevant:{reason}")


def test_not_relevant_without_reason_is_unspecified():
    d, _ = go(relevance="relevant", signal_type="none")       # V7 coerces to not relevant
    assert d.drop_reason == "not_relevant:unspecified"


def test_confidence_below_review_threshold_dropped():
    d, _ = go(confidence=0.49)
    assert status(d) == "drop:low_confidence"
    d, _ = go(confidence=0.50)                                 # boundary: 0.50 is kept (review)
    assert status(d) == "needs_human_review" and d.review_reason == "low_confidence"


def test_past_dropped():
    d, _ = go(time_reference="past")
    assert status(d) == "drop:past_event"


def test_outside_area_dropped():
    d, _ = go(in_long_island_counties="no")
    assert status(d) == "drop:outside_area"


# --- self-reports (R7) -------------------------------------------------------------------------------
SELF = dict(signal_type="self_report", location__location_text="Hempstead", location__location_kind="town_only",
            evidence_quotes=[{"text": "I'm homeless in Hempstead, sleeping in my car", "supports": "situation"}])
SELF_POST = "I'm homeless in Hempstead, sleeping in my car since last week. Any advice?"


def test_self_report_with_town_in_area():
    d, _ = go(None, text=SELF_POST, sr_town="Hempstead", sr_county="Nassau", **SELF)
    assert (d.store, d.status, d.review_reason) == (True, "self_report", None)


def test_self_report_no_town_but_in_area_goes_to_review():
    d, _ = go(None, text=SELF_POST, **SELF)
    assert status(d) == "needs_human_review" and d.review_reason == "self_report_no_town"


def test_self_report_no_town_area_unclear_dropped():
    d, _ = go(None, text=SELF_POST, in_long_island_counties="unclear", **SELF)
    assert status(d) == "drop:outside_area"


def test_self_report_town_without_in_area_county_dropped():
    d, _ = go(None, text=SELF_POST, sr_town="Hempstead", sr_county=None, in_long_island_counties="unclear", **SELF)
    assert status(d) == "drop:outside_area"


def test_self_report_without_verified_quote_reviewed():
    over = {**SELF, "evidence_quotes": [{"text": "I am homeless in Hempstead now", "supports": "situation"}]}
    d, _ = go(None, text=SELF_POST, sr_town="Hempstead", sr_county="Nassau", **over)
    assert status(d) == "needs_human_review" and d.review_reason == "no_verified_quote"


def test_self_report_county_unclear_reviewed():
    d, _ = go(None, text=SELF_POST, sr_town="Hempstead", sr_county="Nassau", in_long_island_counties="unclear", **SELF)
    assert d.review_reason == "county_unclear"


def test_self_report_never_ai_verified_even_with_a_geocode():
    d, _ = go(STATION, text=SELF_POST, sr_town="Hempstead", sr_county="Nassau", **SELF)
    assert d.status == "self_report"


# --- review rows ------------------------------------------------------------------------------------
def test_no_verified_quote_reviewed():
    d, _ = go(evidence_quotes=[{"text": "tents near some station", "supports": "situation"}])
    assert status(d) == "needs_human_review" and d.review_reason == "no_verified_quote"


def test_unclear_time_old_reviewed():
    d, _ = go(time_reference="unclear", age_hours=24 * 12)
    assert d.review_reason == "time_unclear_old"


def test_uncertain_reviewed():
    d, _ = go(relevance="uncertain", confidence=0.9)
    assert status(d) == "needs_human_review" and d.review_reason == "relevance_uncertain"


@pytest.mark.parametrize("geo", [OUTSIDE, REFUSED])
def test_geofence_mismatch_reviewed(geo):
    d, vr = go(geo)
    assert status(d) == "needs_human_review" and d.review_reason == "geofence_mismatch"
    v4 = next(c for c in vr.checks if c.code == "V4")
    assert not v4.passed and "outside" in v4.detail


def test_county_unclear_reviewed_even_inside_polygon():
    d, _ = go(STATION, in_long_island_counties="unclear")
    assert d.review_reason == "county_unclear"
    d, _ = go(OUTSIDE, in_long_island_counties="unclear")
    assert d.review_reason == "county_unclear"


@pytest.mark.parametrize("conf", [0.50, 0.6, 0.699])
def test_located_low_confidence_reviewed(conf):
    d, _ = go(confidence=conf)
    assert status(d) == "needs_human_review" and d.review_reason == "low_confidence"


def test_in_geofence_but_no_county_not_mapped():
    weird = GeoResult(lat=1.0, lon=-1.0, county=None, precision="public_place", source="gazetteer", in_geofence=True)
    d, _ = go(weird)
    assert d.status == "needs_human_review" and d.review_reason == "geofence_mismatch"


# --- unlocated --------------------------------------------------------------------------------------
@pytest.mark.parametrize("geo", [None, NONE, TOWN])
def test_no_verifiable_location_is_unlocated(geo):
    d, _ = go(geo)
    assert (d.store, d.status) == (True, "unlocated")


def test_v3_failure_ignores_geocode():
    d, _ = go(STATION, location__location_text="the Hicksville LIRR station")
    assert d.status == "unlocated"


def test_unlocated_low_confidence_still_unlocated():
    d, _ = go(None, confidence=0.6)
    assert d.status == "unlocated"


# --- mapped -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("geo", [STATION, HAMLET])
@pytest.mark.parametrize("conf", [0.70, 0.95])
def test_ai_verified(geo, conf):
    d, vr = go(geo, confidence=conf)
    assert (d.store, d.status, d.drop_reason, d.review_reason) == (True, "ai_verified", None, None)
    assert [c.code for c in vr.checks] == ["V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8"]
    assert next(c for c in vr.checks if c.code == "V4").passed


def test_unclear_time_recent_mapped():
    d, _ = go(time_reference="unclear", age_hours=24 * 3)
    assert d.status == "ai_verified"


# --- V4 ---------------------------------------------------------------------------------------------
def test_check_v4_outcomes():
    v_yes, v_no, v_unclear = (make_verdict(in_long_island_counties=a) for a in ("yes", "no", "unclear"))
    assert check_v4(v_yes, STATION).passed and "Suffolk" in check_v4(v_yes, STATION).detail
    assert not check_v4(v_no, STATION).passed
    assert not check_v4(v_yes, OUTSIDE).passed and "Queens" in check_v4(v_yes, OUTSIDE).detail
    assert not check_v4(v_unclear, STATION).passed
    assert not check_v4(v_yes, None).passed and "no geocoded point" in check_v4(v_yes, None).detail
    assert not check_v4(v_yes, NONE).passed and "no geocoded point" in check_v4(v_yes, NONE).detail
    assert not check_v4(v_yes, REFUSED).passed and "county mismatch" in check_v4(v_yes, REFUSED).detail


# --- results without a verdict ---------------------------------------------------------------------
def test_decide_without_verdict():
    d = decide_without_verdict(ClassifierResult(kind="needs_human_review", reason="refusal:bio"))
    assert (d.store, d.status, d.review_reason) == (True, "needs_human_review", "refusal:bio")
    for kind in ("retry_later", "provider_down", "error"):
        assert decide_without_verdict(ClassifierResult(kind=kind)) is None
