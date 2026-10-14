"""Gazetteer of Long Island public places (docs/06 §2.2, §3). STUB - geography builder."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session


@dataclass
class PlaceRec:
    name: str
    kind: str                     # town | city | village | cdp | lirr_station | park | beach | mall | transit | library | hospital | other_public
    county: str                   # Nassau | Suffolk
    lat: float
    lon: float
    source: str                   # census_tigerweb | census_gazetteer | osm_overpass
    source_id: str
    aliases: list[str] = field(default_factory=list)
    parent_name: Optional[str] = None   # village/CDP containing a landmark or station
    town: Optional[str] = None          # town/city (county subdivision) containing it
    id: Optional[int] = None            # places.id once synced to the DB

    @property
    def is_public_place(self) -> bool:
        return self.kind not in ("town", "city", "village", "cdp")

    @property
    def label(self) -> str:
        """Display label, e.g. 'Ronkonkoma LIRR station', 'Bay Shore', 'Heckscher State Park'."""
        raise NotImplementedError


@dataclass(frozen=True)
class PlaceMention:
    place: PlaceRec
    start: int
    end: int
    matched: str


class Gazetteer:
    places: list[PlaceRec]

    @classmethod
    def load(cls, path: Path) -> "Gazetteer":
        """Load li_places.json (built by app.pipeline.geo.build). Raises FileNotFoundError if absent."""
        raise NotImplementedError

    def find_in_text(self, text: str) -> list[PlaceMention]:
        """Word-boundary, case-insensitive, longest-match-first mentions of names/aliases (non-overlapping).
        Very short or ambiguous common-word names must not match on their own (e.g. a hamlet named 'Centre'
        or 'Flanders' is fine; guard generic words like 'Park', 'Beach', 'Station')."""
        raise NotImplementedError

    def match_location(self, location_text: str, *, context_text: str = "", query_place_id: Optional[int] = None) -> Optional[PlaceRec]:
        """docs/06 §3 steps 1-3: exact/alias, then prefer landmark whose parent matches a hamlet in the
        text/context, then fuzzy (rapidfuzz token_set_ratio >= 92) restricted to the mentioned hamlet/town."""
        raise NotImplementedError

    def by_id(self, place_id: int) -> Optional[PlaceRec]:
        raise NotImplementedError

    def by_name(self, name: str, kinds: Optional[tuple[str, ...]] = None) -> list[PlaceRec]:
        raise NotImplementedError

    def nearest_public_place(self, lat: float, lon: float, max_m: float) -> Optional[PlaceRec]:
        raise NotImplementedError

    def containing_locality(self, place: PlaceRec) -> Optional[PlaceRec]:
        """The village/CDP (or town) record a landmark belongs to, for hamlet fallback."""
        raise NotImplementedError

    def rotation_places(self) -> list[PlaceRec]:
        """Places for the planner rotation: towns, cities, villages, CDPs and LIRR stations, stable order."""
        raise NotImplementedError

    def reference_list_text(self) -> str:
        """The 'Reference: places in scope' block appended to the classifier system prompt
        (prompts/classifier-system.md). Deterministic output (sorted) so prompt caching works."""
        raise NotImplementedError

    def search(self, q: str, limit: int = 10) -> list[PlaceRec]:
        """Prefix/fuzzy search for the map search box."""
        raise NotImplementedError


def sync_places(db: Session, gaz: Gazetteer) -> int:
    """Upsert every PlaceRec into `places` by (source, source_id), set parent_id/town, and write the DB id
    back onto each PlaceRec. Returns the number of rows. Caller commits."""
    raise NotImplementedError
