"""Deterministic prefilter and score (docs/04 §5). STUB - text-pipeline builder."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from app.pipeline.types import NormalizedItem, PrefilterResult

if TYPE_CHECKING:
    from app.pipeline.geo.gazetteer import Gazetteer
    from app.pipeline.lexicon import Lexicon


def prefilter(item: NormalizedItem, *, lexicon: "Lexicon", gazetteer: Optional["Gazetteer"]) -> PrefilterResult:
    """Rules 1-3 + score from docs/04 §5. Uses item.context.source_scope == 'li_scoped' and
    item.context.query_place_name for the anchor rule; gazetteer.find_in_text for place names
    (if gazetteer is None, only region anchors / LI-scoped sources can satisfy rule 2).
    drop_reason: 'no_signal_term' | 'no_li_place' | 'exclusion:animal' | 'exclusion:nyc_only' | 'exclusion:out_of_area'."""
    raise NotImplementedError
