# 11 — Build Plan and Claude Code Prompts

Build in this order. Each phase ends with a check you can see, and later phases depend on earlier ones. The prompts are written to paste into Claude Code in a session where **you** control permissions and network (local machine recommended; see docs/09 §7), with this repository checked out.

**Before you start (Phase 0, by hand):**
1. Rotate the SocialCrawl, Exa, Firecrawl and Anthropic keys shared during planning. Put the new ones in `pit-radar/.env`, copying `config/.env.example`. `.env` is git-ignored.
2. Record leadership approval (docs/01 §6).
3. Confirm credits: SocialCrawl balance (`/v1/credits/balance`, free) and the Exa/Firecrawl plans.

---

### Phase 1 — Scaffold, config, database, auth (≈ 1 day)

**Done when:** `uvicorn` serves a login page; a CLI-created admin can log in and out; every other route returns 401; Alembic migrations create all tables from docs/03; tests pass.

```text
Read pit-radar/README.md and pit-radar/docs/01..03, 07 (auth section), 09 (§1-3). In pit-radar/, scaffold the FastAPI app exactly per the repository layout in docs/02 §3: app/main.py, config.py (pydantic-settings reading every variable in config/.env.example), db.py, models.py with the SQLAlchemy 2.x models for every table in docs/03 §2, Alembic migrations, auth.py (argon2id, session table, CSRF, lockout, roles), the /api/auth/* and /api/me routes, /healthz, scripts/create_user.py, security headers + CSP from docs/09 §2, and a minimal static login page. Never hard-code secrets; read them from the environment. Add pytest tests for auth and for "every route except login/healthz is 401 without a session". Pin all dependency versions in requirements.txt. Run the tests and show me the results.
```

### Phase 2 — Geography data (≈ 1 day, needs network)

**Done when:** `data/li_counties.geojson`, `data/nyc_li_counties.geojson`, `data/li_places.json` and `data/README.md` exist with counts and sources; every place passes the geofence; geofence and geocoder unit tests pass.

```text
Read pit-radar/docs/06-geography.md. Write scripts/build_geo.py that fetches Nassau (36059), Suffolk (36103), Queens (36081) and Kings (36047) county polygons from the Census TIGERweb REST service (first read the service directory to get the right layer ids and fields), towns/cities, villages and CDPs from TIGERweb, and LIRR stations plus public landmarks from OpenStreetMap Overpass as listed in docs/06 §2.2. Save raw responses under data/raw/ (git-ignored), build the outputs, validate every place with point-in-polygon, and write data/README.md with sources, retrieval date and counts by kind and county. Never invent coordinates. Then implement app/pipeline/geo/ (gazetteer, nominatim client with User-Agent + 1 req/s + cache, geofence, snapping) per docs/06 §3-4 with the tests listed in docs/10 §1. Show me the counts and test results.
```

### Phase 3 — Collectors (≈ 2 days, needs network and keys)

**Done when:** `scripts/smoke_providers.py` prints real results from each provider; SocialCrawl discovery resolves every capability; endpoint ids are pinned in `config/sources.yaml`; normalization/dedupe/date-filter/prefilter tests pass.

```text
Read pit-radar/docs/04-collectors-and-query-plan.md, schemas/normalized-item.schema.json, config/sources.yaml and config/lexicon.yaml. First verify every item marked [verify] in docs/04 against the live docs (docs.exa.ai, docs.firecrawl.dev, and SocialCrawl's free /v1/utility/* catalog endpoints with SOCIALCRAWL_API_KEY from .env) and update docs/04 with what you find. Then implement the Collector protocol, socialcrawl.py with runtime endpoint discovery and parameter validation (docs/04 §2.2), exa.py, firecrawl.py (including scrape-for-date), normalize.py (drop all author fields — privacy rule R2), dedupe.py, datefilter.py, prefilter.py and planner.py with budgets and the place rotation (docs/04 §5-6). Write scripts/smoke_providers.py per docs/10 §2 and run it with small limits; show me the item field names returned by each SocialCrawl endpoint and the credits used, and propose the pinned endpoint ids for config/sources.yaml. Unit tests per docs/10 §1.
```

### Phase 4 — Reasoning engine (≈ 1–2 days)

**Done when:** real items classify end to end; the verifier tests pass; cache reads are > 0 on repeated calls; the red-team fixtures in docs/10 §3 produce the expected outcomes.

```text
Read pit-radar/docs/05-reasoning-engine.md, prompts/classifier-system.md and schemas/classification.schema.json. Use the official anthropic Python SDK (AsyncAnthropic) and check the installed SDK's docs/types before writing code; do not guess parameters. Implement classifier.py per docs/05 §1-4 (model from ANTHROPIC_MODEL, default claude-opus-5-5; structured output via output_config.format; cached system block built from the prompt + reference place list generated from data/li_places.json; server-side refusal fallback; refusal/max_tokens/invalid-JSON handling; usage recording) and verifier.py with checks V1-V8 and the decision table in docs/05 §5-6. Load the privacy denylist and patterns from config/lexicon.yaml and show me any entries you think are too broad or missing. Write the unit tests and the red-team fixtures from docs/10 §3, then classify 5 real items collected in Phase 3 and show me verdicts, verifier results, token usage and cost.
```

### Phase 5 — Pipeline, hotspots, scheduler, retention (≈ 1–2 days)

**Done when:** a scheduled scan runs end to end against live providers; `scan_runs.stage_counts` and `drop_reasons` are populated; hotspots appear in the DB; the retention job deletes expired rows.

```text
Read pit-radar/docs/02 §4-5, docs/03 §3-4 and docs/09 §3-4. Implement runner.py (lock, plan, collect, normalize, dedupe, date filter, prefilter, classify, verify, geocode, geofence, privacy choke point, persist, hotspot re-aggregation, SSE publish, usage), privacy.py as the only DB write path for signals, hotspots.py (assignment + score + weekly rollups), retention.py and the in-process scheduler with standard/PIT modes and budgets. Run one manual scan with small budgets against the live providers and show me the stage counts, drop reasons, stored signals (summaries only) and hotspots.
```

### Phase 6 — API and live stream (≈ 1 day)

**Done when:** every endpoint in docs/07 works with role checks; SSE pushes `signal.new` during a scan; exports carry the R9 header and write audit rows.

```text
Read pit-radar/docs/07-api.md. Implement every route, the SSE stream with Last-Event-ID replay and pings, the exports (GeoJSON, CSV, printable route sheet) with the R9 header and audit rows, rate limits, and the admin endpoints including the SocialCrawl endpoint map view. Add API tests for roles, CSRF and exports. Show me the OpenAPI route list and test results.
```

### Phase 7 — Front end: 3D map, feed, drawer, screens, PWA (≈ 3–4 days)

**Done when:** the UI matches docs/08 on desktop and mobile; live updates animate in; the map shows dimmed out-of-scope areas, county glow, extruded hotspot columns, pulses and tooltips; review keyboard flow works; the PWA installs and shows cached data offline.

```text
Read pit-radar/docs/08-ui-ux-design.md fully. First verify current stable versions of maplibre-gl and deck.gl (npm), the deck.gl MapboxOverlay interleaved API, and a free no-key vector basemap with building heights (check terms/attribution); record the choices in docs/08. Build the static front end with no build step (ES modules) using the design tokens in docs/08 §2: top bar, icon rail, live feed with SSE, 3D map with every layer in docs/08 §4, detail drawer with the "Why this was kept" checklist, review queue with keyboard triage, self-reports, PIT Planner with team columns and route export, Sources & Health with the scan funnel chart, admin forms, purpose banner, mobile bottom sheet, PWA manifest + service worker per docs/08 §8, and the accessible List view. Render all post-derived text with textContent only. Vendor maplibre-gl and deck.gl via package.json and the Dockerfile vendor stage (docs/09 §5). Use only real API data; empty states, no placeholder content. Take Playwright screenshots at 1440x900 and 390x844 and show them to me.
```

### Phase 8 — Hardening and tests (≈ 1 day)

**Done when:** the full test suite is green; a security review finds no high issues; CSP has no violations in the browser console.

```text
Run the full test suite and fix failures. Then review pit-radar for security issues against docs/09 §1-3 (auth, CSRF, CSP, XSS via post text, secrets in logs, privacy choke point bypasses, rate limits) and against privacy rules R1-R12 in docs/01 §5; fix what you find and add regression tests. Show me the findings and fixes.
```

### Phase 9 — Deploy to Railway (≈ ½ day)

**Done when:** the Railway URL serves the app over HTTPS; health checks pass; a scheduled scan runs in production; the first admin logs in.

```text
Read pit-radar/docs/09 §5. Verify the Railway details marked [verify] against docs.railway.com, finalize pit-radar/railway.json and the Dockerfile, and give me step-by-step instructions to: connect the repo with Root Directory pit-radar, add a volume at /data (or Postgres), set every variable from config/.env.example (I will paste the secrets myself), set the health check, generate the domain, and create the first admin with scripts/create_user.py. Do not print or store any secret.
```

### Phase 10 — Pilot in shadow mode (2 weeks), then go-live

1. Run standard mode. Reviewers triage daily; nobody is dispatched solely on an AI signal.
2. Build the golden set (docs/10 §4); tune thresholds, lexicon, prompt and effort; measure cost per useful signal by provider and platform, and cut low-yield queries.
3. Check acceptance criteria A1–A10 (docs/10 §5).
4. Turn on **PIT mode** two weeks before the January count; export team route sheets the day before.
