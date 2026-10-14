"""Deterministic verifier V1-V3, V5-V8 (docs/05 §5). V4 needs the geocode and lives in decision.py.

Claude proposes; this code checks the verdict against the post before anything is stored. Both sides of
every substring comparison go through textnorm.norm_for_match (NFKC, straight quotes, unified dashes,
collapsed whitespace, casefold). Privacy checks run on textnorm.clean()-ed text (the Lexicon contract).

CheckResult.detail strings are short, fixed phrases plus counts and lexicon CATEGORY names; they never
contain post text.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import re
from typing import TYPE_CHECKING, Iterable, Optional

from app.pipeline import textnorm
from app.pipeline.types import CheckResult, NormalizedItem, StoredQuote, Verdict, VerificationResult

if TYPE_CHECKING:
    from app.pipeline.lexicon import Lexicon

logger = logging.getLogger(__name__)

TYPE_LABELS = {
    "encampment": "Encampment",
    "individual_unsheltered": "Person(s) sleeping outside",
    "vehicle_dwelling": "Vehicle dwelling",
    "self_report": "Self-report",
    "unknown": "Possible signal",
}
TIME_LABELS = {"current": "current", "ongoing": "ongoing", "past": "past", "unclear": "time unclear"}

MAX_QUOTE_WORDS = 30          # V1: a longer quote is dropped, not trimmed
MIN_QUOTE_WORDS = 2           # a one-word "quote" proves nothing about the situation
MAX_QUOTES = 2                # V1: at most two quotes kept
MAX_SUMMARY_CHARS = 240       # V6
UNCLEAR_TIME_MAX_AGE = dt.timedelta(days=7)   # V5
WITHHELD_BASIS = "Summary withheld by privacy check."
MAX_PLACE_LABEL_CHARS = 120

# Review reasons this module can add (decision.py turns them into needs_human_review).
REVIEW_NO_VERIFIED_QUOTE = "no_verified_quote"
REVIEW_TIME_UNCLEAR_OLD = "time_unclear_old"

# Normalization placeholders (docs/04 §5). In a summary they mean the model referred to a person's
# handle/email/phone, which V6 treats like the raw value.
_PERSON_PLACEHOLDER = re.compile(r"\[(handle|email|phone)\]", re.IGNORECASE)
_ELLIPSIS = re.compile(r"\.\.\.|…|\[\s*\.\.\.\s*\]")


def _category(list_name: str) -> str:
    """'privacy_denylist.drop.age' -> 'age'; 'privacy_patterns.street_address' -> 'street_address'."""
    return (list_name or "").rsplit(".", 1)[-1] or "privacy"


def _categories(names: Iterable[str]) -> str:
    """Distinct category names in first-seen order, comma-separated."""
    cats: list[str] = []
    for c in names:
        if c not in cats:
            cats.append(c)
    return ", ".join(cats)


def _match_categories(matches: list) -> list[str]:
    return [_category(getattr(m, "list_name", "")) for m in matches]


def template_summary(signal_type: str, place_label: str | None, time_reference: str) -> str:
    """V6 replacement: '{Type label} reported at {place label} ({time label}).'"""
    label = TYPE_LABELS.get(signal_type or "", TYPE_LABELS["unknown"])
    when = TIME_LABELS.get(time_reference or "", TIME_LABELS["unclear"])
    place = textnorm.clean(place_label or "")
    if len(place) > MAX_PLACE_LABEL_CHARS:
        place = place[:MAX_PLACE_LABEL_CHARS].rsplit(" ", 1)[0].rstrip()
    if place:
        return f"{label} reported at {place} ({when})."
    return f"{label} reported ({when})."


def _privacy_text_ok(text: str, lexicon: "Lexicon") -> tuple[bool, str, str]:
    """V6 test for model-written text. Returns (ok, cleaned_text, short_reason)."""
    c = textnorm.clean(text)
    if not c:
        return False, c, "empty"
    if len(c) > MAX_SUMMARY_CHARS:
        return False, c, f"over {MAX_SUMMARY_CHARS} chars"
    if _PERSON_PLACEHOLDER.search(c):
        return False, c, "refers to a handle, email or phone"
    hits = lexicon.privacy_any(c)
    if hits:
        return False, c, f"denylist ({_categories(_match_categories(hits))})"
    return True, c, ""


def stored_location_text(location_text: str, lexicon: "Lexicon") -> Optional[str]:
    """The copy of a V3-verified location span that may be stored: None when it contains a street
    address, plate, handle, email, phone, URL or drop-tier term; gendered words masked."""
    c = textnorm.clean(location_text)
    if not c:
        return None
    if lexicon.privacy_drop_matches(c) or _PERSON_PLACEHOLDER.search(c):
        return None
    masked, _changed = lexicon.privacy_mask(c)
    return masked


def _has_street_address(text: str, lexicon: "Lexicon") -> bool:
    c = textnorm.clean(text)
    return bool(c) and any(_category(getattr(m, "list_name", "")) == "street_address"
                           for m in lexicon.privacy_drop_matches(c))


def _aware(d: dt.datetime) -> dt.datetime:
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _select_quotes(cands: list[StoredQuote]) -> list[StoredQuote]:
    """At most MAX_QUOTES, in model order, preferring distinct `supports` values; duplicates removed."""
    unique: list[StoredQuote] = []
    seen: set[str] = set()
    for q in cands:
        key = textnorm.norm_for_match(q.text)
        if key in seen:
            continue
        seen.add(key)
        unique.append(q)
    chosen: list[StoredQuote] = []
    for q in unique:
        if len(chosen) >= MAX_QUOTES:
            break
        if all(q.supports != c.supports for c in chosen):
            chosen.append(q)
    for q in unique:
        if len(chosen) >= MAX_QUOTES:
            break
        if q not in chosen:
            chosen.append(q)
    order = {id(q): i for i, q in enumerate(unique)}
    chosen.sort(key=lambda q: order[id(q)])
    return chosen


def verify(item: NormalizedItem, verdict: Verdict, *, lexicon: "Lexicon", now: dt.datetime) -> VerificationResult:
    """Apply V1 (verbatim quotes via textnorm.norm_for_match, <=30 words, <=2 kept, no ellipsis-joined spans),
    V2 (privacy patterns/drop -> drop + evidence_redacted; mask -> masked copy), V3 (location span),
    V5 (time), V6 (summary/decision_basis privacy + 240 chars), V7 (consistency), V8 (residence).
    After V1-V2 a relevant verdict with 0 quotes adds review reason 'no_verified_quote'.

    checks holds one CheckResult each for V1, V2, V3, V5, V6, V7, V8 (in that order); decision.check_v4 adds
    V4. review_reasons may contain 'no_verified_quote' and 'time_unclear_old'."""
    post_norm = textnorm.norm_for_match(item.text)
    checks: list[CheckResult] = []
    review_reasons: list[str] = []

    # --- V7 consistency first: the coerced relevance decides whether missing quotes need review -------
    relevance = verdict.relevance
    signal_type = verdict.signal_type
    coerced: list[str] = []
    if relevance == "not_relevant" and signal_type != "none":
        signal_type = "none"
        coerced.append("type set to none for a not-relevant verdict")
    if signal_type == "none" and relevance != "not_relevant":
        relevance = "not_relevant"
        coerced.append("relevance set to not_relevant (no signal type)")
    count = verdict.people_count_estimate
    if count < -1:
        coerced.append("negative people count cleared")
    people_count: Optional[int] = count if count >= 0 else None

    # --- V1 verbatim quotes, then V2 privacy on the survivors --------------------------------------
    v1_ok = 0
    v1_fail: dict[str, int] = {}
    survivors: list[StoredQuote] = []
    n_masked = 0
    dropped_cats: list[str] = []
    evidence_redacted = False
    for q in verdict.evidence_quotes:
        qc = textnorm.clean(q.text)
        words = textnorm.word_count(qc)
        if not qc:
            why = "empty"
        elif words > MAX_QUOTE_WORDS:
            why = f"over {MAX_QUOTE_WORDS} words"
        elif words < MIN_QUOTE_WORDS:
            why = "too short"
        elif textnorm.norm_for_match(qc) in post_norm:
            why = ""
        elif _ELLIPSIS.search(qc):
            why = "ellipsis-joined spans"
        else:
            why = "not found verbatim in post"
        if why:
            v1_fail[why] = v1_fail.get(why, 0) + 1
            continue
        v1_ok += 1
        drops = lexicon.privacy_drop_matches(qc)
        if drops or _PERSON_PLACEHOLDER.search(qc):
            evidence_redacted = True
            dropped_cats.extend(_match_categories(drops) or ["placeholder"])
            continue
        masked, changed = lexicon.privacy_mask(qc)
        if changed:
            n_masked += 1
        survivors.append(StoredQuote(text=masked, supports=q.supports, masked=bool(changed)))
    quotes = _select_quotes(survivors)

    n_quotes = len(verdict.evidence_quotes)
    if n_quotes == 0:
        v1_detail = "no quotes given"
    else:
        v1_detail = f"{v1_ok} of {n_quotes} quotes verbatim"
        if v1_fail:
            v1_detail += "; " + ", ".join(f"{n} {why}" for why, n in v1_fail.items())
    checks.append(CheckResult("V1", v1_ok > 0, v1_detail))

    n_dropped = v1_ok - len(survivors)
    v2_parts: list[str] = []
    if n_dropped:
        v2_parts.append(f"{n_dropped} quote{'s' if n_dropped != 1 else ''} dropped ({_categories(dropped_cats)})")
    if n_masked:
        v2_parts.append(f"{n_masked} quote{'s' if n_masked != 1 else ''} masked")
    if v1_ok and not v2_parts:
        v2_parts.append("quotes clean")
    if not v1_ok:
        v2_parts.append("no verified quotes to check")
    checks.append(CheckResult("V2", n_dropped == 0, "; ".join(v2_parts)))

    if relevance != "not_relevant" and not quotes:
        review_reasons.append(REVIEW_NO_VERIFIED_QUOTE)

    # --- V3 location span ---------------------------------------------------------------------------
    loc = verdict.location
    loc_clean = textnorm.clean(loc.location_text)
    if not loc.mentioned or loc.location_kind == "none" or not loc_clean:
        location_ok = False
        v3_detail = "no location given"
    elif textnorm.norm_for_match(loc_clean) in post_norm:
        location_ok = True
        v3_detail = "location span found in post"
    else:
        location_ok = False
        v3_detail = "location span not found in post; treated as no location"
    stored_loc = stored_location_text(loc_clean, lexicon) if location_ok else None
    if location_ok and stored_loc is None:
        v3_detail += "; span withheld from storage (privacy)"
    checks.append(CheckResult("V3", location_ok, v3_detail))

    # --- V5 time ------------------------------------------------------------------------------------
    tr = verdict.time_reference
    time_ok = time_needs_review = time_past = False
    if tr in ("current", "ongoing"):
        time_ok = True
        v5_detail = f"time reference {tr}"
    elif tr == "past":
        time_past = True
        v5_detail = "past event"
    else:
        age = _aware(now) - _aware(item.published_at)
        days = max(0, int(age.total_seconds() // 86400))
        if age <= UNCLEAR_TIME_MAX_AGE:
            time_ok = True
            v5_detail = f"time unclear; published {days} day{'s' if days != 1 else ''} ago (within 7)"
        else:
            time_needs_review = True
            review_reasons.append(REVIEW_TIME_UNCLEAR_OLD)
            v5_detail = f"time unclear; published {days} days ago (over 7)"
    checks.append(CheckResult("V5", time_ok, v5_detail))

    # --- V6 summary / decision_basis privacy -------------------------------------------------------
    if textnorm.clean(verdict.neutral_summary):
        sum_ok, sum_clean, sum_why = _privacy_text_ok(verdict.neutral_summary, lexicon)
        summary = sum_clean if sum_ok else template_summary(signal_type, None, tr)
        sum_detail = "summary ok" if sum_ok else f"summary replaced by template ({sum_why})"
    else:                                  # nothing to check; storage still needs a summary
        sum_ok, summary, sum_detail = True, template_summary(signal_type, None, tr), "no summary; template used"
    if textnorm.clean(verdict.decision_basis):
        basis_ok, basis_clean, basis_why = _privacy_text_ok(verdict.decision_basis, lexicon)
        decision_basis = basis_clean if basis_ok else WITHHELD_BASIS
        basis_detail = "basis ok" if basis_ok else f"basis withheld ({basis_why})"
    else:
        basis_ok, decision_basis, basis_detail = True, "", "no basis given"
    checks.append(CheckResult("V6", sum_ok and basis_ok, f"{sum_detail}; {basis_detail}"))

    # --- V7 (computed above) ------------------------------------------------------------------------
    checks.append(CheckResult("V7", not coerced, "; ".join(coerced) if coerced else "consistent"))

    # --- V8 residence -------------------------------------------------------------------------------
    address_in_location = bool(loc_clean) and _has_street_address(loc_clean, lexicon)
    is_private_residence = bool(loc.is_private_residence) or address_in_location
    if loc.is_private_residence:
        v8_detail = "private residence: hamlet-level point only"
    elif address_in_location:
        v8_detail = "street address in location: hamlet-level point only"
    else:
        v8_detail = "not a private residence"
    checks.append(CheckResult("V8", True, v8_detail))

    extra = {}
    if "location_text" in _VR_FIELDS:   # forward-compatible with the proposed VerificationResult.location_text
        extra["location_text"] = stored_loc
    vr = VerificationResult(
        quotes=quotes,
        evidence_redacted=evidence_redacted,
        location_ok=location_ok,
        time_ok=time_ok,
        time_needs_review=time_needs_review,
        time_past=time_past,
        summary=summary,
        decision_basis=decision_basis,
        people_count=people_count,
        relevance=relevance,
        signal_type=signal_type,
        is_private_residence=is_private_residence,
        checks=checks,
        review_reasons=review_reasons,
        **extra,
    )
    logger.debug("verifier: item %s quotes=%d/%d redacted=%s location_ok=%s time_ok=%s review=%s",
                 (item.item_hash or "")[:12], len(quotes), n_quotes, evidence_redacted, location_ok, time_ok,
                 review_reasons)
    return vr


_VR_FIELDS = frozenset(f.name for f in dataclasses.fields(VerificationResult))
