"""Lexicon loader and matchers (config/lexicon.yaml; docs/04 §5, docs/05 §5 V2/V6).

STUB - implemented by the text-pipeline builder. Public API below is the contract used by the
prefilter, verifier (V2/V6), privacy (notes check) and planner.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.paths import CONFIG_DIR

SIGNAL_LISTS = ("encampment_terms", "individual_sighting_terms", "vehicle_dwelling_terms",
                "self_report_terms", "spanish_terms")


@dataclass(frozen=True)
class TermMatch:
    list_name: str        # e.g. "encampment_terms", "privacy_denylist.drop.age", "privacy_patterns.street_address"
    entry: str            # the lexicon entry (plain text or "re:..." pattern) that matched
    start: int            # character offsets into the text passed in
    end: int
    matched: str          # the matched substring


class Lexicon:
    """Compiled lexicon. Matching rules are in the header of config/lexicon.yaml."""

    version: int
    query_templates: dict[str, Any]

    @classmethod
    def load(cls, path: Path = CONFIG_DIR / "lexicon.yaml", overrides: Optional[dict[str, Any]] = None) -> "Lexicon":
        """Load + compile. `overrides` (settings key lexicon.overrides) replaces whole top-level lists."""
        raise NotImplementedError

    # -- generic --------------------------------------------------------------
    def find(self, text: str, list_name: str) -> list[TermMatch]:
        """All non-overlapping matches of one list (dotted names for nested lists)."""
        raise NotImplementedError

    # -- prefilter (docs/04 §5) -------------------------------------------------
    def signal_matches(self, text: str) -> list[TermMatch]:
        """Matches from the five signal lists (SIGNAL_LISTS)."""
        raise NotImplementedError

    def region_anchor_matches(self, text: str) -> list[TermMatch]:
        """region_anchor_terms matches, EXCLUDING 'Long Island' inside 'Long Island City'."""
        raise NotImplementedError

    def place_type_matches(self, text: str) -> list[TermMatch]:
        raise NotImplementedError

    def recency_matches(self, text: str) -> list[TermMatch]:
        raise NotImplementedError

    def soft_exclusion_matches(self, text: str) -> list[TermMatch]:
        raise NotImplementedError

    def hard_exclusion(self, text: str, *, has_li_place: bool) -> Optional[str]:
        """Return 'animal' | 'nyc_only' | 'out_of_area' | None per lexicon.yaml §4 rules.
        nyc_only applies only when not has_li_place and no region anchor; out_of_area only with no region anchor."""
        raise NotImplementedError

    # -- privacy (docs/05 V2, V6; notes check docs/07 §3) ------------------------
    def privacy_drop_matches(self, text: str) -> list[TermMatch]:
        """privacy_denylist.drop.* matches + privacy_patterns matches (any => drop the quote)."""
        raise NotImplementedError

    def privacy_mask(self, text: str) -> tuple[str, bool]:
        """Replace privacy_denylist.mask words with their bracketed neutral word. Returns (text, changed)."""
        raise NotImplementedError

    def privacy_any(self, text: str) -> list[TermMatch]:
        """Matches from BOTH tiers + patterns (V6 summaries, staff notes)."""
        raise NotImplementedError
