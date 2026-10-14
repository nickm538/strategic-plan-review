"""Deterministic prefilter and score (docs/04 §5).

Keep an item only if all hold:
  1. its text matches >= 1 homelessness signal term (lexicon SIGNAL_LISTS);
  3. no hard exclusion (lexicon.hard_exclusion; has_li_place = any gazetteer mention in the text);
  2. a Long Island anchor: a gazetteer mention, an LI-scoped source, the query's place name in the text,
     or a region anchor term; for comments the thread title (context.parent_title) may supply it.
Rule 3 is checked before rule 2 so the drop reason names the more specific cause (e.g. "Jamaica station"
-> exclusion:nyc_only rather than no_li_place). The score orders the classification queue:
base 0.3; +0.2 signal term within 15 words of a place term (gazetteer mention, place-type term, region
anchor or the query place); +0.2 any recency term; +0.1 per additional distinct signal term (max +0.2);
+0.1 LI-scoped source; -0.2 any soft exclusion; clamped to 0..1 and rounded to 3 decimals.
"""
from __future__ import annotations

import bisect
import logging
import re
from typing import TYPE_CHECKING, Any, Optional, Sequence

from app.pipeline import textnorm
from app.pipeline.lexicon import plain_pattern
from app.pipeline.types import NormalizedItem, PrefilterResult

if TYPE_CHECKING:
    from app.pipeline.geo.gazetteer import Gazetteer
    from app.pipeline.lexicon import Lexicon, TermMatch

logger = logging.getLogger(__name__)

BASE_SCORE = 0.3
PROXIMITY_BONUS = 0.2
PROXIMITY_WORDS = 15
RECENCY_BONUS = 0.2
EXTRA_TERM_BONUS = 0.1
EXTRA_TERM_MAX = 0.2
LI_SCOPED_BONUS = 0.1
SOFT_EXCLUSION_PENALTY = 0.2

_WORD = re.compile(r"\S+")
_gazetteer_warned = False


def _mentions(gazetteer: Optional["Gazetteer"], text: str) -> list[Any]:
    """gazetteer.find_in_text, tolerant of a missing or not-yet-ready gazetteer (never raises)."""
    global _gazetteer_warned
    if gazetteer is None or not text:
        return []
    try:
        return list(gazetteer.find_in_text(text) or [])
    except NotImplementedError:
        return []
    except Exception as exc:  # noqa: BLE001 - a geo failure must not stop the scan
        if not _gazetteer_warned:
            logger.warning("prefilter: gazetteer lookup failed (%s); using region anchors only", type(exc).__name__)
            _gazetteer_warned = True
        return []


def _mention_name(m: Any) -> str:
    place = getattr(m, "place", None)
    name = getattr(place, "name", None) if place is not None else None
    return str(name or getattr(m, "matched", "") or "")


def _span(m: Any) -> Optional[tuple[int, int]]:
    start, end = getattr(m, "start", None), getattr(m, "end", None)
    if isinstance(start, int) and isinstance(end, int) and end > start:
        return start, end
    return None


def _name_spans(name: Optional[str], text: str) -> list[tuple[int, int]]:
    if not name or not text:
        return []
    try:
        pat = plain_pattern(name)
    except ValueError:
        return []
    return [(m.start(), m.end()) for m in pat.finditer(text)]


def _distinct_terms(matches: Sequence["TermMatch"]) -> list[str]:
    """One matched phrase per distinct lexicon entry (casefolded, whitespace-collapsed), in text order."""
    seen: set[str] = set()
    out: list[str] = []
    for m in matches:
        if m.entry in seen:
            continue
        seen.add(m.entry)
        out.append(" ".join(m.matched.casefold().split()))
    return out


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        key = v.casefold()
        if v and key not in seen:
            seen.add(key)
            out.append(v)
    return out


def _near(text: str, a_spans: list[tuple[int, int]], b_spans: list[tuple[int, int]], max_words: int) -> bool:
    """True if any span in a_spans is within max_words words of any span in b_spans."""
    if not a_spans or not b_spans:
        return False
    starts = [m.start() for m in _WORD.finditer(text)]
    if not starts:
        return False

    def word_range(span: tuple[int, int]) -> tuple[int, int]:
        lo = max(0, bisect.bisect_right(starts, span[0]) - 1)
        hi = max(lo, bisect.bisect_right(starts, max(span[0], span[1] - 1)) - 1)
        return lo, hi

    a_words = [word_range(s) for s in a_spans]
    b_words = [word_range(s) for s in b_spans]
    for a0, a1 in a_words:
        for b0, b1 in b_words:
            if max(0, b0 - a1, a0 - b1) <= max_words:
                return True
    return False


def prefilter(item: NormalizedItem, *, lexicon: "Lexicon", gazetteer: Optional["Gazetteer"]) -> PrefilterResult:
    """Rules 1-3 + score from docs/04 §5. Uses item.context.source_scope == 'li_scoped' and
    item.context.query_place_name for the anchor rule; gazetteer.find_in_text for place names
    (if gazetteer is None, only region anchors / LI-scoped sources can satisfy rule 2).
    drop_reason: 'no_signal_term' | 'no_li_place' | 'exclusion:animal' | 'exclusion:nyc_only' | 'exclusion:out_of_area'."""
    text = textnorm.clean(item.text or "")
    ctx = item.context

    signals = lexicon.signal_matches(text)
    terms = _distinct_terms(signals)
    mentions = _mentions(gazetteer, text)
    places = _unique([_mention_name(m) for m in mentions])

    # Rule 1: a homelessness signal term.
    if not signals:
        return PrefilterResult(keep=False, score=0.0, matched_signal_terms=[], matched_places=places,
                               drop_reason="no_signal_term")

    # Rule 3: hard exclusions (animal / NYC-only / out-of-area).
    exclusion = lexicon.hard_exclusion(text, has_li_place=bool(mentions))
    if exclusion:
        return PrefilterResult(keep=False, score=0.0, matched_signal_terms=terms, matched_places=places,
                               drop_reason=f"exclusion:{exclusion}")

    # Rule 2: Long Island anchor.
    anchors = lexicon.region_anchor_matches(text)
    li_scoped = getattr(ctx, "source_scope", "general") == "li_scoped"
    query_place = getattr(ctx, "query_place_name", None)
    query_spans = _name_spans(query_place, text)
    anchored = bool(mentions) or li_scoped or bool(query_spans) or bool(anchors)
    parent = getattr(ctx, "parent_title", None)
    if not anchored and parent:
        ptext = textnorm.clean(parent)
        anchored = (bool(_mentions(gazetteer, ptext)) or bool(lexicon.region_anchor_matches(ptext))
                    or bool(_name_spans(query_place, ptext)))
    if not anchored:
        return PrefilterResult(keep=False, score=0.0, matched_signal_terms=terms, matched_places=places,
                               drop_reason="no_li_place")

    # Score.
    score = BASE_SCORE
    signal_spans = [(m.start, m.end) for m in signals]
    place_spans: list[tuple[int, int]] = [s for s in (_span(m) for m in mentions) if s]
    place_spans += [(m.start, m.end) for m in lexicon.place_type_matches(text)]
    place_spans += [(m.start, m.end) for m in anchors]
    place_spans += query_spans
    if _near(text, signal_spans, place_spans, PROXIMITY_WORDS):
        score += PROXIMITY_BONUS
    if lexicon.recency_matches(text):
        score += RECENCY_BONUS
    score += min(EXTRA_TERM_MAX, EXTRA_TERM_BONUS * max(0, len(terms) - 1))
    if li_scoped:
        score += LI_SCOPED_BONUS
    if lexicon.soft_exclusion_matches(text):
        score -= SOFT_EXCLUSION_PENALTY
    score = round(min(1.0, max(0.0, score)), 3)

    return PrefilterResult(keep=True, score=score, matched_signal_terms=terms, matched_places=places,
                           drop_reason=None)
