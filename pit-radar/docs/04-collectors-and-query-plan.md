# 04 — Collectors, Normalization and Query Planning

Three providers feed the pipeline. Each collector implements one interface:

```python
class Collector(Protocol):
    name: str                                   # "socialcrawl" | "exa" | "firecrawl"
    async def run(self, queries: list[PlannedQuery], budget: Budget) -> list[NormalizedItem]: ...
    async def health(self) -> ProviderHealth: ...  # cheap/free check, e.g. SocialCrawl /v1/credits/balance
```

Every request is logged to `query_log`. Every response is converted to a `NormalizedItem` (schema: [schemas/normalized-item.schema.json](../schemas/normalized-item.schema.json)). **Author fields are dropped during normalization (R2)**; they never reach the database.

---

## 1. Facts verified on Oct 14, 2026 vs. to verify

| Item | Status | Source |
|---|---|---|
| SocialCrawl base URL `https://www.socialcrawl.dev/v1/{platform}/{resource}`, header `x-api-key`, unified envelope, list pagination, error types, credit tiers, rate limits, free catalog endpoints | **[verified]** | socialcrawl.dev/docs |
| SocialCrawl platform coverage (Reddit 14 endpoints, Twitter/X 15, Instagram 38, TikTok 37, Facebook 29, YouTube 29, Threads 6, Bluesky 4, Google News 1, Google 12, Web Scraping 22; 68 platforms / 648 endpoints total) | **[verified]** | socialcrawl.dev/docs |
| SocialCrawl **exact endpoint ids and parameters** for keyword search, subreddit listings, comments | **[verify]**, resolved at runtime from the free catalog (§2.2) | — |
| SocialCrawl `Idempotency-Key` header (replay within 24 h costs 0) | [verified via docs summary in search results; confirm on the docs page] | — |
| SocialCrawl credit **price per credit** | **[verify]** (a third-party listing quoted 2,500 credits for £15; not authoritative) | — |
| Exa and Firecrawl request/response shapes below | **[verify]** against docs.exa.ai and docs.firecrawl.dev at build time; written from prior knowledge of these APIs | — |

---

## 2. SocialCrawl

### 2.1 Envelope and errors [verified]

Every response, success or failure:

```json
{
  "success": true,
  "platform": "reddit",
  "endpoint": "/v1/reddit/…",
  "data": { "items": [ … ] },
  "pagination": { "next_cursor": "…", "has_more": true },
  "credits_used": 1,
  "credits_remaining": 2481,
  "request_id": "req-…",
  "cached": false
}
```

- **Branch on `success` first.** On failure, branch on `error.type`, never on message text.
- **Pagination.** List endpoints return `data.items` and a pagination block. Send `next_cursor` back as `cursor`; stop when `has_more` is false. The docs also describe list data as `{ items, next_cursor }`, so read the cursor from either place.
- **Computed fields** (for example `computed.engagement_rate`) are ignored by this app.
- **Credits.** Read `credits_remaining` from every response into `api_usage` and the health panel; no separate balance call is needed.

| `error.type` | HTTP | Handling |
|---|---|---|
| `MISSING_API_KEY`, `INVALID_API_KEY` | 401 | Mark provider unhealthy ("check SOCIALCRAWL_API_KEY"); stop SocialCrawl for this scan |
| `INVALID_REQUEST` | 400 | Not billed. Log; disable that endpoint+param combo until catalog refresh (the parameter map is wrong) |
| `INSUFFICIENT_CREDITS` | 402 | Mark unhealthy ("out of credits"); stop SocialCrawl until balance > 0 |
| `ENDPOINT_NOT_FOUND` | 404 | Endpoint id is wrong. Refresh catalog |
| `RESOURCE_NOT_FOUND` | 404 | Real "nothing there" (refunded); treat as empty |
| `CONCURRENCY_LIMIT` | 429 | Honor `Retry-After`; keep in-flight ≤ 10 (documented max 50) |

**Limits [verified]:** 600 requests/min and 50 in flight per key. Standard endpoints cost 1 credit, Advanced 5, Premium 10. Cache hits cost 0. Failed calls and empty results are refunded; invalid parameters are rejected before billing.

### 2.2 Runtime endpoint discovery (do this instead of hard-coding paths)

SocialCrawl's catalog endpoints are **free** and generated from its live registry [verified]:

| Call | Use |
|---|---|
| `GET /v1/utility/endpoints?search=<term>` | Find endpoints by keyword. Returns path, method, platform and credit cost |
| `GET /v1/utility/endpoint?id=<platform/resource>` | Full guide for one endpoint: parameters, cost, paging, example call |
| `GET /v1/utility/quickstart?platform=<p>` | Platform overview |
| `GET /v1/credits/balance` | Balance (0 credits); use for `health()` |
| `GET /v1/openapi.json`, `/llms.txt`, `/v1/utility/llms?platform=<p>` | Full spec / machine index |

**At startup and once a day**, `socialcrawl.discover()`:
1. For each wanted **capability** in `config/sources.yaml` → `socialcrawl.capabilities` (e.g. `keyword_search` on `reddit`, `twitter`, `tiktok`, `threads`, `bluesky`, `youtube`, `facebook`, `instagram`; `subreddit_new`; `subreddit_search`; `post_comments`; `hashtag_posts` on `instagram`/`tiktok`; `news_search` on `google_news`), call `/v1/utility/endpoints?search=…` with the capability's search words and the platform filter.
2. Pick the best match. Prefer, in order: the endpoint id pinned in `sources.yaml` if it still exists; otherwise the cheapest endpoint whose description matches the capability. Fetch `/v1/utility/endpoint?id=…` and cache the parameter list.
3. Write the resolved map to the `settings` table (`socialcrawl.resolved_endpoints`), and show it on the admin page so a human can confirm it once.
4. Before every call, validate the planned parameters against the cached parameter list: only known parameter names, and required ones present. This avoids `INVALID_REQUEST` loops.

**Build-time task:** run discovery once, inspect results, then **pin** the chosen endpoint ids in `config/sources.yaml` (`pinned_endpoint`). Discovery stays as the safety net if a pinned id disappears.

### 2.3 What to pull from SocialCrawl

| Capability | Platforms | Per scan | Notes |
|---|---|---|---|
| `subreddit_new` | Reddit, each subreddit in `sources.yaml` | 1 page | Newest posts; prefilter locally. Cheapest high-yield source for Long Island |
| `subreddit_search` | Reddit | Core terms × LI subreddits, recency-sorted if a sort/time param exists | Catches older threads with new comments |
| `post_comments` | Reddit | Only for threads that passed the prefilter | Each comment becomes its own item (sightings are often in comments) |
| `keyword_search` | X, Threads, Bluesky, TikTok, YouTube, Facebook (public) | Region-wide queries every scan; place queries on rotation | Prefer "latest/recent" sort params when the catalog shows them |
| `hashtag_posts` | Instagram, TikTok | Hashtags in `sources.yaml` (LI town hashtags combined with homelessness terms are mostly noise, so keep this list short) | Captions only; never fetch media (R1) |
| `news_search` | Google News | Region-wide terms | News about encampments counts as a signal source (`platform = news`) |

### 2.4 Mapping a SocialCrawl item to `NormalizedItem`

Field names differ by platform. Map them in one table-driven function, populated from the discovered response schemas (`/v1/utility/endpoint` shows them). Required outputs:
- `platform_post_id`
- `url` (permalink)
- `published_at`: convert unix seconds or ISO to UTC
- `text`: title + body/caption, or the comment body
- `context.subreddit`, `context.hashtags`, `context.platform_place_tag` (a platform geotag *name* if present; never coordinates of a person)
- `media_present`

**Drop** every author/user field. If the platform gives no reliable created time, **discard the item** (F3).

---

## 3. Exa [verify request/response shapes at build]

Use: semantic discovery over the open web and news, especially local outlets and forums that SocialCrawl doesn't cover.

```http
POST https://api.exa.ai/search
x-api-key: $EXA_API_KEY
Content-Type: application/json

{
  "query": "people sleeping at the Hicksville LIRR station homeless",
  "type": "auto",
  "numResults": 10,
  "startPublishedDate": "<now - INGEST_MAX_AGE_DAYS, ISO 8601>",
  "includeDomains": ["reddit.com", "newsday.com", "liherald.com", "…"],   // built from config/sources.yaml news_domains
  "contents": { "text": { "maxCharacters": 4000 } }
}
```

Read `results[].url`, `title`, `publishedDate`, `text` (and `highlights` if requested). **Discard results without `publishedDate`.** Run two query families: (a) region-wide semantic queries ("homeless encampment Long Island woods", "living in car parking lot Suffolk County"); (b) place-rotation queries. Record Exa's cost field in `api_usage` if the response includes one.

## 4. Firecrawl [verify request/response shapes at build]

Use: Google-style web and news search with time filters; scraping a page when an Exa/news result lacks text or date.

```http
POST https://api.firecrawl.dev/v2/search
Authorization: Bearer $FIRECRAWL_API_KEY
Content-Type: application/json

{
  "query": "\"Bay Shore\" homeless encampment",
  "limit": 10,
  "tbs": "qdr:w",
  "sources": ["web", "news"],
  "location": "New York, United States"
}
```

- Read `data.web[]` (`url`, `title`, `description`) and `data.news[]` (`url`, `title`, `snippet`, `date`).
- Web results often have **no publish date**. For a promising result (title/description passes the prefilter), call `POST /v2/scrape` with `{"url": …, "formats": ["markdown"], "onlyMainContent": true}`, then take the date from the page metadata (`publishedTime` / `article:published_time`). No date means discard.
- News `date` may be relative ("3 days ago"). Parse it relative to `collected_at` and set `date_source = "relative"`.

## 5. Normalization, de-duplication, date filter, prefilter

**Canonical URL.** Lowercase the host; strip tracking params (`utm_*`, `fbclid`, `si`, `s`, `ref`); drop the fragment. Rewrite `old.reddit.com` and `np.reddit.com` to `www.reddit.com`, and `twitter.com` to `x.com`.

**De-duplicate.** Use `sha256(canonical_url)`, then `sha256(normalized_text)` to catch the same story across outlets. Keep the earliest `published_at`. Merge provider lists so a hotspot can credit multiple **independent** sources. Syndicated copies of one article count as one source.

**Date filter.** Drop items where `published_at < now - INGEST_MAX_AGE_DAYS` (default 30) or `published_at > now + 1h` (clock garbage). A Reddit comment uses its own timestamp, not the thread's.

**Prefilter** (deterministic, cheap; keeps the LLM bill small). Keep an item only if all hold:
1. Text matches ≥ 1 **homelessness signal term** (lexicon `encampment_terms`, `individual_sighting_terms`, `vehicle_dwelling_terms`, `self_report_terms`, `spanish_terms`), word-boundary and case-insensitive.
2. **Long Island anchor:** a gazetteer place name or alias appears in the text, **or** the item comes from an LI-scoped source (an LI subreddit, an LI news domain, a query with a place name whose `context.query_place` appears in the text), **or** the text says "Long Island", "Nassau", "Suffolk" or "LIRR".
3. No **hard exclusion**: animal terms near "homeless" ("homeless dog/cat/kitten/puppy/pet"); an NYC-only location ("Long Island City", "Queens", "Brooklyn", "Jamaica station", "Penn Station") **without** any Nassau/Suffolk place; or an out-of-area hint ("Huntington, WV", "Levittown, PA") **without** a region anchor term. Lists: `config/lexicon.yaml` → `hard_exclusions`.

**Prefilter score** (0–1) orders the classification queue. Base 0.3, plus:
- +0.2 if a signal term sits within 15 words of a place term
- +0.2 for recency words ("tonight", "this morning", "right now", "every night", "for weeks")
- +0.1 for each additional distinct signal term (max +0.2)
- +0.1 if from an LI-scoped source
- −0.2 for soft-exclusion terms (fundraiser, donate, gala, statistics, "point-in-time count" press, legislation, "shelter beds")

## 6. Query planning and rotation

`planner.plan(scan)` builds the scan's query list from `config/lexicon.yaml` (`query_templates`) and the gazetteer:

1. **Always:**
   - `subreddit_new` for every LI subreddit
   - region-wide templates (e.g. `homeless "Long Island"`, `encampment "Suffolk County"`, `"sleeping in" LIRR station homeless`, `"living in" car "Nassau County"`) on each keyword-search platform, plus Google News and Exa
2. **Hot places:** every active hotspot's place, re-queried every scan, capped at `HOT_PLACES_PER_SCAN` (default 5).
3. **Rotation:** the next `PLACES_PER_SCAN` (default 8) places from a round-robin cursor over towns, villages, CDPs and LIRR stations (about 300 entries). Each place gets 1–2 templates on 2 rotating platforms. With these defaults the whole Island is covered roughly every 38 scans.
4. **Budget check** before each call (`Budget.charge(provider, est_cost)`). When a cap is hit, the remaining queries for that provider are deferred to the next scan (logged, never silently dropped).

### Default budgets (tune after the pilot)

PIT-mode values use the same names with a `PIT_` prefix (for example `PIT_SCAN_INTERVAL_MIN`); all are listed in `config/.env.example`. `HOT_PLACES_PER_SCAN` (default 5) is the same in both modes.

| Setting | Standard mode | PIT mode (2 weeks before the count) |
|---|---|---|
| `SCAN_INTERVAL_MIN` | 60 | 15 |
| `PLACES_PER_SCAN` | 8 | 12 |
| `SOCIALCRAWL_CREDITS_PER_SCAN` | 40 | 60 |
| `SOCIALCRAWL_CREDITS_PER_DAY` | 900 | 4,000 |
| `EXA_SEARCHES_PER_SCAN` | 6 | 10 |
| `FIRECRAWL_SEARCHES_PER_SCAN` | 6 | 10 |
| `MAX_CLASSIFICATIONS_PER_SCAN` | 60 | 120 |
| `DAILY_LLM_BUDGET_USD` | 5 | 20 |

**Cost model to fill in after verifying prices.** In standard mode SocialCrawl uses ≤ 900 credits/day (≈ 27k/month); multiply by the verified price per credit. The Claude cost model is in [05 §7](05-reasoning-engine.md). The pilot measures yield per credit by provider and platform; then cut low-yield platforms and spend the credits on high-yield ones.
