"""Query planning and rotation (docs/04 §6).

One scan's plan, highest priority first:
  deferred (100) > subreddit_new (80) > post_comments (70, planned separately) > region (60) > hot (40)
  > rotation (20).

Budget envelopes keep each plan inside the per-scan caps, so deferral stays the exception and nothing
starves (a plan that always overflowed would push the same queries to "deferred" forever):
  * SocialCrawl: SOCIALCRAWL_CREDITS_PER_SCAN, using each endpoint's resolved credit cost. subreddit_new and
    carried-over deferred queries are always planned; region, hot and rotation queries are added while they
    fit, keeping back room for the post_comments pass the runner plans after the prefilter (two threads,
    at most a quarter of the cap). The rotation cursor advances only past places that were actually
    planned, so every place is reached in turn however tight the budget is.
  * Exa: EXA_SEARCHES_PER_SCAN requests. Firecrawl: FIRECRAWL_SEARCHES_PER_SCAN requests, of which up to
    half are left for scrape-for-date calls (each scrape counts as one Firecrawl request).
Region-wide keyword templates rotate across platforms (one template per platform per scan, a different one
on each platform), so all templates reach all platforms over a few scans. Output is deterministic for the
same inputs.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Optional

from app.pipeline.types import NormalizedItem, PlannedQuery

if TYPE_CHECKING:
    from app.pipeline.geo.gazetteer import Gazetteer, PlaceRec
    from app.pipeline.lexicon import Lexicon
    from app.pipeline.sources import SourcesConfig
    from app.runtime_settings import Effective

logger = logging.getLogger(__name__)

PRIO_DEFERRED = 100
PRIO_SUBREDDIT_NEW = 80
PRIO_COMMENTS = 70
PRIO_REGION = 60
PRIO_HOT = 40
PRIO_ROTATION = 20

MAX_DEFERRED_PER_PROVIDER = 60       # bound on carried-over queries per provider
COMMENT_RESERVE_THREADS = 2          # SocialCrawl credits kept back for the Reddit comment pass ...
COMMENT_RESERVE_MAX_SHARE = 0.25     # ... but never more than this share of the per-scan cap
SUBREDDIT_SEARCH_PER_SCAN = 2        # (subreddit x core term) pairs per scan, rotating
SUBREDDIT_SEARCH_TERMS = ("homeless", "encampment", "unhoused", "sleeping in car", "tent")
PLATFORMS_PER_PLACE = 2
NO_GAZETTEER_CURSOR_MOD = 1_000_000  # the cursor still advances (template rotation) before geo is built
PROVIDERS = ("socialcrawl", "exa", "firecrawl")
_LOCALITY_KINDS = frozenset({"town", "city", "village", "cdp"})


@dataclass
class PlanResult:
    queries: list[PlannedQuery]
    new_cursor: int
    notes: list[str] = field(default_factory=list)   # e.g. "socialcrawl keyword_search on tiktok unavailable"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _mix(n: int, salt: str) -> int:
    """Stable, well-spread offset derived from the cursor (deterministic; not crypto)."""
    return int.from_bytes(hashlib.sha256(f"{salt}:{n}".encode()).digest()[:4], "big")


def _resolved_entry(resolved: dict[str, Any], capability: str, platform: str) -> Optional[dict[str, Any]]:
    caps = resolved.get("capabilities") if isinstance(resolved, dict) else None
    if not isinstance(caps, dict):
        return None
    plat = caps.get(capability)
    entry = plat.get(platform) if isinstance(plat, dict) else None
    return entry if isinstance(entry, dict) and (entry.get("path") or entry.get("id")) else None


def _credits(entry: Optional[dict[str, Any]]) -> float:
    try:
        c = float((entry or {}).get("credits"))
    except (TypeError, ValueError):
        return 1.0
    return c if math.isfinite(c) and c >= 0 else 1.0


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return default


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) and f >= 0 else default


def _str_list(node: Any) -> list[str]:
    return [s for s in (node or []) if isinstance(s, str) and s.strip()] if isinstance(node, list) else []


def template_group(kind: str) -> str:
    """Place kind -> lexicon query_templates.place group."""
    if kind in _LOCALITY_KINDS:
        return "town_village_cdp"
    if kind == "lirr_station":
        return "lirr_station"
    return "landmark"


def station_name(name: str) -> str:
    """'Hicksville Station' -> 'Hicksville' (drops one trailing ' station', case-insensitive)."""
    n = (name or "").strip()
    return n[: -len(" station")].rstrip() if n.lower().endswith(" station") else n


def fill_template(template: str, place_name: str) -> str:
    """Substitute {place} and {place_station} (plain replace, so other braces are harmless)."""
    return template.replace("{place_station}", station_name(place_name)).replace("{place}", place_name.strip())


class _Plan:
    def __init__(self, sc_cap: float, exa_cap: int, fc_search_cap: int) -> None:
        self.queries: list[PlannedQuery] = []
        self.keys: set[str] = set()
        self.notes: list[str] = []
        self.sc_cap, self.exa_cap, self.fc_cap = sc_cap, exa_cap, fc_search_cap
        self.sc_used = 0.0
        self.exa_used = 0
        self.fc_used = 0
        self.left_out: dict[str, int] = {}

    def add(self, q: PlannedQuery, *, force: bool = False) -> bool:
        """Append unless a query with the same key exists or (when not forced) its provider's envelope is full."""
        k = q.key()
        if k in self.keys:
            return True                   # already planned (e.g. as a deferred query): counts as covered
        if not force and not self.fits(q):
            self.left_out[q.family] = self.left_out.get(q.family, 0) + 1
            return False
        self.keys.add(k)
        self.queries.append(q)
        if q.provider == "socialcrawl":
            self.sc_used += q.est_cost
        elif q.provider == "exa":
            self.exa_used += 1
        elif q.provider == "firecrawl":
            self.fc_used += 1
        return True

    def fits(self, q: PlannedQuery) -> bool:
        if q.provider == "socialcrawl":
            return self.sc_used + q.est_cost <= self.sc_cap + 1e-9
        if q.provider == "exa":
            return self.exa_used < self.exa_cap
        if q.provider == "firecrawl":
            return self.fc_used < self.fc_cap
        return False

    def sc_room(self) -> float:
        return max(0.0, self.sc_cap - self.sc_used)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
def plan_scan(
    *,
    eff: "Effective",
    lexicon: "Lexicon",
    sources: "SourcesConfig",
    gazetteer: Optional["Gazetteer"],
    rotation_cursor: int,
    hot_places: list["PlaceRec"],
    resolved_endpoints: dict[str, Any],
    deferred: Optional[list[PlannedQuery]] = None,
    enabled_providers: Optional[Iterable[str]] = None,
) -> PlanResult:
    """Build the scan's query list: (1) subreddit_new for every enabled subreddit; region-wide keyword
    templates on each enabled SocialCrawl keyword platform whose capability resolved, plus news_search,
    Exa (semantic templates) and Firecrawl (keyword templates); (2) hot places (<= HOT_PLACES_PER_SCAN);
    (3) rotation: next PLACES_PER_SCAN places from gazetteer.rotation_places() starting at rotation_cursor,
    1-2 templates each on 2 rotating platforms; (4) previously deferred queries first.
    Priorities: deferred > subreddit_new > region > hot > rotation. Without a gazetteer, skip (2)-(3).

    `enabled_providers` (optional) limits planning to providers the runner can call (e.g. keys set)."""
    providers = set(enabled_providers) if enabled_providers is not None else set(PROVIDERS)
    resolved = resolved_endpoints if isinstance(resolved_endpoints, dict) else {}
    places_per_scan = _as_int(eff.mode_value("places_per_scan"), 8)
    hot_cap = _as_int(eff.mode_value("hot_places_per_scan"), 5)
    exa_cap = _as_int(eff.mode_value("exa_searches_per_scan"), 6) if "exa" in providers else 0
    fc_total = _as_int(eff.mode_value("firecrawl_searches_per_scan"), 6) if "firecrawl" in providers else 0
    fc_cap = fc_total - min(sources.firecrawl_scrape_max_per_scan(), fc_total // 2)
    sc_cap = _as_float(eff.mode_value("socialcrawl_credits_per_scan"), 40.0) if "socialcrawl" in providers else 0.0
    comments_entry = _resolved_entry(resolved, "post_comments", "reddit")
    reserve = 0.0
    if comments_entry is not None and sc_cap > 0:
        reserve = min(_credits(comments_entry) * COMMENT_RESERVE_THREADS, sc_cap * COMMENT_RESERVE_MAX_SHARE)
    plan = _Plan(sc_cap - reserve, exa_cap, fc_cap)
    cursor_in = _as_int(rotation_cursor, 0)
    offset = _mix(cursor_in, "region")

    templates = getattr(lexicon, "query_templates", None) or {}
    region = templates.get("region_wide") if isinstance(templates, dict) else None
    region_kw = _str_list((region or {}).get("keyword"))
    region_sem = _str_list((region or {}).get("semantic"))
    place_tmpl = templates.get("place") if isinstance(templates, dict) else None
    place_tmpl = place_tmpl if isinstance(place_tmpl, dict) else {}
    if reserve:
        plan.notes.append(f"{reserve:g} SocialCrawl credits kept for the comment pass")
    if not region_kw and not region_sem:
        plan.notes.append("lexicon has no region_wide query templates")

    # --- SocialCrawl capability availability --------------------------------------------------
    sc_on = "socialcrawl" in providers
    if not sc_on:
        plan.notes.append("socialcrawl not enabled for this scan")

    def available(cap: str, slugs: list[str]) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        for slug in slugs:
            entry = _resolved_entry(resolved, cap, slug)
            if entry is None:
                if sc_on:
                    plan.notes.append(f"socialcrawl {cap} on {slug} unavailable (not resolved)")
                continue
            out.append((slug, entry))
        return out

    kw_plats = available("keyword_search", sources.capability_platforms("keyword_search")) if sc_on else []
    sub_new = available("subreddit_new", sources.capability_platforms("subreddit_new")) if sc_on else []
    sub_search = available("subreddit_search", sources.capability_platforms("subreddit_search")) if sc_on else []
    news = available("news_search", sources.capability_platforms("news_search")) if sc_on else []
    hashtag_plats = available("hashtag_posts", sources.capability_platforms("hashtag_posts")) if sc_on else []
    if sc_on:
        available("post_comments", sources.capability_platforms("post_comments"))   # notes only
    kw_entries = dict(kw_plats)
    kw_slugs = [s for s, _ in kw_plats]

    # --- (4) deferred queries first ------------------------------------------------------------
    carried: dict[str, int] = {}
    dropped_over_cap = 0
    for q in sorted(deferred or [], key=lambda d: -int(d.priority or 0)):
        if not isinstance(q, PlannedQuery) or q.provider not in providers:
            continue
        if q.provider == "socialcrawl" and _resolved_entry(resolved, q.capability, q.platform or "") is None:
            plan.notes.append(f"deferred {q.capability} on {q.platform} dropped: endpoint no longer resolved")
            continue
        if carried.get(q.provider, 0) >= MAX_DEFERRED_PER_PROVIDER:
            dropped_over_cap += 1
            continue
        if plan.add(dataclasses.replace(q, priority=PRIO_DEFERRED, params=dict(q.params)), force=True):
            carried[q.provider] = carried.get(q.provider, 0) + 1
    if carried:
        plan.notes.append("deferred carried over: " + ", ".join(f"{p} {n}" for p, n in sorted(carried.items())))
    if dropped_over_cap:
        plan.notes.append(f"{dropped_over_cap} deferred queries dropped (over {MAX_DEFERRED_PER_PROVIDER} per provider)")

    # --- (1) subreddit_new for every enabled subreddit (always) ---------------------------------
    if sub_new:
        slug, entry = sub_new[0]
        for sub in sources.enabled_subreddits():
            plan.add(PlannedQuery(provider="socialcrawl", capability="subreddit_new", query="", platform=slug,
                                  family="subreddit", subreddit=sub,
                                  source_scope=sources.subreddit_scope(sub) or "li_scoped",
                                  priority=PRIO_SUBREDDIT_NEW, est_cost=_credits(entry)), force=True)

    # --- (1) region-wide ------------------------------------------------------------------------
    if region_kw:
        for j, slug in enumerate(kw_slugs):           # one template per platform, different on each
            t = region_kw[(offset + j) % len(region_kw)]
            plan.add(PlannedQuery(provider="socialcrawl", capability="keyword_search", query=t, platform=slug,
                                  family="region", priority=PRIO_REGION, est_cost=_credits(kw_entries[slug])))
        for n, (slug, entry) in enumerate(news):
            t = region_kw[(offset + len(kw_slugs) + n) % len(region_kw)]
            plan.add(PlannedQuery(provider="socialcrawl", capability="news_search", query=t, platform=slug,
                                  family="region", priority=PRIO_REGION, est_cost=_credits(entry)))
    if sub_search:
        slug, entry = sub_search[0]
        subs = sources.enabled_subreddits()
        for n in range(SUBREDDIT_SEARCH_PER_SCAN if subs else 0):
            idx = offset + n
            sub = subs[idx % len(subs)]
            term = SUBREDDIT_SEARCH_TERMS[(idx // len(subs) + n) % len(SUBREDDIT_SEARCH_TERMS)]
            plan.add(PlannedQuery(provider="socialcrawl", capability="subreddit_search", query=term, platform=slug,
                                  family="subreddit", subreddit=sub,
                                  source_scope=sources.subreddit_scope(sub) or "li_scoped",
                                  priority=PRIO_REGION, est_cost=_credits(entry)))
    hashtag_entries = dict(hashtag_plats)
    for tag, plats in sources.enabled_hashtags():
        for slug in plats:
            if slug in hashtag_entries:
                plan.add(PlannedQuery(provider="socialcrawl", capability="hashtag_posts", query=tag, platform=slug,
                                      family="region", params={"hashtag": tag}, priority=PRIO_REGION,
                                      est_cost=_credits(hashtag_entries[slug])))
    if region_sem and exa_cap:
        plan.add(PlannedQuery(provider="exa", capability="search", query=region_sem[offset % len(region_sem)],
                              family="region", priority=PRIO_REGION))
        open_t = region_sem[(offset + 1) % len(region_sem)]
        plan.add(PlannedQuery(provider="exa", capability="search", query=open_t, family="region",
                              params={"unrestricted": True}, priority=PRIO_REGION))
    if region_kw and fc_cap:
        plan.add(PlannedQuery(provider="firecrawl", capability="search", query=region_kw[offset % len(region_kw)],
                              family="region", priority=PRIO_REGION))

    # --- (2) hot places and (3) rotation need the gazetteer -------------------------------------
    new_cursor = (cursor_in + 1) % NO_GAZETTEER_CURSOR_MOD
    if gazetteer is None:
        plan.notes.append("no gazetteer yet: hot places and place rotation skipped")
        return _finish(plan, new_cursor)

    def place_kw(place: "PlaceRec") -> list[str]:
        grp = place_tmpl.get(template_group(getattr(place, "kind", "")))
        return _str_list((grp or {}).get("keyword")) if isinstance(grp, dict) else []

    def place_sem(place: "PlaceRec") -> list[str]:
        grp = place_tmpl.get(template_group(getattr(place, "kind", "")))
        return _str_list((grp or {}).get("semantic")) if isinstance(grp, dict) else []

    def sc_place_queries(place: "PlaceRec", seed: int, family: str, prio: int) -> list[PlannedQuery]:
        kws = place_kw(place)
        if not kws or not kw_slugs:
            return []
        m = len(kw_slugs)
        n_plats = min(PLATFORMS_PER_PLACE, m)
        out: list[PlannedQuery] = []
        for k in range(n_plats):
            slug = kw_slugs[(seed * PLATFORMS_PER_PLACE + k) % m]
            t = fill_template(kws[(seed + k) % len(kws)], place.name)
            out.append(PlannedQuery(provider="socialcrawl", capability="keyword_search", query=t, platform=slug,
                                    family=family, place_id=place.id, place_name=place.name, priority=prio,
                                    est_cost=_credits(kw_entries[slug])))
        return out

    def exa_place_query(place: "PlaceRec", seed: int, family: str, prio: int) -> Optional[PlannedQuery]:
        sems = place_sem(place)
        if not sems:
            return None
        return PlannedQuery(provider="exa", capability="search", query=fill_template(sems[seed % len(sems)], place.name),
                            family=family, place_id=place.id, place_name=place.name, priority=prio)

    def fc_place_query(place: "PlaceRec", seed: int, family: str, prio: int) -> Optional[PlannedQuery]:
        kws = place_kw(place)
        if not kws:
            return None
        return PlannedQuery(provider="firecrawl", capability="search", query=fill_template(kws[seed % len(kws)], place.name),
                            family=family, place_id=place.id, place_name=place.name, priority=prio)

    try:
        rot_places = list(gazetteer.rotation_places() or [])
    except NotImplementedError:
        rot_places = []
        plan.notes.append("gazetteer rotation not available yet: place rotation skipped")
    except Exception as exc:  # noqa: BLE001 - a geo failure must not stop planning
        rot_places = []
        logger.warning("planner: gazetteer.rotation_places failed (%s)", type(exc).__name__)
        plan.notes.append("gazetteer rotation failed: place rotation skipped")
    n_places = len(rot_places)
    cursor0 = cursor_in % n_places if n_places else 0

    # Exa/Firecrawl place slots: hot places take at most half when rotation places are waiting.
    exa_slots = max(0, plan.exa_cap - plan.exa_used)
    fc_slots = max(0, plan.fc_cap - plan.fc_used)
    rotating = places_per_scan > 0 and n_places > 0

    # --- (2) hot places ---
    hot: list["PlaceRec"] = []
    seen_hot: set[Any] = set()
    for p in hot_places or []:
        key = getattr(p, "id", None) or (getattr(p, "name", ""), getattr(p, "kind", ""))
        if not getattr(p, "name", "") or key in seen_hot:
            continue
        seen_hot.add(key)
        hot.append(p)
        if len(hot) >= hot_cap:
            break
    exa_hot_max = math.ceil(exa_slots / 2) if rotating else exa_slots
    fc_hot_max = math.ceil(fc_slots / 2) if rotating else fc_slots
    exa_hot = fc_hot = 0
    for h, place in enumerate(hot):
        seed = _mix(cursor_in + h, "hot")
        for q in sc_place_queries(place, seed, "hot", PRIO_HOT):
            plan.add(q)
        if exa_hot < exa_hot_max:
            eq = exa_place_query(place, seed, "hot", PRIO_HOT)
            if eq and plan.add(eq):
                exa_hot += 1
        if fc_hot < fc_hot_max:
            fq = fc_place_query(place, seed, "hot", PRIO_HOT)
            if fq and plan.add(fq):
                fc_hot += 1
    if hot:
        plan.notes.append(f"hot places: {len(hot)} planned")

    # --- (3) rotation ---
    window = 0
    if rotating:
        limit = min(places_per_scan, n_places)
        if not kw_slugs:   # no SocialCrawl keyword platform: the window is what Exa/Firecrawl can cover
            limit = min(limit, max(plan.exa_cap - plan.exa_used, plan.fc_cap - plan.fc_used))
        no_templates = 0
        sc_exhausted = False
        for k in range(limit):
            idx = (cursor0 + k) % n_places
            place = rot_places[idx]
            seed = idx + cursor0
            sc_qs = [] if sc_exhausted else sc_place_queries(place, seed, "place", PRIO_ROTATION)
            got_any = blocked = False
            if sc_qs:
                cost = sum(q.est_cost for q in sc_qs if q.key() not in plan.keys)
                if cost > plan.sc_room() + 1e-9:
                    if window > 0:
                        plan.notes.append(f"rotation: {limit - k} place(s) wait for the next scan "
                                          f"(SOCIALCRAWL_CREDITS_PER_SCAN reached)")
                        break             # the window ends here; the cursor resumes at this place
                    # not even one place fits: let Exa/Firecrawl carry the rotation this scan
                    sc_exhausted = True
                    plan.notes.append("rotation: no SocialCrawl credits left; places covered by Exa/Firecrawl only")
                else:
                    for q in sc_qs:
                        plan.add(q, force=True)
                    got_any = True
            for extra in (exa_place_query(place, seed, "place", PRIO_ROTATION),
                          fc_place_query(place, seed, "place", PRIO_ROTATION)):
                if extra is None:
                    continue
                if plan.fits(extra):
                    got_any = plan.add(extra) or got_any
                else:
                    blocked = True
            if not got_any:
                if blocked or sc_exhausted:
                    break                 # nothing could take this place: resume here next scan
                no_templates += 1         # no template applies to this place: pass over it
            window += 1
        if no_templates:
            plan.notes.append(f"rotation: {no_templates} place(s) had no applicable query template")
        if window:
            first = cursor0 + 1
            plan.notes.append(f"rotation: places {first}-{cursor0 + window} of {n_places} ({window} planned)")
    elif n_places == 0:
        plan.notes.append("gazetteer has no rotation places")

    if n_places:
        advance = window if window > 0 else (1 if places_per_scan == 0 else 0)
        new_cursor = (cursor0 + advance) % n_places
    return _finish(plan, new_cursor)


def _finish(plan: _Plan, new_cursor: int) -> PlanResult:
    for fam, n in sorted(plan.left_out.items()):
        plan.notes.append(f"{n} {fam} queries left out to stay within the per-scan caps")
    indexed = list(enumerate(plan.queries))
    indexed.sort(key=lambda t: (-int(t[1].priority), t[0]))
    queries = [q for _, q in indexed]
    by_provider: dict[str, int] = {}
    for q in queries:
        by_provider[q.provider] = by_provider.get(q.provider, 0) + 1
    logger.info("planner: %d queries (%s), est %.0f SocialCrawl credits", len(queries),
                ", ".join(f"{p} {n}" for p, n in sorted(by_provider.items())) or "none", plan.sc_used)
    return PlanResult(queries=queries, new_cursor=int(new_cursor), notes=plan.notes)


def plan_comment_queries(items: list[NormalizedItem], *, resolved_endpoints: dict[str, Any], max_threads: int = 10) -> list[PlannedQuery]:
    """post_comments queries for Reddit threads (not comments) that passed the prefilter, newest first.

    Items whose `prefilter` is set and says keep=False are skipped; items without a prefilter result are
    taken as already filtered by the caller."""
    entry = _resolved_entry(resolved_endpoints if isinstance(resolved_endpoints, dict) else {},
                            "post_comments", "reddit")
    if entry is None or max_threads <= 0:
        return []
    cands = [it for it in items
             if it.platform == "reddit" and not it.context.is_comment
             and (it.prefilter is None or it.prefilter.keep)]
    cands.sort(key=lambda it: (-it.published_at.timestamp(), it.url))
    out: list[PlannedQuery] = []
    seen: set[str] = set()
    for it in cands:
        if it.url in seen:
            continue
        seen.add(it.url)
        out.append(PlannedQuery(
            provider="socialcrawl", capability="post_comments", query="", platform="reddit", family="comments",
            place_id=it.context.query_place_id, place_name=it.context.query_place_name,
            subreddit=it.context.subreddit, params={"post_url": it.url, "post_id": it.platform_post_id},
            source_scope=it.context.source_scope, priority=PRIO_COMMENTS, est_cost=_credits(entry),
        ))
        if len(out) >= max_threads:
            break
    return out
