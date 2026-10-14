"""sync_places: upsert the gazetteer into the `places` table (docs/03 §2), idempotently."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from app.models import Place
from app.pipeline.geo.gazetteer import Gazetteer, sync_places

FIX = Path(__file__).parent / "fixtures" / "geo"


def _load() -> Gazetteer:
    return Gazetteer.load(FIX / "places.json")


def test_sync_inserts_and_writes_ids_back(db):
    gaz = _load()
    n = sync_places(db, gaz)
    db.commit()
    assert n == len(gaz.places)
    assert db.scalar(select(func.count()).select_from(Place)) == n
    assert all(p.id is not None for p in gaz.places)
    assert len({p.id for p in gaz.places}) == n
    lib = gaz.by_name("Patchogue-Medford Library", ("library",))[0]
    row = db.get(Place, lib.id)
    assert (row.name, row.kind, row.county, row.source, row.source_id) == (
        "Patchogue-Medford Library", "library", "Suffolk", "osm_overpass", "way/9301")
    assert row.town == "Brookhaven"
    village = gaz.by_name("Patchogue", ("village",))[0]
    assert row.parent_id == village.id
    assert gaz.by_id(lib.id) is lib
    assert db.get(Place, village.id).parent_id is None
    bs = gaz.by_name("Bay Shore", ("cdp",))[0]
    assert "Bayshore" in db.get(Place, bs.id).aliases


def test_sync_is_idempotent_and_updates_in_place(db):
    gaz = _load()
    sync_places(db, gaz)
    db.commit()
    ids = {(p.source, p.source_id): p.id for p in gaz.places}

    gaz2 = _load()                          # a fresh load (e.g. after a restart or a rebuild)
    moved = gaz2.by_name("Medford", ("cdp",))[0]
    moved.lat += 0.001
    n = sync_places(db, gaz2)
    db.commit()
    assert n == len(gaz2.places)
    assert db.scalar(select(func.count()).select_from(Place)) == len(gaz2.places)
    assert {(p.source, p.source_id): p.id for p in gaz2.places} == ids
    assert db.get(Place, moved.id).lat == moved.lat


def test_sync_keeps_rows_no_longer_in_the_build(db):
    gaz = _load()
    sync_places(db, gaz)
    db.commit()
    keep = [p for p in gaz.places if p.name != "Main Beach"]
    smaller = Gazetteer(keep)
    sync_places(db, smaller)
    db.commit()
    # Rows referenced by old signals/hotspots are never deleted by a rebuild.
    assert db.scalar(select(func.count()).select_from(Place)) == len(gaz.places)
