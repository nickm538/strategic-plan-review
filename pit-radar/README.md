# PIT Radar — Build Specification

**What it is:** an internal, login-only web app for the Long Island Coalition for the Homeless (NY-603 CoC lead agency) that watches **public** social media and local news for **recent** signs of where people experiencing **unsheltered** homelessness may be staying in **Nassau or Suffolk County**. It maps those signals as **places** (hotspots), so outreach teams can plan Point-in-Time (PIT) count routes and year-round outreach.

**What it is not:** a tool for tracking or profiling individuals. It never stores photos, names, physical descriptions or poster handles. It is not for enforcement, and its data is not shared with law enforcement or code enforcement. See [docs/01](docs/01-requirements-and-guardrails.md).

> **Status:** specification only. Nothing here has been built or run yet. This spec was written in a sandbox that could not reach the data APIs. Facts confirmed against live sources on **Oct 14, 2026** are marked **[verified]**; everything marked **[verify]** must be confirmed during the build. See [docs/11](docs/11-build-plan-and-prompts.md) for the build order and ready-to-paste prompts.

## How it works (one paragraph)

Every scan (every 60 minutes by default; every 15 minutes in PIT mode), a **query planner** sends Long Island-specific keyword and place queries to three providers:
- **SocialCrawl** — Reddit, X, TikTok, Instagram, Threads, Bluesky, Facebook public content, and Google News.
- **Exa** — semantic web and news search.
- **Firecrawl** — web and news search with time filters.

Results are normalized, de-duplicated and **date-filtered**. Posts older than the window are dropped. A cheap **prefilter** keeps only items that mention both a homelessness term and a Long Island place. **Claude** then reads each remaining post and returns a strict JSON verdict:
- relevant or not, and what type of signal
- the verbatim location text
- verbatim evidence quotes
- a neutral, non-identifying summary

Code then checks the verdict:
- every quote must appear in the post
- the location must geocode **inside Nassau or Suffolk**
- the post must be recent

Only signals that pass every check reach the map. Signals cluster into **hotspots** scored by recency, confidence and independent sources. Staff review each one (confirm or dismiss) in a 3D map-and-feed interface that works on phones as an installable app. Raw post data auto-deletes after 30 days.

## Contents

| File | What it specifies |
|---|---|
| [docs/01-requirements-and-guardrails.md](docs/01-requirements-and-guardrails.md) | Goals, users, scope, non-goals, **privacy rules R1–R12**, governance |
| [docs/02-architecture.md](docs/02-architecture.md) | Components, data flow, tech stack, scan lifecycle |
| [docs/03-data-model.md](docs/03-data-model.md) | Tables, fields, retention, SQL DDL |
| [docs/04-collectors-and-query-plan.md](docs/04-collectors-and-query-plan.md) | SocialCrawl / Exa / Firecrawl integration, normalization, query planning, budgets |
| [docs/05-reasoning-engine.md](docs/05-reasoning-engine.md) | Claude classifier: model, request shape, verification rules, decision logic, costs |
| [docs/06-geography.md](docs/06-geography.md) | Geofence, gazetteer build, geocoding, disambiguation, place snapping |
| [docs/07-api.md](docs/07-api.md) | REST + Server-Sent Events API, auth, roles |
| [docs/08-ui-ux-design.md](docs/08-ui-ux-design.md) | 3D map, feed, PIT Planner, design tokens, mobile/PWA, accessibility |
| [docs/09-security-privacy-ops.md](docs/09-security-privacy-ops.md) | Threat model, auth, retention jobs, audit, Railway deployment, monitoring |
| [docs/10-testing-and-acceptance.md](docs/10-testing-and-acceptance.md) | Unit/integration tests, golden set, red-team cases, acceptance criteria |
| [docs/11-build-plan-and-prompts.md](docs/11-build-plan-and-prompts.md) | Phased build plan with Claude Code prompts per phase |
| [prompts/classifier-system.md](prompts/classifier-system.md) | Full system prompt for the classifier |
| [schemas/classification.schema.json](schemas/classification.schema.json) | Structured-output schema Claude must return |
| [schemas/normalized-item.schema.json](schemas/normalized-item.schema.json) | Common shape for every collected item |
| [config/lexicon.yaml](config/lexicon.yaml) | Search and prefilter vocabulary (English + Spanish) and exclusions |
| [config/sources.yaml](config/sources.yaml) | Candidate subreddits, news domains, hashtags (to verify) |
| [config/.env.example](config/.env.example) | Every environment variable with its default; secrets left blank |

## Secrets

API keys (SocialCrawl, Exa, Firecrawl, Anthropic) are **only** ever set as environment variables: in a local `.env` that git ignores, and in Railway's Variables. They are never written into code, docs or commits. The keys shared in the planning chat should be **rotated** before go-live.
