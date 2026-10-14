"""End-to-end: one scan through the REAL runner, lexicon, prefilter, verifier, decision, geocoder, privacy,
hotspots and API, with only the provider and the Claude call replaced by test-only fakes.

Geography is the SYNTHETIC fixture set (tests/fixtures/geo: rectangles and made-up coordinates), copied into the
test's own data dir. Post texts are synthetic.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil

import pytest

from app.config import get_settings, reset_settings_cache
from app.db import SessionLocal, utcnow
from app.models import Hotspot, ItemSeen, ScanRun, Signal
from app.paths import COUNTIES_FILE, GEO_MANIFEST_FILE, NYC_COUNTIES_FILE, PLACES_FILE, geo_dir
from app.pipeline.types import (
    ClassifierResult,
    CollectResult,
    ItemContext,
    LLMUsage,
    ProviderHealth,
    QueryLogEntry,
    Verdict,
)
from tests.conftest import FIXTURES

PASSWORD = "correct horse battery staple"


def _verdict(**kw) -> Verdict:
    base = dict(
        relevance="relevant", signal_type="encampment", not_relevant_reason="none", in_long_island_counties="yes",
        area_basis="names an LI station", time_reference="ongoing",
        location=dict(mentioned=True, location_text="", location_kind="named_place", is_private_residence=False),
        people_count_estimate=-1,
        flags=dict(possible_minors=False, medical_concern=False, weather_exposure=False, safety_concern=False),
        evidence_quotes=[], neutral_summary="", decision_basis="", confidence=0.86)
    base.update(kw)
    return Verdict.model_validate(base)


# Synthetic posts: (key, subreddit or None, hours old, text)
POSTS = [
    ("tents", "longisland", 5,
     "Tents set up behind the Ronkonkoma station parking lot again. They have been there for weeks now."),
    ("jamaica", None, 3, "Homeless guy sleeping at Jamaica station again tonight."),
    ("selfreport", "longisland", 2,
     "I'm homeless in Patchogue and sleeping in my car this week. Does anyone know a shelter that has room?"),
    ("gala", None, 6, "Gala raising money for homeless families at the Hempstead hall next month. Donate today!"),
    ("bench", "longisland", 8, "Someone has been sleeping on the bench at the Bay Shore station every night."),
    ("old", "longisland", 24 * 40, "Homeless camp in the woods near Medford, tents everywhere."),
]


def _canned(text: str) -> Verdict:
    if "Ronkonkoma" in text:
        return _verdict(
            location=dict(mentioned=True, location_text="behind the Ronkonkoma station parking lot",
                          location_kind="named_place", is_private_residence=False),
            evidence_quotes=[{"text": "Tents set up behind the Ronkonkoma station parking lot again", "supports": "location"},
                             {"text": "They have been there for weeks now", "supports": "recency"}],
            neutral_summary="Post reports tents behind the Ronkonkoma station parking lot, there for weeks.",
            decision_basis="Quotes place tents behind the station lot for weeks.")
    if "Patchogue" in text:
        return _verdict(signal_type="self_report", time_reference="current", confidence=0.9,
                        location=dict(mentioned=True, location_text="Patchogue", location_kind="town_only",
                                      is_private_residence=False),
                        evidence_quotes=[{"text": "I'm homeless in Patchogue", "supports": "situation"}],
                        neutral_summary="Poster says they are currently homeless in Patchogue and sleeping in a car.",
                        decision_basis="Poster states current homelessness in Patchogue.")
    if "Gala" in text:
        return _verdict(relevance="not_relevant", signal_type="none",
                        not_relevant_reason="fundraiser_event_or_promotion", confidence=0.95,
                        location=dict(mentioned=False, location_text="", location_kind="none",
                                      is_private_residence=False))
    if "Bay Shore" in text:
        return _verdict(signal_type="individual_unsheltered", time_reference="ongoing",
                        location=dict(mentioned=True, location_text="the bench at the Bay Shore station",
                                      location_kind="named_place", is_private_residence=False),
                        # Paraphrased: not a verbatim span of the post -> V1 drops it -> needs human review.
                        evidence_quotes=[{"text": "a person sleeps nightly on the Bay Shore bench", "supports": "situation"}],
                        neutral_summary="Post says a person sleeps on a bench at the Bay Shore station nightly.",
                        decision_basis="Post describes nightly sleeping at the station.")
    raise AssertionError(f"classifier called for an item that should have been filtered: {text[:40]}")


class FakeExa:
    """Test-only stand-in for the Exa collector: returns the synthetic posts for whatever was planned."""

    name = "exa"

    def __init__(self, now: dt.datetime):
        self.now = now

    async def run(self, queries, budget):
        from app.pipeline.normalize import build_item

        items = []
        for key, sub, hours, text in POSTS:
            url = (f"https://www.reddit.com/r/{sub}/comments/syn{key}/x/" if sub
                   else f"https://example-news.test/story/{key}")
            ctx = ItemContext(query=queries[0].query if queries else "", subreddit=sub,
                              source_scope="li_scoped" if sub else "general")
            it = build_item(provider="exa", platform="reddit" if sub else "web", url=url, platform_post_id=f"syn{key}",
                            published_at=self.now - dt.timedelta(hours=hours), date_source="provider",
                            collected_at=self.now, title="", body=text, context=ctx)
            assert it is not None
            items.append(it)
        budget.try_charge("exa", 1.0)
        return CollectResult(items=items, requests=1,
                             logs=[QueryLogEntry(provider="exa", endpoint="/search", query_text="synthetic",
                                                 status="ok", items=len(items))],
                             health=ProviderHealth(provider="exa", status="ok"))

    async def health(self):
        return ProviderHealth(provider="exa", status="ok")


@pytest.fixture
def geo_files():
    out = geo_dir()
    shutil.copy(FIXTURES / "geo" / "counties.geojson", out / COUNTIES_FILE)
    shutil.copy(FIXTURES / "geo" / "nyc_counties.geojson", out / NYC_COUNTIES_FILE)
    shutil.copy(FIXTURES / "geo" / "places.json", out / PLACES_FILE)
    (out / GEO_MANIFEST_FILE).write_text(json.dumps({"built_at": utcnow().isoformat(), "counts": {}, "warnings": [],
                                                     "sources": {"note": "synthetic test fixture"}}))
    return out


async def test_full_scan_through_real_pipeline(engine, geo_files, monkeypatch, make_user):
    from app.main import build_state
    from app.pipeline import classifier, runner
    from app.scheduler import Scheduler

    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    reset_settings_cache()
    now = utcnow()
    state = build_state(get_settings())
    state.anthropic = object()          # non-None: classify() itself is replaced below
    await Scheduler(state)._load_geography(geo_files, {"counts": {}})
    assert state.geo_ready and state.system_blocks

    monkeypatch.setattr(runner, "make_collector", lambda provider, **kw: FakeExa(now))
    calls: list[str] = []

    async def fake_classify(item, **kw):
        calls.append(item.text)
        return ClassifierResult(kind="ok", verdict=_canned(item.text), model="claude-opus-5-5",
                                usage=LLMUsage(input_tokens=900, output_tokens=700, cost_usd=0.02, model="claude-opus-5-5"))

    monkeypatch.setattr(classifier, "classify", fake_classify)

    scan_id = await runner.run_scan(state, trigger="manual")
    assert scan_id is not None
    await state.http.aclose()

    db = SessionLocal()
    try:
        run = db.get(ScanRun, scan_id)
        assert run.status in ("ok", "partial"), run.errors
        sc = run.stage_counts
        assert sc["collected"] == 6 and sc["in_window"] == 5
        assert run.drop_reasons.get("too_old") == 1
        assert any(k.startswith("exclusion") for k in run.drop_reasons), run.drop_reasons   # Jamaica (NYC only)
        assert len(calls) == 4                                                               # tents, self, gala, bench
        assert not any("Jamaica" in c for c in calls)

        sigs = {s.signal_type: s for s in db.query(Signal).all()}
        assert set(sigs) == {"encampment", "self_report", "individual_unsheltered"}

        enc = sigs["encampment"]
        assert enc.status == "ai_verified"
        assert enc.county == "Suffolk" and enc.precision == "public_place"
        assert (round(enc.lat, 3), round(enc.lon, 3)) == (40.808, -73.106)                  # snapped to the station
        assert enc.hotspot_id is not None
        assert [q["text"] for q in enc.evidence_quotes][0].startswith("Tents set up behind")
        assert enc.checks["V1"]["passed"] and enc.checks["V4"]["passed"]

        selfr = sigs["self_report"]
        assert selfr.status == "self_report" and selfr.lat is None and selfr.lon is None     # R7
        assert selfr.town == "Patchogue" and selfr.county == "Suffolk"

        bench = sigs["individual_unsheltered"]
        assert bench.status == "needs_human_review" and bench.evidence_quotes == []          # paraphrase dropped

        hs = db.get(Hotspot, enc.hotspot_id)
        assert hs.county == "Suffolk" and hs.score > 0 and hs.primary_type == "encampment"
        gala = [r for r in db.query(ItemSeen).all() if r.decision.startswith("not_relevant")]
        assert len(gala) == 1
    finally:
        db.close()

    # SSE: viewers get the mapped signal without coordinates; reviewers get coordinates.
    viewer_new = [e for e in state.events.replay_after(0, "viewer") if e.event == "signal.new"]
    rev_new = [e for e in state.events.replay_after(0, "reviewer") if e.event == "signal.new"]
    assert viewer_new and all(e.data["lat"] is None for e in viewer_new)
    assert all(e.data["status"] in ("ai_verified", "confirmed") for e in viewer_new)
    assert any(e.data.get("lat") is not None for e in rev_new)

    # API on top of the stored data.
    from fastapi.testclient import TestClient

    from app.main import create_app

    make_user("vic", "viewer", PASSWORD)
    make_user("rae", "reviewer", PASSWORD)
    app = create_app(start_background=False)
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"name": "vic", "password": PASSWORD})
        hot = c.get("/api/hotspots").json()["items"]
        assert len(hot) == 1 and hot[0]["county"] == "Suffolk"
        vs = c.get("/api/signals").json()["items"]
        assert [s["status"] for s in vs] == ["ai_verified"] and vs[0]["lat"] is None          # R11
        assert c.get("/api/self-reports").status_code == 403
        assert c.get(f"/api/signals/{bench.id}").status_code == 404                           # viewers never see review items

        c.post("/api/auth/logout", headers={"X-CSRF-Token": c.get("/api/me").json()["csrf_token"]})
        r = c.post("/api/auth/login", json={"name": "rae", "password": PASSWORD})
        csrf = r.json()["csrf_token"]
        rs = c.get("/api/signals").json()["items"]
        assert {s["status"] for s in rs} == {"ai_verified", "needs_human_review"}
        assert next(s for s in rs if s["status"] == "ai_verified")["lat"] is not None
        q = c.get("/api/review-queue").json()
        assert q["total"] == 2
        sr = c.get("/api/self-reports").json()["items"]
        assert len(sr) == 1 and sr[0]["town"] == "Patchogue" and "lat" not in sr[0]
        r = c.post(f"/api/signals/{enc.id}/review", json={"action": "confirm"}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200 and r.json()["status"] == "confirmed"
        r = c.post(f"/api/signals/{bench.id}/review", json={"action": "dismiss"}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 422                                                          # reason required
        r = c.post(f"/api/signals/{bench.id}/review", json={"action": "dismiss", "reason": "not_relevant"},
                   headers={"X-CSRF-Token": csrf})
        assert r.json()["status"] == "dismissed"
        assert c.get("/api/review-queue").json()["total"] == 0
        hs = c.get(f"/api/hotspots/{enc.hotspot_id}").json()
        assert hs["confirmed_30d"] == 1
        csv_text = c.get("/api/export/hotspots.csv").text
        assert csv_text.startswith("# For outreach and PIT planning only.") and "Ronkonkoma" in csv_text
        assert "reddit.com" not in csv_text                                                   # no post URLs in exports
        stats = c.get("/api/stats").json()
        assert stats["funnel_last_scan"]["collected"] == 6
