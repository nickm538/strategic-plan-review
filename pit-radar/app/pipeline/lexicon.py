"""Lexicon loader and matchers (config/lexicon.yaml; docs/04 §5, docs/05 §5 V2/V6).

Public API is the contract used by the prefilter, verifier (V2/V6), privacy (notes check) and planner.

Text contract: every matcher expects text that has ALREADY been passed through `textnorm.clean()`
(NFKC, straight quotes, unified dashes, collapsed whitespace). Matching itself does not re-normalize, so
the `start`/`end` offsets of each TermMatch index into exactly the string the caller passed in. Callers
that slice or rewrite text (privacy_mask) must keep using that cleaned string.

Matching rules (header of config/lexicon.yaml):
  * plain entries: case-insensitive, whole words only, any run of whitespace between words. Compiled as
    (?<!\\w) word (\\s+ word)* (?!\\w), so entries such as "7-Eleven" or "point-in-time count" work.
  * "re:" entries: Python regular expressions compiled with IGNORECASE; scoped inline flags such as
    (?-i:...) switch case sensitivity back on for part of a pattern (e.g. "ICE", "LIC", "WV").
  * find() returns non-overlapping matches, longest first (ties: earliest start, then list order).

Overrides (settings key `lexicon.overrides`, docs/07 PUT /api/admin/lexicon) replace whole lists and are
addressed by dotted names, e.g. {"soft_exclusions": [...], "privacy_denylist.drop.age": [...]}. A nested
mapping value is the same as giving each of its lists by dotted name. New lists may be added only under
privacy_denylist.mask (key must be a bracket token such as "[person]"), privacy_denylist.drop and
privacy_patterns. Use `Lexicon.validate_overrides()` before saving; it compiles every entry.
"""
from __future__ import annotations

import copy
import functools
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

import yaml

from app.paths import CONFIG_DIR
from app.pipeline import textnorm

logger = logging.getLogger(__name__)

SIGNAL_LISTS = ("encampment_terms", "individual_sighting_terms", "vehicle_dwelling_terms",
                "self_report_terms", "spanish_terms")

DEFAULT_PATH = CONFIG_DIR / "lexicon.yaml"

# Top-level sections that are data, not match lists.
_DATA_SECTIONS = frozenset({"version", "query_templates"})
_UNOVERRIDABLE = frozenset({"version"})
# Mappings under which an override may add a new list (a new mask token, drop category or pattern).
_OPEN_MAPPINGS = frozenset({"privacy_denylist.mask", "privacy_denylist.drop", "privacy_patterns"})
_MASK_TOKEN = re.compile(r"^\[[^\[\].]+\]$")
_MAX_ENTRY_LEN = 1000
_MAX_LIST_LEN = 5000

# Names the matcher methods use. If the file lacks one, it behaves as an empty list (never a KeyError).
_KNOWN_NAMES = frozenset(SIGNAL_LISTS) | {
    "region_anchor_terms", "place_type_terms", "recency_terms", "soft_exclusions",
    "hard_exclusions", "hard_exclusions.animal", "hard_exclusions.nyc_only_locations",
    "hard_exclusions.out_of_area_hints", "hard_exclusions.border_ambiguous_note",
    "privacy_denylist", "privacy_denylist.mask", "privacy_denylist.drop", "privacy_patterns",
}

# "Long Island" inside "Long Island City" (Queens) is not a Long Island anchor (lexicon.yaml §4).
_LIC = re.compile(r"(?<!\w)Long\s+Island\s+City(?!\w)", re.IGNORECASE)


@dataclass(frozen=True)
class TermMatch:
    list_name: str        # e.g. "encampment_terms", "privacy_denylist.drop.age", "privacy_patterns.street_address"
    entry: str            # the lexicon entry (plain text or "re:..." pattern) that matched
    start: int            # character offsets into the text passed in
    end: int
    matched: str          # the matched substring


# ---------------------------------------------------------------------------
# Compilation (cached process-wide: the same entry is compiled once)
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=8192)
def compile_entry(entry: str) -> re.Pattern[str]:
    """Compile one lexicon entry per the file's matching rules. Raises ValueError if empty or invalid."""
    if not isinstance(entry, str):
        raise ValueError("entries must be strings")
    raw = entry.strip()
    if raw.startswith("re:"):
        body = raw[3:]
        if not body.strip():
            raise ValueError("empty regular expression")
        try:
            pat = re.compile(body, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid regular expression ({exc})") from None
        if pat.search("") is not None:
            raise ValueError("regular expression can match empty text")
        return pat
    words = textnorm.clean(raw).split()
    if not words:
        raise ValueError("empty entry")
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)", re.IGNORECASE)


def plain_pattern(term: str) -> re.Pattern[str]:
    """Pattern for a literal term (e.g. a place name) using the plain-entry rules; never read as a regex,
    even if the term happens to start with 're:'. Raises ValueError for an empty term."""
    words = textnorm.clean(term or "").split()
    if not words:
        raise ValueError("empty term")
    return _plain_cached(" ".join(words))


@functools.lru_cache(maxsize=4096)
def _plain_cached(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in term.split()) + r"(?!\w)", re.IGNORECASE)


# Fast skip for plain entries. A plain entry's first word is always a whole \w+ token of any text it
# matches (the pattern starts with (?<!\w) and that word is followed by a non-word char or the end). So if
# its ASCII first word, lowercased, is not among the text's lowercased \w+ tokens, the entry cannot match.
# Under re.IGNORECASE exactly four non-ASCII characters match ASCII letters (U+0130 I with dot, U+0131
# dotless i, U+017F long s, U+212A Kelvin sign; checked by tests/test_text_lexicon.py); when the text
# contains any of them no entry is skipped, so the shortcut never changes results.
_WORD_TOKEN = re.compile(r"\w+")
_ASCII_CASE_TRAPS = ("\u0130", "\u0131", "\u017f", "\u212a")


def _token_key(entry: str) -> Optional[str]:
    raw = entry.strip()
    if raw.startswith("re:"):
        return None
    words = textnorm.clean(raw).split()
    if not words:
        return None
    m = _WORD_TOKEN.match(words[0])
    if not m or not m.group(0).isascii():
        return None
    return m.group(0).lower()


def _text_tokens(text: str) -> Optional[set[str]]:
    if any(ch in text for ch in _ASCII_CASE_TRAPS):
        return None
    return {t.lower() for t in _WORD_TOKEN.findall(text)}


# ---------------------------------------------------------------------------
# YAML tree helpers
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=8)
def _read_yaml_cached(path_str: str, mtime_ns: int, size: int) -> dict[str, Any]:
    with open(path_str, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path_str}: lexicon file must be a mapping")
    return data


def _read_base(path: Path) -> dict[str, Any]:
    p = Path(path)
    st = p.stat()
    return copy.deepcopy(_read_yaml_cached(str(p.resolve()), st.st_mtime_ns, st.st_size))


def _iter_leaves(node: Any, prefix: str) -> Iterator[tuple[str, list[Any]]]:
    """Yield (dotted_name, entries) for every list (or single-string pattern) under node."""
    if isinstance(node, dict):
        for key, child in node.items():
            yield from _iter_leaves(child, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(node, list):
        yield prefix, node
    elif isinstance(node, str):
        yield prefix, [node]
    elif node is None:
        yield prefix, []
    else:
        raise ValueError(f"{prefix}: unsupported value in lexicon ({type(node).__name__})")


def _lookup(tree: dict[str, Any], parts: list[str]) -> tuple[bool, Any]:
    node: Any = tree
    for p in parts:
        if not isinstance(node, dict) or p not in node:
            return False, None
        node = node[p]
    return True, node


def _set_path(tree: dict[str, Any], parts: list[str], value: Any) -> None:
    node = tree
    for p in parts[:-1]:
        nxt = node.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            node[p] = nxt
        node = nxt
    node[parts[-1]] = value


def _flatten_overrides(overrides: Any) -> tuple[list[tuple[str, Any]], list[str]]:
    """{"a": {"b": [...]}} -> [("a.b", [...])]. Returns (pairs, errors)."""
    if overrides is None:
        return [], []
    if not isinstance(overrides, dict):
        return [], ["overrides must be a mapping of list name to entries"]
    pairs: list[tuple[str, Any]] = []
    errors: list[str] = []

    def walk(key: str, value: Any) -> None:
        if isinstance(value, dict):
            if not value:
                errors.append(f"{key}: empty mapping")
            for k, v in value.items():
                if not isinstance(k, str) or not k.strip() or "." in k:
                    errors.append(f"{key}: invalid list name {k!r}")
                    continue
                walk(f"{key}.{k}", v)
        else:
            pairs.append((key, value))

    for key, value in overrides.items():
        if not isinstance(key, str) or not key.strip():
            errors.append("override names must be non-empty strings")
            continue
        walk(key.strip(), value)
    pairs.sort(key=lambda kv: kv[0].count("."))
    return pairs, errors


def _validate_entry(where: str, entry: Any, *, compile_it: bool) -> list[str]:
    if not isinstance(entry, str) or not entry.strip():
        return [f"{where}: entries must be non-empty strings"]
    if len(entry) > _MAX_ENTRY_LEN:
        return [f"{where}: entry longer than {_MAX_ENTRY_LEN} characters"]
    if compile_it:
        try:
            compile_entry(entry)
        except ValueError as exc:
            return [f"{where}: {exc}"]
    return []


def _validate_leaf_override(base: dict[str, Any], key: str, value: Any) -> list[str]:
    parts = key.split(".")
    if any(not p for p in parts):
        return [f"{key}: invalid list name"]
    if parts[0] in _UNOVERRIDABLE:
        return [f"{key}: cannot be overridden"]
    if value is None:            # None = no override (revert to the file default)
        return []
    exists, node = _lookup(base, parts)
    parent = ".".join(parts[:-1])
    if not exists:
        if parent not in _OPEN_MAPPINGS:
            return [f"{key}: unknown lexicon list"]
        if parent == "privacy_denylist.mask" and not _MASK_TOKEN.match(parts[-1]):
            return [f"{key}: mask keys must be bracket tokens such as [person]"]
        node = "" if parent == "privacy_patterns" else []
    compile_it = parts[0] not in _DATA_SECTIONS
    if isinstance(node, list):
        if not isinstance(value, list):
            return [f"{key}: expected a list of strings"]
        if len(value) > _MAX_LIST_LEN:
            return [f"{key}: more than {_MAX_LIST_LEN} entries"]
        errors: list[str] = []
        for i, entry in enumerate(value):
            errors.extend(_validate_entry(f"{key}[{i}]", entry, compile_it=compile_it))
        return errors
    if isinstance(node, str):
        if not isinstance(value, str):
            return [f"{key}: expected a single pattern string"]
        return _validate_entry(key, value, compile_it=compile_it)
    if isinstance(node, dict):
        return [f"{key}: expected a mapping of lists"]
    return [f"{key}: cannot be overridden"]


def _check_overrides(base: dict[str, Any], overrides: Any) -> tuple[list[tuple[str, Any]], list[str], set[str]]:
    """Returns (pairs, errors, bad_keys). bad_keys are the dotted names whose override is invalid."""
    pairs, errors = _flatten_overrides(overrides)
    bad: set[str] = set()
    for key, value in pairs:
        errs = _validate_leaf_override(base, key, value)
        if errs:
            errors.extend(errs)
            bad.add(key)
    return pairs, errors, bad


# ---------------------------------------------------------------------------
# Lexicon
# ---------------------------------------------------------------------------
class Lexicon:
    """Compiled lexicon. Matching rules are in the header of config/lexicon.yaml (and this module)."""

    version: int
    query_templates: dict[str, Any]

    def __init__(self, data: dict[str, Any], *, overrides: Optional[dict[str, Any]] = None,
                 override_errors: Optional[list[str]] = None) -> None:
        self._data = data
        self.version = int(data.get("version") or 1)
        self.query_templates = copy.deepcopy(data.get("query_templates") or {})
        self.overrides: dict[str, Any] = dict(overrides or {})
        self.override_errors: list[str] = list(override_errors or [])
        # leaf name -> [(entry, pattern, token_key)]; token_key: see _token_key()
        self._lists: dict[str, list[tuple[str, re.Pattern[str], Optional[str]]]] = {}
        errors: list[str] = []
        for top, node in data.items():
            if top in _DATA_SECTIONS:
                continue
            for name, entries in _iter_leaves(node, str(top)):
                compiled: list[tuple[str, re.Pattern[str], Optional[str]]] = []
                for i, entry in enumerate(entries):
                    try:
                        pat = compile_entry(entry)
                    except ValueError as exc:
                        errors.append(f"{name}[{i}]: {exc}")
                        continue
                    compiled.append((entry.strip(), pat, _token_key(entry)))
                self._lists[name] = compiled
        if errors:
            raise ValueError("lexicon has invalid entries: " + "; ".join(errors))
        self._groups: dict[str, tuple[str, ...]] = {}

    # -- loading ----------------------------------------------------------------
    @classmethod
    def load(cls, path: Path = CONFIG_DIR / "lexicon.yaml", overrides: Optional[dict[str, Any]] = None,
             *, strict: bool = False) -> "Lexicon":
        """Load + compile. `overrides` (settings key lexicon.overrides) replaces whole lists by dotted name.

        Invalid overrides are skipped (the file default stays in force), logged by name, and listed in
        `override_errors`; with strict=True they raise ValueError instead. A broken lexicon FILE always raises."""
        base = _read_base(path)
        data = copy.deepcopy(base)
        pairs, errors, bad = _check_overrides(base, overrides)
        if errors and strict:
            raise ValueError("invalid lexicon overrides: " + "; ".join(errors))
        applied: dict[str, Any] = {}
        for key, value in pairs:
            if key in bad or value is None:
                continue
            _set_path(data, key.split("."), copy.deepcopy(value))
            applied[key] = copy.deepcopy(value)
        if errors:
            logger.warning("lexicon: ignored %d invalid override(s): %s", len(errors),
                           ", ".join(sorted(bad)) or "malformed overrides")
        return cls(data, overrides=applied, override_errors=errors)

    @classmethod
    def validate_overrides(cls, overrides: Any, *, path: Path = DEFAULT_PATH) -> list[str]:
        """Human-readable errors for a proposed overrides mapping ([] = valid). Compiles every entry."""
        try:
            base = _read_base(path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            return [f"lexicon file unreadable: {type(exc).__name__}"]
        _pairs, errors, _bad = _check_overrides(base, overrides)
        return errors

    def to_dict(self) -> dict[str, Any]:
        """The effective lexicon (file + applied overrides) as plain data."""
        return copy.deepcopy(self._data)

    def list_names(self) -> list[str]:
        """Every dotted list name that find() accepts as a leaf."""
        return sorted(self._lists)

    def entries(self, list_name: str) -> list[str]:
        """Raw entries of one list or group (dotted name)."""
        return [entry for leaf in self._resolve(list_name) for entry, _pat, _key in self._lists[leaf]]

    # -- generic --------------------------------------------------------------
    def _resolve(self, list_name: str) -> tuple[str, ...]:
        cached = self._groups.get(list_name)
        if cached is not None:
            return cached
        if list_name in self._lists:
            leaves: tuple[str, ...] = (list_name,)
        else:
            prefix = list_name + "."
            leaves = tuple(n for n in self._lists if n.startswith(prefix))
            if not leaves and list_name not in _KNOWN_NAMES:
                raise KeyError(list_name)
        self._groups[list_name] = leaves
        return leaves

    def _find_leaves(self, text: str, leaves: tuple[str, ...]) -> list[TermMatch]:
        if not text or not leaves:
            return []
        tokens = _text_tokens(text)
        cands: list[tuple[int, int, int, str, str, str]] = []
        order = 0
        for leaf in leaves:
            for entry, pat, key in self._lists.get(leaf, ()):
                if key is not None and tokens is not None and key not in tokens:
                    order += 1
                    continue                      # first word absent: this entry cannot match
                for m in pat.finditer(text):
                    if m.end() > m.start():
                        cands.append((m.start(), m.end(), order, leaf, entry, m.group(0)))
                order += 1
        if not cands:
            return []
        cands.sort(key=lambda c: (c[0] - c[1], c[0], c[2]))   # longest first, then earliest, then list order
        taken: list[tuple[int, int]] = []
        out: list[TermMatch] = []
        for start, end, _order, leaf, entry, matched in cands:
            if any(start < t_end and t_start < end for t_start, t_end in taken):
                continue
            taken.append((start, end))
            out.append(TermMatch(list_name=leaf, entry=entry, start=start, end=end, matched=matched))
        out.sort(key=lambda m: (m.start, m.end))
        return out

    def find(self, text: str, list_name: str) -> list[TermMatch]:
        """All non-overlapping matches of one list (dotted names for nested lists). A group name such as
        "privacy_denylist.drop" searches all of its lists together. Unknown names raise KeyError."""
        return self._find_leaves(text, self._resolve(list_name))

    def _find_many(self, text: str, names: tuple[str, ...]) -> list[TermMatch]:
        leaves: list[str] = []
        for n in names:
            for leaf in self._resolve(n):
                if leaf not in leaves:
                    leaves.append(leaf)
        return self._find_leaves(text, tuple(leaves))

    # -- prefilter (docs/04 §5) -------------------------------------------------
    def signal_matches(self, text: str) -> list[TermMatch]:
        """Matches from the five signal lists (SIGNAL_LISTS), resolved together (longest wins), so
        'homeless encampment' counts once, not as 'homeless' + 'encampment'."""
        return self._find_many(text, SIGNAL_LISTS)

    def _blocked_anchor_spans(self, text: str) -> list[tuple[int, int]]:
        spans = [(m.start(), m.end()) for m in _LIC.finditer(text)]
        spans.extend((m.start, m.end) for m in self.find(text, "hard_exclusions.out_of_area_hints"))
        return spans

    def region_anchor_matches(self, text: str) -> list[TermMatch]:
        """region_anchor_terms matches, EXCLUDING 'Long Island' inside 'Long Island City' and anchors that
        are part of an out-of-area hint ('Suffolk' in 'Suffolk, VA', 'Nassau' in 'Nassau, Bahamas')."""
        matches = self.find(text, "region_anchor_terms")
        if not matches:
            return []
        blocked = self._blocked_anchor_spans(text)
        return [m for m in matches if not any(bs <= m.start and m.end <= be for bs, be in blocked)]

    def place_type_matches(self, text: str) -> list[TermMatch]:
        return self.find(text, "place_type_terms")

    def recency_matches(self, text: str) -> list[TermMatch]:
        return self.find(text, "recency_terms")

    def soft_exclusion_matches(self, text: str) -> list[TermMatch]:
        return self.find(text, "soft_exclusions")

    def hard_exclusion(self, text: str, *, has_li_place: bool) -> Optional[str]:
        """Return 'animal' | 'nyc_only' | 'out_of_area' | None per lexicon.yaml §4 rules.
        nyc_only applies only when not has_li_place and no region anchor; out_of_area only with no region anchor.
        border_ambiguous_note is informational and never excludes."""
        if self.find(text, "hard_exclusions.animal"):
            return "animal"
        anchors = self.region_anchor_matches(text)
        if not anchors and not has_li_place and self.find(text, "hard_exclusions.nyc_only_locations"):
            return "nyc_only"
        if not anchors and self.find(text, "hard_exclusions.out_of_area_hints"):
            return "out_of_area"
        return None

    # -- privacy (docs/05 V2, V6; notes check docs/07 §3) ------------------------
    def privacy_drop_matches(self, text: str) -> list[TermMatch]:
        """privacy_denylist.drop.* matches + privacy_patterns matches (any => drop the quote)."""
        out = self.find(text, "privacy_denylist.drop") + self.find(text, "privacy_patterns")
        out.sort(key=lambda m: (m.start, m.end))
        return out

    def privacy_mask(self, text: str) -> tuple[str, bool]:
        """Replace privacy_denylist.mask words with their bracketed neutral word. Returns (text, changed).
        Everything outside the replaced words is preserved exactly."""
        matches = self.find(text, "privacy_denylist.mask")
        if not matches:
            return text, False
        parts: list[str] = []
        pos = 0
        for m in matches:
            parts.append(text[pos:m.start])
            parts.append(m.list_name.rsplit(".", 1)[-1])
            pos = m.end
        parts.append(text[pos:])
        return "".join(parts), True

    def privacy_any(self, text: str) -> list[TermMatch]:
        """Matches from BOTH tiers + patterns (V6 summaries, staff notes)."""
        out = self.find(text, "privacy_denylist.mask") + self.privacy_drop_matches(text)
        out.sort(key=lambda m: (m.start, m.end))
        return out
