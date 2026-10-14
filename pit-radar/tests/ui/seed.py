"""TEST-ONLY: build a throwaway data dir for UI screenshots.

Copies the SYNTHETIC geography fixtures (rectangles + made-up coordinates), migrates a fresh SQLite DB, creates
test users, and runs one real scan through the pipeline with a fake provider and a fake classifier, so the UI
renders real API responses. Nothing here is used by the running app.

Usage: PIT_DATA_DIR=/tmp/x .venv/bin/python tests/ui/seed.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("SESSION_SECRET", "ui-test-secret-0123456789abcdef0123456789")
os.environ["EXA_API_KEY"] = "ui-test-fake"

from app.config import get_settings, reset_settings_cache  # noqa: E402
from app.db import SessionLocal, utcnow  # noqa: E402
from app.paths import COUNTIES_FILE, GEO_MANIFEST_FILE, NYC_COUNTIES_FILE, PLACES_FILE, geo_dir  # noqa: E402
from app.pipeline.types import ClassifierResult, CollectResult, ItemContext, LLMUsage, ProviderHealth, Verdict  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "geo"
PASSWORD = "synthetic ui test password"

# (key, hours_old, text, verdict overrides). Synthetic texts; places exist in the synthetic gazetteer.
POSTS = [
    ("hix", 3, "People have been sleeping at the Hicksville LIRR station every night this week.",
     dict(signal_type="individual_unsheltered", loc="the Hicksville LIRR station", quote="sleeping at the Hicksville LIRR station every night", conf=0.88)),
    ("htc", 7, "Tents set up behind the Hempstead Transit Center again, three or four of them.",
     dict(signal_type="encampment", loc="behind the Hempstead Transit Center", quote="Tents set up behind the Hempstead Transit Center again", conf=0.9, count=4)),
    ("htc2", 30, "Still a homeless camp behind the Hempstead Transit Center, been there for weeks.",
     dict(signal_type="encampment", loc="behind the Hempstead Transit Center", quote="homeless camp behind the Hempstead Transit Center", conf=0.84)),
    ("eis", 20, "A couple living in their car in the Eisenhower Park lot for days now.",
     dict(signal_type="vehicle_dwelling", loc="the Eisenhower Park lot", quote="living in their car in the Eisenhower Park lot", conf=0.81)),
    ("bay", 2, "Tents pitched in the woods by the Bay Shore station, people there every night.",
     dict(signal_type="encampment", loc="the Bay Shore station", quote="Tents pitched in the woods by the Bay Shore station", conf=0.86)),
    ("ronk", 12, "Homeless encampment behind the Ronkonkoma station parking lot again tonight.",
     dict(signal_type="encampment", loc="behind the Ronkonkoma station parking lot", quote="Homeless encampment behind the Ronkonkoma station parking lot", conf=0.92)),
    ("pat", 40, "Someone has been sleeping at the Patchogue station platform for weeks.",
     dict(signal_type="individual_unsheltered", loc="the Patchogue station platform", quote="sleeping at the Patchogue station platform for weeks", conf=0.79)),
    ("hunt", 60, "Homeless guy sleeping at the Huntington LIRR station most nights lately.",
     dict(signal_type="individual_unsheltered", loc="the Huntington LIRR station", quote="sleeping at the Huntington LIRR station most nights lately", conf=0.62)),
    ("self", 5, "I'm homeless in Medford and sleeping in my car this week. Anyone know a shelter?",
     dict(signal_type="self_report", loc="Medford", kind="town_only", quote="I'm homeless in Medford", conf=0.9)),
    ("mont", 90, "Tents set up in the woods near Montauk, been there for weeks now.",
     dict(signal_type="encampment", loc="Montauk", kind="town_only", quote="Tents set up in the woods near Montauk", conf=0.74)),
]


def verdict(spec: dict) -> Verdict:
    return Verdict.model_validate(dict(
        relevance="relevant", signal_type=spec["signal_type"], not_relevant_reason="none", in_long_island_counties="yes",
        area_basis="names a Long Island place", time_reference="ongoing",
        location=dict(mentioned=True, location_text=spec["loc"], location_kind=spec.get("kind", "named_place"),
                      is_private_residence=False),
        people_count_estimate=spec.get("count", -1),
        flags=dict(possible_minors=False, medical_concern=False, weather_exposure=spec["signal_type"] == "encampment",
                   safety_concern=False),
        evidence_quotes=[{"text": spec["quote"], "supports": "situation"}],
        neutral_summary=f"Post reports a situation at {spec['loc']}.", decision_basis="Quote names the place and the situation.",
        confidence=spec["conf"]))


class FakeProvider:
    name = "exa"

    def __init__(self, now: dt.datetime):
        self.now = now

    async def run(self, queries, budget):
        from app.pipeline.normalize import build_item
        items = []
        for key, hours, text, _spec in POSTS:
            ctx = ItemContext(query="synthetic", subreddit="longisland", source_scope="li_scoped")
            items.append(build_item(provider="exa", platform="reddit", url=f"https://www.reddit.com/r/longisland/comments/ui{key}/x/",
                                    platform_post_id=f"ui{key}", published_at=self.now - dt.timedelta(hours=hours),
                                    date_source="provider", collected_at=self.now, title="", body=text, context=ctx))
        budget.try_charge("exa", 1.0)
        return CollectResult(items=[i for i in items if i], requests=1, health=ProviderHealth(provider="exa", status="ok"))

    async def health(self):
        return ProviderHealth(provider="exa", status="ok")


async def main() -> None:
    reset_settings_cache()
    out = geo_dir()
    shutil.copy(FIX / "counties.geojson", out / COUNTIES_FILE)
    shutil.copy(FIX / "nyc_counties.geojson", out / NYC_COUNTIES_FILE)
    shutil.copy(FIX / "places.json", out / PLACES_FILE)
    (out / GEO_MANIFEST_FILE).write_text(json.dumps({"built_at": utcnow().isoformat(), "counts": {}, "warnings": [],
                                                     "sources": {"note": "SYNTHETIC test fixture"}}))
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, check=True, capture_output=True)

    from app.auth import hash_password
    from app.models import User
    db = SessionLocal()
    for name, role in (("ui-admin", "admin"), ("ui-viewer", "viewer")):
        if not db.query(User).filter(User.name == name).first():
            db.add(User(name=name, role=role, password_hash=hash_password(PASSWORD), active=True))
    db.commit()
    db.close()

    from app.main import build_state
    from app.pipeline import classifier, runner
    from app.scheduler import Scheduler

    now = utcnow()
    state = build_state(get_settings())
    state.anthropic = object()
    await Scheduler(state)._load_geography(out, {"counts": {}})
    runner.make_collector = lambda provider, **kw: FakeProvider(now)
    by_text = {text: spec for _k, _h, text, spec in POSTS}

    async def fake_classify(item, **kw):
        spec = next(s for t, s in by_text.items() if t in item.text)
        return ClassifierResult(kind="ok", verdict=verdict(spec), model="claude-opus-5-5",
                                usage=LLMUsage(input_tokens=1200, output_tokens=800, cost_usd=0.021, model="claude-opus-5-5"))

    classifier.classify = fake_classify
    scan_id = await runner.run_scan(state, trigger="manual")
    await state.http.aclose()
    print(json.dumps({"scan_id": scan_id, "data_dir": str(out.parent)}))


if __name__ == "__main__":
    asyncio.run(main())
