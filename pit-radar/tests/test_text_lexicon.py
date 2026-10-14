"""Lexicon loader and matchers (config/lexicon.yaml; docs/04 §5, docs/05 V2/V6, docs/10 §1)."""
from __future__ import annotations

import re

import pytest
import yaml

from app.paths import CONFIG_DIR
from app.pipeline.lexicon import SIGNAL_LISTS, Lexicon, TermMatch, compile_entry, plain_pattern
from app.pipeline.textnorm import clean

LEXICON_FILE = CONFIG_DIR / "lexicon.yaml"


@pytest.fixture(scope="module")
def lx() -> Lexicon:
    return Lexicon.load()


def _raw_entries(node, prefix=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _raw_entries(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(node, list):
        for i, e in enumerate(node):
            yield f"{prefix}[{i}]", e
    elif isinstance(node, str):
        yield prefix, node


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------
def test_every_lexicon_entry_compiles():
    raw = yaml.safe_load(LEXICON_FILE.read_text(encoding="utf-8"))
    count = 0
    for top, node in raw.items():
        if top in ("version", "query_templates"):
            continue
        for where, entry in _raw_entries(node, top):
            assert isinstance(entry, str) and entry.strip(), where
            pat = compile_entry(entry)
            assert pat.search("") is None, f"{where} matches empty text"
            count += 1
    assert count > 300


def test_load_exposes_every_list_and_templates(lx):
    names = set(lx.list_names())
    for n in SIGNAL_LISTS:
        assert n in names
    for n in ("region_anchor_terms", "place_type_terms", "recency_terms", "soft_exclusions",
              "hard_exclusions.animal", "hard_exclusions.nyc_only_locations", "hard_exclusions.out_of_area_hints",
              "privacy_denylist.drop.age", "privacy_denylist.mask.[person]", "privacy_patterns.street_address"):
        assert n in names
    assert lx.version == 1
    assert "region_wide" in lx.query_templates and "place" in lx.query_templates
    for name in names:
        assert isinstance(lx.find("", name), list)


def test_regex_entries_honor_scoped_case_flags():
    ice = compile_entry("re:(?-i:\\bICE\\b)")
    assert ice.search("ICE agents near the station")
    assert not ice.search("ice on the platform")
    assert compile_entry("re:\\bhomeless\\b").search("HOMELESS camp")


def test_plain_entries_use_word_boundaries_and_flexible_whitespace():
    seven = compile_entry("7-Eleven")
    assert seven.search("sleeping behind the 7-Eleven on Main")
    assert seven.search("7-ELEVEN lot")
    assert not seven.search("17-Eleven")
    assert not seven.search("7-Elevens")
    tent = compile_entry("tent city")
    assert tent.search("a TENT   CITY by the tracks")
    assert not tent.search("tent cityscape")
    homeless = compile_entry("homeless")
    assert not homeless.search("homelessness services")
    bay = plain_pattern("Bay  Shore")
    assert bay.search("tents in bay shore") and not bay.search("Bayshore")
    assert plain_pattern("re:(").search("re:( literal")          # a place name is never read as a regex


@pytest.mark.parametrize("bad", ["", "   ", "re:", "re:(", "re:a*", "re:(?<!\\w)"])
def test_compile_entry_rejects_bad_entries(bad):
    with pytest.raises(ValueError):
        compile_entry(bad)


def test_unknown_list_name_raises(lx):
    with pytest.raises(KeyError):
        lx.find("homeless", "no_such_list")


# ---------------------------------------------------------------------------
# find(): non-overlapping, longest first, offsets into the passed text
# ---------------------------------------------------------------------------
def test_signal_matches_longest_first_non_overlapping(lx):
    text = clean("There is a homeless encampment behind the mall and a homeless man nearby.")
    ms = lx.signal_matches(text)
    assert [m.matched for m in ms] == ["homeless encampment", "homeless"]
    assert ms[0].list_name == "encampment_terms" and ms[1].list_name == "individual_sighting_terms"
    for m in ms:
        assert isinstance(m, TermMatch)
        assert text[m.start:m.end] == m.matched


def test_self_report_beats_bare_homeless(lx):
    text = clean("I’m homeless in Hempstead, sleeping in my car")
    ms = lx.signal_matches(text)
    lists = {m.list_name for m in ms}
    assert "self_report_terms" in lists
    assert all(m.matched.lower() != "homeless" for m in ms)


def test_group_names_search_all_sublists(lx):
    ms = lx.find("an elderly guy wearing a hoodie", "privacy_denylist.drop")
    assert {m.list_name for m in ms} == {"privacy_denylist.drop.age", "privacy_denylist.drop.appearance_and_clothing"}


# ---------------------------------------------------------------------------
# docs/10 §1 pattern cases
# ---------------------------------------------------------------------------
def test_ice_vs_ice(lx):
    assert not lx.privacy_drop_matches("there was ice on the platform this morning")
    hits = lx.privacy_drop_matches("ICE was at the station")
    assert [m.list_name for m in hits] == ["privacy_denylist.drop.immigration_status"]


@pytest.mark.parametrize("text", [
    "they camp in Huntington in the woods behind the school",
    "homeless camp somewhere between Medford or Patchogue",
    "Huntington is where the camp is",
    "Huntington Station homeless camp",
])
def test_ordinary_words_are_not_out_of_area(lx, text):
    assert not lx.find(text, "hard_exclusions.out_of_area_hints")
    assert lx.hard_exclusion(text, has_li_place=True) is None


@pytest.mark.parametrize("text", [
    "Huntington, WV homeless camp cleared",
    "Huntington WV homeless camp cleared",
    "homeless camp in Huntington, West Virginia",
    "Huntington, IN encampment",
    "Levittown, PA encampment",
    "Long Beach, California tent city",
    "Suffolk, VA homeless camp",
    "Nassau, Bahamas homeless",
])
def test_out_of_area_hints_excluded_without_region_anchor(lx, text):
    assert lx.hard_exclusion(text, has_li_place=True) == "out_of_area"


def test_out_of_area_hint_kept_when_region_anchor_present(lx):
    text = "Not Huntington, WV - this is Huntington on Long Island, homeless camp off the LIRR"
    assert lx.region_anchor_matches(text)
    assert lx.hard_exclusion(text, has_li_place=True) is None
    # an anchor word inside the hint itself does not count as an anchor
    assert lx.region_anchor_matches("Suffolk, VA camp") == []
    assert lx.hard_exclusion("Suffolk County, unlike Suffolk, VA, has a camp", has_li_place=False) is None


def test_long_island_city_carve_out(lx):
    text = "Long Island City encampment under the bridge"
    assert lx.region_anchor_matches(text) == []
    assert lx.hard_exclusion(text, has_li_place=False) == "nyc_only"
    # "Long Island" elsewhere in the same text still counts
    both = "Moved from Long Island City out to Long Island proper"
    anchors = lx.region_anchor_matches(both)
    assert [both[m.start:m.end] for m in anchors] == ["Long Island"]
    assert anchors[0].start == both.rindex("Long Island")
    assert lx.hard_exclusion(both, has_li_place=False) is None
    # a Nassau/Suffolk place also lifts the NYC exclusion
    assert lx.hard_exclusion(text, has_li_place=True) is None


@pytest.mark.parametrize("text,expected", [
    ("Homeless guy sleeping at Jamaica station again", "nyc_only"),
    ("encampment in Queens near the park", "nyc_only"),
    ("people sleeping at Penn Station", "nyc_only"),
    ("tents at LIC waterfront", "nyc_only"),
    ("tents at lic waterfront", None),                 # LIC is case-sensitive
    ("Floral Park tents near the station", None),      # border places never excluded
])
def test_nyc_only(lx, text, expected):
    assert lx.hard_exclusion(text, has_li_place=False) == expected


def test_region_anchor_li_uppercase_only(lx):
    assert [m.matched for m in lx.region_anchor_matches("homeless camp on LI")] == ["LI"]
    assert lx.region_anchor_matches("homeless camp on li") == []
    assert [m.matched for m in lx.region_anchor_matches("near the Long Island Rail Road")] == ["Long Island Rail Road"]


@pytest.mark.parametrize("text", [
    "Adopt this homeless kitten in Massapequa!",
    "homeless dogs need a foster",
    "HOMELESS CAT spotted",
    "perros callejeros en el parque",
])
def test_animal_exclusion(lx, text):
    assert lx.hard_exclusion(text, has_li_place=True) == "animal"


def test_homeless_person_is_not_animal(lx):
    assert lx.hard_exclusion("homeless person sleeping at the Hicksville station", has_li_place=True) is None


def test_recency_place_type_soft(lx):
    assert [m.matched for m in lx.recency_matches("still there tonight")] == ["still there", "tonight"]
    assert [m.matched for m in lx.place_type_matches("behind the train station")] == ["train station"]
    assert [m.matched for m in lx.soft_exclusion_matches("Point-in-time count volunteers")] == \
        ["Point-in-time count", "volunteers"]


# ---------------------------------------------------------------------------
# Privacy tiers
# ---------------------------------------------------------------------------
def test_mask_replaces_gendered_words_and_preserves_everything_else(lx):
    text = "He said she saw a man and his Dad (Brother?) by the bench."
    masked, changed = lx.privacy_mask(text)
    assert changed is True
    assert masked == "[they] said [they] saw a [person] and [their] [person] ([person]?) by the bench."


def test_mask_word_boundaries_and_no_change(lx):
    text = "The Manhattan-bound shelter: these men's tents."
    masked, changed = lx.privacy_mask(text)
    assert masked == "The Manhattan-bound shelter: these [people]'s tents."
    assert changed
    same, changed2 = lx.privacy_mask("tents behind the library")
    assert same == "tents behind the library" and changed2 is False


@pytest.mark.parametrize("text,category", [
    ("an elderly person on the bench", "privacy_denylist.drop.age"),
    ("a 45 yo sleeping there", "privacy_denylist.drop.age"),
    ("old man at the station", "privacy_denylist.drop.age"),
    ("wearing a hoodie", "privacy_denylist.drop.appearance_and_clothing"),
    ("seemed drunk", "privacy_denylist.drop.substance_use"),
    ("in a wheelchair", "privacy_denylist.drop.disability_and_health"),
    ("car at 123 Main St", "privacy_patterns.street_address"),
    ("license plate visible", "privacy_patterns.license_plate_word"),
    ("call 631-555-0123", "privacy_patterns.phone"),
    ("ask @someone", "privacy_patterns.handle"),
])
def test_drop_matches(lx, text, category):
    assert category in {m.list_name for m in lx.privacy_drop_matches(text)}


def test_mask_words_are_not_drop_but_are_privacy_any(lx):
    assert lx.privacy_drop_matches("a man sleeping by the bus shelter") == []
    any_hits = lx.privacy_any("a man sleeping by the bus shelter")
    assert [m.list_name for m in any_hits] == ["privacy_denylist.mask.[person]"]
    assert lx.privacy_any("tent behind the library") == []


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------
def test_override_replaces_whole_list_by_dotted_name():
    lx = Lexicon.load(overrides={"privacy_denylist.drop.age": ["toddler"], "soft_exclusions": ["bake sale"]})
    assert lx.entries("privacy_denylist.drop.age") == ["toddler"]
    assert not lx.find("an elderly person", "privacy_denylist.drop.age")
    assert lx.find("a toddler", "privacy_denylist.drop.age")
    assert [m.matched for m in lx.soft_exclusion_matches("bake sale fundraiser")] == ["bake sale"]
    eff = lx.to_dict()
    assert eff["privacy_denylist"]["drop"]["age"] == ["toddler"]
    assert eff["privacy_denylist"]["drop"]["substance_use"]          # siblings untouched
    assert lx.overrides == {"privacy_denylist.drop.age": ["toddler"], "soft_exclusions": ["bake sale"]}
    assert lx.override_errors == []


def test_nested_mapping_override_and_new_mask_token():
    lx = Lexicon.load(overrides={"privacy_denylist": {"mask": {"[someone]": ["bloke"]}}})
    masked, changed = lx.privacy_mask("a bloke and a man")
    assert changed and masked == "a [someone] and a [person]"


def test_validate_overrides_reports_every_problem():
    errors = Lexicon.validate_overrides({
        "privacy_denylist.drop.age": ["re:(", "kid"],
        "no_such_list": ["x"],
        "version": 2,
        "privacy_denylist.mask.notatoken": ["x"],
        "encampment_terms": "encampment",
        "recency_terms": ["re:.*"],
    })
    joined = "\n".join(errors)
    assert "privacy_denylist.drop.age[0]" in joined and "invalid regular expression" in joined
    assert "no_such_list: unknown lexicon list" in joined
    assert "version: cannot be overridden" in joined
    assert "bracket tokens" in joined
    assert "encampment_terms: expected a list of strings" in joined
    assert "recency_terms[0]" in joined and "empty text" in joined
    assert Lexicon.validate_overrides({"encampment_terms": ["tent city", "re:\\bcamp\\b"]}) == []
    assert Lexicon.validate_overrides({"query_templates.region_wide.keyword": ['homeless "{place}"']}) == []
    assert Lexicon.validate_overrides("nope")


def test_invalid_override_is_skipped_unless_strict():
    lx = Lexicon.load(overrides={"encampment_terms": ["re:("], "soft_exclusions": ["gala"]})
    assert lx.override_errors
    assert "encampment" in lx.entries("encampment_terms")            # file default kept
    assert lx.entries("soft_exclusions") == ["gala"]                  # valid override applied
    with pytest.raises(ValueError):
        Lexicon.load(overrides={"encampment_terms": ["re:("]}, strict=True)


def test_none_override_means_file_default():
    lx = Lexicon.load(overrides={"soft_exclusions": None})
    assert "fundraiser" in lx.entries("soft_exclusions")
    assert lx.overrides == {}


def test_query_template_override_is_not_compiled_as_regex():
    lx = Lexicon.load(overrides={"query_templates.region_wide.keyword": ['"(" homeless']})
    assert lx.query_templates["region_wide"]["keyword"] == ['"(" homeless']


# ---------------------------------------------------------------------------
# The first-word fast skip must never change results
# ---------------------------------------------------------------------------
def test_ascii_case_traps_are_exactly_the_four_known_characters():
    import sys
    from app.pipeline.lexicon import _ASCII_CASE_TRAPS
    p = re.compile(r"[a-z0-9_]", re.IGNORECASE)
    found = tuple(chr(c) for c in range(0x80, sys.maxunicode + 1) if p.fullmatch(chr(c)))
    assert set(found) == set(_ASCII_CASE_TRAPS)


@pytest.mark.parametrize("text", [
    "Tent encampment behind the Hicksville station, still there tonight; 7-Eleven lot too.",
    "HOMELESS man (elderly) wearing a hoodie at 123 Main St, LIRR, Long Island City, Queens.",
    "SLEEPİNG at the statıon? ſleeping in my car. Kid in the park (Kelvin K).",
    "Duermen en la calle cerca de la estación, sin hogar desde hace semanas; señora y niño.",
    "point-in-time count volunteers; Code Blue; Huntington, WV; Suffolk, VA; perros callejeros.",
    "",
])
def test_fast_skip_matches_full_scan(lx, monkeypatch, text):
    import app.pipeline.lexicon as lexmod
    text = clean(text)
    fast = {name: lx.find(text, name) for name in lx.list_names()}
    monkeypatch.setattr(lexmod, "_text_tokens", lambda _t: None)
    slow = {name: lx.find(text, name) for name in lx.list_names()}
    assert fast == slow
