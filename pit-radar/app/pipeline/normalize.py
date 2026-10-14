"""Provider payload -> NormalizedItem helpers (docs/04 §2.4, §5).

Collectors map provider payloads with allow-lists and then call `build_item`, which takes no author
fields at all (R2). Everything here is pure and deterministic; nothing is logged except counts/reasons.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import math
import re
from email.utils import parsedate_to_datetime
from typing import Any, Optional, get_args
from urllib.parse import unquote_plus, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from app.pipeline import textnorm
from app.pipeline.types import DateSource, ItemContext, NormalizedItem, Platform, Provider

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 6000
MAX_TITLE_CHARS = 1000
MIN_YEAR = 2005                   # anything older is a parsing accident for this app
_RAW_BODY_CAP = MAX_TEXT_CHARS * 4  # pre-cut huge provider bodies before cleaning/scrubbing (bounded work)
_MAX_DATE_STRING = 256

_PROVIDERS = frozenset(get_args(Provider))
_PLATFORMS = frozenset(get_args(Platform))
_UTC = dt.timezone.utc

# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "igshid", "si", "s", "ref", "ref_src", "mc_cid", "mc_eid"})
_REDDIT_HOSTS = frozenset({"reddit.com", "old.reddit.com", "np.reddit.com", "m.reddit.com", "new.reddit.com",
                           "i.reddit.com"})
_X_HOSTS = frozenset({"twitter.com", "www.twitter.com", "mobile.twitter.com", "m.twitter.com", "www.x.com",
                      "mobile.x.com"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_HAS_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_BARE_HOST = re.compile(r"^(?:[\w-]+\.)+[a-zA-Z]{2,}(?::\d+)?(?:[/?#]|$)")


def _strip_tracking(query: str) -> str:
    kept: list[str] = []
    for seg in query.split("&"):
        if not seg:
            continue
        key = unquote_plus(seg.split("=", 1)[0]).strip().lower()
        if key.startswith("utm_") or key in _TRACKING_PARAMS:
            continue
        kept.append(seg)
    return "&".join(kept)


def canonical_url(url: str) -> str:
    """Lowercase scheme/host; drop userinfo, default ports, fragment and tracking params (utm_*, fbclid,
    gclid, igshid, si, s, ref, ref_src, mc_cid, mc_eid); old./np./m.reddit.com -> www.reddit.com;
    twitter.com/mobile.twitter.com/www.x.com -> x.com; drop a trailing slash. Path case is kept.
    Non-http(s) or unparseable input is returned stripped and otherwise unchanged."""
    if not url:
        return ""
    u = str(url).strip()
    if not u:
        return ""
    if u.startswith("//"):
        u = "https:" + u
    elif not _HAS_SCHEME.match(u):
        if not _BARE_HOST.match(u):
            return u
        u = "https://" + u
    try:
        parts = urlsplit(u)
        port = parts.port
    except ValueError:
        return u
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        return u
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return u
    if host in _REDDIT_HOSTS:
        host = "www.reddit.com"
    elif host in _X_HOSTS:
        host = "x.com"
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc += f":{port}"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, _strip_tracking(parts.query), ""))


def registrable_domain(url: str) -> Optional[str]:
    """'https://www.newsday.com/x' -> 'newsday.com'. Host of the canonical URL, lowercased, without a
    leading www./m./amp.; other subdomains are kept ('longisland.news12.com'). None if there is no host."""
    if not url:
        return None
    c = canonical_url(url)
    try:
        host = urlsplit(c).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.rstrip(".").lower()
    changed = True
    while changed:
        changed = False
        for prefix in ("www.", "m.", "amp."):
            if host.startswith(prefix) and host.count(".") >= 2:
                host = host[len(prefix):]
                changed = True
    return host or None


# ---------------------------------------------------------------------------
# Scrubbing (R2/R4)
# ---------------------------------------------------------------------------
_URL_RE = re.compile(r"""(?:\bhttps?://|(?<![\w.@/-])www\.)[^\s<>"'\[\]{}|\\^`]+""", re.IGNORECASE)
_BARE_URL_RE = re.compile(
    r"""(?<![\w.@/-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"""
    r"""(?:com|org|net|gov|edu|io|co|us|me|ly|tv|ee|gg|app|news|info|biz|site|page|link|social)"""
    r"""/[^\s<>"'\[\]{}|\\^`]*""",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?![\w-])", re.IGNORECASE)
_HANDLE_RE = re.compile(r"(?<![\w@/.])@\w(?:[\w.-]*\w)?")
_REDDIT_USER_RE = re.compile(r"(?<![\w/])/?u/[A-Za-z0-9_-]{2,}", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<![\w+])(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)")
_TRAILING_PUNCT = ".,;:!?'\")]}*_"


def _link_sub(m: re.Match[str]) -> str:
    s = m.group(0)
    trail = ""
    while s and s[-1] in _TRAILING_PUNCT:
        if s[-1] == ")" and s.count("(") >= s.count(")"):
            break                         # balanced parenthesis is part of the URL (e.g. wiki links)
        trail = s[-1] + trail
        s = s[:-1]
    return "[link]" + trail


def scrub_text(text: str) -> str:
    """Replace URLs, emails, @handles, reddit u/names and US phone numbers with [link]/[email]/[handle]/
    [handle]/[phone] (R2/R4), in that order. Times ('10:30 pm'), dates and years are left alone.
    Idempotent; whitespace is not changed."""
    if not text:
        return ""
    t = _URL_RE.sub(_link_sub, text)
    t = _BARE_URL_RE.sub(_link_sub, t)
    t = _EMAIL_RE.sub("[email]", t)
    t = _HANDLE_RE.sub("[handle]", t)
    t = _REDDIT_USER_RE.sub("[handle]", t)
    t = _PHONE_RE.sub("[phone]", t)
    return t


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
_NUMERIC = re.compile(r"^[+]?\d+(?:\.\d+)?$")
_ISO_FRAC = re.compile(r"(\.\d{6})\d+")
_NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "few": 3, "a few": 3, "several": 3, "couple": 2,
    "a couple": 2, "couple of": 2, "a couple of": 2,
    # Spanish ("hace un día")
    "un": 1, "una": 1, "unos": 3, "unas": 3,
}
_N = r"\d+(?:\.\d+)?|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|a\s+few|few|several|(?:a\s+)?couple(?:\s+of)?"
_UNITS_EN = (r"seconds|second|secs|sec|s|minutes|minute|mins|min|m|hours|hour|hrs|hr|h|days|day|d|"
             r"weeks|week|wks|wk|w|months|month|mos|mo|years|year|yrs|yr|y")
_REL_AGO = re.compile(rf"(?<!\w)(?P<n>{_N})\s*(?P<unit>{_UNITS_EN})\.?\s+ago(?!\w)", re.IGNORECASE)
_REL_COMPACT = re.compile(rf"^(?P<n>\d+)\s*(?P<unit>{_UNITS_EN})\.?$", re.IGNORECASE)
_REL_ES = re.compile(r"(?<!\w)hace\s+(?P<n>\d+|un|una|unos|unas)\s+"
                     r"(?P<unit>segundos?|minutos?|horas?|d[ií]as?|semanas?|mes(?:es)?|años?)(?!\w)", re.IGNORECASE)
_REL_NOW = re.compile(r"^(?:just\s+now|right\s+now|now|moments?\s+ago|a\s+moment\s+ago|seconds\s+ago|"
                      r"a\s+few\s+seconds\s+ago|today|hoy|ahora(?:\s+mismo)?)\b", re.IGNORECASE)
_REL_YESTERDAY = re.compile(r"^(?:yesterday|ayer)\b", re.IGNORECASE)
_REL_LAST = re.compile(r"^last\s+(?P<unit>week|month|year)$", re.IGNORECASE)

_UNIT_SECONDS: dict[str, int] = {}
for _names, _secs in (
    (("s", "sec", "secs", "second", "seconds", "segundo", "segundos"), 1),
    (("m", "min", "mins", "minute", "minutes", "minuto", "minutos"), 60),
    (("h", "hr", "hrs", "hour", "hours", "hora", "horas"), 3600),
    (("d", "day", "days", "dia", "dias", "día", "días"), 86400),
    (("w", "wk", "wks", "week", "weeks", "semana", "semanas"), 7 * 86400),
    (("mo", "mos", "month", "months", "mes", "meses"), 30 * 86400),
    (("y", "yr", "yrs", "year", "years", "año", "años"), 365 * 86400),
):
    for _n in _names:
        _UNIT_SECONDS[_n] = _secs

_TZ_ABBR = {
    "UTC": _UTC, "GMT": _UTC, "Z": _UTC,
    "EST": dt.timezone(dt.timedelta(hours=-5)), "EDT": dt.timezone(dt.timedelta(hours=-4)),
}
_NY = ZoneInfo("America/New_York")
_TRAILING_TZ = re.compile(r"\s*\(?\b(UTC|GMT|EST|EDT|ET|Z)\)?$", re.IGNORECASE)
_WEEKDAY = re.compile(r"^(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\.?,?\s+", re.IGNORECASE)
_ORDINAL = re.compile(r"(\d)(?:st|nd|rd|th)\b", re.IGNORECASE)
_AMPM = re.compile(r"\b([ap])\.?\s?m\.?(?!\w)", re.IGNORECASE)
_EXTRA_FORMATS = (
    "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y", "%d %B, %Y", "%d %b, %Y",
    "%B %d, %Y %I:%M %p", "%b %d, %Y %I:%M %p", "%B %d, %Y, %I:%M %p", "%b %d, %Y, %I:%M %p",
    "%B %d, %Y %H:%M", "%b %d, %Y %H:%M", "%d %b %Y %H:%M", "%d %B %Y %H:%M",
    "%Y/%m/%d", "%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S",
    "%m/%d/%Y", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%y",
)


def _aware(d: dt.datetime) -> dt.datetime:
    return d.replace(tzinfo=_UTC) if d.tzinfo is None else d.astimezone(_UTC)


def _from_epoch(x: float) -> Optional[dt.datetime]:
    if not math.isfinite(x) or x <= 0:
        return None
    if x >= 1e17:
        x /= 1e9              # nanoseconds
    elif x >= 1e14:
        x /= 1e6              # microseconds
    elif x >= 1e11:
        x /= 1e3              # milliseconds
    return dt.datetime.fromtimestamp(x, tz=_UTC)


def _parse_relative(s: str, now: dt.datetime) -> Optional[dt.datetime]:
    if _REL_NOW.match(s):
        return now
    if _REL_YESTERDAY.match(s):
        return now - dt.timedelta(days=1)
    m = _REL_LAST.match(s)
    if m:
        return now - dt.timedelta(seconds=_UNIT_SECONDS[m.group("unit").lower()])
    m = _REL_AGO.search(s) or _REL_COMPACT.match(s) or _REL_ES.search(s)
    if not m:
        return None
    n_raw = re.sub(r"\s+", " ", m.group("n").lower())
    n = float(n_raw) if n_raw[0].isdigit() else float(_NUM_WORDS.get(n_raw, 0))
    unit = m.group("unit").lower()
    secs = _UNIT_SECONDS.get(unit)
    if not secs or n < 0:
        return None
    return now - dt.timedelta(seconds=n * secs)


def _parse_absolute_string(s: str) -> Optional[dt.datetime]:
    iso = s[:-1] + "Z" if s.endswith("z") else s
    iso = _ISO_FRAC.sub(r"\1", iso)
    try:
        return dt.datetime.fromisoformat(iso)
    except ValueError:
        pass
    try:
        d = parsedate_to_datetime(s)          # RFC 2822 (RSS, HTTP dates)
        if d is not None:
            return d
    except (TypeError, ValueError, IndexError):
        pass
    t = _WEEKDAY.sub("", s.strip())
    t = re.sub(r"\s+at\s+", " ", t, flags=re.IGNORECASE)
    t = _ORDINAL.sub(r"\1", t)
    t = re.sub(r"\bSept\b", "Sep", t, flags=re.IGNORECASE)
    t = re.sub(r"\b([A-Za-z]{3,9})\.", r"\1", t)          # "Oct." -> "Oct"
    t = _AMPM.sub(lambda m: m.group(1).upper() + "M", t)
    tz: Optional[dt.tzinfo] = None
    m = _TRAILING_TZ.search(t)
    if m:
        abbr = m.group(1).upper()
        tz = _NY if abbr == "ET" else _TZ_ABBR.get(abbr)
        t = t[:m.start()].strip()
    for fmt in _EXTRA_FORMATS:
        try:
            d = dt.datetime.strptime(t, fmt)
        except ValueError:
            continue
        if tz is not None:
            d = d.replace(tzinfo=tz)
        return d
    return None


def parse_datetime(value: Any, *, now: dt.datetime) -> tuple[Optional[dt.datetime], Optional[DateSource]]:
    """Unix seconds/ms (number or numeric string), ISO 8601 (incl. Z and offsets), RFC 2822, common news
    formats ('Oct 14, 2026'), or relative English/Spanish ('3 days ago', 'an hour ago', 'yesterday',
    'just now', '2d', '5h', '3w', '10 min ago', 'hace 2 horas') -> (aware UTC datetime, date_source).
    Relative values (resolved against `now`) return 'relative'; absolute values return 'platform' (callers
    pass their own date_source to build_item when it differs). Naive values are taken as UTC. Years before
    2005 and unparseable input -> (None, None)."""
    now_utc = _aware(now) if isinstance(now, dt.datetime) else dt.datetime.now(_UTC)
    try:
        d, source = _parse_any(value, now_utc)
    except (ValueError, OverflowError, OSError, TypeError, ArithmeticError):
        return None, None
    if d is None:
        return None, None
    try:
        d = _aware(d)
    except (ValueError, OverflowError, OSError):
        return None, None
    if d.year < MIN_YEAR:
        return None, None
    return d, source


def _parse_any(value: Any, now: dt.datetime) -> tuple[Optional[dt.datetime], Optional[DateSource]]:
    if value is None or isinstance(value, bool):
        return None, None
    if isinstance(value, dt.datetime):
        return value, "platform"
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=_UTC), "platform"
    if isinstance(value, (int, float)):
        return _from_epoch(float(value)), "platform"
    if not isinstance(value, str):
        return None, None
    s = textnorm.clean(value[:_MAX_DATE_STRING * 4])
    if not s or len(s) > _MAX_DATE_STRING:
        return None, None
    if _NUMERIC.match(s):
        return _from_epoch(float(s)), "platform"
    d = _parse_absolute_string(s)
    if d is not None:
        return d, "platform"
    d = _parse_relative(s, now)
    if d is not None:
        return d, "relative"
    return None, None


# ---------------------------------------------------------------------------
# Item assembly
# ---------------------------------------------------------------------------
def _clean_multiline(text: str) -> str:
    """textnorm.clean() per line, keeping paragraph breaks (at most one blank line in a row)."""
    if not text:
        return ""
    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    blank = False
    for line in lines:
        c = textnorm.clean(line)
        if not c:
            if out and not blank:
                out.append("")
            blank = True
            continue
        out.append(c)
        blank = False
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _truncate_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if not text[limit].isspace():
        ws = max(cut.rfind(" "), cut.rfind("\n"))
        if ws >= int(limit * 0.8):
            cut = cut[:ws]
    return cut.rstrip()


def build_item(
    *,
    provider: str,
    platform: str,
    url: str,
    platform_post_id: str,
    published_at: Optional[dt.datetime],
    date_source: DateSource,
    collected_at: dt.datetime,
    title: str,
    body: str,
    context: ItemContext,
    lang: Optional[str] = None,
    media_present: bool = False,
) -> Optional[NormalizedItem]:
    """Assemble a NormalizedItem: canonical URL, scrubbed title+body text (<= MAX_TEXT_CHARS), item_hash =
    sha256(canonical_url), content_hash = textnorm.content_fingerprint(text), context.outlet_domain filled.
    Returns None when published_at is None (F3) or text is empty. NEVER accepts author fields.

    text = title + blank line + body (the title is not repeated when the body already starts with it),
    each line textnorm.clean()-ed, scrubbed, then cut at a word boundary to MAX_TEXT_CHARS.
    platform_post_id falls back to sha256(canonical_url)[:32]."""
    if published_at is None or not isinstance(published_at, dt.datetime):
        return None
    if provider not in _PROVIDERS or platform not in _PLATFORMS:
        logger.warning("normalize: rejected item with unknown provider/platform (%s/%s)", provider, platform)
        return None
    curl = canonical_url(url or "")
    if not curl:
        return None

    title_c = scrub_text(textnorm.clean(str(title or "")[:_RAW_BODY_CAP]))
    body_c = scrub_text(_clean_multiline(str(body or "")[:_RAW_BODY_CAP]))
    if title_c and body_c:
        nt, nb = textnorm.norm_for_match(title_c), textnorm.norm_for_match(body_c)
        text = body_c if nb.startswith(nt) else f"{title_c}\n\n{body_c}"
    else:
        text = title_c or body_c
    text = _truncate_words(text, MAX_TEXT_CHARS)
    if not text.strip():
        return None

    item_hash = textnorm.sha256_hex(curl)
    post_id = str(platform_post_id).strip() if platform_post_id is not None else ""
    if not post_id:
        post_id = item_hash[:32]

    ctx = dataclasses.replace(
        context,
        hashtags=[str(h) for h in (context.hashtags or [])],
        parent_title=(scrub_text(textnorm.clean(context.parent_title))[:MAX_TITLE_CHARS] or None)
        if context.parent_title else None,
        platform_place_tag=(textnorm.clean(context.platform_place_tag) or None) if context.platform_place_tag else None,
        outlet_domain=context.outlet_domain or registrable_domain(curl),
    )
    lang_c = (lang or "").strip().lower()[:8] or None

    return NormalizedItem(
        item_hash=item_hash,
        content_hash=textnorm.content_fingerprint(text),
        provider=provider,              # type: ignore[arg-type]
        platform=platform,              # type: ignore[arg-type]
        url=curl,
        platform_post_id=post_id,
        published_at=_aware(published_at),
        date_source=date_source,
        collected_at=_aware(collected_at),
        text=text,
        context=ctx,
        title=_truncate_words(title_c, MAX_TITLE_CHARS),
        lang=lang_c,
        media_present=bool(media_present),
    )
