"""Hotspot assignment, score math, archiving and weekly rollups (docs/03 §4, docs/10 §1).

Places and coordinates below are synthetic test points (not gazetteer data)."""
from __future__ import annotations

import datetime as dt
import math

import pytest

from app.models import Hotspot, HotspotWeekly, Place, ScanRun, Signal
from app.pipeline import hotspots as H

NOW = dt.datetime(2026, 10, 14, 16, 0, tzinfo=dt.timezone.utc)
BASE_LAT, BASE_LON = 40.75, -73.20
M_PER_DEG_LAT = 111_195.0


def north(meters: float) -> float:
    return BASE_LAT + meters / M_PER_DEG_LAT


def east(meters: float, lat: float = BASE_LAT) -> float:
    return BASE_LON + meters / (M_PER_DEG_LAT * math.cos(math.radians(lat)))


@pytest.fixture
def scan(db):
    s = ScanRun(id=1, trigger="manual", status="running", started_at=NOW)
    db.add(s)
    db.flush()
    return s


_counter = {"n": 0}


def make_signal(db, *, lat=BASE_LAT, lon=BASE_LON, place_id=None, town="Exampletown", county="Suffolk",
                status="ai_verified", signal_type="encampment", conf=0.8, age_days=0.0, provider="socialcrawl",
                platform="reddit", url=None, place_label=None) -> Signal:
    _counter["n"] += 1
    n = _counter["n"]
    published = NOW - dt.timedelta(days=age_days)
    s = Signal(
        scan_id=1, provider=provider, platform=platform,
        url=url or f"https://www.reddit.com/r/longisland/comments/t{n}/x",
        post_id_hash=f"{n:064d}", published_at=published, collected_at=published, signal_type=signal_type,
        status=status, neutral_summary="Encampment reported (ongoing).", evidence_quotes=[], flags={}, checks={},
        time_reference="ongoing", ai_confidence=conf, model="claude-opus-5-5", decision_basis="",
        lat=None if status == "self_report" else lat, lon=None if status == "self_report" else lon,
        precision="public_place", county=county, town=town, place_id=place_id, place_label=place_label,
        expires_at=published + dt.timedelta(days=30),
    )
    db.add(s)
    db.flush()
    return s


def make_place(db, *, name, kind="lirr_station", town=None, lat=BASE_LAT, lon=BASE_LON, parent_id=None,
               county="Suffolk") -> Place:
    p = Place(name=name, aliases=[], kind=kind, county=county, lat=lat, lon=lon, parent_id=parent_id, town=town,
              source="osm_overpass", source_id=f"test/{name}")
    db.add(p)
    db.flush()
    return p


def assign(db, s):
    return H.assign_signal(db, s, radius_m=400, east_end_radius_m=800, now=NOW, half_life_days=7)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def test_haversine_known_distance():
    # 1 degree of latitude is ~111.2 km.
    assert H.haversine_m(40.0, -73.0, 41.0, -73.0) == pytest.approx(111_195, rel=1e-3)
    assert H.haversine_m(BASE_LAT, BASE_LON, BASE_LAT, east(300)) == pytest.approx(300, rel=1e-3)


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------
def test_new_hotspot_created_with_label_from_place_label(db, scan):
    s = make_signal(db, place_label="Exampletown LIRR station")
    hs = assign(db, s)
    assert hs is not None and s.hotspot_id == hs.id
    assert hs.label == "Exampletown LIRR station" and hs.county == "Suffolk" and hs.status == "active"
    assert (hs.lat, hs.lon) == (BASE_LAT, BASE_LON)
    assert hs.primary_type == "encampment" and hs.signals_30d == 1 and hs.score > 0
    assert hs.first_seen_at == s.published_at and hs.last_signal_at == s.published_at


def test_label_falls_back_to_town(db, scan):
    hs = assign(db, make_signal(db, place_label=None, town="Exampletown"))
    assert hs.label == "Exampletown"


def test_assignment_by_same_place_even_when_far(db, scan):
    p = make_place(db, name="Example Park", kind="park")
    a = make_signal(db, place_id=p.id)
    b = make_signal(db, place_id=p.id, lat=north(1500))       # 1.5 km away, same gazetteer place
    ha, hb = assign(db, a), assign(db, b)
    assert ha.id == hb.id
    assert db.query(Hotspot).count() == 1


def test_assignment_by_radius_urban(db, scan):
    first = assign(db, make_signal(db))
    near = assign(db, make_signal(db, lat=north(300)))         # within 400 m
    far = assign(db, make_signal(db, lat=north(600)))          # beyond 400 m -> new hotspot
    assert near.id == first.id
    assert far.id != first.id


def test_assignment_picks_nearest_within_radius(db, scan):
    h1 = assign(db, make_signal(db))
    h2 = assign(db, make_signal(db, lat=north(700)))
    assert h1.id != h2.id
    mid = assign(db, make_signal(db, lat=north(450)))          # 450 m from h1, 250 m from h2
    assert mid.id == h2.id


def test_assignment_by_radius_east_end(db, scan):
    first = assign(db, make_signal(db, town="Southampton"))
    near = assign(db, make_signal(db, town="Southampton", lat=north(600)))   # 600 m < 800 m East End radius
    assert near.id == first.id
    far = assign(db, make_signal(db, town="Southampton", lat=north(1500)))
    assert far.id != first.id


def test_east_end_radius_from_place_town(db, scan):
    hamlet = make_place(db, name="Example Bays", kind="cdp", town="Southampton")
    first = assign(db, make_signal(db, town="Example Bays"))
    s = make_signal(db, town="Example Bays", place_id=hamlet.id, lat=north(650))
    hs = assign(db, s)
    assert hs.id == first.id


def test_urban_600m_creates_new(db, scan):
    first = assign(db, make_signal(db, town="Islip"))
    other = assign(db, make_signal(db, town="Islip", lat=north(600)))
    assert other.id != first.id


def test_only_active_hotspots_considered(db, scan):
    first = assign(db, make_signal(db))
    first.status = "visited"
    db.flush()
    again = assign(db, make_signal(db, lat=north(50)))
    assert again.id != first.id and again.status == "active"


def test_unassignable_signals(db, scan):
    for status, stype in (("self_report", "self_report"), ("unlocated", "encampment"), ("dismissed", "encampment")):
        s = make_signal(db, status=status, signal_type=stype)
        assert assign(db, s) is None and s.hotspot_id is None
    s = make_signal(db, lat=None, lon=None, status="needs_human_review")
    assert assign(db, s) is None
    assert db.query(Hotspot).count() == 0


def test_needs_review_with_coordinates_joins_at_half_weight(db, scan):
    s = make_signal(db, status="needs_human_review", conf=1.0, signal_type="individual_unsheltered")
    hs = assign(db, s)
    assert hs is not None
    assert hs.score == pytest.approx(0.5 * 1.0 * 1.0)
    assert hs.needs_review_30d == 1


# ---------------------------------------------------------------------------
# Score math (hand-computed)
# ---------------------------------------------------------------------------
def test_score_matches_hand_computed_example(db, scan):
    a = make_signal(db, status="ai_verified", signal_type="encampment", conf=0.8, age_days=0,
                    provider="socialcrawl", platform="reddit")
    hs = assign(db, a)
    b = make_signal(db, status="confirmed", signal_type="individual_unsheltered", conf=0.9, age_days=7,
                    provider="exa", platform="news", url="https://www.example-news.test/a")
    c = make_signal(db, status="needs_human_review", signal_type="encampment", conf=0.6, age_days=14,
                    provider="firecrawl", platform="web", url="https://example-blog.test/b")
    d = make_signal(db, status="dismissed", signal_type="encampment", conf=1.0, age_days=0)
    for s in (b, c, d):
        s.hotspot_id = hs.id
    db.flush()
    H.recompute(db, hs, now=NOW, half_life_days=7)
    # A: 1.0 * 1.5 * 0.8 * 1     = 1.2
    # B: 1.5 * 1.0 * 0.9 * 0.5   = 0.675
    # C: 0.5 * 1.5 * 0.6 * 0.25  = 0.1125
    # D: dismissed, excluded. Sources: (socialcrawl, reddit), (exa, example-news.test), (firecrawl, example-blog.test)
    # multiplier = min(2, 1 + 0.25 * 2) = 1.5 ; score = 1.5 * 1.9875 = 2.98125
    assert hs.score == pytest.approx(2.98125, rel=1e-6)
    assert hs.distinct_sources_30d == 3
    assert hs.signals_7d == 2 and hs.signals_30d == 3
    assert hs.confirmed_30d == 1 and hs.needs_review_30d == 1
    # encampment weighted share = (1.2 + 0.1125) / 1.9875 = 0.660 > 0.6
    assert hs.primary_type == "encampment"


def test_mixed_when_no_type_exceeds_60_percent(db, scan):
    hs = assign(db, make_signal(db, signal_type="vehicle_dwelling", conf=1.0))
    s2 = make_signal(db, signal_type="individual_unsheltered", conf=1.0, provider="exa", platform="x")
    s2.hotspot_id = hs.id
    db.flush()
    H.recompute(db, hs, now=NOW, half_life_days=7)
    assert hs.primary_type == "mixed"
    assert hs.score == pytest.approx(1.25 * 2.0)     # 2 sources -> x1.25


def test_source_diversity_cap(db, scan):
    hs = assign(db, make_signal(db, signal_type="individual_unsheltered", conf=1.0))
    sources = [("socialcrawl", "x"), ("socialcrawl", "tiktok"), ("socialcrawl", "instagram"),
               ("exa", "reddit"), ("firecrawl", "news")]
    for i, (prov, plat) in enumerate(sources):
        s = make_signal(db, signal_type="individual_unsheltered", conf=1.0, provider=prov, platform=plat,
                        url=f"https://outlet{i}.example.test/story")
        s.hotspot_id = hs.id
    db.flush()
    H.recompute(db, hs, now=NOW, half_life_days=7)
    assert hs.distinct_sources_30d == 6
    assert hs.score == pytest.approx(2.0 * 6.0)       # min(2, 1 + 0.25 * 5 = 2.25)


def test_same_provider_platform_is_one_source_but_news_hosts_differ(db, scan):
    hs = assign(db, make_signal(db))
    for url in ("https://www.reddit.com/r/longisland/comments/z1/a", "https://old.reddit.com/r/x/comments/z2/b"):
        s = make_signal(db, url=url)
        s.hotspot_id = hs.id
    for url in ("https://www.outlet-a.test/1", "https://outlet-b.test/2", "https://outlet-a.test/3"):
        s = make_signal(db, provider="exa", platform="news", url=url)
        s.hotspot_id = hs.id
    db.flush()
    H.recompute(db, hs, now=NOW, half_life_days=7)
    assert hs.distinct_sources_30d == 3      # (socialcrawl, reddit), (exa, outlet-a.test), (exa, outlet-b.test)


def test_score_decays_with_half_life(db, scan):
    hs = assign(db, make_signal(db, signal_type="individual_unsheltered", conf=1.0))
    H.recompute(db, hs, now=NOW + dt.timedelta(days=7), half_life_days=7)
    assert hs.score == pytest.approx(0.5)
    H.recompute(db, hs, now=NOW + dt.timedelta(days=14), half_life_days=7)
    assert hs.score == pytest.approx(0.25)


def test_dismissing_all_signals_zeroes_score(db, scan):
    s = make_signal(db)
    hs = assign(db, s)
    s.status = "dismissed"
    db.flush()
    H.recompute(db, hs, now=NOW, half_life_days=7)
    assert hs.score == 0 and hs.signals_30d == 0 and hs.last_signal_at == s.published_at


# ---------------------------------------------------------------------------
# recompute_all and archiving
# ---------------------------------------------------------------------------
def test_recompute_all_archives_after_90_days(db, scan):
    old = Hotspot(label="Old place", county="Nassau", lat=40.7, lon=-73.6, primary_type="encampment",
                  first_seen_at=NOW - dt.timedelta(days=120), last_signal_at=NOW - dt.timedelta(days=91),
                  status="visited", notes="check behind the shed", assigned_team="Team 2", score=1.0)
    recent = Hotspot(label="Recent place", county="Nassau", lat=40.71, lon=-73.61, primary_type="encampment",
                     first_seen_at=NOW - dt.timedelta(days=100), last_signal_at=NOW - dt.timedelta(days=89),
                     status="active", notes="keep", assigned_team="Team 1")
    gone = Hotspot(label="Archived", county="Nassau", lat=40.72, lon=-73.62, primary_type="mixed",
                   first_seen_at=NOW - dt.timedelta(days=400), last_signal_at=NOW - dt.timedelta(days=300),
                   status="archived")
    db.add_all([old, recent, gone])
    db.flush()
    n = H.recompute_all(db, now=NOW, half_life_days=7, archive_days=90)
    assert n == 2
    assert old.status == "archived" and old.notes is None and old.assigned_team is None and old.score == 0
    assert recent.status == "active" and recent.notes == "keep" and recent.assigned_team == "Team 1"
    assert recent.last_signal_at == NOW - dt.timedelta(days=89)   # kept even though no member rows remain


def test_recompute_all_refreshes_decayed_scores(db, scan):
    hs = assign(db, make_signal(db, signal_type="individual_unsheltered", conf=1.0))
    assert hs.score == pytest.approx(1.0)
    H.recompute_all(db, now=NOW + dt.timedelta(days=7), half_life_days=7, archive_days=90)
    assert hs.score == pytest.approx(0.5)
    assert hs.status == "active"


# ---------------------------------------------------------------------------
# Weekly rollup
# ---------------------------------------------------------------------------
def test_rollup_weekly_uses_local_monday(db, scan):
    hs = assign(db, make_signal(db))
    # 2026-10-12 03:00 UTC is Sunday 2026-10-11 23:00 in New York -> week of Monday 2026-10-05.
    sun_local = make_signal(db, status="confirmed")
    sun_local.published_at = dt.datetime(2026, 10, 12, 3, 0, tzinfo=dt.timezone.utc)
    mon_local = make_signal(db)
    mon_local.published_at = dt.datetime(2026, 10, 12, 4, 30, tzinfo=dt.timezone.utc)   # Mon 00:30 local
    dismissed = make_signal(db, status="dismissed")
    for s in (sun_local, mon_local, dismissed):
        s.hotspot_id = hs.id
    db.flush()
    H.rollup_weekly(db, [sun_local, mon_local, dismissed])
    rows = {(r.week_start, r.signal_type): (r.n_signals, r.n_confirmed) for r in db.query(HotspotWeekly)}
    assert rows == {(dt.date(2026, 10, 5), "encampment"): (1, 1), (dt.date(2026, 10, 12), "encampment"): (1, 0)}
    H.rollup_weekly(db, [mon_local])      # adds to the existing row
    row = db.get(HotspotWeekly, (hs.id, dt.date(2026, 10, 12), "encampment"))
    assert row.n_signals == 2


def test_week_start_local_helper():
    assert H.week_start_local(dt.datetime(2026, 10, 14, 16, tzinfo=dt.timezone.utc), "America/New_York") == \
        dt.date(2026, 10, 12)
