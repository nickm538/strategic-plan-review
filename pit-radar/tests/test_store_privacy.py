"""Privacy choke point (docs/09 §3, docs/10 §1): allow-list, R1/R2 URLs, R4 quotes, R5 location, R7 self-reports.

All content below is synthetic (no real people or handles)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
import string
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.pipeline import privacy
from app.pipeline.privacy import (
    ALLOWED_SIGNAL_FIELDS,
    PrivacyViolation,
    build_signal_values,
    hash_post_id,
)
from app.pipeline.types import (
    CheckResult,
    ClassifierResult,
    Decision,
    GeoResult,
    ItemContext,
    LLMUsage,
    NormalizedItem,
    StoredQuote,
    Verdict,
    VerificationResult,
)

NOW = dt.datetime(2026, 10, 14, 16, 0, tzinfo=dt.timezone.utc)
POST_TEXT = ("Tents set up again in the wooded area behind the Exampleville station parking lot, "
             "been there for weeks now.")


def make_item(**kw) -> NormalizedItem:
    base = dict(
        item_hash="a" * 64, content_hash="b" * 64, provider="socialcrawl", platform="reddit",
        url="https://www.reddit.com/r/longisland/comments/abc123/tents_behind_station",
        platform_post_id="abc123", published_at=NOW - dt.timedelta(days=1), date_source="platform",
        collected_at=NOW - dt.timedelta(hours=2), text=POST_TEXT,
        context=ItemContext(query="homeless encampment", source_scope="li_scoped", subreddit="longisland"),
        title="Tents behind the station", media_present=True,
    )
    base.update(kw)
    return NormalizedItem(**base)


def make_verdict(**kw) -> Verdict:
    base = dict(
        relevance="relevant", signal_type="encampment", not_relevant_reason="none",
        in_long_island_counties="yes", area_basis="names a Long Island station",
        time_reference="ongoing",
        location=dict(mentioned=True, location_text="Exampleville station parking lot", location_kind="named_place",
                      is_private_residence=False),
        people_count_estimate=-1,
        flags=dict(possible_minors=False, medical_concern=False, weather_exposure=True, safety_concern=False),
        evidence_quotes=[dict(text="Tents set up again in the wooded area", supports="situation")],
        neutral_summary="Post reports tents in the wooded area behind the station parking lot.",
        decision_basis="Quote describes tents at a named station.",
        confidence=0.86,
    )
    base.update(kw)
    return Verdict.model_validate(base)


def make_vr(**kw) -> VerificationResult:
    base = dict(
        quotes=[StoredQuote(text="Tents set up again in the wooded area", supports="situation", masked=False)],
        evidence_redacted=False, location_ok=True, time_ok=True, time_needs_review=False, time_past=False,
        summary="Post reports tents in the wooded area behind the station parking lot.",
        decision_basis="Quote describes tents at a named station.",
        people_count=None, relevance="relevant", signal_type="encampment", is_private_residence=False,
        checks=[CheckResult("V1", True, "1 of 1 quotes verbatim"), CheckResult("V2", True, "quotes clean"),
                CheckResult("V3", True, "location span found in post"), CheckResult("V4", True, "inside Suffolk"),
                CheckResult("V5", True, "time reference ongoing"), CheckResult("V6", True, "summary ok; basis ok"),
                CheckResult("V7", True, "consistent"), CheckResult("V8", True, "not a private residence")],
        review_reasons=[],
        location_text="Exampleville station parking lot",  # the verifier's privacy-checked span
    )
    base.update(kw)
    return VerificationResult(**base)


def make_geo(**kw) -> GeoResult:
    base = dict(lat=40.8081, lon=-73.1054, county="Suffolk", precision="public_place", place_id=None,
                place_label="Exampleville LIRR station", town="Exampleville", source="gazetteer", in_geofence=True)
    base.update(kw)
    return GeoResult(**base)


def make_result(verdict=None, **kw) -> ClassifierResult:
    base = dict(kind="ok", verdict=verdict if verdict is not None else make_verdict(), reason="",
                model="claude-opus-5-5", usage=LLMUsage(model="claude-opus-5-5"))
    base.update(kw)
    return ClassifierResult(**base)


def build(**kw):
    args = dict(item=make_item(), result=make_result(), vr=make_vr(), geo=make_geo(),
                decision=Decision(store=True, status="ai_verified"), scan_id=1, now=NOW, retention_days=30)
    args.update(kw)
    return build_signal_values(**args)


# ---------------------------------------------------------------------------
def test_hash_post_id_is_sha256_of_platform_and_id():
    assert hash_post_id("reddit", "abc123") == hashlib.sha256(b"reddit:abc123").hexdigest()
    assert hash_post_id("reddit", "abc123") != hash_post_id("x", "abc123")


def test_basic_signal_values_shape_and_fields():
    v = build()
    assert set(v) == set(ALLOWED_SIGNAL_FIELDS)
    assert v["status"] == "ai_verified" and v["signal_type"] == "encampment"
    assert v["lat"] == pytest.approx(40.8081) and v["precision"] == "public_place" and v["county"] == "Suffolk"
    assert v["post_id_hash"] == hash_post_id("reddit", "abc123")
    assert v["expires_at"] == v["collected_at"] + dt.timedelta(days=30)
    assert v["flags"] == {"possible_minors": False, "medical_concern": False, "weather_exposure": True,
                          "safety_concern": False}
    assert v["checks"]["V1"] == {"passed": True, "detail": "1 of 1 quotes verbatim"}
    assert set(v["checks"]) == {f"V{i}" for i in range(1, 9)}
    assert v["evidence_quotes"] == [{"text": "Tents set up again in the wooded area", "supports": "situation",
                                     "masked": False}]
    assert v["location_text"] == "Exampleville station parking lot"
    assert v["people_count_est"] is None and v["review_reason"] is None
    assert v["model"] == "claude-opus-5-5"


def test_signal_values_persist(db):
    from app.models import ScanRun, Signal
    db.add(ScanRun(id=1, trigger="manual", status="running", started_at=NOW))
    db.flush()
    db.add(Signal(**build()))
    db.commit()
    assert db.query(Signal).count() == 1


# ---------------------------------------------------------------------------
# Property-style: random extra attributes never reach the output
# ---------------------------------------------------------------------------
_FORBIDDEN_KEYS = ("author", "author_handle", "username", "user_name", "display_name", "profile_url",
                   "author_url", "followers", "follower_count", "avatar", "avatar_url", "media", "media_url",
                   "media_urls", "image_url", "thumbnail", "video_url", "raw", "payload", "email", "phone",
                   "user_id", "owner", "poster")


def _rand_token(rng: random.Random) -> str:
    return "ZQX" + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(12))


def _rand_value(rng: random.Random, sentinel: str):
    kind = rng.randrange(5)
    if kind == 0:
        return sentinel
    if kind == 1:
        return {"nested": sentinel, "url": f"https://cdn.example.test/{sentinel}.jpg"}
    if kind == 2:
        return [sentinel, f"@{sentinel}"]
    if kind == 3:
        return f"https://www.reddit.com/user/{sentinel}"
    return SimpleNamespace(handle=sentinel)


@pytest.mark.parametrize("seed", range(40))
def test_property_extra_fields_never_persist(seed):
    rng = random.Random(seed)
    sentinels: list[str] = []

    def inject(obj, n):
        for _ in range(n):
            s = _rand_token(rng)
            sentinels.append(s)
            key = rng.choice(_FORBIDDEN_KEYS + (_rand_token(rng).lower(),))
            object.__setattr__(obj, key, _rand_value(rng, s))

    item = make_item()
    ctx = item.context
    # Sentinels in item text fields that must never be copied (only verified quotes are stored).
    for field in ("title",):
        s = _rand_token(rng)
        sentinels.append(s)
        setattr(item, field, s)
    s = _rand_token(rng)
    sentinels.append(s)
    ctx.query, ctx.parent_title, ctx.subreddit, ctx.platform_place_tag = s, s, s, s
    ctx.hashtags = [s]
    item.text = POST_TEXT + " " + s
    inject(item, rng.randint(1, 6))
    inject(ctx, rng.randint(1, 4))

    # Verdict-like inputs: duck-typed copies that carry extra attributes.
    verdict = make_verdict()
    vdict = verdict.model_dump()
    vlike = SimpleNamespace(**{k: v for k, v in vdict.items() if k not in ("location", "flags")})
    vlike.location = SimpleNamespace(**vdict["location"])
    vlike.flags = SimpleNamespace(**vdict["flags"])
    inject(vlike, rng.randint(1, 6))
    inject(vlike.location, rng.randint(1, 3))
    inject(vlike.flags, rng.randint(1, 3))   # unknown flag keys must be dropped

    vr = make_vr()
    inject(vr, rng.randint(1, 6))
    for q in vr.quotes:
        inject(q, rng.randint(1, 3))
    for c in vr.checks:
        inject(c, 1)
    geo = make_geo()
    inject(geo, rng.randint(1, 4))
    result = make_result()
    object.__setattr__(result, "verdict", vlike)
    inject(result, rng.randint(1, 4))
    inject(result.usage, rng.randint(1, 3))
    decision = Decision(store=True, status=rng.choice(["ai_verified", "needs_human_review"]),
                        review_reason="low_confidence")
    inject(decision, rng.randint(1, 3))

    v = build_signal_values(item=item, result=result, vr=vr, geo=geo, decision=decision, scan_id=7, now=NOW,
                            retention_days=30)
    assert set(v) <= ALLOWED_SIGNAL_FIELDS
    assert not set(v) & set(_FORBIDDEN_KEYS)
    assert set(v["flags"]) <= set(privacy.FLAG_KEYS)
    for q in v["evidence_quotes"]:
        assert set(q) == {"text", "supports", "masked"}
    for c in v["checks"].values():
        assert set(c) == {"passed", "detail"}
    blob = json.dumps(v, default=str)
    for s in sentinels:
        assert s not in blob, "an injected value reached the stored signal"
    assert "cdn.example.test" not in blob and "/user/" not in blob


def test_no_verdict_path_refusal():
    result = ClassifierResult(kind="needs_human_review", verdict=None, reason="refusal:violence",
                              model="claude-opus-5-5")
    v = build(result=result, vr=None, geo=make_geo(),
              decision=Decision(store=True, status="needs_human_review", review_reason="refusal:violence"))
    assert v["signal_type"] == "unknown"
    assert v["neutral_summary"] == "Post flagged for staff review; no AI summary."
    assert v["ai_confidence"] == 0.0
    assert v["evidence_quotes"] == [] and v["flags"] == {} and v["checks"] == {}
    assert v["lat"] is None and v["lon"] is None and v["location_text"] is None and v["county"] is None
    assert v["review_reason"] == "refusal:violence"
    assert v["status"] == "needs_human_review" and v["relevance"] is None
    assert set(v) == set(ALLOWED_SIGNAL_FIELDS)


def test_decision_that_does_not_store_raises():
    with pytest.raises(ValueError):
        build(decision=Decision(store=False, drop_reason="not_relevant:animal"))


# ---------------------------------------------------------------------------
# R1 / R2 URLs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "https://www.reddit.com/user/example_person",
    "https://www.reddit.com/u/example_person",
    "https://www.reddit.com/user/example_person/comments/abc/title",
    "https://www.reddit.com/r/u_example_person",
    "https://x.com/example_person",
    "https://twitter.com/example_person/likes",
    "https://www.instagram.com/example_person/",
    "https://www.tiktok.com/@example_person",
    "https://www.facebook.com/example.person",
    "https://www.facebook.com/profile.php?id=1234567",
    "https://www.threads.net/@example_person",
    "https://bsky.app/profile/example.bsky.social",
    "https://www.youtube.com/@examplechannel",
    "https://www.linkedin.com/in/example-person",
    "https://i.redd.it/abcdef.jpg",
    "https://pbs.twimg.com/media/XYZ?format=jpg",
    "https://example-news.test/images/photo.png",
    "ftp://example-news.test/story",
    "https://user:pw@example-news.test/story",
    "not a url",
])
def test_profile_and_media_urls_rejected(url):
    with pytest.raises(PrivacyViolation):
        build(item=make_item(url=url))


@pytest.mark.parametrize("url", [
    "https://www.reddit.com/r/longisland/comments/abc123/tents_behind_station",
    "https://x.com/example_person/status/1234567890",
    "https://x.com/i/status/1234567890",
    "https://www.instagram.com/p/AbCdEf/",
    "https://www.instagram.com/reel/AbCdEf/",
    "https://www.tiktok.com/@example_person/video/1234567890",
    "https://www.facebook.com/examplepage/posts/1234567890",
    "https://www.facebook.com/permalink.php?story_fbid=1&id=2",
    "https://www.facebook.com/story.php?story_fbid=1&id=2",
    "https://www.facebook.com/groups/examplegroup/permalink/123/",
    "https://www.threads.net/@example_person/post/AbC123",
    "https://bsky.app/profile/example.bsky.social/post/3kabc",
    "https://www.youtube.com/watch?v=abc123",
    "https://www.example-news.test/news/2026/10/13/story-about-station",
])
def test_post_urls_accepted(url):
    v = build(item=make_item(url=url))
    assert v["url"] == url


def test_profile_url_raises_a_valueerror_subclass_without_the_url_in_message():
    with pytest.raises(ValueError) as ei:
        build(item=make_item(url="https://x.com/example_person"))
    assert "example_person" not in str(ei.value)


# ---------------------------------------------------------------------------
# R7 self-reports
# ---------------------------------------------------------------------------
def test_self_report_has_no_coordinates_and_town_only(db):
    from app.models import ScanRun, Signal
    verdict = make_verdict(signal_type="self_report")
    vr = make_vr(signal_type="self_report")
    v = build(result=make_result(verdict), vr=vr, geo=make_geo(place_id=None),
              decision=Decision(store=True, status="self_report"),
              self_report_town="Exampleville", self_report_county="Suffolk")
    assert v["status"] == "self_report" and v["signal_type"] == "self_report"
    assert v["lat"] is None and v["lon"] is None and v["precision"] is None
    assert v["place_id"] is None and v["place_label"] is None and v["location_text"] is None
    assert v["location_kind"] is None
    assert v["town"] == "Exampleville" and v["county"] == "Suffolk"
    db.add(ScanRun(id=1, trigger="manual", status="running", started_at=NOW))
    db.add(Signal(**v))
    db.commit()


def test_self_report_needing_review_also_has_no_coordinates():
    verdict = make_verdict(signal_type="self_report")
    vr = make_vr(signal_type="self_report", review_reasons=["no_verified_quote"])
    v = build(result=make_result(verdict), vr=vr, geo=make_geo(),
              decision=Decision(store=True, status="needs_human_review", review_reason="no_verified_quote"),
              self_report_town=None, self_report_county=None)
    assert v["lat"] is None and v["lon"] is None and v["precision"] is None and v["town"] is None


def test_db_check_constraint_blocks_self_report_coordinates(db):
    from app.models import ScanRun, Signal
    db.add(ScanRun(id=1, trigger="manual", status="running", started_at=NOW))
    db.flush()
    v = build()
    v.update(status="self_report", signal_type="self_report")   # bypass the choke point on purpose
    db.add(Signal(**v))
    with pytest.raises(IntegrityError):
        db.flush()


# ---------------------------------------------------------------------------
# R4 quotes
# ---------------------------------------------------------------------------
def test_quotes_trimmed_scrubbed_and_capped():
    long_quote = " ".join(f"word{i}" for i in range(45))
    quotes = [
        StoredQuote(text=long_quote, supports="situation"),
        StoredQuote(text="tents behind the lot, call 631-555-0142 or write to someone@example.test",
                    supports="location"),
        StoredQuote(text="third quote should be cut", supports="recency"),
    ]
    v = build(vr=make_vr(quotes=quotes))
    q = v["evidence_quotes"]
    assert len(q) == 2
    assert len(q[0]["text"].split()) == 30 and q[0]["text"].startswith("word0 word1")
    assert "631" not in q[1]["text"] and "example.test" not in q[1]["text"]
    assert "[phone]" in q[1]["text"] and "[email]" in q[1]["text"]
    assert q[1]["masked"] is True and q[0]["masked"] is False
    assert all(len(x["text"].split()) <= 30 for x in q)


def test_quote_handles_and_links_scrubbed_and_bad_supports_normalized():
    quotes = [StoredQuote(text="saw it again, ask @some_user or u/another_user, see https://example.test/x",
                          supports="bogus")]
    v = build(vr=make_vr(quotes=quotes))
    (q,) = v["evidence_quotes"]
    assert "@some_user" not in q["text"] and "another_user" not in q["text"] and "https" not in q["text"]
    assert q["supports"] == "situation"


def test_quote_with_street_address_dropped_and_redacted():
    quotes = [StoredQuote(text="sleeping on the porch at 12 Example St every night", supports="location")]
    v = build(vr=make_vr(quotes=quotes))
    assert v["evidence_quotes"] == [] and v["evidence_redacted"] is True


def test_masked_flag_from_verifier_kept():
    quotes = [StoredQuote(text="[person] sleeping in the bus shelter", supports="situation", masked=True)]
    v = build(vr=make_vr(quotes=quotes))
    assert v["evidence_quotes"][0]["masked"] is True


# ---------------------------------------------------------------------------
# R5 location text and coordinates
# ---------------------------------------------------------------------------
def test_location_text_dropped_for_private_residence():
    verdict = make_verdict(location=dict(mentioned=True, location_text="Exampleville station parking lot",
                                         location_kind="named_place", is_private_residence=True))
    v = build(result=make_result(verdict), vr=make_vr(is_private_residence=True),
              geo=make_geo(precision="hamlet", place_label="Exampleville"))
    assert v["location_text"] is None
    assert v["precision"] == "hamlet"


def test_location_text_dropped_for_street_kind():
    verdict = make_verdict(location=dict(mentioned=True, location_text="Example Avenue",
                                         location_kind="street", is_private_residence=False))
    v = build(result=make_result(verdict), geo=make_geo(precision="hamlet"))
    assert v["location_text"] is None and v["location_kind"] == "street"


def test_location_text_dropped_for_street_address_pattern():
    verdict = make_verdict(location=dict(mentioned=True, location_text="45 Example Lane",
                                         location_kind="named_place", is_private_residence=False))
    # Defense in depth: even if a verifier copy still carried the address, privacy must refuse to store it.
    v = build(result=make_result(verdict), vr=make_vr(location_text="45 Example Lane"), geo=make_geo(precision="hamlet"))
    assert v["location_text"] is None


def test_location_text_dropped_when_not_verified():
    v = build(vr=make_vr(location_ok=False), decision=Decision(store=True, status="unlocated"))
    assert v["location_text"] is None and v["lat"] is None


def test_public_place_point_refused_for_residence_downgrades_to_unlocated():
    verdict = make_verdict(location=dict(mentioned=True, location_text="Exampleville station parking lot",
                                         location_kind="named_place", is_private_residence=True))
    v = build(result=make_result(verdict), vr=make_vr(is_private_residence=True), geo=make_geo())
    assert v["lat"] is None and v["lon"] is None and v["status"] == "unlocated"


def test_coordinates_only_inside_geofence():
    v = build(geo=make_geo(in_geofence=False, county=None, outside_reason="point in Queens"),
              decision=Decision(store=True, status="needs_human_review", review_reason="geofence_mismatch"))
    assert v["lat"] is None and v["county"] is None and v["place_label"] is None
    assert v["review_reason"] == "geofence_mismatch"


def test_needs_review_with_point_keeps_coordinates():
    v = build(decision=Decision(store=True, status="needs_human_review", review_reason="low_confidence"))
    assert v["lat"] is not None and v["precision"] == "public_place"


def test_unlocated_has_no_coordinates_but_keeps_county():
    v = build(geo=make_geo(precision="town", place_label="Example Town", lat=40.8, lon=-73.1),
              decision=Decision(store=True, status="unlocated"))
    assert v["lat"] is None and v["precision"] is None and v["county"] == "Suffolk"
    assert v["place_label"] == "Example Town"


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------
def test_summary_with_contact_details_replaced_by_template():
    v = build(vr=make_vr(summary="Post by @someone reports tents near the station."))
    assert "@someone" not in v["neutral_summary"]
    assert v["neutral_summary"] == "Encampment reported at Exampleville LIRR station (ongoing)."


def test_summary_too_long_replaced_and_basis_withheld():
    v = build(vr=make_vr(summary="x " * 200, decision_basis="Call 631-555-0199 for info."))
    assert len(v["neutral_summary"]) <= 240
    assert v["decision_basis"] == privacy.WITHHELD_BASIS


def test_lexicon_recheck_when_given():
    from app.pipeline.lexicon import Lexicon
    try:
        lex = Lexicon.load()
    except NotImplementedError:
        pytest.skip("waiting for app.pipeline.lexicon")
    v = build(vr=make_vr(summary="Post reports an elderly person sleeping at the station."), lexicon=lex)
    assert "elderly" not in v["neutral_summary"]


# ---------------------------------------------------------------------------
# Staff notes
# ---------------------------------------------------------------------------
@pytest.fixture
def lexicon():
    from app.pipeline.lexicon import Lexicon
    try:
        return Lexicon.load()
    except NotImplementedError:
        pytest.skip("waiting for app.pipeline.lexicon")


def test_check_note_ok(lexicon):
    assert privacy.check_note("Tents behind the station lot; check the treeline near the north entrance.",
                              lexicon) is None
    assert privacy.check_note("", lexicon) is None


def test_check_note_names_age_category(lexicon):
    msg = privacy.check_note("Elderly person sleeping by the bus shelter", lexicon)
    assert msg is not None and "age" in msg
    assert msg.startswith("Notes must describe the place and situation only")


def test_check_note_contact_details_and_multiple_categories(lexicon):
    msg = privacy.check_note("Wearing a jacket; email someone@example.test", lexicon)
    assert msg is not None
    assert "appearance" in msg and "contact details" in msg


def test_check_note_health_and_address(lexicon):
    msg = privacy.check_note("injured, staying at 12 Example St", lexicon)
    assert msg is not None and "health" in msg and "street addresses" in msg


def test_check_note_too_long(lexicon):
    msg = privacy.check_note("a" * 2001, lexicon)
    assert msg is not None and "2,000" in msg
    assert privacy.check_note("tents " * 300, lexicon) is None   # 1800 chars, fine


def test_self_report_template_summary_is_town_level():
    verdict = make_verdict(signal_type="self_report")
    vr = make_vr(signal_type="self_report", summary="Contact @someone for details")
    v = build(result=make_result(verdict), vr=vr, decision=Decision(store=True, status="self_report"),
              self_report_town="Exampleville", self_report_county="Suffolk")
    assert v["neutral_summary"] == "Self-report in Exampleville (ongoing)."
