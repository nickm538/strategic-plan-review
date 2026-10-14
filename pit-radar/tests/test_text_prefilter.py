"""Prefilter rules and score (docs/04 §5) incl. red-team cases 1-4 (docs/10 §3).

Uses a tiny duck-typed fake gazetteer (find_in_text only) so these tests do not depend on the geo build.
All place names are public Long Island place names; all post texts are synthetic.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
from dataclasses import dataclass

import pytest

from app.pipeline.lexicon import Lexicon
from app.pipeline.normalize import build_item
from app.pipeline.prefilter import prefilter
from app.pipeline.types import ItemContext, PrefilterResult


@dataclass
class _Place:
    name: str


@dataclass(frozen=True)
class _Mention:
    place: _Place
    start: int
    end: int
    matched: str


class FakeGazetteer:
    """find_in_text: word-boundary, case-insensitive, longest-first, non-overlapping (like the real one)."""

    def __init__(self, names: dict[str, str]):
        # alias/name -> canonical name
        self._pats = [(canon, re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, alias.split())) + r"(?!\w)", re.I))
                      for alias, canon in sorted(names.items(), key=lambda kv: -len(kv[0]))]
        self.calls = 0

    def find_in_text(self, text: str):
        self.calls += 1
        out: list[_Mention] = []
        taken: list[tuple[int, int]] = []
        for canon, pat in self._pats:
            for m in pat.finditer(text):
                if any(m.start() < e and s < m.end() for s, e in taken):
                    continue
                taken.append((m.start(), m.end()))
                out.append(_Mention(_Place(canon), m.start(), m.end(), m.group(0)))
        return sorted(out, key=lambda x: x.start)


class RaisingGazetteer:
    def __init__(self, exc: Exception):
        self.exc = exc

    def find_in_text(self, text: str):
        raise self.exc


_NAMES = ["Massapequa", "Huntington", "Huntington Station", "Hicksville", "Bay Shore", "Hempstead", "Merrick",
          "Patchogue", "Medford", "Commack", "Garden City", "Ronkonkoma"]
GAZ = FakeGazetteer({**{n: n for n in _NAMES}, "Bayshore": "Bay Shore"})
FILLER = " ".join(["blah"] * 20)


@pytest.fixture(scope="module")
def lx() -> Lexicon:
    return Lexicon.load()


NOW = dt.datetime(2026, 10, 14, 16, 0, tzinfo=dt.timezone.utc)


def _item(text: str, *, scope: str = "general", place: str | None = None, is_comment: bool = False,
          parent_title: str | None = None):
    item = build_item(provider="socialcrawl", platform="reddit", url="https://www.reddit.com/r/test/comments/1",
                      platform_post_id="1", published_at=NOW - dt.timedelta(hours=1), date_source="platform",
                      collected_at=NOW, title="", body=text,
                      context=ItemContext(query="q", source_scope=scope, query_place_name=place,
                                          is_comment=is_comment, parent_title=parent_title))
    assert item is not None
    return item


def run(lx, text, gaz=GAZ, **kw) -> PrefilterResult:
    return prefilter(_item(text, **kw), lexicon=lx, gazetteer=gaz)


# ---------------------------------------------------------------------------
# Red-team cases 1-4 (docs/10 §3): must not reach the classifier
# ---------------------------------------------------------------------------
def test_redteam_1_jamaica_station(lx):
    r = run(lx, "Homeless guy sleeping at Jamaica station again")
    assert r.keep is False and r.drop_reason == "exclusion:nyc_only"
    # even from an LI-scoped source (e.g. r/longisland) the NYC-only location excludes it
    r2 = run(lx, "Homeless guy sleeping at Jamaica station again", scope="li_scoped")
    assert r2.keep is False and r2.drop_reason == "exclusion:nyc_only"


def test_redteam_2_long_island_city(lx):
    r = run(lx, "Long Island City encampment under the bridge")
    assert r.keep is False and r.drop_reason == "exclusion:nyc_only"
    r2 = run(lx, "Long Island City encampment under the bridge", gaz=None, scope="li_scoped")
    assert r2.drop_reason == "exclusion:nyc_only"


def test_redteam_3_huntington_wv(lx):
    r = run(lx, "Huntington, WV homeless camp cleared")
    assert r.keep is False and r.drop_reason == "exclusion:out_of_area"
    assert r.matched_places == ["Huntington"]          # the LI town name alone does not rescue it


def test_redteam_4_homeless_kitten(lx):
    r = run(lx, "Adopt this homeless kitten in Massapequa!")
    assert r.keep is False and r.drop_reason == "exclusion:animal"
    assert r.matched_places == ["Massapequa"]


def test_other_redteam_texts_pass_or_drop_as_expected(lx):
    assert run(lx, "I'm homeless in Hempstead, sleeping in my car").keep is True          # self-report: classifier
    gala = run(lx, "Gala raising money for homeless families - Garden City Hotel")
    no_gala = run(lx, "Raising money for homeless families - Garden City Hotel")
    assert gala.keep is True and gala.score == 0.3 and no_gala.score == 0.5               # soft exclusion -0.2
    assert run(lx, "Suffolk County Legislature votes on shelter funding").drop_reason == "no_signal_term"


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def test_rule1_needs_a_signal_term(lx):
    r = run(lx, "Nice sunset at the Hicksville station")
    assert r.keep is False and r.drop_reason == "no_signal_term" and r.matched_signal_terms == []
    assert run(lx, "homelessness policy forum in Hicksville").drop_reason == "no_signal_term"


def test_rule2_needs_a_long_island_anchor(lx):
    r = run(lx, "homeless people sleeping at the station")
    assert r.keep is False and r.drop_reason == "no_li_place"
    assert r.matched_signal_terms


def test_rule2_li_scoped_source_without_place_name(lx):
    r = run(lx, "homeless guy sleeping by the dumpsters again", scope="li_scoped", gaz=None)
    assert r.keep is True and r.matched_places == []


def test_rule2_region_anchor_without_gazetteer(lx):
    assert run(lx, "tent city in the woods off the LIE, Suffolk County", gaz=None).keep is True
    assert run(lx, "homeless camp near the LIRR tracks", gaz=None).keep is True
    assert run(lx, "homeless camp near the tracks", gaz=None).drop_reason == "no_li_place"


def test_rule2_query_place_name(lx):
    r = run(lx, "people living in tents behind ronkonkoma station", gaz=None, place="Ronkonkoma")
    assert r.keep is True
    assert run(lx, "people living in tents behind the station", gaz=None, place="Ronkonkoma").drop_reason == "no_li_place"


def test_rule2_comment_uses_parent_title(lx):
    body = "someone has been sleeping in the car every night"
    r = run(lx, body, is_comment=True, parent_title="Car parked at the Bayshore train station lot")
    assert r.keep is True
    assert r.matched_places == []                       # places are counted from the item text only
    assert run(lx, body, is_comment=True, parent_title="Anyone else notice this?").drop_reason == "no_li_place"
    assert run(lx, body, is_comment=True, parent_title="Long Island folks, thoughts?", gaz=None).keep is True


def test_gazetteer_alias_reports_canonical_name(lx):
    r = run(lx, "homeless encampment behind the Bayshore mall")
    assert r.keep is True and r.matched_places == ["Bay Shore"]


def test_gazetteer_failures_do_not_raise(lx):
    for gaz in (RaisingGazetteer(NotImplementedError()), RaisingGazetteer(RuntimeError("boom"))):
        assert run(lx, "homeless camp in Hicksville", gaz=gaz).drop_reason == "no_li_place"
        assert run(lx, "homeless camp on Long Island", gaz=gaz).keep is True


def test_prefilter_does_not_mutate_item(lx):
    item = _item("homeless camp in Hicksville")
    before = dataclasses.asdict(item)
    prefilter(item, lexicon=lx, gazetteer=GAZ)
    assert dataclasses.asdict(item) == before and item.prefilter is None


# ---------------------------------------------------------------------------
# Score (docs/04 §5)
# ---------------------------------------------------------------------------
def test_score_base_only(lx):
    r = run(lx, f"Hicksville {FILLER} homeless")
    assert r.keep and r.score == 0.3


def test_score_proximity_bonus(lx):
    assert run(lx, "homeless person near Hicksville").score == 0.5
    # the 15-word window is inclusive: 14 filler words between = 15 words apart
    near = "homeless " + " ".join(["blah"] * 14) + " Hicksville"
    far = "homeless " + " ".join(["blah"] * 15) + " Hicksville"
    assert run(lx, near).score == 0.5
    assert run(lx, far).score == 0.3
    # place-type terms and region anchors count as place terms too
    assert run(lx, f"Hicksville {FILLER} homeless person on the boardwalk").score == 0.5
    assert run(lx, "homeless camp on Long Island", gaz=None).score == 0.5


def test_score_recency_bonus(lx):
    assert run(lx, f"Hicksville {FILLER} homeless tonight").score == 0.5


def test_score_additional_distinct_terms_capped(lx):
    assert run(lx, f"Hicksville {FILLER} homeless and unhoused").score == 0.4
    assert run(lx, f"Hicksville {FILLER} homeless and unhoused and unsheltered and vagrants").score == 0.5
    assert run(lx, f"Hicksville {FILLER} homeless homeless HOMELESS").score == 0.3      # same term repeated


def test_score_li_scoped_bonus(lx):
    assert run(lx, "homeless guy again", scope="li_scoped", gaz=None).score == 0.4


def test_score_soft_exclusion_penalty(lx):
    assert run(lx, f"Hicksville {FILLER} homeless fundraiser and gala").score == 0.1


def test_score_full_and_clamped(lx):
    text = "Unhoused people and a homeless encampment in the woods by the Commack park for weeks now, unsheltered"
    r = run(lx, text, scope="li_scoped")
    assert r.keep and r.score == 1.0
    r2 = run(lx, text + ", volunteers please donate", scope="li_scoped")
    assert r2.score == 0.8


def test_score_reference_example(lx):
    r = run(lx, "Tent encampment behind the Hicksville station, still there tonight.")
    assert r.keep and r.score == 0.7
    assert r.matched_signal_terms == ["tent encampment"]
    assert r.matched_places == ["Hicksville"]


def test_matched_terms_one_per_distinct_entry(lx):
    r = run(lx, "Homeless in Patchogue. HOMELESS in Medford or Patchogue. Unhoused too.")
    assert r.keep
    assert r.matched_signal_terms == ["homeless", "unhoused"]
    assert r.matched_places == ["Patchogue", "Medford"]
    assert isinstance(r.score, float) and r.score == round(r.score, 3)
