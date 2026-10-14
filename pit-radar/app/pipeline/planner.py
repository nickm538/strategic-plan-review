"""Query planning and rotation (docs/04 §6). STUB - collectors builder."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from app.pipeline.types import NormalizedItem, PlannedQuery

if TYPE_CHECKING:
    from app.pipeline.geo.gazetteer import Gazetteer, PlaceRec
    from app.pipeline.lexicon import Lexicon
    from app.pipeline.sources import SourcesConfig
    from app.runtime_settings import Effective


@dataclass
class PlanResult:
    queries: list[PlannedQuery]
    new_cursor: int
    notes: list[str] = field(default_factory=list)   # e.g. "socialcrawl keyword_search on tiktok unavailable"


def plan_scan(
    *,
    eff: "Effective",
    lexicon: "Lexicon",
    sources: "SourcesConfig",
    gazetteer: Optional["Gazetteer"],
    rotation_cursor: int,
    hot_places: list["PlaceRec"],
    resolved_endpoints: dict[str, Any],
    deferred: Optional[list[PlannedQuery]] = None,
) -> PlanResult:
    """Build the scan's query list: (1) subreddit_new for every enabled subreddit; region-wide keyword
    templates on each enabled SocialCrawl keyword platform whose capability resolved, plus news_search,
    Exa (semantic templates) and Firecrawl (keyword templates); (2) hot places (<= HOT_PLACES_PER_SCAN);
    (3) rotation: next PLACES_PER_SCAN places from gazetteer.rotation_places() starting at rotation_cursor,
    1-2 templates each on 2 rotating platforms; (4) previously deferred queries first.
    Priorities: deferred > subreddit_new > region > hot > rotation. Without a gazetteer, skip (2)-(3)."""
    raise NotImplementedError


def plan_comment_queries(items: list[NormalizedItem], *, resolved_endpoints: dict[str, Any], max_threads: int = 10) -> list[PlannedQuery]:
    """post_comments queries for Reddit threads (not comments) that passed the prefilter, newest first."""
    raise NotImplementedError
