"""Gazetteer matching (docs/06 §2.2-§3, docs/10 §1 Geocoding row) on the SYNTHETIC fixture gazetteer.

The fixture uses real Long Island names with made-up coordinates (tests/fixtures/geo/README.md).
Curated aliases come from the committed data/aliases.yaml, which is reviewed hand-made input.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.paths import REPO_DATA_DIR
from app.pipeline.geo.gazetteer import (
    Gazetteer,
    PlaceRec,
    apply_aliases,
    generated_aliases,
    load_alias_groups,
    normkey,
)

FIX = Path(__file__).parent / "fixtures" / "geo"
PLACES = FIX / "places.json"


@pytest.fixture(scope="module")
def gaz() -> Gazetteer:
    return Gazetteer.load(PLACES)          # default curated aliases: data/aliases.yaml


def found(gaz: Gazetteer, text: str) -> list[tuple[str, str, str]]:
    return [(m.matched, m.place.name, m.place.kind) for m in gaz.find_in_text(text)]


def one(gaz: Gazetteer, name: str, kind: str, parent: str | None = None) -> PlaceRec:
    recs = [p for p in gaz.by_name(name, (kind,)) if parent is None or p.parent_name == parent]
    assert len(recs) == 1, (name, kind, parent, recs)
    return recs[0]


# ---------------------------------------------------------------------------- loading / aliases
def test_load_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        Gazetteer.load(tmp_path / "li_places.json")


def test_load_skips_invalid_rows(tmp_path):
    data = json.loads(PLACES.read_text())
    good = data["places"][:3]
    bad = [
        {**good[0], "kind": "restaurant", "source_id": "x1"},
        {**good[0], "county": "Queens", "source_id": "x2"},
        {**good[0], "lat": "nan", "source_id": "x3"},
        {**good[0], "source_id": ""},
        "not a dict",
    ]
    p = tmp_path / "li_places.json"
    p.write_text(json.dumps({"places": good + bad}))
    g = Gazetteer.load(p, aliases_path=tmp_path / "none.yaml")
    assert len(g.places) == 3


def test_load_accepts_bare_list_and_parent_place_key(tmp_path):
    rows = [{"name": "Patchogue-Medford Library", "kind": "library", "county": "Suffolk", "lat": 40.766,
             "lon": -73.013, "source": "osm_overpass", "source_id": "way/1", "parent_place": "Patchogue"}]
    p = tmp_path / "li_places.json"
    p.write_text(json.dumps(rows))
    g = Gazetteer.load(p, aliases_path=tmp_path / "none.yaml")
    assert g.places[0].parent_name == "Patchogue"


def test_committed_aliases_file_is_names_only():
    groups = load_alias_groups(REPO_DATA_DIR / "aliases.yaml")
    assert len(groups) >= 20
    assert ["Bay Shore", "Bayshore"] in groups
    for g in groups:
        for name in g:
            assert not any(ch.isdigit() for ch in name), name       # no coordinates, ever


def test_generated_aliases():
    st = PlaceRec("St. James", "cdp", "Suffolk", 40.88, -73.15, "census_tigerweb", "place:1")
    mt = PlaceRec("Mount Sinai", "cdp", "Suffolk", 40.94, -73.02, "census_tigerweb", "place:2")
    pj = PlaceRec("Port Jefferson", "village", "Suffolk", 40.94, -73.06, "census_tigerweb", "place:3")
    town = PlaceRec("Hempstead", "town", "Nassau", 40.65, -73.62, "census_tigerweb", "cousub:4")
    stn = PlaceRec("Ronkonkoma", "lirr_station", "Suffolk", 40.81, -73.11, "osm_overpass", "node/5")
    eb = PlaceRec("East Northport", "cdp", "Suffolk", 40.88, -73.32, "census_tigerweb", "place:6")
    assert {"Saint James", "St James"} <= set(generated_aliases(st))
    assert {"Mt. Sinai", "Mt Sinai"} <= set(generated_aliases(mt))
    assert {"Pt. Jefferson", "Pt Jefferson", "Village of Port Jefferson", "Port Jefferson Village"} <= set(generated_aliases(pj))
    assert "Town of Hempstead" in generated_aliases(town)
    assert {"Ronkonkoma station", "Ronkonkoma LIRR", "Ronkonkoma train station",
            "Ronkonkoma LIRR station"} <= set(generated_aliases(stn))
    assert "E. Northport" in generated_aliases(eb)


def test_apply_aliases_is_idempotent_and_both_ways():
    recs = [PlaceRec("Bay Shore", "cdp", "Suffolk", 40.72, -73.24, "t", "1"),
            PlaceRec("Bay Shore", "lirr_station", "Suffolk", 40.72, -73.24, "t", "2"),
            PlaceRec("Bayshore", "park", "Suffolk", 40.73, -73.25, "t", "3")]
    groups = [["Bay Shore", "Bayshore"]]
    apply_aliases(recs, groups)
    first = [list(r.aliases) for r in recs]
    apply_aliases(recs, groups)
    assert [list(r.aliases) for r in recs] == first
    assert "Bayshore" in recs[0].aliases
    assert "Bayshore station" in recs[1].aliases and "Bay Shore LIRR station" in recs[1].aliases
    assert "Bay Shore" in recs[2].aliases                       # applied both ways
    assert not any(normkey(a) == normkey(r.name) for r in recs for a in r.aliases)
    assert not any("station station" in a.lower() for a in recs[1].aliases)


def test_label():
    stn = PlaceRec("Ronkonkoma", "lirr_station", "Suffolk", 40.8, -73.1, "osm_overpass", "node/1")
    park = PlaceRec("Heckscher State Park", "park", "Suffolk", 40.7, -73.1, "osm_overpass", "way/1")
    cdp = PlaceRec("Bay Shore", "cdp", "Suffolk", 40.7, -73.2, "census_tigerweb", "place:1")
    assert stn.label == "Ronkonkoma LIRR station"
    assert park.label == "Heckscher State Park"
    assert cdp.label == "Bay Shore"
    assert stn.is_public_place and park.is_public_place and not cdp.is_public_place


# ---------------------------------------------------------------------------- find_in_text
def test_alias_bayshore(gaz):
    assert found(gaz, "someone sleeping in bayshore again") == [("bayshore", "Bay Shore", "cdp")]


def test_longest_match_first(gaz):
    assert found(gaz, "camp near Huntington Station") == [("Huntington Station", "Huntington Station", "cdp")]
    assert found(gaz, "the Huntington LIRR station lot") == [("Huntington LIRR station", "Huntington", "lirr_station")]
    assert found(gaz, "Patchogue-Medford Library steps") == [
        ("Patchogue-Medford Library", "Patchogue-Medford Library", "library")]


def test_generic_word_guard(gaz):
    assert found(gaz, "sleeping in the park by the beach near the station") == []
    assert found(gaz, "walked along the bay shore to the point") == []        # common words, lowercase
    assert found(gaz, "a camp up on the ridge") == []
    assert found(gaz, "a camp in Ridge off the main road") == [("Ridge", "Ridge", "cdp")]
    assert found(gaz, "the kids at island park") == []
    assert found(gaz, "Island Park bridge") == [("Island Park", "Island Park", "village")]


def test_generic_landmark_names_are_not_anchors(gaz):
    # Several "Town Park"s, and one generic "Veterans Memorial Park": not Long Island anchors on their own.
    assert found(gaz, "tents in Town Park") == []
    assert found(gaz, "tents in Veterans Memorial Park") == []
    assert found(gaz, "tents in Heckscher State Park") == [
        ("Heckscher State Park", "Heckscher State Park", "park")]


def test_nyc_names_match_nothing(gaz):
    assert found(gaz, "Long Island City encampment under the bridge") == []
    assert found(gaz, "Homeless guy sleeping at Jamaica station again") == []


def test_same_name_records_prefer_village_or_cdp(gaz):
    assert found(gaz, "in Hempstead today") == [("Hempstead", "Hempstead", "village")]
    assert found(gaz, "the Town of Hempstead board") == [("Town of Hempstead", "Hempstead", "town")]
    assert found(gaz, "Hempstead Village police") == [("Hempstead Village", "Hempstead", "village")]
    assert found(gaz, "Huntington library") == [("Huntington", "Huntington", "cdp")]


def test_abbreviation_and_punctuation_variants(gaz):
    assert [x[1] for x in found(gaz, "Saint James, St James and St. James")] == ["St. James"] * 3
    assert found(gaz, "out on Fisher's Island") == [("Fisher's Island", "Fishers Island", "cdp")]
    assert found(gaz, "RVC and rvc") == [("RVC", "Rockville Centre", "village")]       # short all-caps alias
    assert found(gaz, "Rockville Center") == [("Rockville Center", "Rockville Centre", "village")]
    assert found(gaz, "Ronk station") == [("Ronk station", "Ronkonkoma", "lirr_station")]


def test_mentions_have_offsets(gaz):
    text = "Seen near the bayshore train station and in Medford."
    ms = gaz.find_in_text(text)
    assert [text[m.start:m.end] for m in ms] == [m.matched for m in ms]
    assert [m.place.name for m in ms] == ["Bay Shore", "Medford"]
    assert gaz.find_in_text("") == []


# ---------------------------------------------------------------------------- match_location
def ml(gaz, text, ctx="", qid=None):
    r = gaz.match_location(text, context_text=ctx, query_place_id=qid)
    return None if r is None else (r.name, r.kind, r.parent_name)


def test_landmark_preferred_over_hamlet(gaz):
    assert ml(gaz, "the library in Patchogue") == ("Patchogue-Medford Library", "library", "Patchogue")
    assert ml(gaz, "bus terminal in Hempstead") == ("Hempstead Transit Center", "transit", "Hempstead")
    assert ml(gaz, "the beach in Montauk") == ("Main Beach", "beach", "Montauk")
    assert ml(gaz, "Patchogue") == ("Patchogue", "village", None)


def test_same_name_parks_disambiguated_by_parent(gaz):
    assert ml(gaz, "Town Park in Bay Shore") == ("Town Park", "park", "Bay Shore")
    assert ml(gaz, "Town Park in Patchogue") == ("Town Park", "park", "Patchogue")
    assert ml(gaz, "Town Park", ctx="people in Patchogue keep saying") == ("Town Park", "park", "Patchogue")
    assert ml(gaz, "Town Park in Hempstead") == ("Town Park", "park", "Hempstead")
    assert ml(gaz, "Town Park") is None                                   # still ambiguous
    assert ml(gaz, "Town Park in Medford") == ("Medford", "cdp", None)    # no Town Park there: hamlet level


def test_query_place_disambiguates(gaz):
    patchogue = one(gaz, "Patchogue", "village")
    patchogue.id = 4242
    gaz._reindex_ids()
    try:
        assert ml(gaz, "Town Park", qid=4242) == ("Town Park", "park", "Patchogue")
    finally:
        patchogue.id = None
        gaz._reindex_ids()


def test_generic_landmark_needs_an_agreeing_hamlet(gaz):
    assert ml(gaz, "Veterans Memorial Park") is None
    assert ml(gaz, "Veterans Memorial Park", ctx="Medford") == ("Veterans Memorial Park", "park", "Medford")
    assert ml(gaz, "Veterans Memorial Park in Bay Shore") == ("Bay Shore", "cdp", None)


def test_distinctive_landmark_accepted_unless_span_contradicts(gaz):
    assert ml(gaz, "Heckscher State Park") == ("Heckscher State Park", "park", None)
    assert ml(gaz, "Eisenhower Park", ctx="I live in Bay Shore") == ("Eisenhower Park", "park", None)
    assert ml(gaz, "Heckscher State Park in Medford") == ("Medford", "cdp", None)


def test_station_flag(gaz):
    assert ml(gaz, "the train station in Bay Shore") == ("Bay Shore", "lirr_station", "Bay Shore")
    assert ml(gaz, "the Hicksville train station") == ("Hicksville", "lirr_station", "Hicksville")
    assert ml(gaz, "Hicksville") == ("Hicksville", "cdp", None)
    assert ml(gaz, "Huntington Station") == ("Huntington Station", "cdp", None)
    assert ml(gaz, "LIRR platform in Huntington Station") == ("Huntington", "lirr_station", "Huntington Station")
    assert ml(gaz, "Bayshore station") == ("Bay Shore", "lirr_station", "Bay Shore")
    # An unknown named station is not "the station" of a hamlet mentioned elsewhere.
    assert ml(gaz, "Penn Station", ctx="took the train from Hicksville") is None
    assert ml(gaz, "Jamaica station", ctx="Hicksville") is None
    assert ml(gaz, "the station", ctx="Hicksville") == ("Hicksville", "lirr_station", "Hicksville")
    # Tracks and trestles are not the station.
    assert ml(gaz, "the train tracks behind the Target in Patchogue") == ("Patchogue", "village", None)
    assert ml(gaz, "under the LIRR trestle in Patchogue") == ("Patchogue", "village", None)
    assert ml(gaz, "the LIRR in Patchogue") == ("Patchogue", "lirr_station", "Patchogue")
    assert ml(gaz, "Patchogue train station platform") == ("Patchogue", "lirr_station", "Patchogue")


def test_fuzzy_match(gaz):
    assert ml(gaz, "Hecksher State Park") == ("Heckscher State Park", "park", None)
    assert ml(gaz, "the Hecksher Park lot in Bay Shore") == ("Bay Shore", "cdp", None)  # wrong town: hamlet
    assert ml(gaz, "behind the Wawa") is None
    assert ml(gaz, "the park") is None


def test_town_phrasing_and_bare_names(gaz):
    assert ml(gaz, "Town of Hempstead") == ("Hempstead", "town", None)
    assert ml(gaz, "Hempstead") == ("Hempstead", "village", None)
    assert ml(gaz, "behind the Wawa in Medford") == ("Medford", "cdp", None)
    assert ml(gaz, "") is None
    assert ml(gaz, "Long Island City") is None


# ---------------------------------------------------------------------------- lookups
def test_nearest_public_place(gaz):
    lib = one(gaz, "Patchogue-Medford Library", "library")
    assert gaz.nearest_public_place(lib.lat + 0.0009, lib.lon, 250) is lib      # ~100 m away
    assert gaz.nearest_public_place(lib.lat + 0.01, lib.lon + 0.03, 250) is None
    assert gaz.nearest_public_place(float("nan"), lib.lon, 250) is None


def test_containing_locality(gaz):
    lib = one(gaz, "Patchogue-Medford Library", "library")
    assert gaz.containing_locality(lib).name == "Patchogue"
    hk = one(gaz, "Heckscher State Park", "park")
    assert (gaz.containing_locality(hk).name, gaz.containing_locality(hk).kind) == ("Islip", "town")
    bs = one(gaz, "Bay Shore", "cdp")
    assert gaz.containing_locality(bs) is bs


def test_rotation_places_stable_and_scoped(gaz):
    rot = gaz.rotation_places()
    assert rot == gaz.rotation_places()
    assert {p.kind for p in rot} == {"town", "city", "village", "cdp", "lirr_station"}
    assert not any(p.kind in ("park", "library", "beach", "transit") for p in rot)
    names = [p.name.casefold() for p in rot]
    assert names == sorted(names)
    again = Gazetteer.load(PLACES)
    assert [(p.name, p.kind, p.source_id) for p in again.rotation_places()] == [(p.name, p.kind, p.source_id) for p in rot]


def test_reference_list_text_format_and_determinism(gaz, tmp_path):
    text = gaz.reference_list_text()
    lines = text.splitlines()
    assert lines[0] == "## Reference: places in scope"
    assert lines[1] == "Nassau County — towns and cities: Hempstead, Oyster Bay, Long Beach (city)."
    assert lines[2] == "Suffolk County — towns: Brookhaven, East Hampton, Huntington, Islip, Southold."
    assert lines[3] == "Nassau villages and hamlets: Hempstead, Hicksville, Island Park, Rockville Centre."
    assert lines[4].startswith("Suffolk villages and hamlets: Bay Shore, Fishers Island, Huntington, ")
    assert lines[5] == "LIRR stations in Nassau/Suffolk: Bay Shore, Hicksville, Huntington, Patchogue, Ronkonkoma."
    assert lines[6].startswith("Not in scope (NYC): Queens, Brooklyn, Long Island City, Jamaica")
    assert "Town Park" not in text and "Library" not in text
    # Same input in a different order -> byte-identical block (prompt caching).
    data = json.loads(PLACES.read_text())
    data["places"].reverse()
    p = tmp_path / "li_places.json"
    p.write_text(json.dumps(data))
    assert Gazetteer.load(p).reference_list_text() == text


def test_search(gaz):
    assert [(p.name, p.kind) for p in gaz.search("hun")][:3] == [
        ("Huntington", "cdp"), ("Huntington", "lirr_station"), ("Huntington", "town")]
    assert gaz.search("Bayshore")[0].name == "Bay Shore"
    assert gaz.search("patchouge")[0].name == "Patchogue"                   # fuzzy
    assert len(gaz.search("park", limit=2)) == 2
    assert gaz.search("") == [] and gaz.search("   ") == []


def test_by_name_and_by_id(gaz):
    assert {p.kind for p in gaz.by_name("Hempstead")} == {"village", "town"}
    assert [p.kind for p in gaz.by_name("hempstead", ("town",))] == ["town"]
    assert gaz.by_id(999999) is None
    assert gaz.by_id("nope") is None  # type: ignore[arg-type]


def test_mentioned_localities(gaz):
    got = gaz.mentioned_localities("Sleeping at the Patchogue-Medford Library, then the Town of Islip lot")
    assert [(p.name, p.kind) for p in got] == [("Patchogue", "village"), ("Islip", "town")]
