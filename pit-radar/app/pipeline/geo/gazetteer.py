"""Gazetteer of Long Island public places (docs/06 §2.2, §3).

Built at runtime by app.pipeline.geo.build into <geo_dir>/li_places.json. Every record comes from a
fetched dataset (Census TIGERweb / Census Gazetteer / OSM Overpass) with its source_id; nothing here
invents coordinates. Hand-curated spelling variants come from data/aliases.yaml (names only).

Matching (find_in_text, match_location) uses ONE compiled alternation per mode, built as a token trie
so it stays fast with thousands of names: case-insensitive, word-bounded, longest match first,
non-overlapping. Names that are ordinary English words ("Bay Shore", "Ridge", "Orient") must be
capitalized in the original text; bare generic words ("Park", "Beach", "Station") never match alone.
"""
from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import yaml
from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.paths import REPO_DATA_DIR

logger = logging.getLogger(__name__)

PLACES_FORMAT = "pit-radar-places/1"
ALIASES_FILE = "aliases.yaml"

COUNTIES = ("Nassau", "Suffolk")
TOWN_KINDS = ("town", "city")                       # county subdivisions
LOCALITY_KINDS = ("village", "cdp", "city")         # hamlet-level centroids
ADMIN_KINDS = ("town", "city", "village", "cdp")
STATION_KIND = "lirr_station"
LANDMARK_KINDS = ("park", "beach", "mall", "transit", "library", "hospital", "other_public")
ALL_KINDS = ADMIN_KINDS + (STATION_KIND,) + LANDMARK_KINDS
ROTATION_KINDS = ("town", "city", "village", "cdp", STATION_KIND)

# Preference when several records share a name: a bare "Hempstead" is the village, not the town.
_KIND_RANK = {"village": 0, "cdp": 0, "city": 1, STATION_KIND: 2, "town": 3}
_LANDMARK_RANK = 4

FUZZY_MIN = 92                      # docs/06 §3 step 3: rapidfuzz token_set_ratio >= 92
_FUZZY_TOKEN_MIN = 85               # a distinctive token must also be close

NOT_IN_SCOPE_LINE = ("Not in scope (NYC): Queens, Brooklyn, Long Island City, Jamaica, Flushing, Far Rockaway, "
                     "Rockaways, Atlantic Terminal, Penn Station, Grand Central Madison.")

# English words that occur in Long Island place names. A name made only of these words must be
# capitalized in the text to count as a place ("Bay Shore" yes, "the bay shore" no).
COMMON_WORDS = frozenset("""
bay bays shore shores island islands park lake lakes grove valley stream great neck river riverside garden gardens
city rocky stony point mill mills old field fields head harbor harbour success kings sands north south east west
northwest haven shelter fire ocean oceanside beach long lookout glen sea cliff flower hill hills upper lower cove
laurel hollow cold spring springs fort middle ridge orient water sound wading blue deer terrace heights central
center centre village branch the of port mount saint manor estates plains lawn view landing meadow meadows wood
woods forest rock brook creek new little green isle plaza square station bridge salt marsh pond bluff crest dale
farm gate inlet key lane reach side vista way
""".split())

# Single words that never count as a place on their own (unless they are a Census locality's own name).
GENERIC_SINGLE = frozenset("""
bay park harbor harbour point beach station center centre island village town city hospital library mall plaza
field lake pond river creek woods green commons square terminal county state north south east west main shore cove
neck hill hills heights landing manor gardens garden estates terrace grove valley stream marina preserve playground
school depot street road avenue lirr train bus the
""".split())

# Words that do not make a landmark name distinctive (with COMMON_WORDS). "Veterans Memorial Park" is
# generic; "Heckscher State Park" is not.
GENERIC_TOKENS = frozenset("""
memorial veterans veteran town county state national community public free municipal recreation recreational
sports athletic family childrens children kids dog pocket linear greenway trail heritage liberty freedom peace
unity friendship victory independence sunset sunrise lincoln washington jefferson kennedy martin luther king jr
mlk pine oak maple elm cedar willow marina playground commons common plaza preserve reserve nature sanctuary
library hospital medical health care mall shopping shops terminal transit bus and at on in for st saint mt main
street avenue road school high elementary church senior citizens center centre complex area lot parking beach
park field fields pool rink courts boat ramp launch dock pier boardwalk point pavilion lodge museum garden
gardens arboretum farm a an the of
""".split())

# "station"/"platform"/"LIRR" mean the station; "train tracks" or "LIRR trestle" do not.
_STATION_WORDS = re.compile(
    r"\b(?:stations?|stn|platforms?|(?:train|railroad|rail\s*road)\s+stations?"
    r"|lirr(?!\s+(?:tracks?|trestle|bridge|overpass|underpass|line|crossing|right[\s-]of[\s-]way)\b))\b", re.I)
_KIND_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("library", re.compile(r"\blibrar(?:y|ies)\b", re.I)),
    ("hospital", re.compile(r"\bhospitals?\b|\bmedical center\b", re.I)),
    ("mall", re.compile(r"\bmalls?\b|\bshopping (?:center|centre)\b", re.I)),
    ("beach", re.compile(r"\bbeach(?:es)?\b", re.I)),
    ("park", re.compile(r"\bparks?\b|\bpreserve\b|\bnature (?:area|reserve)\b", re.I)),
    ("transit", re.compile(r"\bbus (?:station|terminal|depot|hub)\b|\btransit (?:center|centre|hub)\b", re.I)),
)
_FILLER_WORDS = frozenset("""
the a an at in on near by behind outside inside across from front of next to beside under underneath around off
along over across corner area spot around between and or
""".split())

_WORD_RE = re.compile(r"[^\W\d_][\w'&.]*")
_STREET_REGION_WORDS = frozenset("""
route rt rte exit highway hwy expressway expy parkway pkwy turnpike tpke road rd street st avenue ave boulevard
blvd lane ln drive dr place pl court ct way terrace corner intersection overpass underpass bridge ny nys li usa us
nassau suffolk county york long island lirr mta
""".split())

# Same-length character mapping for scanning (keeps match offsets valid in the original text).
_SAME_LEN = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'", "`": "'",
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u00a0": " ", "\u2009": " ", "\u200a": " ", "\u202f": " ",
})
_NK_DROP = re.compile("[.'`\u2018\u2019]")
_NK_SEP = re.compile("[\\s\\-\u2010-\u2015\u2212/]+")
_TOK_SPLIT = re.compile("[\\s\\-\u2010-\u2015\u2212/]+")
_SEP_RE = r"(?:\s*-\s*|\s+)"
_ALNUM = re.compile(r"[^\W_]")
_END = "\x00"


def normkey(s: str) -> str:
    """Comparison key for a name or alias: NFKC, casefold, no dots/apostrophes, hyphens -> spaces."""
    t = unicodedata.normalize("NFKC", s or "").casefold()
    t = _NK_DROP.sub("", t)
    return _NK_SEP.sub(" ", t).strip()


def _tokens(form: str) -> list[str]:
    t = unicodedata.normalize("NFKC", form or "").translate(_SAME_LEN).casefold()
    return [tok for tok in _TOK_SPLIT.split(t) if tok and _ALNUM.search(tok)]


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(min(1.0, math.sqrt(a)))


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
        if self.kind == STATION_KIND:
            return f"{self.name} LIRR station"
        return self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "county": self.county,
            "lat": self.lat, "lon": self.lon, "source": self.source, "source_id": self.source_id,
            "aliases": list(self.aliases), "parent_name": self.parent_name, "town": self.town,
        }


@dataclass(frozen=True)
class PlaceMention:
    place: PlaceRec
    start: int
    end: int
    matched: str


@dataclass(frozen=True)
class _Entry:
    rec: PlaceRec
    alias: bool
    caps: Optional[str]           # None | "title" (must be capitalized) | "exact" (short all-caps alias)
    form: str


@dataclass
class _Hit:
    start: int
    end: int
    matched: str
    key: str
    entries: list[_Entry]


def kind_rank(kind: str) -> int:
    return _KIND_RANK.get(kind, _LANDMARK_RANK)


def _entry_sort_key(e: _Entry) -> tuple[Any, ...]:
    return (e.alias, kind_rank(e.rec.kind), e.rec.name.casefold(), e.rec.county, e.rec.source, e.rec.source_id)


def _rec_sort_key(r: PlaceRec) -> tuple[Any, ...]:
    return (kind_rank(r.kind), r.county, r.name.casefold(), r.source, r.source_id)


def _finite(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def rec_from_dict(d: Any) -> Optional[PlaceRec]:
    """Validated PlaceRec from a li_places.json row (None when unusable)."""
    if not isinstance(d, dict):
        return None
    name = str(d.get("name") or "").strip()
    kind = str(d.get("kind") or "").strip()
    county = str(d.get("county") or "").strip()
    lat, lon = _finite(d.get("lat")), _finite(d.get("lon"))
    source = str(d.get("source") or "").strip()
    source_id = str(d.get("source_id") or "").strip()
    if not name or kind not in ALL_KINDS or county not in COUNTIES or lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or not source or not source_id:
        return None
    aliases = [str(a).strip() for a in (d.get("aliases") or []) if isinstance(a, str) and str(a).strip()]
    parent = d.get("parent_name", d.get("parent_place"))
    town = d.get("town")
    return PlaceRec(name=name, kind=kind, county=county, lat=lat, lon=lon, source=source, source_id=source_id,
                    aliases=aliases, parent_name=str(parent).strip() if parent else None,
                    town=str(town).strip() if town else None)


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------
def load_alias_groups(path: Optional[Path]) -> list[list[str]]:
    """Groups of equivalent spellings from aliases.yaml ({groups: [[a, b, ...], ...]} or a bare list)."""
    if path is None:
        return []
    p = Path(path)
    if not p.is_file():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("gazetteer: %s unreadable (%s); no curated aliases", p.name, type(exc).__name__)
        return []
    raw = data.get("groups") if isinstance(data, dict) else data
    groups: list[list[str]] = []
    for g in raw or []:
        if isinstance(g, (list, tuple)):
            names = [str(x).strip() for x in g if isinstance(x, (str, int)) and str(x).strip()]
            if len(names) >= 2:
                groups.append(names)
    return groups


_ST_RE = re.compile(r"\b(?:St\.?|Saint)(?=\s+\S)")
_MT_RE = re.compile(r"\b(?:Mt\.?|Mount)(?=\s+\S)")
_PT_RE = re.compile(r"\b(?:Pt\.?|Port)(?=\s+[A-Z])")
_COMPASS_RE = re.compile(r"^(North|South|East|West)\s+(?=[A-Z])")
_COMPASS_ABBR = {"North": "N.", "South": "S.", "East": "E.", "West": "W."}


def _abbrev_variants(form: str) -> list[str]:
    out: list[str] = []
    for rx, variants in ((_ST_RE, ("St.", "St", "Saint")), (_MT_RE, ("Mt.", "Mt", "Mount")),
                         (_PT_RE, ("Pt.", "Pt", "Port"))):
        if rx.search(form):
            out.extend(rx.sub(v, form) for v in variants)
    m = _COMPASS_RE.match(form)
    if m:
        out.append(_COMPASS_ABBR[m.group(1)] + " " + form[m.end():])
    return out


_GENERATED_LIKE = re.compile(r"\b(?:station|lirr|railroad)$|^(?:town|city|village) of\b|\bvillage$", re.I)


def generated_aliases(rec: PlaceRec) -> list[str]:
    """Spelling variants generated from a record's name and curated aliases (docs/06 §2.2). Aliases that
    already look generated are not expanded again, so applying this twice adds nothing new."""
    bases = [rec.name] + [a for a in rec.aliases if not _GENERATED_LIKE.search(a.strip())]
    expanded = list(bases)
    for b in bases:
        expanded.extend(_abbrev_variants(b))
    out = list(expanded[len(bases):])
    if rec.kind == "town":
        out.append(f"Town of {rec.name}")
    elif rec.kind == "city":
        out.append(f"City of {rec.name}")
    elif rec.kind == "village" and not rec.name.casefold().startswith("village of"):
        out.extend((f"Village of {rec.name}", f"{rec.name} Village"))
    elif rec.kind == STATION_KIND:
        for b in dict.fromkeys(expanded):
            out.extend((f"{b} station", f"{b} LIRR", f"{b} train station", f"{b} LIRR station",
                        f"{b} railroad station"))
    return out


def apply_aliases(places: Sequence[PlaceRec], groups: Sequence[Sequence[str]] = ()) -> None:
    """Attach curated alias groups (both ways, by canonical name) and generated variants to each record,
    deduplicated by normkey and never repeating the record's own name. Idempotent."""
    by_key: dict[str, list[list[str]]] = {}
    for g in groups:
        for member in g:
            by_key.setdefault(normkey(member), []).append(list(g))
    def add(merged: list[str], seen: set[str], alias: str) -> None:
        k = normkey(alias)
        if k and k not in seen:
            seen.add(k)
            merged.append(" ".join(alias.split()))

    for rec in places:
        own = normkey(rec.name)
        seen = {own}
        merged: list[str] = []
        for a in rec.aliases:
            add(merged, seen, a)
        for g in by_key.get(own, []):
            for member in g:
                add(merged, seen, member)
        rec.aliases = merged
        for a in generated_aliases(rec):
            add(merged, seen, a)
        rec.aliases = merged


# ---------------------------------------------------------------------------
# Regex trie
# ---------------------------------------------------------------------------
_SEP_UNIT = "\x01"


def _unit_pat(unit: str) -> str:
    if unit == _SEP_UNIT:
        return _SEP_RE
    if unit == ".":
        return r"\.?"
    if unit == "'":
        return "'?"
    return re.escape(unit)


def _trie_regex(node: dict[str, Any]) -> str:
    """Regex for a character-level trie node. Greedy optional tails give longest-match-first; branches
    differ in their first character, so the engine dispatches quickly even with thousands of names."""
    parts = [_unit_pat(k) + _trie_regex(node[k]) for k in sorted(k for k in node if k != _END)]
    if not parts:
        return ""
    body = parts[0] if len(parts) == 1 else "(?:" + "|".join(parts) + ")"
    if _END in node:
        body = f"(?:{body})?"
    return body


def _compile_forms(forms: Iterable[list[str]]) -> Optional[re.Pattern[str]]:
    """One compiled alternation of every form (token lists), word-bounded on both sides."""
    root: dict[str, Any] = {}
    n = 0
    for toks in forms:
        node = root
        for i, tok in enumerate(toks):
            if i:
                node = node.setdefault(_SEP_UNIT, {})
            if len(tok) > 3 and tok.endswith("s") and "'" not in tok and tok[-2].isalpha():
                tok = tok[:-1] + "'s"         # "Fishers" also matches "Fisher's" (normkey drops the apostrophe)
            for ch in tok:
                node = node.setdefault(ch, {})
        node[_END] = {}
        n += 1
    if not n:
        return None
    return re.compile(r"(?<![\w'])" + _trie_regex(root) + r"(?![\w])", re.IGNORECASE)


def _caps_mode(form: str, key: str) -> Optional[str]:
    letters = re.sub(r"[^A-Za-z]", "", form)
    if letters and letters.isupper() and len(key.replace(" ", "")) <= 4:
        return "exact"
    toks = key.split()
    if toks and all(t in COMMON_WORDS for t in toks):
        return "title"
    return None


def _first_alpha(s: str) -> str:
    for ch in s:
        if ch.isalpha():
            return ch
    return ""


def _case_ok(entry: _Entry, matched: str, *, strict: bool) -> bool:
    if entry.caps is None:
        return True
    if entry.caps == "exact":
        return " ".join(matched.split()) == entry.form
    form_toks = [t for t in _TOK_SPLIT.split(entry.form) if t]
    if not strict and len(form_toks) > 1:
        return True        # lenient (verified location span): only single common words need capitals
    text_toks = [t for t in _TOK_SPLIT.split(matched.translate(_SAME_LEN)) if t]
    if len(text_toks) != len(form_toks):
        return False
    for ft, tt in zip(form_toks, text_toks, strict=True):
        f0, t0 = _first_alpha(ft), _first_alpha(tt)
        if f0.isupper() and not t0.isupper():
            return False
    return True


_PREV_WORD = re.compile(r"([\w'.&-]+)\W*$")


def _generic_use(rx: re.Pattern[str], residual: str) -> bool:
    """True when a type word ("station", "library", "park") is used generically ("the park", "train station")
    rather than as part of an unknown proper name ("Penn Station", "Hecksher Park")."""
    for m in rx.finditer(residual):
        prev = _PREV_WORD.search(residual[:m.start()])
        if prev is None:
            return True
        word = prev.group(1)
        low = normkey(word)
        if (not word[:1].isupper() or low in _FILLER_WORDS or low in GENERIC_TOKENS or low in COMMON_WORDS
                or _STATION_WORDS.fullmatch(word)):
            return True
    return False


def _distinctive(key: str) -> bool:
    return any(len(t) >= 3 and not t.isdigit() and t not in COMMON_WORDS and t not in GENERIC_TOKENS
               for t in key.split())


def _distinct_tokens(key: str) -> list[str]:
    return [t for t in key.split() if len(t) >= 3 and not t.isdigit()
            and t not in COMMON_WORDS and t not in GENERIC_TOKENS and t not in _FILLER_WORDS]


# ---------------------------------------------------------------------------
# Gazetteer
# ---------------------------------------------------------------------------
class Gazetteer:
    places: list[PlaceRec]

    def __init__(self, places: Iterable[PlaceRec], *, alias_groups: Sequence[Sequence[str]] = ()):
        recs = [p for p in places if isinstance(p, PlaceRec)]
        apply_aliases(recs, alias_groups)
        recs.sort(key=_rec_sort_key)
        self.places = recs
        self._build_index()
        self._pat_all: Optional[re.Pattern[str]] = None
        self._pat_strict: Optional[re.Pattern[str]] = None
        self._compiled = False
        self._ensure_compiled()          # ~1-2 s for a full build; done once here, not on the first scan
        self._reference: Optional[str] = None
        self._by_id: dict[int, PlaceRec] = {}
        self._reindex_ids()

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: Path, *, aliases_path: Optional[Path] = None) -> "Gazetteer":
        """Load li_places.json (built by app.pipeline.geo.build). Raises FileNotFoundError if absent.
        Curated alias groups come from aliases_path (default data/aliases.yaml when present).
        CPU-bound (indexes and compiles the matchers): from async code, call it via asyncio.to_thread."""
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        rows = data.get("places") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError(f"{p.name}: no 'places' list")
        places: list[PlaceRec] = []
        bad = 0
        for row in rows:
            rec = rec_from_dict(row)
            if rec is None:
                bad += 1
            else:
                places.append(rec)
        if bad:
            logger.warning("gazetteer: skipped %d invalid record(s) in %s", bad, p.name)
        groups = load_alias_groups(aliases_path if aliases_path is not None else REPO_DATA_DIR / ALIASES_FILE)
        gaz = cls(places, alias_groups=groups)
        logger.info("gazetteer: loaded %d places", len(gaz.places))
        return gaz

    def _build_index(self) -> None:
        self._index: dict[str, list[_Entry]] = {}
        self._strict_index: dict[str, list[_Entry]] = {}
        self._forms_all: list[list[str]] = []
        self._forms_strict: list[list[str]] = []
        lm_counts: Counter[str] = Counter()
        for rec in self.places:
            if rec.kind in LANDMARK_KINDS:
                for k in {normkey(f) for f in [rec.name, *rec.aliases]}:
                    lm_counts[k] += 1
        for rec in self.places:
            seen: set[str] = set()
            for i, form in enumerate([rec.name, *rec.aliases]):
                key = normkey(form)
                if not key or key in seen or len(key.replace(" ", "")) < 3:
                    continue
                seen.add(key)
                toks = _tokens(form)
                if not toks:
                    continue
                alias = i > 0
                if len(toks) == 1 and key in GENERIC_SINGLE and not (not alias and rec.kind in ADMIN_KINDS):
                    continue
                entry = _Entry(rec=rec, alias=alias, caps=_caps_mode(form, key), form=" ".join(form.split()))
                self._index.setdefault(key, []).append(entry)
                self._forms_all.append(toks)
                strict_ok = (rec.kind in ADMIN_KINDS or rec.kind == STATION_KIND
                             or (lm_counts[key] == 1 and _distinctive(key)))
                if strict_ok:
                    self._strict_index.setdefault(key, []).append(entry)
                    self._forms_strict.append(toks)
        for idx in (self._index, self._strict_index):
            for entries in idx.values():
                entries.sort(key=_entry_sort_key)

    def _ensure_compiled(self) -> None:
        if not self._compiled:
            self._pat_all = _compile_forms(self._forms_all)
            self._pat_strict = _compile_forms(self._forms_strict)
            self._compiled = True

    def _reindex_ids(self) -> None:
        self._by_id = {p.id: p for p in self.places if p.id is not None}

    # ------------------------------------------------------------------ scanning
    def _scan(self, text: str, *, strict: bool) -> list[_Hit]:
        if not text:
            return []
        self._ensure_compiled()
        pat = self._pat_strict if strict else self._pat_all
        if pat is None:
            return []
        index = self._strict_index if strict else self._index
        scan_text = text.translate(_SAME_LEN)
        hits: list[_Hit] = []
        pos = 0
        n = len(scan_text)
        while pos < n:
            m = pat.search(scan_text, pos)
            if m is None:
                break
            matched = text[m.start():m.end()]
            entries = [e for e in index.get(normkey(matched), ()) if _case_ok(e, matched, strict=strict)]
            if entries:
                hits.append(_Hit(m.start(), m.end(), matched, normkey(matched), entries))
                pos = m.end()
            else:
                pos = m.start() + 1
        return hits

    @staticmethod
    def _residual(text: str, hits: Sequence[_Hit]) -> str:
        chars = list(text)
        for h in hits:
            for i in range(h.start, h.end):
                chars[i] = " "
        return "".join(chars)

    @staticmethod
    def _preferred(entries: Sequence[_Entry]) -> PlaceRec:
        return entries[0].rec            # entries are sorted: canonical before alias, then kind rank

    def find_in_text(self, text: str) -> list[PlaceMention]:
        """Word-boundary, case-insensitive, longest-match-first mentions of names/aliases (non-overlapping).
        Very short or ambiguous common-word names must not match on their own (e.g. a hamlet named 'Centre'
        or 'Flanders' is fine; guard generic words like 'Park', 'Beach', 'Station')."""
        return [PlaceMention(place=self._preferred(h.entries), start=h.start, end=h.end, matched=h.matched)
                for h in self._scan(text or "", strict=True)]

    def unmatched_words(self, text: str) -> list[str]:
        """Capitalized words of a location span that name something the gazetteer does not know
        ("the Wawa in Medford" -> ["Wawa"]); generic, street, region and filler words are ignored."""
        residual = self._residual(text or "", self._scan(text or "", strict=False))
        out: list[str] = []
        for m in _WORD_RE.finditer(residual):
            word = m.group(0)
            key = normkey(word)
            if (not word[:1].isupper() or len(key) < 2 or key in _FILLER_WORDS or key in GENERIC_TOKENS
                    or key in COMMON_WORDS or key in _STREET_REGION_WORDS or _STATION_WORDS.fullmatch(word)):
                continue
            out.append(word)
        return out

    def mentioned_localities(self, text: str, *, strict: bool = True) -> list[PlaceRec]:
        """Villages/CDPs/cities and towns named in the text, in order (a public place counts as its
        containing locality). Deduplicated."""
        out: list[PlaceRec] = []
        seen: set[int] = set()
        for h in self._scan(text or "", strict=strict):
            for rec in self._hit_admin(h):
                if id(rec) not in seen:
                    seen.add(id(rec))
                    out.append(rec)
        return out

    def _hit_admin(self, h: _Hit, *, via_public: bool = True) -> list[PlaceRec]:
        locs = [e.rec for e in h.entries if e.rec.kind in LOCALITY_KINDS]
        towns = [e.rec for e in h.entries if e.rec.kind == "town"]
        out: list[PlaceRec] = []
        if locs:
            out.append(locs[0])
        out.extend(towns[:1])
        if not out and via_public:
            pub = [e.rec for e in h.entries if e.rec.is_public_place]
            if len({id(p) for p in pub}) == 1:
                loc = self.containing_locality(pub[0])
                if loc is not None:
                    out.append(loc)
        return out

    # ------------------------------------------------------------------ matching
    def _anchors(self, hits: Sequence[_Hit]) -> tuple[list[PlaceRec], list[PlaceRec]]:
        """Hamlets/villages and towns NAMED in the hits (a public place's own parent is not an anchor, so a
        landmark can never confirm itself)."""
        locs: list[PlaceRec] = []
        towns: list[PlaceRec] = []
        for h in hits:
            for rec in self._hit_admin(h, via_public=False):
                if rec.kind in LOCALITY_KINDS:
                    if rec not in locs:
                        locs.append(rec)
                    if rec.kind == "city":
                        if rec not in towns:
                            towns.append(rec)
                elif rec.kind == "town" and rec not in towns:
                    towns.append(rec)
        return locs, towns

    @staticmethod
    def _tiers(cands: Sequence[PlaceRec], locs: Sequence[PlaceRec], towns: Sequence[PlaceRec], *,
               loose: bool = True) -> list[PlaceRec]:
        """Candidates whose parent/town agree with the anchors, best tier first (empty if none agree)."""
        loc_keys = {normkey(a.name) for a in locs}
        town_keys = {normkey(t.name) for t in towns}
        loc_town_keys = {normkey(a.town) for a in locs if a.town}

        def tier1(c: PlaceRec) -> bool:
            return (bool(c.parent_name) and normkey(c.parent_name or "") in loc_keys) or (
                c.kind == STATION_KIND and normkey(c.name) in loc_keys)

        def tier2(c: PlaceRec) -> bool:
            return bool(c.town) and normkey(c.town or "") in town_keys

        def tier3(c: PlaceRec) -> bool:      # same town, outside every village/CDP (e.g. a state park)
            return not c.parent_name and bool(c.town) and normkey(c.town or "") in loc_town_keys

        tiers = (tier1, tier2, tier3) if loose else (tier1, tier2)
        for test in tiers:
            got = [c for c in cands if test(c)]
            if got:
                return got
        return []

    def _pick_public(self, cands: list[PlaceRec], span: tuple[list[PlaceRec], list[PlaceRec]],
                     everywhere: tuple[list[PlaceRec], list[PlaceRec]]) -> Optional[PlaceRec]:
        """Choose among public places matched by name. `span` holds the hamlets/towns named in the location
        span itself, `everywhere` adds those from the post/context. A unique distinctive name is accepted
        unless the span contradicts it; a generic name ("Town Park") needs a hamlet/town that agrees;
        several same-name places are told apart by their parent (else ambiguous -> None)."""
        uniq: list[PlaceRec] = []
        for c in cands:
            if c not in uniq:
                uniq.append(c)
        if not uniq:
            return None
        if len(uniq) == 1:
            c = uniq[0]
            if c.kind == STATION_KIND or _distinctive(normkey(c.name)):
                if (span[0] or span[1]) and not self._tiers([c], *span):
                    return None
                return c
            return c if self._tiers([c], *everywhere) else None
        for locs, towns in (span, everywhere):
            if locs or towns:
                agreed = self._tiers(uniq, locs, towns)
                if len(agreed) == 1:
                    return agreed[0]
                if len(agreed) > 1:
                    return None
        return None

    def _by_kind_near(self, kinds: Sequence[str], locs: list[PlaceRec], towns: list[PlaceRec]) -> Optional[PlaceRec]:
        cands = [p for p in self.places if p.kind in kinds]
        agreed = self._tiers(cands, locs, towns, loose=False)
        if len(agreed) == 1:
            return agreed[0]
        if len(agreed) > 1:
            names = {normkey(a.name) for a in locs} | {normkey(t.name) for t in towns}
            named = [c for c in agreed if any(n and n in normkey(c.name) for n in names)]
            if len(named) == 1:
                return named[0]
            if kinds == (STATION_KIND,):
                exact = [c for c in agreed if normkey(c.name) in names]
                if len(exact) == 1:
                    return exact[0]
        return None

    def _fuzzy(self, text: str, locs: list[PlaceRec], towns: list[PlaceRec]) -> Optional[PlaceRec]:
        words = [w for w in normkey(text).split() if w not in _FILLER_WORDS]
        q = " ".join(words)
        q_distinct = _distinct_tokens(q)
        if not q or not any(len(t) >= 4 for t in q_distinct):
            return None
        if locs or towns:
            loc_keys = {normkey(a.name) for a in locs}
            town_keys = {normkey(t.name) for t in towns} | {normkey(a.town) for a in locs if a.town}
            pool = [p for p in self.places
                    if (p.is_public_place and ((p.parent_name and normkey(p.parent_name) in loc_keys)
                                               or (p.town and normkey(p.town) in town_keys)))
                    or (p.kind in LOCALITY_KINDS and p.town and normkey(p.town) in town_keys)]
        else:
            pool = [p for p in self.places
                    if p.kind in LOCALITY_KINDS or p.kind == STATION_KIND
                    or (p.kind in LANDMARK_KINDS and _distinctive(normkey(p.name)))]
        choices: dict[str, list[PlaceRec]] = {}
        for p in pool:
            for form in [p.name, *p.aliases]:
                k = normkey(form)
                if k:
                    choices.setdefault(k, [])
                    if p not in choices[k]:
                        choices[k].append(p)
        if not choices:
            return None
        res = process.extract(q, list(choices.keys()), scorer=fuzz.token_set_ratio, score_cutoff=FUZZY_MIN,
                              limit=None)
        best: dict[int, tuple[float, PlaceRec]] = {}
        for key, score, _ in res:
            c_distinct = _distinct_tokens(key)
            if not c_distinct:
                continue
            if not any(fuzz.ratio(a, b) >= _FUZZY_TOKEN_MIN for a in q_distinct for b in c_distinct):
                continue
            for p in choices[key]:
                prev = best.get(id(p))
                if prev is None or score > prev[0]:
                    best[id(p)] = (score, p)
        if not best:
            return None
        ranked = sorted(best.values(), key=lambda sp: (-sp[0], kind_rank(sp[1].kind)))
        top = ranked[0][0]
        tops = [p for s, p in ranked if s == top]
        if len(tops) == 1:
            return tops[0]
        locs_only = [p for p in tops if p.kind in LOCALITY_KINDS]
        if len(locs_only) == 1 and all(p.kind in LOCALITY_KINDS or p.kind == "town" for p in tops):
            return locs_only[0]
        return None

    def match_location(self, location_text: str, *, context_text: str = "", query_place_id: Optional[int] = None) -> Optional[PlaceRec]:
        """docs/06 §3 steps 1-3: exact/alias, then prefer landmark whose parent matches a hamlet in the
        text/context, then fuzzy (rapidfuzz token_set_ratio >= 92) restricted to the mentioned hamlet/town."""
        loc_text = " ".join((location_text or "").split())
        if not loc_text:
            return None
        hits = self._scan(loc_text, strict=False)
        residual = self._residual(loc_text, hits)
        station_flag = _generic_use(_STATION_WORDS, residual)
        kinds_named = [k for k, rx in _KIND_WORDS if _generic_use(rx, residual)]

        locs, towns = self._anchors(hits)
        ctx_locs, ctx_towns = self._anchors(self._scan(context_text or "", strict=True)) if context_text else ([], [])
        if query_place_id is not None:
            qp = self.by_id(query_place_id)
            if qp is not None:
                q_loc = qp if not qp.is_public_place else self.containing_locality(qp)
                if q_loc is not None:
                    (ctx_towns if q_loc.kind == "town" else ctx_locs).append(q_loc)
        all_locs = locs + [c for c in ctx_locs if c not in locs]
        all_towns = towns + [c for c in ctx_towns if c not in towns]

        # 1. A public place named in the location span (station, park, library, ...).
        span, everywhere = (locs, towns), (all_locs, all_towns)
        for h in hits:
            loc_entries = [e for e in h.entries if e.rec.kind in ADMIN_KINDS]
            stations = [e.rec for e in h.entries if e.rec.kind == STATION_KIND]
            landmarks = [e.rec for e in h.entries if e.rec.kind in LANDMARK_KINDS]
            if stations and (station_flag or not loc_entries):
                pick = self._pick_public(stations, span, everywhere)
                if pick is not None:
                    return pick
            if landmarks and not loc_entries:
                pick = self._pick_public(landmarks, span, everywhere)
                if pick is not None:
                    return pick

        # 2. A generic kind word plus a hamlet: "the library in Patchogue", "the train station in Bay Shore".
        for anchor_locs, anchor_towns in ((locs, towns), (all_locs, all_towns)):
            if not (anchor_locs or anchor_towns):
                continue
            if station_flag:
                pick = self._by_kind_near((STATION_KIND,), anchor_locs, anchor_towns)
                if pick is not None:
                    return pick
            for kind in kinds_named:
                pick = self._by_kind_near((kind,), anchor_locs, anchor_towns)
                if pick is not None:
                    return pick

        # 3. Fuzzy match on what is left (misspellings), restricted to the mentioned hamlet/town if any.
        if not hits or residual.strip():
            pick = self._fuzzy(loc_text if not hits else residual, all_locs, all_towns)
            if pick is not None:
                return pick

        # 4. The hamlet/village (or town) named in the span.
        if locs:
            return locs[0]
        if towns:
            return towns[0]
        return None

    # ------------------------------------------------------------------ lookups
    def by_id(self, place_id: int) -> Optional[PlaceRec]:
        try:
            pid = int(place_id)
        except (TypeError, ValueError):
            return None
        rec = self._by_id.get(pid)
        if rec is None:
            for p in self.places:
                if p.id == pid:
                    self._by_id[pid] = p
                    return p
        return rec

    def by_name(self, name: str, kinds: Optional[tuple[str, ...]] = None) -> list[PlaceRec]:
        out: list[PlaceRec] = []
        for e in self._index.get(normkey(name or ""), ()):
            if (kinds is None or e.rec.kind in kinds) and e.rec not in out:
                out.append(e.rec)
        return out

    def nearest_public_place(self, lat: float, lon: float, max_m: float) -> Optional[PlaceRec]:
        return self._nearest(lat, lon, max_m, lambda p: p.is_public_place)

    def nearest_locality(self, lat: float, lon: float, max_m: float) -> Optional[PlaceRec]:
        return self._nearest(lat, lon, max_m, lambda p: p.kind in LOCALITY_KINDS)

    def _nearest(self, lat: float, lon: float, max_m: float, pred: Any) -> Optional[PlaceRec]:
        la, lo, mx = _finite(lat), _finite(lon), _finite(max_m)
        if la is None or lo is None or mx is None or mx < 0:
            return None
        dlat = mx / 111_000.0 + 1e-9
        dlon = mx / (111_000.0 * max(0.1, math.cos(math.radians(la)))) + 1e-9
        best: Optional[tuple[float, PlaceRec]] = None
        for p in self.places:
            if abs(p.lat - la) > dlat or abs(p.lon - lo) > dlon or not pred(p):
                continue
            d = distance_m(la, lo, p.lat, p.lon)
            if d <= mx and (best is None or d < best[0]):
                best = (d, p)
        return best[1] if best else None

    def containing_locality(self, place: PlaceRec) -> Optional[PlaceRec]:
        """The village/CDP (or town) record a landmark belongs to, for hamlet fallback."""
        if place.kind in ADMIN_KINDS:
            return place
        if place.parent_name:
            cands = [p for p in self.by_name(place.parent_name, LOCALITY_KINDS) if p.county == place.county]
            if place.town:
                same_town = [p for p in cands if p.town and normkey(p.town) == normkey(place.town)]
                cands = same_town or cands
            if cands:
                return cands[0]
        if place.town:
            towns = [p for p in self.by_name(place.town, TOWN_KINDS) if p.county == place.county]
            if towns:
                return towns[0]
        return self.nearest_locality(place.lat, place.lon, 5000.0)

    def rotation_places(self) -> list[PlaceRec]:
        """Places for the planner rotation: towns, cities, villages, CDPs and LIRR stations, stable order."""
        recs = [p for p in self.places if p.kind in ROTATION_KINDS]
        return sorted(recs, key=lambda p: (p.name.casefold(), kind_rank(p.kind), p.county, p.source, p.source_id))

    def reference_list_text(self) -> str:
        """The 'Reference: places in scope' block appended to the classifier system prompt
        (prompts/classifier-system.md). Deterministic output (sorted) so prompt caching works."""
        if self._reference is not None:
            return self._reference

        def names(pred: Any) -> list[str]:
            return sorted({p.name for p in self.places if pred(p)}, key=lambda s: (s.casefold(), s))

        lines = ["## Reference: places in scope"]
        for county in COUNTIES:
            towns = names(lambda p, c=county: p.county == c and p.kind == "town")
            cities = names(lambda p, c=county: p.county == c and p.kind == "city")
            items = towns + [f"{c} (city)" for c in cities]
            if items:
                label = "towns and cities" if cities else "towns"
                lines.append(f"{county} County — {label}: {', '.join(items)}.")
        for county in COUNTIES:
            locs = names(lambda p, c=county: p.county == c and p.kind in ("village", "cdp"))
            if locs:
                lines.append(f"{county} villages and hamlets: {', '.join(locs)}.")
        stations = names(lambda p: p.kind == STATION_KIND)
        if stations:
            lines.append(f"LIRR stations in Nassau/Suffolk: {', '.join(stations)}.")
        lines.append(NOT_IN_SCOPE_LINE)
        self._reference = "\n".join(lines)
        return self._reference

    def search(self, q: str, limit: int = 10) -> list[PlaceRec]:
        """Prefix/fuzzy search for the map search box."""
        key = normkey(q or "")[:100]
        if not key:
            return []
        try:
            limit = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            limit = 10
        scores: dict[int, tuple[float, PlaceRec]] = {}

        def bump(rec: PlaceRec, s: float) -> None:
            prev = scores.get(id(rec))
            if prev is None or s > prev[0]:
                scores[id(rec)] = (s, rec)

        padded = f" {key}"
        for k, entries in self._index.items():
            if k == key:
                s = 100.0
            elif k.startswith(key):
                s = 90.0 - min(10.0, (len(k) - len(key)) / 3.0)
            elif padded in f" {k}":
                s = 75.0
            else:
                continue
            for e in entries:
                bump(e.rec, s - (2.0 if e.alias else 0.0))
        if len(scores) < limit and len(key) >= 3:
            keys = list(self._index.keys())
            for k, sc, _ in process.extract(key, keys, scorer=fuzz.WRatio, score_cutoff=85, limit=limit * 3):
                for e in self._index[k]:
                    bump(e.rec, float(sc) * 0.7)
        ranked = sorted(scores.values(), key=lambda sp: (-sp[0], kind_rank(sp[1].kind), sp[1].name.casefold(),
                                                        sp[1].county, sp[1].source_id))
        return [rec for _, rec in ranked[:limit]]


# ---------------------------------------------------------------------------
# Database sync
# ---------------------------------------------------------------------------
def sync_places(db: Session, gaz: Gazetteer) -> int:
    """Upsert every PlaceRec into `places` by (source, source_id), set parent_id/town, and write the DB id
    back onto each PlaceRec. Returns the number of rows. Caller commits."""
    from app.models import Place

    existing = {(row.source, row.source_id): row for row in db.execute(select(Place)).scalars()}
    pairs: list[tuple[PlaceRec, Any]] = []
    for rec in gaz.places:
        row = existing.get((rec.source, rec.source_id))
        values = dict(name=rec.name, aliases=list(rec.aliases), kind=rec.kind, county=rec.county,
                      lat=float(rec.lat), lon=float(rec.lon), town=rec.town)
        if row is None:
            row = Place(source=rec.source, source_id=rec.source_id, **values)
            db.add(row)
            existing[(rec.source, rec.source_id)] = row
        else:
            for k, v in values.items():
                if getattr(row, k) != v:
                    setattr(row, k, v)
        pairs.append((rec, row))
    db.flush()
    for rec, row in pairs:
        rec.id = row.id
    gaz._reindex_ids()
    for rec, row in pairs:
        parent_id = None
        if rec.is_public_place and rec.parent_name:
            cands = [p for p in gaz.by_name(rec.parent_name, LOCALITY_KINDS) if p.county == rec.county]
            if rec.town:
                cands = [p for p in cands if p.town and normkey(p.town) == normkey(rec.town)] or cands
            if cands and cands[0].id is not None and cands[0].id != row.id:
                parent_id = cands[0].id
        if row.parent_id != parent_id:
            row.parent_id = parent_id
    db.flush()
    return len(pairs)
