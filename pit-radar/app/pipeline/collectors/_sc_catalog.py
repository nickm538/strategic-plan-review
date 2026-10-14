"""SocialCrawl free-catalog parsing, endpoint ranking, parameter roles and call validation (docs/04 §2.2).

Verified on Oct 14, 2026 from socialcrawl.dev/llms-*.txt and /docs (read through a docs fetch, not the API):
  * endpoint ids are `platform/resource` (e.g. reddit/search, reddit/subreddit, reddit/post/comments,
    reddit/subreddit/search, twitter/search/tweets, threads/search, bluesky/search, tiktok/search,
    facebook/search/posts, instagram/search/hashtag, tiktok/search/hashtag, google_news/search);
  * GET /v1/utility/endpoints?platform=&search= lists entries with path, method, platform, credit cost and
    parameters; GET /v1/utility/endpoint?id= returns one endpoint's guide (parameters with type, cost,
    paging). [verify] the exact JSON key names of both: the parsers below accept several plausible shapes.
  * parameter names seen in the docs: reddit/search query|sort(relevance,new,top,comment_count)|timeframe|
    after; reddit/subreddit subreddit|sort(best,hot,new,top,rising)|timeframe|after; reddit/post/comments
    url|cursor; reddit/subreddit/search subreddit|query|sort|timeframe|cursor; google_news/search keyword.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from app.pipeline.collectors._common import as_float

# ---------------------------------------------------------------------------
# Catalog parsing
# ---------------------------------------------------------------------------
_LIST_KEYS = ("items", "endpoints", "results", "data", "catalog", "entries")
_ID_KEYS = ("id", "endpoint_id", "slug", "key")
_PATH_KEYS = ("path", "endpoint", "url", "route")
_CREDIT_KEYS = ("credits", "cost", "credit_cost", "credits_per_call", "price", "pricing", "credit")
_DESC_KEYS = ("description", "summary", "title", "name")
_PARAM_KEYS = ("parameters", "params", "query_parameters", "query_params", "arguments", "args")
_TIER_CREDITS = {"free": 0.0, "standard": 1.0, "advanced": 5.0, "premium": 10.0}


def _first_list(node: Any, depth: int = 0) -> Optional[list[Any]]:
    """The endpoint list inside a catalog response: data, data.items, data.endpoints, endpoints, results..."""
    if isinstance(node, list):
        return node
    if not isinstance(node, dict) or depth > 3:
        return None
    for key in _LIST_KEYS:
        if key in node:
            found = _first_list(node[key], depth + 1)
            if found is not None:
                return found
    plats = node.get("platforms")
    if isinstance(plats, dict):          # {"platforms": {"reddit": [...], ...}}
        flat: list[Any] = []
        for v in plats.values():
            if isinstance(v, list):
                flat.extend(v)
            elif isinstance(v, dict):
                inner = _first_list(v, depth + 1)
                if inner:
                    flat.extend(inner)
        if flat:
            return flat
    return None


def parse_credits(value: Any) -> Optional[float]:
    """1 | "1" | "1-34 (metered)" | {"min": 1, "max": 34} | {"credits": 5} | "advanced" -> lowest credit cost."""
    if value is None or isinstance(value, bool):
        return None
    f = as_float(value)
    if f is not None:
        return max(0.0, f)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TIER_CREDITS:
            return _TIER_CREDITS[low]
        if low.startswith("free"):
            return 0.0
        nums = re.findall(r"\d+(?:\.\d+)?", low)
        return min(float(n) for n in nums) if nums else None
    if isinstance(value, dict):
        for k in ("min", "base", "credits", "amount", "value", "per_call", "per_request", "cost", "tier"):
            if k in value:
                got = parse_credits(value[k])
                if got is not None:
                    return got
    return None


def endpoint_id_from_path(path: str) -> Optional[str]:
    """'/v1/reddit/post/comments' -> 'reddit/post/comments'."""
    p = str(path or "").strip()
    if "://" in p:
        p = re.sub(r"^[a-z]+://[^/]+", "", p)
    p = p.split("?", 1)[0].strip("/")
    if p.startswith("v1/"):
        p = p[3:]
    return p or None


def _enum_of(spec: dict[str, Any]) -> Optional[list[str]]:
    for k in ("enum", "values", "allowed", "allowed_values", "options", "choices"):
        v = spec.get(k)
        if isinstance(v, (list, tuple)) and v:
            return [str(x) for x in v if isinstance(x, (str, int, float)) and not isinstance(x, bool)]
    schema = spec.get("schema")
    if isinstance(schema, dict):
        return _enum_of(schema)
    return None


def _param_from(spec: Any, *, name: Optional[str] = None, required: Optional[bool] = None) -> Optional[dict[str, Any]]:
    if isinstance(spec, str) and name is None:
        nm = spec.strip()
        return {"name": nm, "required": bool(required), "type": "", "description": "", "enum": None} if nm else None
    if isinstance(spec, str):            # name -> description
        return {"name": name, "required": bool(required), "type": "", "description": spec[:500], "enum": None}
    if not isinstance(spec, dict):
        return None
    nm = name or spec.get("name") or spec.get("param") or spec.get("key")
    if not isinstance(nm, str) or not nm.strip():
        return None
    if spec.get("in") not in (None, "query"):      # OpenAPI header/path params are not ours to send
        return None
    req = spec.get("required")
    if req is None and "optional" in spec:
        req = not bool(spec.get("optional"))
    if required is not None and req is None:
        req = required
    typ = spec.get("type")
    if typ is None and isinstance(spec.get("schema"), dict):
        typ = spec["schema"].get("type")
    desc = spec.get("description") or spec.get("desc") or spec.get("summary") or ""
    return {"name": nm.strip(), "required": bool(req), "type": str(typ or ""),
            "description": str(desc)[:500], "enum": _enum_of(spec)}


def parse_params(node: Any) -> Optional[list[dict[str, Any]]]:
    """Normalize a parameter description into [{name, required, type, description, enum}].
    Accepts a list (of dicts or names), {"required": [...], "optional": [...]}, or {name: spec}."""
    out: list[dict[str, Any]] = []
    if isinstance(node, list):
        for spec in node:
            p = _param_from(spec)
            if p:
                out.append(p)
    elif isinstance(node, dict):
        grouped = {k for k in node if k in ("required", "optional")}
        if grouped and all(isinstance(node[k], (list, dict)) for k in grouped):
            for k in ("required", "optional"):
                group = node.get(k)
                if isinstance(group, list):
                    for spec in group:
                        p = _param_from(spec, required=(k == "required"))
                        if p:
                            p["required"] = (k == "required") or p["required"]
                            out.append(p)
                elif isinstance(group, dict):
                    for nm, spec in group.items():
                        p = _param_from(spec, name=str(nm), required=(k == "required"))
                        if p:
                            out.append(p)
        else:
            for nm, spec in node.items():
                if nm in ("featured", "constraints", "notes", "examples", "example"):
                    continue
                p = _param_from(spec, name=str(nm))
                if p:
                    out.append(p)
    else:
        return None
    seen: dict[str, dict[str, Any]] = {}
    for p in out:                        # de-duplicate by name; a 'required' mark wins
        prev = seen.get(p["name"])
        if prev is None:
            seen[p["name"]] = p
        else:
            prev["required"] = prev["required"] or p["required"]
            prev["enum"] = prev["enum"] or p["enum"]
            prev["description"] = prev["description"] or p["description"]
    return list(seen.values())


def normalize_entry(raw: Any, *, platform_hint: Optional[str] = None) -> Optional[dict[str, Any]]:
    """One catalog/guide entry -> {id, path, method, platform, credits, description, params|None}."""
    if not isinstance(raw, dict):
        return None
    node = raw
    for wrap in ("endpoint", "guide"):                # data.endpoint / data.guide wrappers
        if isinstance(node.get(wrap), dict) and not any(isinstance(node.get(k), str) for k in ("id", "path")):
            node = node[wrap]
    path = next((node[k] for k in _PATH_KEYS if isinstance(node.get(k), str) and "/" in node[k]), None)
    eid = next((node[k] for k in _ID_KEYS if isinstance(node.get(k), str) and node[k].strip()), None)
    if eid and eid.startswith("/"):
        eid = endpoint_id_from_path(eid)
    if not eid and path:
        eid = endpoint_id_from_path(path)
    if not eid:
        return None
    eid = eid.strip().strip("/")
    if eid.startswith("v1/"):
        eid = eid[3:]
    if not path:
        path = "/v1/" + eid
    elif "://" in path:
        path = "/" + (endpoint_id_from_path(path) or "")
        path = path if path.startswith("/v1/") else "/v1" + path
    elif not path.startswith("/"):
        path = "/" + path
    if not path.startswith("/v1/"):
        path = "/v1" + path if not path.startswith("/v1") else path
    method = str(node.get("method") or node.get("http_method") or "GET").upper()
    platform = node.get("platform") or node.get("platform_slug") or eid.split("/", 1)[0] or platform_hint
    credits = None
    for k in _CREDIT_KEYS:
        if k in node:
            credits = parse_credits(node[k])
            if credits is not None:
                break
    if credits is None and isinstance(node.get("tier"), str):
        credits = parse_credits(node["tier"])
    desc = next((str(node[k]) for k in _DESC_KEYS if isinstance(node.get(k), str) and node[k].strip()), "")
    params = None
    for k in _PARAM_KEYS:
        if k in node:
            params = parse_params(node[k])
            if params is not None:
                break
    return {"id": eid, "path": path.split("?", 1)[0], "method": method, "platform": str(platform or ""),
            "credits": credits, "description": desc[:500], "params": params}


def parse_catalog(body: Any, *, platform: Optional[str] = None) -> list[dict[str, Any]]:
    """Entries from GET /v1/utility/endpoints (any of the accepted shapes), filtered to `platform`."""
    data = body.get("data") if isinstance(body, dict) and "data" in body else body
    rows = _first_list(data) or (_first_list(body) if isinstance(body, dict) else None) or []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        e = normalize_entry(raw, platform_hint=platform)
        if not e or e["id"] in seen:
            continue
        if platform and e["platform"] and e["platform"] != platform and not e["id"].startswith(platform + "/"):
            continue
        seen.add(e["id"])
        out.append(e)
    return out


def parse_guide(body: Any) -> Optional[dict[str, Any]]:
    """Entry from GET /v1/utility/endpoint?id=... (data, data.endpoint or data.guide)."""
    data = body.get("data") if isinstance(body, dict) and "data" in body else body
    if not isinstance(data, dict):
        return None
    return normalize_entry(data)


# ---------------------------------------------------------------------------
# Ranking candidates for a capability
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CapRule:
    require: tuple[frozenset[str], ...]   # every group needs >= 1 token present in the resource path
    exclude: frozenset[str]               # any of these tokens disqualifies
    prefer: tuple[str, ...]               # resource paths (after the platform), best first
    roles: tuple[str, ...]                # parameter roles that must resolve


_POST_WORDS = frozenset({"post", "posts", "tweet", "tweets", "video", "videos", "thread", "threads", "reel", "reels",
                         "news", "articles", "article", "submissions"})
_NON_POST_DESC = frozenset({"accounts", "users", "profiles", "creators", "people", "pages", "channels",
                            "companies", "advertisers"})

RULES: dict[str, CapRule] = {
    "keyword_search": CapRule(
        require=(frozenset({"search"}),),
        exclude=frozenset({"user", "users", "profile", "profiles", "people", "page", "pages", "group", "groups",
                           "hashtag", "hashtags", "subreddit", "subreddits", "comment", "comments", "media",
                           "music", "location", "locations", "ads", "ad", "adlibrary", "marketplace", "events",
                           "companies", "company", "suggestions", "suggestion", "omni", "ai", "channels",
                           "channel", "playlists", "playlist", "places", "place", "jobs", "products",
                           "product", "sounds", "sound", "creators", "communities", "community", "lists"}),
        prefer=("search", "search/posts", "search/tweets", "search/latest", "search/videos", "search/top",
                "search/reels"),
        roles=("query",)),
    "subreddit_new": CapRule(
        require=(frozenset({"subreddit"}),),
        exclude=frozenset({"search", "details", "subreddits", "about", "rules", "wiki", "comments",
                           "moderators", "flair", "top"}),
        prefer=("subreddit", "subreddit/new", "subreddit/posts"),
        roles=("subreddit",)),
    "subreddit_search": CapRule(
        require=(frozenset({"subreddit"}), frozenset({"search"})),
        exclude=frozenset({"subreddits", "details", "comments", "media"}),
        prefer=("subreddit/search",),
        roles=("subreddit",)),
    "post_comments": CapRule(
        require=(frozenset({"comments"}), frozenset({"post", "thread", "submission", "tweet", "video"})),
        exclude=frozenset({"profile", "user", "users", "search", "replies", "reply", "transcript", "count"}),
        prefer=("post/comments", "comments", "thread/comments"),
        roles=("post",)),
    "hashtag_posts": CapRule(
        require=(frozenset({"hashtag", "hashtags", "tag"}),),
        exclude=frozenset({"popular", "trending", "users", "music", "sounds", "suggestions", "info", "details"}),
        prefer=("search/hashtag", "hashtag/posts", "hashtag/videos", "hashtag", "hashtag/feed"),
        roles=("hashtag",)),
    "news_search": CapRule(
        require=(frozenset({"search"}),),
        exclude=frozenset({"top", "stories", "trending", "topics", "suggestions", "users"}),
        prefer=("search",),
        roles=("query",)),
}


def resource_of(endpoint_id: str) -> str:
    """'reddit/post/comments' -> 'post/comments'."""
    return endpoint_id.split("/", 1)[1] if "/" in endpoint_id else endpoint_id


def _tokens(resource: str) -> set[str]:
    toks: set[str] = set()
    for seg in resource.lower().split("/"):
        if not seg:
            continue
        toks.add(seg)
        toks.update(t for t in re.split(r"[-_]", seg) if t)
    return toks


def matches_rule(entry: dict[str, Any], capability: str) -> bool:
    rule = RULES.get(capability)
    res = resource_of(entry["id"])
    toks = _tokens(res)
    if rule is None:                     # admin-added capability: every word of its name must appear
        words = [w for w in capability.lower().split("_") if w]
        hay = (entry["id"] + " " + entry.get("description", "")).lower()
        return bool(words) and all(w in hay or w.rstrip("s") in hay for w in words)
    if any(not (g & toks) for g in rule.require):
        return False
    if toks & rule.exclude:
        return False
    desc_words = set(re.findall(r"[a-z]+", (entry.get("description") or "").lower()))
    if capability == "keyword_search" and desc_words & _NON_POST_DESC and not desc_words & _POST_WORDS:
        return False
    return True


def rank_candidates(entries: Sequence[dict[str, Any]], capability: str) -> list[dict[str, Any]]:
    """Matching GET endpoints, cheapest first, then the rule's preferred resource, then the shortest id."""
    rule = RULES.get(capability)
    prefer = rule.prefer if rule else ()

    def key(e: dict[str, Any]) -> tuple[float, int, int, str]:
        res = resource_of(e["id"])
        pref = prefer.index(res) if res in prefer else len(prefer)
        credits = e["credits"] if e.get("credits") is not None else 1.0
        return (credits, pref, res.count("/"), e["id"])

    ok = [e for e in entries if e.get("method", "GET") == "GET" and matches_rule(e, capability)]
    return sorted(ok, key=key)


# ---------------------------------------------------------------------------
# Parameter roles
# ---------------------------------------------------------------------------
ROLE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "query": ("query", "q", "keyword", "keywords", "search", "term"),
    "subreddit": ("subreddit", "sub", "community", "name"),
    "post": ("post_id", "id", "url", "post_url", "thread_id", "link"),
    "sort": ("sort", "sort_by", "order", "sortBy", "search_type", "order_by", "sort_type"),
    "time": ("time", "t", "time_range", "timeframe", "period"),
    "cursor": ("cursor", "next_cursor", "after"),
    "hashtag": ("hashtag", "tag", "hashtag_name", "name", "keyword", "query"),
}
CAPABILITY_ROLES: dict[str, tuple[str, ...]] = {
    "keyword_search": ("query",),
    "subreddit_new": ("subreddit",),
    "subreddit_search": ("subreddit", "query"),
    "post_comments": ("post",),
    "hashtag_posts": ("hashtag",),
    "news_search": ("query",),
}
DEFAULT_SORT_PREFS = ("new", "latest", "recent", "date", "newest")
DEFAULT_TIME_PREFS = ("week", "month")


def _pick_name(names: dict[str, str], role: str, capability: str) -> Optional[str]:
    for cand in ROLE_CANDIDATES[role]:
        if cand == "name" and role == "subreddit" and capability not in ("subreddit_new", "subreddit_search"):
            continue
        if cand == "id" and role == "post" and capability != "post_comments":
            continue
        if cand.lower() in names:
            return names[cand.lower()]
    return None


def _choose_value(spec: dict[str, Any], prefs: Sequence[str]) -> Optional[str]:
    enum = spec.get("enum") or []
    if enum:
        lowered = {str(v).lower(): str(v) for v in enum}
        for p in prefs:
            if p.lower() in lowered:
                return lowered[p.lower()]
        return None
    desc = (spec.get("description") or "").lower()
    for p in prefs:
        if re.search(rf"\b{re.escape(p.lower())}\b", desc):
            return p
    return None


def derive_roles(capability: str, params: Sequence[dict[str, Any]], *,
                 sort_prefs: Sequence[str] = DEFAULT_SORT_PREFS,
                 time_prefs: Sequence[str] = DEFAULT_TIME_PREFS) -> dict[str, Any]:
    """Map the endpoint's parameter names to the roles this app fills (query/subreddit/post/sort/time/
    cursor/hashtag), and pick recency-oriented sort/time values from each parameter's enum or description."""
    names = {p["name"].lower(): p["name"] for p in params}
    by_name = {p["name"]: p for p in params}
    out: dict[str, Any] = {
        "query_param": None, "subreddit_param": None, "post_param": None, "hashtag_param": None,
        "sort_param": None, "sort_value": None, "time_param": None, "time_value": None, "cursor_param": None,
        "extra_params": {},
    }
    if capability in ("keyword_search", "subreddit_search", "news_search") or capability not in CAPABILITY_ROLES:
        out["query_param"] = _pick_name(names, "query", capability)
    if capability in ("subreddit_new", "subreddit_search"):
        out["subreddit_param"] = _pick_name(names, "subreddit", capability)
    if capability == "post_comments":
        out["post_param"] = _pick_name(names, "post", capability)
    if capability == "hashtag_posts":
        out["hashtag_param"] = _pick_name(names, "hashtag", capability)
    sp = _pick_name(names, "sort", capability)
    if sp:
        val = _choose_value(by_name[sp], sort_prefs)
        if val:
            out["sort_param"], out["sort_value"] = sp, val
    tp = _pick_name(names, "time", capability)
    if tp:
        val = _choose_value(by_name[tp], time_prefs)
        if val:
            out["time_param"], out["time_value"] = tp, val
    out["cursor_param"] = _pick_name(names, "cursor", capability)
    judg = by_name.get(names.get("judgments", ""), None)
    if judg is not None:                 # free SocialCrawl labels we never use: turn them off when allowed
        enum = [str(v).lower() for v in (judg.get("enum") or [])]
        if not enum or "off" in enum:
            out["extra_params"] = {judg["name"]: "off"}
    return out


def roles_satisfied(capability: str, roles: dict[str, Any]) -> bool:
    """The roles a capability cannot work without resolved to a parameter."""
    need = {
        "keyword_search": ("query_param",),
        "subreddit_new": ("subreddit_param",),
        "subreddit_search": ("subreddit_param",),
        "post_comments": ("post_param",),
        "hashtag_posts": ("hashtag_param",),
        "news_search": ("query_param",),
    }.get(capability, ())
    return all(roles.get(k) for k in need)


def validate_call(params_spec: Sequence[dict[str, Any]], call: dict[str, Any]) -> Optional[str]:
    """None if `call` only uses known parameter names, has every required one, and respects enums;
    else a short reason (no values echoed)."""
    known = {p["name"]: p for p in params_spec}
    unknown = sorted(k for k in call if k not in known)
    if unknown:
        return "unknown parameter(s): " + ", ".join(unknown)
    missing = sorted(p["name"] for p in params_spec if p.get("required") and call.get(p["name"]) in (None, ""))
    if missing:
        return "missing required parameter(s): " + ", ".join(missing)
    for name, value in call.items():
        enum = known[name].get("enum")
        if enum and str(value).lower() not in {str(v).lower() for v in enum}:
            return f"value not allowed for {name}"
    return None
