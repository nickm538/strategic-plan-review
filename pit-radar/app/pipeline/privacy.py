"""Privacy choke point: the ONLY path from pipeline objects to `signals` rows (docs/09 §3, R1-R7).

`build_signal_values` builds the column dict for one Signal from an explicit allow-list. It reads only the
named attributes it needs from the pipeline objects (never `vars()`/`asdict()`), so an author field, a media
URL or any other unexpected attribute on an input can never reach the database. The result is checked
against ALLOWED_SIGNAL_FIELDS before it is returned.

Defense in depth: the verifier (V1-V8) and normalization already scrub handles, emails, phones and links
and check quotes and summaries against the lexicon. This module re-scrubs every stored string with its
own small regex set, so a regression elsewhere cannot leak contact details into the database.

`PrivacyViolation` means the item must not be stored at all (e.g. a profile URL, R2). The runner catches
it and drops the item.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import math
import re
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlsplit

from app.pipeline import textnorm
from app.pipeline.types import (
    ClassifierResult,
    Decision,
    GeoResult,
    NormalizedItem,
    VerificationResult,
)

if TYPE_CHECKING:
    from app.pipeline.lexicon import Lexicon

logger = logging.getLogger(__name__)

# Exactly the Signal columns the pipeline may write. Anything else is dropped (property-tested).
ALLOWED_SIGNAL_FIELDS = frozenset({
    "scan_id", "provider", "platform", "url", "post_id_hash", "published_at", "collected_at", "signal_type",
    "status", "relevance", "review_reason", "neutral_summary", "evidence_quotes", "evidence_redacted",
    "location_text", "location_kind", "place_id", "place_label", "town", "lat", "lon", "precision", "county",
    "people_count_est", "flags", "checks", "time_reference", "ai_confidence", "model", "decision_basis",
    "expires_at",
})


class PrivacyViolation(ValueError):
    """The item cannot be stored without breaking a privacy rule (R1/R2). The runner drops it.
    The message names the rule only; it never contains the URL or post text."""


# ---------------------------------------------------------------------------
# Vocabularies (mirrors app.models / schemas; kept local so this module validates on its own)
# ---------------------------------------------------------------------------
_PROVIDERS = frozenset({"socialcrawl", "exa", "firecrawl"})
_PLATFORMS = frozenset({"reddit", "x", "tiktok", "instagram", "threads", "bluesky", "facebook", "youtube",
                        "news", "web"})
_STATUSES = frozenset({"ai_verified", "confirmed", "dismissed", "needs_human_review", "unlocated", "self_report"})
_MAPPED_STATUSES = frozenset({"ai_verified", "confirmed"})
_COORD_STATUSES = frozenset({"ai_verified", "confirmed", "needs_human_review"})
_SIGNAL_TYPES = frozenset({"encampment", "individual_unsheltered", "vehicle_dwelling", "self_report"})
_COUNTIES = frozenset({"Nassau", "Suffolk"})
_MAPPABLE_PRECISIONS = frozenset({"public_place", "hamlet"})
_LOCATION_KINDS = frozenset({"named_place", "intersection", "street", "business", "town_only", "none"})
_SUPPORTS = frozenset({"situation", "location", "recency", "count"})
_TIME_REFS = frozenset({"current", "ongoing", "past", "unclear"})
_RELEVANCE_STORED = frozenset({"relevant", "uncertain"})
FLAG_KEYS = ("possible_minors", "medical_concern", "weather_exposure", "safety_concern")

MAX_QUOTES = 2
MAX_QUOTE_WORDS = 30
MAX_QUOTE_CHARS = 400
MAX_SUMMARY_CHARS = 240
MAX_BASIS_CHARS = 240
MAX_LOCATION_CHARS = 160
MAX_LABEL_CHARS = 120
MAX_TOWN_CHARS = 80
MAX_REVIEW_REASON_CHARS = 120
MAX_CHECK_DETAIL_CHARS = 240
MAX_MODEL_CHARS = 64
MAX_URL_CHARS = 2048
MAX_PEOPLE_COUNT = 9999
MAX_NOTE_CHARS = 2000

NO_VERDICT_SUMMARY = "Post flagged for staff review; no AI summary."
WITHHELD_BASIS = "Decision basis withheld by privacy check."

_TYPE_LABELS = {
    "encampment": "Encampment",
    "individual_unsheltered": "Person(s) sleeping outside",
    "vehicle_dwelling": "Vehicle dwelling",
    "self_report": "Self-report",
    "unknown": "Possible signal",
}
_TIME_LABELS = {"current": "current", "ongoing": "ongoing", "past": "past", "unclear": "time unclear"}

# ---------------------------------------------------------------------------
# Own regex set (defense in depth; independent of normalize.py and the lexicon)
# ---------------------------------------------------------------------------
_URL_RE = re.compile(r"(?:\b[a-z][a-z0-9+.-]*://|(?<![\w.@/-])www\.)[^\s<>\"'\]\[)(]+", re.IGNORECASE)
# Scheme-less links ("newsday.com/story", "bit.ly/x", "name.bsky.social"). The TLD must be lowercase so
# run-together sentences ("the station.Com...") are not mistaken for links.
_BARE_DOMAIN_RE = re.compile(
    r"(?<![\w.@/-])(?:[a-z0-9-]+\.)+(?-i:com|net|org|edu|gov|io|ly|info|news|social)"
    r"(?:/[^\s<>\"'\]\[)(]*)?(?![\w-])",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?![\w-])", re.IGNORECASE)
_HANDLE_RE = re.compile(r"(?<![\w@/.])@[A-Za-z0-9_](?:[A-Za-z0-9_.]{0,30}[A-Za-z0-9_])?")
_REDDIT_USER_RE = re.compile(r"(?<![\w/])/?u/[A-Za-z0-9_-]{2,}", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<![\w+])(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)")
_PLACEHOLDER_RE = re.compile(r"\[(?:handle|email|phone|link)\]", re.IGNORECASE)

# Street address: house number + up to 3 name words + a street suffix. Matches whose name words contain a
# connective ("2 guys on Main St") or whose number is a route/exit number are not addresses.
_STREET_RE = re.compile(
    r"(?<![\w-])(\d{1,5}[A-Za-z]?)\s+((?:[A-Za-z0-9'.-]+\s+){0,3}?)"
    r"(st|street|ave|avenue|av|rd|road|ln|lane|dr|drive|ct|court|pl|place|blvd|boulevard|way|ter|terrace|"
    r"hwy|highway|tpke|turnpike|pkwy|parkway|cir|circle|sq|square)\b\.?",
    re.IGNORECASE,
)
_NOT_ADDRESS_WORDS = frozenset({"and", "or", "on", "at", "near", "by", "in", "from", "to", "of", "off", "past",
                                "behind", "outside", "across", "under", "between", "the", "a", "an", "&",
                                "am", "pm", "a.m.", "p.m.", "days", "weeks", "months", "nights", "hours"})
_ROUTE_BEFORE = re.compile(r"(?:\b(?:route|rte|rt|exit|hwy|highway|cr|sr|nys|ny|us|i|interstate|road|rd|bus|"
                           r"county\s+road|state\s+road)\s*[-#]?\s*)$", re.IGNORECASE)


def _clean(text: Any) -> str:
    return textnorm.clean(text if isinstance(text, str) else "")


def scrub_contacts(text: str) -> tuple[str, bool]:
    """Replace URLs, emails, @handles, reddit u/names and US phone numbers with [link]/[email]/[handle]/
    [phone]. Returns (text, changed)."""
    t = _URL_RE.sub("[link]", text)
    t = _BARE_DOMAIN_RE.sub("[link]", t)
    t = _EMAIL_RE.sub("[email]", t)
    t = _HANDLE_RE.sub("[handle]", t)
    t = _REDDIT_USER_RE.sub("[handle]", t)
    t = _PHONE_RE.sub("[phone]", t)
    return t, t != text


def _has_contacts(text: str) -> bool:
    return scrub_contacts(text)[1] or bool(_PLACEHOLDER_RE.search(text))


def has_street_address(text: str) -> bool:
    """True when the text contains something shaped like a residential street address ('12 Oak St')."""
    for m in _STREET_RE.finditer(text or ""):
        words = [w.lower() for w in m.group(2).split()]
        if any(w in _NOT_ADDRESS_WORDS for w in words):
            continue
        if _ROUTE_BEFORE.search(text[:m.start()].rstrip() + " "):
            continue
        return True
    return False


def _truncate_words(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-")


# ---------------------------------------------------------------------------
# URLs (R1 media, R2 profiles)
# ---------------------------------------------------------------------------
_MEDIA_EXT = re.compile(r"\.(?:jpe?g|png|gif|webp|heic|heif|bmp|tiff?|svg|mp4|mov|m4v|webm|avi|mkv|m3u8|mpd|"
                        r"mp3|m4a|wav|ogg)$", re.IGNORECASE)
_MEDIA_HOSTS = ("i.redd.it", "v.redd.it", "preview.redd.it", "external-preview.redd.it", "i.imgur.com",
                "pbs.twimg.com", "video.twimg.com", "cdninstagram.com", "fbcdn.net", "tiktokcdn.com",
                "tiktokcdn-us.com", "ytimg.com", "googlevideo.com", "cdn.bsky.app", "video.bsky.app")


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def check_public_url(url: str) -> str:
    """Return the URL (stripped) if it may be stored; raise PrivacyViolation for a profile URL (R2), a media
    file URL (R1), credentials in the URL, or anything that is not a plain http(s) link."""
    u = (url or "").strip()
    if not u or len(u) > MAX_URL_CHARS or any(ch.isspace() for ch in u):
        raise PrivacyViolation("url: not a usable public link")
    try:
        parts = urlsplit(u)
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        raise PrivacyViolation("url: unparseable") from None
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise PrivacyViolation("url: not an http(s) link")
    if parts.username is not None or parts.password is not None:
        raise PrivacyViolation("url: carries credentials")
    path = parts.path or "/"
    segs = _segments(path)
    low = [s.lower() for s in segs]
    query = (parts.query or "").lower()

    # R1: direct media files and media CDNs.
    if any(_host_matches(host, d) for d in _MEDIA_HOSTS) or _MEDIA_EXT.search(path):
        raise PrivacyViolation("R1: media URL")

    # R2: profile pages.
    if _host_matches(host, "reddit.com"):
        if any(s in ("user", "u") for s in low) or (len(low) >= 2 and low[0] == "r" and low[1].startswith("u_")):
            raise PrivacyViolation("R2: profile URL")
    elif host in ("x.com", "twitter.com") or _host_matches(host, "x.com") or _host_matches(host, "twitter.com"):
        if "status" not in low and "statuses" not in low:
            raise PrivacyViolation("R2: profile URL")
    elif _host_matches(host, "instagram.com"):
        if not any(s in ("p", "reel", "reels", "tv") for s in low[:2]):
            raise PrivacyViolation("R2: profile URL")
    elif _host_matches(host, "tiktok.com"):
        if any(s.startswith("@") for s in segs) and "video" not in low and "photo" not in low:
            raise PrivacyViolation("R2: profile URL")
    elif _host_matches(host, "facebook.com") or _host_matches(host, "fb.com"):
        is_post = ("posts" in low or any(s.startswith(("story", "permalink")) for s in low)
                   or (len(low) >= 2 and low[0] == "share" and low[1] in ("p", "r", "v")))
        if not is_post or "profile.php" in low:
            raise PrivacyViolation("R2: profile URL")
    elif _host_matches(host, "threads.net") or _host_matches(host, "threads.com"):
        if "post" not in low and low[:1] != ["t"]:
            raise PrivacyViolation("R2: profile URL")
    elif host == "bsky.app" or _host_matches(host, "bsky.app"):
        if "post" not in low:
            raise PrivacyViolation("R2: profile URL")
    elif _host_matches(host, "youtube.com"):
        is_video = (low[:1] == ["watch"] and "v=" in query) or (low[:1] in (["shorts"], ["live"], ["embed"]))
        if not is_video:
            raise PrivacyViolation("R2: profile URL")
    elif _host_matches(host, "linkedin.com") and low[:1] in (["in"], ["pub"]):
        raise PrivacyViolation("R2: profile URL")
    return u


def hash_post_id(platform: str, platform_post_id: str) -> str:
    """sha256(platform + ':' + platform_post_id) hex."""
    return hashlib.sha256(f"{platform}:{platform_post_id}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Field builders
# ---------------------------------------------------------------------------
def _aware(d: Any, field: str) -> dt.datetime:
    if not isinstance(d, dt.datetime):
        raise ValueError(f"{field} must be a datetime")
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def _finite(x: Any) -> Optional[float]:
    if isinstance(x, bool) or x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _template_summary(signal_type: str, place: Optional[str], time_reference: str) -> str:
    label = _TYPE_LABELS.get(signal_type, _TYPE_LABELS["unknown"])
    when = _TIME_LABELS.get(time_reference, _TIME_LABELS["unclear"])
    place = _truncate_words(_clean(place), MAX_LABEL_CHARS) if place else ""
    if place and (_has_contacts(place) or has_street_address(place)):
        place = ""
    if signal_type == "self_report":      # town-level only (R7)
        return f"Self-report in {place} ({when})." if place else f"Self-report ({when})."
    if place:
        return f"{label} reported at {place} ({when})."
    return f"{label} reported ({when})."


def _text_ok(text: str, lexicon: Optional["Lexicon"], max_chars: int) -> bool:
    if not text or len(text) > max_chars:
        return False
    if _has_contacts(text) or has_street_address(text):
        return False
    return not (lexicon is not None and lexicon.privacy_any(text))


def _quote_parts(q: Any) -> tuple[str, str, bool]:
    if isinstance(q, dict):
        return q.get("text") or "", q.get("supports") or "", bool(q.get("masked", False))
    return getattr(q, "text", "") or "", getattr(q, "supports", "") or "", bool(getattr(q, "masked", False))


def _build_quotes(raw_quotes: Any, lexicon: Optional["Lexicon"]) -> tuple[list[dict[str, Any]], bool]:
    """R4: <= 2 quotes, <= 30 words each, contact details scrubbed. Returns (quotes, any_dropped_for_privacy)."""
    out: list[dict[str, Any]] = []
    dropped = False
    seen: set[str] = set()
    for q in list(raw_quotes or []):
        if len(out) >= MAX_QUOTES:
            break
        text, supports, masked = _quote_parts(q)
        text = _clean(text)
        if not text:
            continue
        words = text.split()
        if len(words) > MAX_QUOTE_WORDS:
            text = " ".join(words[:MAX_QUOTE_WORDS])
        text = _truncate_words(text, MAX_QUOTE_CHARS)
        if has_street_address(text) or (lexicon is not None and lexicon.privacy_drop_matches(text)):
            dropped = True
            continue
        scrubbed, changed = scrub_contacts(text)
        if changed:
            masked = True
            if not _PLACEHOLDER_RE.sub(" ", scrubbed).strip(" .,;:!?-"):
                dropped = True          # nothing but contact details
                continue
        key = textnorm.norm_for_match(scrubbed)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "text": scrubbed,
            "supports": supports if supports in _SUPPORTS else "situation",
            "masked": bool(masked),
        })
    return out, dropped


def _build_checks(raw_checks: Any) -> dict[str, dict[str, Any]]:
    """[CheckResult] -> {code: {passed, detail}} (V1..V8 only)."""
    out: dict[str, dict[str, Any]] = {}
    for c in list(raw_checks or []):
        if isinstance(c, dict):
            code, passed, detail = c.get("code"), c.get("passed"), c.get("detail", "")
        else:
            code, passed, detail = getattr(c, "code", None), getattr(c, "passed", None), getattr(c, "detail", "")
        if not isinstance(code, str) or not re.fullmatch(r"V[1-8]", code):
            continue
        detail_s, _ = scrub_contacts(_clean(detail if isinstance(detail, str) else ""))
        out[code] = {"passed": bool(passed), "detail": _truncate_words(detail_s, MAX_CHECK_DETAIL_CHARS)}
    return dict(sorted(out.items()))


def _build_flags(raw_flags: Any) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for k in FLAG_KEYS:
        v = raw_flags.get(k) if isinstance(raw_flags, dict) else getattr(raw_flags, k, None)
        out[k] = v is True
    return out


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default) if obj is not None else default


def _geo_in_area(geo: Optional[GeoResult]) -> bool:
    return geo is not None and _attr(geo, "in_geofence") is True and _attr(geo, "county") in _COUNTIES


def _stored_location_text(*, verdict: Any, vr: VerificationResult, lexicon: Optional["Lexicon"]) -> Optional[str]:
    """A verbatim public-place span, or None (R5: never a street address or a residence)."""
    loc = _attr(verdict, "location")
    if loc is None or _attr(vr, "location_ok") is not True:
        return None
    kind = _attr(loc, "location_kind")
    if kind not in _LOCATION_KINDS or kind in ("street", "none"):
        return None
    if _attr(loc, "is_private_residence") is True or _attr(vr, "is_private_residence") is True:
        return None
    # Prefer the verifier's privacy-processed copy when the VerificationResult carries one.
    raw = vr.location_text if hasattr(vr, "location_text") else _attr(loc, "location_text")
    text = _clean(raw)
    if not text or len(text) > MAX_LOCATION_CHARS:
        return None
    if _has_contacts(text) or has_street_address(text):
        return None
    if lexicon is not None:
        if lexicon.privacy_drop_matches(text):
            return None
        text, _changed = lexicon.privacy_mask(text)
    return text


def build_signal_values(*, item: NormalizedItem, result: ClassifierResult, vr: Optional[VerificationResult],
                        geo: Optional[GeoResult], decision: Decision, scan_id: int, now: dt.datetime,
                        retention_days: int, self_report_town: Optional[str] = None,
                        self_report_county: Optional[str] = None,
                        lexicon: Optional["Lexicon"] = None) -> dict[str, Any]:
    """Return a dict with ONLY ALLOWED_SIGNAL_FIELDS keys. Enforces: no author fields (R2), no media (R1),
    quotes <= 2 x 30 words with handles/emails/phones/URLs scrubbed (R4), coordinates only from GeoResult
    (already snapped, R5), self_report -> lat/lon None + town only (R7), expires_at = collected_at +
    retention_days (R6). For refusal/needs_human_review with no verdict: signal_type 'unknown',
    neutral_summary 'Post flagged for staff review; no AI summary.', confidence 0.

    Raises PrivacyViolation when the item must be dropped (profile or media URL), ValueError when the
    decision does not store a signal. `lexicon`, if given, adds a lexicon re-check of the stored text."""
    if not _attr(decision, "store"):
        raise ValueError("decision does not store a signal")
    status = _attr(decision, "status")
    if status not in _STATUSES:
        raise ValueError("decision has no valid signal status")
    if not isinstance(retention_days, int) or isinstance(retention_days, bool) or retention_days < 1:
        raise ValueError("retention_days must be a positive integer")

    # --- identity of the post (R2: platform + URL + hashed id only) --------------------------------
    provider = _attr(item, "provider")
    if provider not in _PROVIDERS:
        raise ValueError("unknown provider")
    platform = _attr(item, "platform")
    if platform not in _PLATFORMS:
        platform = "web"
    url = check_public_url(_attr(item, "url") or "")
    post_id = str(_attr(item, "platform_post_id") or "").strip() or url
    now_utc = _aware(now, "now")
    published_at = _aware(_attr(item, "published_at"), "published_at")
    collected_at = min(_aware(_attr(item, "collected_at"), "collected_at"), now_utc)

    verdict = _attr(result, "verdict")
    has_verdict = verdict is not None and vr is not None

    values: dict[str, Any] = {
        "scan_id": int(scan_id),
        "provider": provider,
        "platform": platform,
        "url": url,
        "post_id_hash": hash_post_id(platform, post_id),
        "published_at": published_at,
        "collected_at": collected_at,
        "expires_at": collected_at + dt.timedelta(days=retention_days),
        "model": _truncate_words(_clean(_attr(result, "model") or _attr(_attr(result, "usage"), "model") or ""),
                                 MAX_MODEL_CHARS) or "unknown",
        "review_reason": None,
    }
    if status == "needs_human_review":
        reason, _ = scrub_contacts(_clean(_attr(decision, "review_reason") or "classifier_review"))
        values["review_reason"] = reason[:MAX_REVIEW_REASON_CHARS]

    if not has_verdict:
        # Refusal / invalid output: no AI verdict is invented (docs/05 §3). Staff see the post link only.
        if status in _MAPPED_STATUSES or status == "self_report":
            raise ValueError("a mapped or self-report signal needs a verified verdict")
        values.update({
            "status": status,
            "signal_type": "unknown",
            "relevance": None,
            "neutral_summary": NO_VERDICT_SUMMARY,
            "evidence_quotes": [],
            "evidence_redacted": False,
            "location_text": None, "location_kind": None, "place_id": None, "place_label": None,
            "town": None, "lat": None, "lon": None, "precision": None, "county": None,
            "people_count_est": None,
            "flags": {},
            "checks": {},
            "time_reference": "unclear",
            "ai_confidence": 0.0,
            "decision_basis": "",
        })
        return _finalize(values)

    # --- verdict path --------------------------------------------------------------------------------
    signal_type = _attr(vr, "signal_type")
    if status == "self_report":
        signal_type = "self_report"
    if signal_type not in _SIGNAL_TYPES:
        signal_type = "unknown"
    is_self_report = signal_type == "self_report" or status == "self_report"
    time_reference = _attr(verdict, "time_reference")
    if time_reference not in _TIME_REFS:
        time_reference = "unclear"
    loc = _attr(verdict, "location")
    location_kind = _attr(loc, "location_kind")
    if location_kind not in _LOCATION_KINDS:
        location_kind = None
    residence = _attr(loc, "is_private_residence") is True or _attr(vr, "is_private_residence") is True

    # Coordinates come only from the snapped GeoResult, inside the polygon, at a mappable precision (R5).
    lat = lon = None
    precision = place_id = place_label = None
    town = county = None
    geo_ok = _geo_in_area(geo) and _attr(vr, "location_ok") is True
    if is_self_report:
        # R7: never a pin; town and county only.
        town = _truncate_words(_clean(self_report_town), MAX_TOWN_CHARS) or None
        county = self_report_county if self_report_county in _COUNTIES else None
        location_kind = None
    else:
        if geo_ok:
            county = _attr(geo, "county")
            town = _truncate_words(_clean(_attr(geo, "town")), MAX_TOWN_CHARS) or None
            g_lat, g_lon = _finite(_attr(geo, "lat")), _finite(_attr(geo, "lon"))
            g_prec = _attr(geo, "precision")
            point_ok = (status in _COORD_STATUSES and g_lat is not None and g_lon is not None
                        and -90 <= g_lat <= 90 and -180 <= g_lon <= 180 and g_prec in _MAPPABLE_PRECISIONS)
            if point_ok and g_prec == "public_place" and (residence or location_kind == "street"):
                # A residence or bare street must be hamlet-level (R5, V8); never keep a precise point.
                logger.warning("privacy: public_place point refused for a residence/street location (item %s)",
                               (_attr(item, "item_hash") or "")[:12])
                point_ok = False
            if point_ok:
                lat, lon, precision = g_lat, g_lon, g_prec
            if point_ok or g_prec == "town":
                pid = _attr(geo, "place_id")
                place_id = pid if isinstance(pid, int) and not isinstance(pid, bool) else None
                label = _truncate_words(_clean(_attr(geo, "place_label")), MAX_LABEL_CHARS)
                place_label = label if label and not _has_contacts(label) and not has_street_address(label) else None
        if status in _MAPPED_STATUSES and lat is None:
            # A mapped status without a stored point would be an invisible pin: list it as unlocated.
            logger.warning("privacy: %s signal without a storable point stored as unlocated (item %s)",
                           status, (_attr(item, "item_hash") or "")[:12])
            status = "unlocated"

    # Text fields: verifier-cleaned, re-checked here.
    summary = _clean(_attr(vr, "summary"))
    if not _text_ok(summary, lexicon, MAX_SUMMARY_CHARS):
        summary = _template_summary(signal_type, town if is_self_report else (place_label or town), time_reference)
    basis = _clean(_attr(vr, "decision_basis"))
    if basis and not _text_ok(basis, lexicon, MAX_BASIS_CHARS):
        basis = WITHHELD_BASIS

    quotes, dropped = _build_quotes(_attr(vr, "quotes"), lexicon)
    location_text = None if is_self_report else _stored_location_text(verdict=verdict, vr=vr, lexicon=lexicon)

    relevance = _attr(vr, "relevance")
    people = _attr(vr, "people_count")
    if isinstance(people, bool) or not isinstance(people, int) or people < 0 or people > MAX_PEOPLE_COUNT:
        people = None
    conf = _finite(_attr(verdict, "confidence"))
    conf = 0.0 if conf is None else min(1.0, max(0.0, conf))

    values.update({
        "status": status,
        "signal_type": signal_type,
        "relevance": relevance if relevance in _RELEVANCE_STORED else None,
        "neutral_summary": summary,
        "evidence_quotes": quotes,
        "evidence_redacted": bool(_attr(vr, "evidence_redacted") is True or dropped),
        "location_text": location_text,
        "location_kind": location_kind,
        "place_id": place_id,
        "place_label": place_label,
        "town": town,
        "lat": lat,
        "lon": lon,
        "precision": precision,
        "county": county,
        "people_count_est": people,
        "flags": _build_flags(_attr(verdict, "flags")),
        "checks": _build_checks(_attr(vr, "checks")),
        "time_reference": time_reference,
        "ai_confidence": conf,
        "decision_basis": basis,
    })
    if values["status"] != "needs_human_review":
        values["review_reason"] = None
    return _finalize(values)


def _finalize(values: dict[str, Any]) -> dict[str, Any]:
    """Final allow-list gate and R7 invariant (also enforced by a DB CHECK constraint)."""
    out = {k: v for k, v in values.items() if k in ALLOWED_SIGNAL_FIELDS}
    if out.get("signal_type") == "self_report" or out.get("status") == "self_report":
        out["lat"] = out["lon"] = out["precision"] = None
        out["place_id"] = out["place_label"] = out["location_text"] = None
    extra = set(out) - ALLOWED_SIGNAL_FIELDS
    if extra:  # explicit check (not `assert`) so it also holds under python -O
        raise AssertionError(f"privacy: non-allow-listed signal fields: {sorted(extra)}")
    missing = ALLOWED_SIGNAL_FIELDS - set(out)
    if missing:
        raise AssertionError(f"privacy: signal fields not set: {sorted(missing)}")
    return out


# ---------------------------------------------------------------------------
# Staff notes (docs/07 §3)
# ---------------------------------------------------------------------------
_PEOPLE_CATEGORY_LABELS = {
    "age": "age",
    "race_ethnicity_nationality": "race, ethnicity or nationality",
    "immigration_status": "immigration status",
    "gender_identity": "gender identity",
    "appearance_and_clothing": "appearance or clothing",
    "disability_and_health": "health or disability",
    "substance_use": "substance use",
}
_DETAIL_CATEGORY_LABELS = {
    "handle": "contact details",
    "email": "contact details",
    "phone": "contact details",
    "url": "links",
    "street_address": "street addresses",
    "license_plate_word": "license plates",
    "ny_plate_format": "license plates",
}


def _note_category(list_name: str) -> tuple[str, str]:
    """('people' | 'detail', friendly label) for a lexicon list name."""
    parts = (list_name or "").split(".")
    if parts[:2] == ["privacy_denylist", "mask"]:
        return "people", "gender"
    leaf = parts[-1].strip().replace("_", " ") if parts else ""
    if parts[0] == "privacy_patterns":
        return "detail", _DETAIL_CATEGORY_LABELS.get(parts[-1], leaf or "personal details")
    return "people", _PEOPLE_CATEGORY_LABELS.get(parts[-1], leaf or "descriptions of people")


def check_note(text: str, lexicon: "Lexicon") -> Optional[str]:
    """Staff notes (docs/07 §3): None if OK, else a friendly error message naming the problem category
    ('Notes must describe the place and situation only - remove descriptions of people (age).')."""
    raw = text if isinstance(text, str) else ""
    if len(raw) > MAX_NOTE_CHARS:
        return f"Notes must be {MAX_NOTE_CHARS:,} characters or fewer."
    c = textnorm.clean(raw)
    if not c:
        return None
    people: list[str] = []
    details: list[str] = []

    def add(kind: str, label: str) -> None:
        bucket = people if kind == "people" else details
        if label not in bucket:
            bucket.append(label)

    for m in lexicon.privacy_any(c):
        add(*_note_category(m.list_name))
    if _URL_RE.search(c) or _BARE_DOMAIN_RE.search(c):
        add("detail", "links")
    if (_EMAIL_RE.search(c) or _HANDLE_RE.search(c) or _REDDIT_USER_RE.search(c) or _PHONE_RE.search(c)
            or _PLACEHOLDER_RE.search(c)):
        add("detail", "contact details")
    if has_street_address(c):
        add("detail", "street addresses")
    if not people and not details:
        return None
    cats = ", ".join(people + details)
    if people and details:
        what = "descriptions of people and personal details"
    elif people:
        what = "descriptions of people"
    else:
        what = "personal details"
    return f"Notes must describe the place and situation only - remove {what} ({cats})."
