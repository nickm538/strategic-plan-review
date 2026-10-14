"""Verifier V1-V3, V5-V8 (docs/05 §5; docs/10 §1 'Verifier V1-V8'). Most tests use the FakeLexicon; the
privacy cases are repeated against the real Lexicon (guarded)."""
from __future__ import annotations


import pytest

from app.pipeline import verifier as ver
from app.pipeline.decision import decide
from tests.test_reason_support import DEFAULT_POST, NOW, FakeLexicon, load_real_lexicon, make_item, make_verdict

LEX = FakeLexicon()


def run(text=DEFAULT_POST, *, lexicon=LEX, age_hours=3.0, **verdict_overrides):
    item = make_item(text, age_hours=age_hours)
    verdict = make_verdict(**verdict_overrides)
    return verdict, ver.verify(item, verdict, lexicon=lexicon, now=NOW)


def q(text, supports="situation"):
    return {"text": text, "supports": supports}


def check(vr, code):
    (c,) = [c for c in vr.checks if c.code == code]
    return c


# ---------------------------------------------------------------------------
# V1 verbatim quotes
# ---------------------------------------------------------------------------
def test_v1_exact_substring_passes_across_quote_dash_case_whitespace_variants():
    post = "They’ve set up a tarp — and two tents — behind the   Bay Shore library."
    _, vr = run(post, evidence_quotes=[q("they've set up a tarp - and two TENTS - behind the Bay Shore library")])
    assert [s.text for s in vr.quotes] == ["they've set up a tarp - and two TENTS - behind the Bay Shore library"]
    assert check(vr, "V1").passed and "1 of 1" in check(vr, "V1").detail
    assert vr.review_reasons == []


def test_v1_paraphrased_quote_fails():
    _, vr = run(evidence_quotes=[q("a couple of tents are set up behind the Ronkonkoma station")])
    assert vr.quotes == []
    assert not check(vr, "V1").passed and "not found verbatim" in check(vr, "V1").detail
    assert vr.review_reasons == ["no_verified_quote"]


def test_v1_ellipsis_joined_quote_fails():
    _, vr = run(evidence_quotes=[q("two tents in the woods ... on my way to the platform")])
    assert vr.quotes == [] and "ellipsis" in check(vr, "V1").detail
    _, vr = run(evidence_quotes=[q("two tents in the woods… on my way to the platform")])
    assert vr.quotes == []


def test_v1_quote_over_30_words_is_dropped_not_trimmed():
    words = ("one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
             "seventeen eighteen nineteen twenty twentyone twentytwo twentythree twentyfour twentyfive twentysix "
             "twentyseven twentyeight twentynine thirty thirtyone").split()
    post = " ".join(words) + " tents behind the station."
    _, vr = run(post, evidence_quotes=[q(" ".join(words)), q(" ".join(words[:30]), "count")])
    assert [s.text for s in vr.quotes] == [" ".join(words[:30])]
    assert "over 30 words" in check(vr, "V1").detail


def test_v1_one_word_quote_rejected():
    _, vr = run(evidence_quotes=[q("tents")])
    assert vr.quotes == [] and "too short" in check(vr, "V1").detail


def test_v1_keeps_at_most_two_preferring_distinct_supports():
    post = ("Two tents behind the Ronkonkoma train station. Someone is sleeping in one of them. "
            "They have been there for weeks. I counted three people last night.")
    quotes = [q("Two tents behind the Ronkonkoma train station", "situation"),
              q("Someone is sleeping in one of them", "situation"),
              q("They have been there for weeks", "recency"),
              q("I counted three people last night", "count")]
    _, vr = run(post, evidence_quotes=quotes)
    assert [(s.text, s.supports) for s in vr.quotes] == [
        ("Two tents behind the Ronkonkoma train station", "situation"),
        ("They have been there for weeks", "recency"),
    ]
    _, vr = run(post, evidence_quotes=quotes[:2])
    assert len(vr.quotes) == 2                      # same supports: still two kept
    _, vr = run(post, evidence_quotes=[quotes[0], quotes[0]])
    assert len(vr.quotes) == 1                      # duplicates collapse


# ---------------------------------------------------------------------------
# V2 quote privacy
# ---------------------------------------------------------------------------
def test_v2_gendered_word_masked_in_stored_copy():
    post = "A guy has been sleeping on the bench at the Hicksville station every night and his bags are there."
    _, vr = run(post, evidence_quotes=[q("A guy has been sleeping on the bench at the Hicksville station"),
                                       q("his bags are there", "recency")])
    assert [(s.text, s.masked) for s in vr.quotes] == [
        ("A [person] has been sleeping on the bench at the Hicksville station", True),
        ("[their] bags are there", True),
    ]
    assert not vr.evidence_redacted and check(vr, "V2").passed and "2 quotes masked" in check(vr, "V2").detail


@pytest.mark.parametrize("bad_quote", [
    "an older man in a hoodie sleeping at the Hicksville station",     # age + clothing
    "sleeping in a car outside 123 Main St in Merrick",                # street address
    "the plate number is on the van at the Merrick station",           # plate
])
def test_v2_drop_terms_remove_quote_and_set_redacted(bad_quote):
    post = f"Update: {bad_quote}. People are sleeping at the station every night."
    _, vr = run(post, evidence_quotes=[q(bad_quote), q("People are sleeping at the station every night", "recency")])
    assert [s.text for s in vr.quotes] == ["People are sleeping at the station every night"]
    assert vr.evidence_redacted is True
    assert not check(vr, "V2").passed and "1 quote dropped" in check(vr, "V2").detail


def test_v2_dropping_last_quote_forces_review():
    post = "An older man in a hoodie is sleeping at the Hicksville station."
    _, vr = run(post, evidence_quotes=[q("An older man in a hoodie is sleeping at the Hicksville station")])
    assert vr.quotes == [] and vr.evidence_redacted
    assert vr.review_reasons == ["no_verified_quote"]


def test_v2_ice_on_platform_not_flagged_but_ICE_is():
    post = "Watch out for ice on the platform where people sleep at the Babylon station."
    _, vr = run(post, evidence_quotes=[q("ice on the platform where people sleep at the Babylon station")])
    assert len(vr.quotes) == 1 and not vr.evidence_redacted
    post2 = "ICE was at the Babylon station where people sleep."
    _, vr2 = run(post2, evidence_quotes=[q("ICE was at the Babylon station where people sleep")])
    assert vr2.quotes == [] and vr2.evidence_redacted


def test_v2_placeholder_handle_quote_dropped():
    post = "[handle] has been sleeping behind the Patchogue library all week."
    _, vr = run(post, evidence_quotes=[q("[handle] has been sleeping behind the Patchogue library")])
    assert vr.quotes == [] and vr.evidence_redacted


def test_v2_privacy_checks_only_v1_survivors():
    # a paraphrased (V1-failing) quote with a drop term is not counted as a privacy drop
    _, vr = run(evidence_quotes=[q("an older man in a hoodie at the station")])
    assert vr.evidence_redacted is False


# ---------------------------------------------------------------------------
# V3 location span
# ---------------------------------------------------------------------------
def test_v3_location_in_text_ok():
    _, vr = run()
    assert vr.location_ok and check(vr, "V3").passed


def test_v3_location_not_in_text_is_unlocated():
    verdict, vr = run(location__location_text="the Hicksville LIRR station")
    assert not vr.location_ok and not check(vr, "V3").passed
    d = decide(verdict, vr, None, min_confidence_map=0.7, min_confidence_review=0.5)
    assert (d.store, d.status) == (True, "unlocated")


def test_v3_not_mentioned_or_none_kind():
    _, vr = run(location__mentioned=False)
    assert not vr.location_ok
    _, vr = run(location__location_kind="none")
    assert not vr.location_ok
    _, vr = run(location__location_text="")
    assert not vr.location_ok and check(vr, "V3").detail == "no location given"


def test_v3_stored_location_text_withholds_addresses():
    assert ver.stored_location_text("my street in Merrick (123 Smith Ave)", LEX) is None
    assert ver.stored_location_text("the guy's spot behind the Merrick library", LEX) == \
        "the [person]'s spot behind the Merrick library"
    assert ver.stored_location_text("the Ronkonkoma train station", LEX) == "the Ronkonkoma train station"


# ---------------------------------------------------------------------------
# V5 time
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tr", ["current", "ongoing"])
def test_v5_current_and_ongoing_ok(tr):
    _, vr = run(time_reference=tr, age_hours=24 * 20)
    assert vr.time_ok and not vr.time_past and not vr.time_needs_review


def test_v5_past_discarded():
    verdict, vr = run(time_reference="past")
    assert vr.time_past and not vr.time_ok
    d = decide(verdict, vr, None, min_confidence_map=0.7, min_confidence_review=0.5)
    assert (d.store, d.drop_reason) == (False, "past_event")


def test_v5_unclear_recent_kept_old_reviewed():
    _, vr = run(time_reference="unclear", age_hours=24 * 3)
    assert vr.time_ok and vr.review_reasons == []
    _, vr = run(time_reference="unclear", age_hours=24 * 12)
    assert not vr.time_ok and vr.time_needs_review and vr.review_reasons == ["time_unclear_old"]
    _, vr = run(time_reference="unclear", age_hours=24 * 7)          # boundary: exactly 7 days is kept
    assert vr.time_ok


# ---------------------------------------------------------------------------
# V6 summary privacy
# ---------------------------------------------------------------------------
def test_v6_clean_summary_kept():
    verdict, vr = run()
    assert vr.summary == verdict.neutral_summary and check(vr, "V6").passed


@pytest.mark.parametrize("summary", [
    "An older man in a hoodie sleeps at the Hicksville station.",     # drop tier
    "A guy sleeps at the Hicksville station.",                        # mask tier also fails V6
    "Post by [handle] reports tents at the station.",                 # placeholder
    "Call 631-555-0100 about the tents at the station.",              # phone
    "Tents at the station. " * 20,                                    # > 240 chars
])
def test_v6_bad_summary_replaced_by_template(summary):
    _, vr = run(neutral_summary=summary, time_reference="ongoing")
    assert vr.summary == "Encampment reported (ongoing)."
    assert not check(vr, "V6").passed


def test_v6_decision_basis_withheld():
    _, vr = run(decision_basis="Post says a drunk guy is at the station.")
    assert vr.decision_basis == "Summary withheld by privacy check."
    assert not check(vr, "V6").passed
    assert vr.summary != "Summary withheld by privacy check."


def test_v6_empty_summary_gets_template_without_failing():
    _, vr = run(neutral_summary="", signal_type="vehicle_dwelling", time_reference="current")
    assert vr.summary == "Vehicle dwelling reported (current)." and check(vr, "V6").passed


def test_template_summary_formats():
    assert ver.template_summary("encampment", None, "ongoing") == "Encampment reported (ongoing)."
    assert ver.template_summary("individual_unsheltered", "Ronkonkoma LIRR station", "current") == \
        "Person(s) sleeping outside reported at Ronkonkoma LIRR station (current)."
    assert ver.template_summary("none", None, "unclear") == "Possible signal reported (time unclear)."
    assert len(ver.template_summary("encampment", "x" * 1000, "past")) <= 240


# ---------------------------------------------------------------------------
# V7 consistency
# ---------------------------------------------------------------------------
def test_v7_not_relevant_forces_type_none():
    _, vr = run(relevance="not_relevant", signal_type="encampment", not_relevant_reason="policy_news_or_statistics")
    assert (vr.relevance, vr.signal_type) == ("not_relevant", "none")
    assert not check(vr, "V7").passed
    assert vr.review_reasons == []              # not relevant: missing quotes do not matter


@pytest.mark.parametrize("relevance", ["relevant", "uncertain"])
def test_v7_type_none_forces_not_relevant(relevance):
    _, vr = run(relevance=relevance, signal_type="none")
    assert vr.relevance == "not_relevant" and not check(vr, "V7").passed


@pytest.mark.parametrize("raw, stored, consistent", [(-1, None, True), (-7, None, False), (0, 0, True), (3, 3, True)])
def test_v7_people_count(raw, stored, consistent):
    _, vr = run(people_count_estimate=raw)
    assert vr.people_count == stored and check(vr, "V7").passed is consistent


# ---------------------------------------------------------------------------
# V8 residence
# ---------------------------------------------------------------------------
def test_v8_private_residence_flag():
    _, vr = run(location__is_private_residence=True)
    assert vr.is_private_residence and "hamlet-level" in check(vr, "V8").detail


def test_v8_street_address_in_location_forces_hamlet_level():
    post = "Guy sleeping in a car on my street in Merrick (123 Smith Ave) every night."
    _, vr = run(post, location__location_text="my street in Merrick (123 Smith Ave)", location__location_kind="street",
                evidence_quotes=[q("sleeping in a car on my street in Merrick")])
    assert vr.location_ok and vr.is_private_residence
    assert "street address" in check(vr, "V8").detail


# ---------------------------------------------------------------------------
# Checks list
# ---------------------------------------------------------------------------
def test_checks_codes_and_v4_added_once_by_decide():
    verdict, vr = run()
    assert [c.code for c in vr.checks] == ["V1", "V2", "V3", "V5", "V6", "V7", "V8"]
    decide(verdict, vr, None, min_confidence_map=0.7, min_confidence_review=0.5)
    decide(verdict, vr, None, min_confidence_map=0.7, min_confidence_review=0.5)
    assert [c.code for c in vr.checks] == ["V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8"]


def test_check_details_never_contain_post_text():
    secret = "Zorblax"
    post = f"Two tents behind the {secret} plaza in Bay Shore and an older man in a hoodie sleeps there."
    _, vr = run(post, evidence_quotes=[q("an older man in a hoodie sleeps there"), q(f"behind the {secret} wrong")],
                location__location_text=f"the {secret} plaza in Bay Shore", neutral_summary=f"Drunk guy at {secret}.")
    for c in vr.checks:
        assert secret.lower() not in c.detail.lower()
        assert "hoodie" not in c.detail and "older" not in c.detail


# ---------------------------------------------------------------------------
# Same privacy cases with the real lexicon (guarded)
# ---------------------------------------------------------------------------
def test_real_lexicon_privacy_cases():
    real = load_real_lexicon()
    post = ("A guy in a hoodie is sleeping on the bench at the Hicksville station. "
            "He has been there every night this week. Watch for ice on the platform.")
    _, vr = run(post, lexicon=real,
                evidence_quotes=[q("A guy in a hoodie is sleeping on the bench at the Hicksville station"),
                                 q("He has been there every night this week", "recency"),
                                 q("Watch for ice on the platform", "location")],
                neutral_summary="A man in a hoodie sleeps at the Hicksville station.")
    assert vr.evidence_redacted
    assert [(s.text, s.masked) for s in vr.quotes] == [
        ("[they] has been there every night this week", True),
        ("Watch for ice on the platform", False),
    ]
    assert vr.summary == "Encampment reported (current)."
    assert ver.stored_location_text("my street in Merrick (123 Smith Ave)", real) is None
