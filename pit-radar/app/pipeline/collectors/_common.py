"""Small helpers shared by the collectors: allow-list field access, post-URL rewriting and checks.

Everything here reads only the keys it is asked for (allow-list, privacy rule R2). Nothing is logged.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import parse_qs, urlsplit, urlunsplit

from app.pipeline.types import PlannedQuery, QueryLogEntry

UTC = dt.timezone.utc

# Scope precedence li_scoped > regional > general lives in sources.scope_max.


# ---------------------------------------------------------------------------
# Allow-list field access
# ---------------------------------------------------------------------------
def dig(obj: Any, path: str) -> Any:
    """obj['a']['b'] for path 'a.b'; None when any step is missing or not a mapping."""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def first_value(obj: Any, paths: Sequence[str]) -> Any:
    """First present, non-empty value among dotted `paths` (0 and False count as present)."""
    for p in paths:
        v = dig(obj, p)
        if v is None:
            continue
        if isinstance(v, (str, list, dict, tuple)) and not v:
            continue
        return v
    return None


def first_str(obj: Any, paths: Sequence[str], *, max_len: int = 24000) -> str:
    """First non-empty STRING among `paths`, stripped and cut to max_len (non-strings are skipped)."""
    for p in paths:
        v = dig(obj, p)
        if isinstance(v, str) and v.strip():
            return v.strip()[:max_len]
    return ""


def as_float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def iso_z(d: dt.datetime) -> str:
    """UTC ISO 8601 with milliseconds and a Z suffix (Exa/SocialCrawl friendly)."""
    d = d.astimezone(UTC) if d.tzinfo else d.replace(tzinfo=UTC)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
def _split(url: str):
    u = str(url or "").strip()
    if u.startswith("//"):
        u = "https:" + u
    try:
        return urlsplit(u)
    except ValueError:
        return None


def host(url: str) -> str:
    parts = _split(url)
    h = (parts.hostname or "") if parts else ""
    h = h.rstrip(".").lower()
    return h[4:] if h.startswith("www.") else h


def is_http_url(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    parts = _split(url)
    return bool(parts and parts.scheme in ("http", "https") and parts.hostname)


def strip_query(url: str) -> str:
    """Drop the query string and fragment (provider tracking / session params)."""
    parts = _split(url)
    if not parts or not parts.scheme:
        return str(url or "").strip()
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


_X_STATUS = re.compile(r"/status(?:es)?/(\d{5,25})")
_IG_CODE = re.compile(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]{5,64})")
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_PATH = re.compile(r"/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})")
_AT_URI = re.compile(r"^at://(did:[a-z0-9]+:[A-Za-z0-9._:%-]+)/app\.bsky\.feed\.post/([A-Za-z0-9]+)$")
_DIGITS = re.compile(r"^\d{5,25}$")


def youtube_id(post_id: str, url: str) -> Optional[str]:
    pid = (post_id or "").strip()
    if _YT_ID.match(pid):
        return pid
    parts = _split(url)
    if not parts:
        return None
    h = (parts.hostname or "").lower()
    if h.endswith("youtu.be"):
        seg = parts.path.strip("/").split("/")[0] if parts.path else ""
        return seg if _YT_ID.match(seg) else None
    if "youtube" in h:
        v = (parse_qs(parts.query).get("v") or [""])[0]
        if _YT_ID.match(v):
            return v
        m = _YT_PATH.search(parts.path or "")
        if m:
            return m.group(1)
    return None


def x_status_id(post_id: str, url: str) -> Optional[str]:
    pid = (post_id or "").strip()
    if _DIGITS.match(pid):
        return pid
    m = _X_STATUS.search(url or "")
    return m.group(1) if m else None


def instagram_code(shortcode: str, url: str) -> Optional[str]:
    sc = (shortcode or "").strip()
    if sc and re.match(r"^[A-Za-z0-9_-]{5,64}$", sc) and not sc.isdigit():
        return sc
    m = _IG_CODE.search(url or "")
    return m.group(1) if m else None


def bluesky_url(post_id: str, url: str) -> Optional[str]:
    """Provider post URL (minus query) or one built from an at:// URI. [verify] which one the provider sends."""
    if is_http_url(url) and "/post/" in url:
        return strip_query(url)
    m = _AT_URI.match((post_id or "").strip())
    if m:
        return f"https://bsky.app/profile/{m.group(1)}/post/{m.group(2)}"
    return None


def reddit_thread_url(permalink: str, url: str, post_id: str) -> Optional[str]:
    """Reddit thread/comment permalink: relative permalinks get https://www.reddit.com; a link post's
    external `url` is NOT the thread, so fall back to /comments/<id36>."""
    pl = (permalink or "").strip()
    if pl.startswith("/"):
        return strip_query("https://www.reddit.com" + pl)
    if is_http_url(pl) and "reddit.com" in host(pl):
        return strip_query(pl)
    if is_http_url(url) and (host(url) == "reddit.com" or host(url).endswith(".reddit.com")) and "/comments/" in url:
        return strip_query(url)
    pid = (post_id or "").strip()
    if pid.startswith("t3_"):
        pid = pid[3:]
    if re.match(r"^[a-z0-9]{3,12}$", pid):
        return f"https://www.reddit.com/comments/{pid}"
    return None


def is_profile_url(platform: str, url: str) -> bool:
    """True when `url` points at an account/profile rather than a post (R2: never store those)."""
    parts = _split(url)
    if not parts or not parts.hostname:
        return True
    path = (parts.path or "/").lower()
    h = (parts.hostname or "").lower()
    if platform == "reddit":
        return path.startswith(("/user/", "/u/")) or "/comments/" not in path
    if platform == "x":
        return "/status/" not in path
    if platform == "instagram":
        return not re.match(r"^/(p|reel|reels|tv)/", path)
    if platform == "tiktok":
        return not re.search(r"/(video|photo)/\d+", path) and not path.startswith("/v/")
    if platform == "threads":
        return "/post/" not in path
    if platform == "bluesky":
        return "/post/" not in path
    if platform == "youtube":
        if h.endswith("youtu.be"):
            return False
        return not (path.startswith("/watch") or path.startswith("/shorts/") or path.startswith("/live/"))
    if platform == "facebook":
        if "profile.php" in path:
            return True
        markers = ("/posts/", "/permalink", "/videos/", "/reel/", "/photo", "story.php", "/watch",
                   "/share/p/", "/share/v/", "/share/r/", "/events/")
        return not any(m in path for m in markers)
    # news / web: a page URL; author index pages are filtered by the prefilter/classifier
    return False


def rewrite_post_url(platform: str, *, url: str, post_id: str = "", shortcode: str = "",
                     permalink: str = "") -> Optional[str]:
    """Handle-free canonical post URLs where a stable form is known; else the provider URL minus query.
    Returns None when no usable, non-profile post URL can be formed."""
    out: Optional[str] = None
    if platform == "x":
        sid = x_status_id(post_id, url)
        out = f"https://x.com/i/web/status/{sid}" if sid else None
    elif platform == "instagram":
        code = instagram_code(shortcode, url)
        out = f"https://www.instagram.com/p/{code}/" if code else None
    elif platform == "youtube":
        vid = youtube_id(post_id, url)
        out = f"https://www.youtube.com/watch?v={vid}" if vid else None
    elif platform == "reddit":
        out = reddit_thread_url(permalink, url, post_id)
    elif platform == "bluesky":
        out = bluesky_url(post_id, url)
    elif platform in ("tiktok", "threads", "facebook"):
        # [verify] no documented handle-free permalink form; keep the provider's post URL minus query
        out = strip_query(url) if is_http_url(url) else None
    else:
        out = url if is_http_url(url) else None
    if not out or is_profile_url(platform, out):
        return None
    return out


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
_HASHTAG_RE = re.compile(r"(?<![\w&])#([A-Za-z][\w]{1,60})")


def hashtags_from(values: Any, text: str = "", *, limit: int = 30) -> list[str]:
    """Hashtag names (no '#') from a structured list (strings or {title|name|tag|hashtag_name}) or, when
    none, from the text itself."""
    out: list[str] = []

    def add(s: Any) -> None:
        if isinstance(s, str):
            t = s.strip().lstrip("#").strip()
            if t and len(t) <= 100 and t not in out:
                out.append(t)

    if isinstance(values, (list, tuple)):
        for v in values:
            if isinstance(v, dict):
                for k in ("title", "name", "tag", "hashtag_name", "hashtag"):
                    if isinstance(v.get(k), str):
                        add(v[k])
                        break
            else:
                add(v)
            if len(out) >= limit:
                break
    elif isinstance(values, str):
        for part in re.split(r"[,\s]+", values):
            add(part)
    if not out and text:
        for m in _HASHTAG_RE.finditer(text):
            add(m.group(1))
            if len(out) >= limit:
                break
    return out[:limit]


def place_tag_name(value: Any) -> Optional[str]:
    """A platform geotag NAME only (string, or name/title/full_name of a dict). Never coordinates."""
    if isinstance(value, str):
        s = value.strip()
        return s[:200] if s and not re.match(r"^[-+]?\d+(\.\d+)?\s*,\s*[-+]?\d+(\.\d+)?$", s) else None
    if isinstance(value, dict):
        for k in ("name", "title", "full_name", "short_name", "location_name"):
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:200]
    return None


def truthy_media(obj: Any, paths: Iterable[str]) -> bool:
    for p in paths:
        v = dig(obj, p)
        if v is None or v is False:
            continue
        if isinstance(v, (list, tuple, dict, str)) and not v:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v <= 0:
            continue
        return True
    return False


def query_label(q: PlannedQuery) -> str:
    """What to show in query_log / item context for a planned query (never post content)."""
    if q.query:
        return q.query
    if q.capability == "subreddit_new" and q.subreddit:
        return f"r/{q.subreddit} (new)"
    if q.capability == "post_comments":
        return f"comments r/{q.subreddit}" if q.subreddit else "comments"
    if q.subreddit:
        return f"r/{q.subreddit}"
    return q.capability


def log_entry(q: PlannedQuery, *, provider: str, endpoint: str, status: str, http_status: Optional[int] = None,
              items: int = 0, credits: float = 0.0, cost_usd: float = 0.0, latency_ms: Optional[int] = None,
              error_type: Optional[str] = None, request_id: Optional[str] = None,
              at: Optional[dt.datetime] = None) -> QueryLogEntry:
    e = QueryLogEntry(provider=provider, endpoint=endpoint, query_text=query_label(q)[:500], status=status,  # type: ignore[arg-type]
                      place_id=q.place_id, http_status=http_status, items=items, credits=float(credits or 0.0),
                      cost_usd=float(cost_usd or 0.0), latency_ms=latency_ms, error_type=error_type,
                      request_id=(str(request_id)[:120] if request_id else None))
    if at is not None:
        e.at = at
    return e


def sort_by_priority(queries: Iterable[PlannedQuery]) -> list[PlannedQuery]:
    """Highest priority first; stable for equal priorities (planner order)."""
    indexed = list(enumerate(queries))
    indexed.sort(key=lambda t: (-int(t[1].priority or 0), t[0]))
    return [q for _, q in indexed]
