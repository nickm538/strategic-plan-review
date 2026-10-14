"""config/sources.yaml loader (docs/04).

`SourcesConfig` is the effective sources configuration: the YAML file plus admin overrides (settings key
`sources.overrides`, PUT /api/admin/sources). Overrides deep-merge into the file: dicts merge key by key,
lists and scalars replace the file value wholesale (so an override of `subreddits` is the full new list).

Pure and deterministic; never logs post content.
"""
from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

import yaml

from app.paths import CONFIG_DIR
from app.pipeline.types import SourceScope

logger = logging.getLogger(__name__)

DEFAULT_PATH = CONFIG_DIR / "sources.yaml"

# Scope precedence when several rules apply (an LI subreddit post found by a general keyword search is li_scoped).
_SCOPE_RANK: dict[str, int] = {"general": 0, "regional": 1, "li_scoped": 2}

# Regional domains whose Long Island coverage lives under one section only (sources.yaml notes). A domain
# entry may also carry an explicit `path_prefix` (string or list) which takes precedence over this table.
_REGIONAL_PATH_RULES: dict[str, tuple[str, ...]] = {"cbsnews.com": ("/newyork",)}

_REDDIT_HOSTS = frozenset({"reddit.com", "old.reddit.com", "np.reddit.com", "m.reddit.com", "new.reddit.com"})

# Top-level keys an admin override may touch (PUT /api/admin/sources). Anything else is rejected.
OVERRIDABLE_TOP_KEYS = frozenset({
    "socialcrawl", "subreddits", "excluded_subreddits", "news_domains", "hashtags", "exa", "firecrawl",
})


def deep_merge(base: Any, override: Any) -> Any:
    """Return a new value: dicts merge recursively; lists and scalars from `override` replace `base`."""
    if isinstance(base, dict) and isinstance(override, dict):
        out = {k: copy.deepcopy(v) for k, v in base.items()}
        for k, v in override.items():
            out[k] = deep_merge(out[k], v) if k in out else copy.deepcopy(v)
        return out
    return copy.deepcopy(override)


def scope_max(*scopes: Optional[str]) -> SourceScope:
    """The most specific of several scopes (li_scoped > regional > general)."""
    best = "general"
    for s in scopes:
        if s in _SCOPE_RANK and _SCOPE_RANK[s] > _SCOPE_RANK[best]:
            best = s
    return best  # type: ignore[return-value]


def host_of(url: str) -> Optional[str]:
    """Lowercased host without a leading 'www.'; None when the URL has no parseable host."""
    if not url:
        return None
    u = str(url).strip()
    if u.startswith("//"):
        u = "https:" + u
    elif "://" not in u:
        u = "https://" + u
    try:
        host = urlsplit(u).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.rstrip(".").lower()
    return host[4:] if host.startswith("www.") else host


def host_matches(host: Optional[str], domain: str) -> bool:
    """True if `host` equals `domain` or is a subdomain of it (case-insensitive)."""
    if not host or not domain:
        return False
    d = domain.strip().lower().lstrip(".")
    if d.startswith("www."):
        d = d[4:]
    return host == d or host.endswith("." + d)


def _path_of(url: str) -> str:
    u = str(url or "").strip()
    if "://" not in u and not u.startswith("//"):
        u = "https://" + u
    try:
        return urlsplit(u).path or "/"
    except ValueError:
        return "/"


def subreddit_from_url(url: str) -> Optional[str]:
    """'https://www.reddit.com/r/longisland/comments/..' -> 'longisland' (None for non-reddit URLs)."""
    host = host_of(url)
    if not host or not (host in _REDDIT_HOSTS or host.endswith(".reddit.com")):
        return None
    parts = [p for p in _path_of(url).split("/") if p]
    if len(parts) >= 2 and parts[0].lower() == "r":
        return parts[1]
    return None


def _norm_sub(name: Any) -> str:
    s = str(name or "").strip()
    for prefix in ("/r/", "r/"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
    return s.strip("/").strip()


def _entry_enabled(entry: Any) -> bool:
    return not isinstance(entry, dict) or entry.get("enabled", True) is not False


def _domain_of(entry: Any) -> Optional[str]:
    if isinstance(entry, str):
        d = entry
    elif isinstance(entry, dict):
        d = entry.get("domain") or ""
    else:
        return None
    d = str(d).strip().lower().lstrip(".")
    if d.startswith("www."):
        d = d[4:]
    return d or None


class SourcesConfig:
    """Effective sources configuration (file + overrides). See config/sources.yaml for every key."""

    raw: dict[str, Any]

    def __init__(self, raw: Optional[dict[str, Any]] = None, *, overrides: Optional[dict[str, Any]] = None) -> None:
        self.raw = raw if isinstance(raw, dict) else {}
        self.overrides: dict[str, Any] = copy.deepcopy(overrides or {})

    @classmethod
    def load(cls, path: Path = CONFIG_DIR / "sources.yaml", overrides: Optional[dict[str, Any]] = None) -> "SourcesConfig":
        """Read the YAML file and deep-merge `overrides` (dicts merge, lists replace). A broken FILE raises;
        an override that is not a mapping is ignored with a warning (the file stays in force)."""
        with open(path, "r", encoding="utf-8") as fh:
            base = yaml.safe_load(fh) or {}
        if not isinstance(base, dict):
            raise ValueError(f"{path.name}: top level must be a mapping")
        applied: dict[str, Any] = {}
        if overrides:
            if not isinstance(overrides, dict):
                logger.warning("sources: ignored overrides that are not a mapping (%s)", type(overrides).__name__)
            else:
                errors = validate_overrides(overrides)
                if errors:
                    logger.warning("sources: ignored invalid overrides: %s", "; ".join(errors))
                else:
                    applied = overrides
        merged = deep_merge(base, applied) if applied else base
        return cls(merged, overrides=applied)

    @classmethod
    def from_dict(cls, data: dict[str, Any], overrides: Optional[dict[str, Any]] = None) -> "SourcesConfig":
        """Build from an in-memory mapping (tests, admin previews)."""
        merged = deep_merge(data, overrides) if overrides else copy.deepcopy(data)
        return cls(merged, overrides=overrides)

    def to_dict(self) -> dict[str, Any]:
        """The effective configuration as plain data (GET /api/admin/sources)."""
        return copy.deepcopy(self.raw)

    # -- subreddits --------------------------------------------------------------
    def _subreddit_entries(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for e in self.raw.get("subreddits") or []:
            if isinstance(e, str):
                out.append({"name": e, "enabled": True})
            elif isinstance(e, dict) and e.get("name"):
                out.append(e)
        return out

    def enabled_subreddits(self) -> list[str]:
        """Enabled subreddit names in file order (original casing), never an excluded one, no duplicates."""
        excluded = self.excluded_subreddits()
        seen: set[str] = set()
        out: list[str] = []
        for e in self._subreddit_entries():
            if not _entry_enabled(e):
                continue
            name = _norm_sub(e.get("name"))
            low = name.lower()
            if not name or low in excluded or low in seen:
                continue
            seen.add(low)
            out.append(name)
        return out

    def excluded_subreddits(self) -> set[str]:
        """Lowercased names that must never be collected."""
        out: set[str] = set()
        for e in self.raw.get("excluded_subreddits") or []:
            name = e if isinstance(e, str) else (e.get("name") if isinstance(e, dict) else None)
            n = _norm_sub(name).lower()
            if n:
                out.add(n)
        return out

    def is_excluded_subreddit(self, name: Optional[str]) -> bool:
        return bool(name) and _norm_sub(name).lower() in self.excluded_subreddits()

    def subreddit_scope(self, name: Optional[str]) -> Optional[SourceScope]:
        """source_scope configured for an enabled subreddit (default li_scoped), else None."""
        if not name:
            return None
        low = _norm_sub(name).lower()
        if low in self.excluded_subreddits():
            return None
        for e in self._subreddit_entries():
            if _norm_sub(e.get("name")).lower() == low and _entry_enabled(e):
                scope = e.get("source_scope", "li_scoped")
                return scope if scope in _SCOPE_RANK else "li_scoped"  # type: ignore[return-value]
        return None

    # -- domains and scope -------------------------------------------------------
    def _domain_entries(self, scope: str) -> list[Any]:
        nd = self.raw.get("news_domains") or {}
        entries = nd.get(scope) if isinstance(nd, dict) else None
        return [e for e in (entries or []) if _entry_enabled(e) and _domain_of(e)]

    def news_domains(self, scope: str) -> list[str]:
        """Enabled domains for 'li_scoped' or 'regional', in file order."""
        out: list[str] = []
        for e in self._domain_entries(scope):
            d = _domain_of(e)
            if d and d not in out:
                out.append(d)
        return out

    def li_scoped_domains(self) -> list[str]:
        return self.news_domains("li_scoped")

    def regional_domains(self) -> list[str]:
        return self.news_domains("regional")

    def is_news_url(self, url: str) -> bool:
        """True if the URL's host is (a subdomain of) any configured news domain, li_scoped or regional."""
        host = host_of(url)
        return any(host_matches(host, d) for d in self.li_scoped_domains() + self.regional_domains())

    def _regional_match(self, host: Optional[str], url: str) -> bool:
        for e in self._domain_entries("regional"):
            d = _domain_of(e)
            if not d or not host_matches(host, d):
                continue
            prefixes: tuple[str, ...] = ()
            if isinstance(e, dict) and e.get("path_prefix"):
                pp = e["path_prefix"]
                prefixes = tuple(pp) if isinstance(pp, (list, tuple)) else (str(pp),)
            elif d in _REGIONAL_PATH_RULES:
                prefixes = _REGIONAL_PATH_RULES[d]
            if not prefixes:
                return True
            path = _path_of(url).lower()
            if any(path.startswith(p.lower()) for p in prefixes):
                return True
        return False

    def scope_for(self, *, url: str, subreddit: Optional[str] = None) -> SourceScope:
        """li_scoped for an enabled LI subreddit (given, or read from a reddit URL) or a host equal to / a
        subdomain of an li_scoped news domain; regional for regional domains (patch.com stays regional,
        since its LI town pages cannot be told apart without the gazetteer); else general."""
        sub = subreddit or subreddit_from_url(url or "")
        if sub:
            sc = self.subreddit_scope(sub)
            if sc:
                return sc
        host = host_of(url or "")
        if host:
            if any(host_matches(host, d) for d in self.li_scoped_domains()):
                return "li_scoped"
            if self._regional_match(host, url):
                return "regional"
        return "general"

    # -- SocialCrawl -------------------------------------------------------------
    def socialcrawl(self) -> dict[str, Any]:
        sc = self.raw.get("socialcrawl")
        return sc if isinstance(sc, dict) else {}

    def socialcrawl_capabilities(self) -> dict[str, dict[str, Any]]:
        caps = self.socialcrawl().get("capabilities")
        return {k: v for k, v in (caps or {}).items() if isinstance(v, dict)} if isinstance(caps, dict) else {}

    def socialcrawl_capability(self, capability: str) -> dict[str, Any]:
        """The capability block (description, discovery_search, prefer_params, platforms); {} if unknown."""
        return copy.deepcopy(self.socialcrawl_capabilities().get(capability) or {})

    def capability_platforms(self, capability: str, *, enabled_only: bool = True) -> list[str]:
        """SocialCrawl platform slugs configured for a capability, in file order."""
        plats = self.socialcrawl_capability(capability).get("platforms") or {}
        out: list[str] = []
        if isinstance(plats, dict):
            for slug, cfg in plats.items():
                if enabled_only and isinstance(cfg, dict) and cfg.get("enabled", True) is False:
                    continue
                if enabled_only and cfg is False:
                    continue
                out.append(str(slug))
        return out

    def pinned_endpoint(self, capability: str, platform: str) -> Optional[str]:
        plats = self.socialcrawl_capability(capability).get("platforms") or {}
        cfg = plats.get(platform) if isinstance(plats, dict) else None
        pin = cfg.get("pinned_endpoint") if isinstance(cfg, dict) else None
        return (str(pin).strip() or None) if pin else None

    def socialcrawl_platform_map(self) -> dict[str, str]:
        """SocialCrawl slug -> app platform value (reddit->reddit, twitter->x, google_news->news ...)."""
        pm = self.socialcrawl().get("platform_map") or {}
        return {str(k): str(v) for k, v in pm.items()} if isinstance(pm, dict) else {}

    def socialcrawl_base_url(self, default: str = "https://www.socialcrawl.dev") -> str:
        return str(self.socialcrawl().get("base_url") or default).rstrip("/")

    def socialcrawl_max_in_flight(self, default: int = 10) -> int:
        try:
            return max(1, min(50, int(self.socialcrawl().get("max_in_flight") or default)))
        except (TypeError, ValueError):
            return default

    def enabled_hashtags(self, platform: Optional[str] = None) -> list[tuple[str, list[str]]]:
        """[(tag without '#', [platform slugs])] for enabled hashtags (optionally only those on `platform`)."""
        out: list[tuple[str, list[str]]] = []
        for e in self.raw.get("hashtags") or []:
            if not isinstance(e, dict) or not _entry_enabled(e):
                continue
            tag = str(e.get("tag") or "").strip().lstrip("#")
            plats = [str(p) for p in (e.get("platforms") or [])]
            if not tag:
                continue
            if platform is not None and platform not in plats:
                continue
            out.append((tag, plats))
        return out

    # -- Exa / Firecrawl ---------------------------------------------------------
    def exa(self) -> dict[str, Any]:
        e = self.raw.get("exa")
        return e if isinstance(e, dict) else {}

    def exa_search(self) -> dict[str, Any]:
        s = self.exa().get("search")
        return s if isinstance(s, dict) else {}

    def exa_include_domains(self) -> list[str]:
        """Domains for Exa includeDomains: every list named in `include_domains_from` (dotted paths such as
        news_domains.li_scoped) plus `also_include_domains`; enabled entries only, de-duplicated, file order."""
        search = self.exa_search()
        out: list[str] = []
        for ref in search.get("include_domains_from") or []:
            node: Any = self.raw
            for part in str(ref).split("."):
                node = node.get(part) if isinstance(node, dict) else None
            for e in (node if isinstance(node, list) else []):
                if _entry_enabled(e):
                    d = _domain_of(e)
                    if d and d not in out:
                        out.append(d)
        for e in search.get("also_include_domains") or []:
            d = _domain_of(e)
            if d and d not in out:
                out.append(d)
        return out

    def firecrawl(self) -> dict[str, Any]:
        f = self.raw.get("firecrawl")
        return f if isinstance(f, dict) else {}

    def firecrawl_search(self) -> dict[str, Any]:
        s = self.firecrawl().get("search")
        return s if isinstance(s, dict) else {}

    def firecrawl_scrape(self) -> dict[str, Any]:
        s = self.firecrawl().get("scrape_for_date")
        return s if isinstance(s, dict) else {}

    def firecrawl_scrape_max_per_scan(self, default: int = 10) -> int:
        try:
            return max(0, int(self.firecrawl_scrape().get("max_per_scan", default)))
        except (TypeError, ValueError):
            return default


def validate_overrides(overrides: Any) -> list[str]:
    """Human-readable problems with a proposed overrides mapping ([] = valid). Checks the shapes the
    collectors and planner rely on; values are otherwise taken as given."""
    if not isinstance(overrides, dict):
        return ["overrides must be a mapping"]
    errors: list[str] = []
    for key, value in overrides.items():
        if key not in OVERRIDABLE_TOP_KEYS:
            errors.append(f"{key}: not overridable")
            continue
        if key in ("subreddits", "excluded_subreddits", "hashtags"):
            if not isinstance(value, list):
                errors.append(f"{key}: must be a list")
                continue
            for i, e in enumerate(value):
                if key == "hashtags":
                    if not isinstance(e, dict) or not str(e.get("tag") or "").strip():
                        errors.append(f"{key}[{i}]: needs a tag")
                    elif not isinstance(e.get("platforms", []), list):
                        errors.append(f"{key}[{i}].platforms: must be a list")
                elif not (isinstance(e, str) and e.strip()) and not (isinstance(e, dict) and str(e.get("name") or "").strip()):
                    errors.append(f"{key}[{i}]: needs a name")
                elif isinstance(e, dict) and e.get("source_scope") not in (None, *_SCOPE_RANK):
                    errors.append(f"{key}[{i}].source_scope: must be li_scoped, regional or general")
        elif key == "news_domains":
            if not isinstance(value, dict):
                errors.append("news_domains: must be a mapping")
                continue
            for scope, entries in value.items():
                if scope not in ("li_scoped", "regional"):
                    errors.append(f"news_domains.{scope}: unknown scope")
                elif not isinstance(entries, list) or any(_domain_of(e) is None for e in entries):
                    errors.append(f"news_domains.{scope}: must be a list of domains")
        elif not isinstance(value, dict):
            errors.append(f"{key}: must be a mapping")
    return errors


def iter_capability_platforms(sources: SourcesConfig) -> Iterable[tuple[str, str]]:
    """(capability, platform slug) for every enabled platform of every configured capability."""
    for cap in sources.socialcrawl_capabilities():
        for plat in sources.capability_platforms(cap):
            yield cap, plat
