# 01 — Requirements and Guardrails

## 1. Purpose

Outreach teams need a better **map of where to look**, both for the annual unsheltered PIT count (HUD requires it on a single night in the last ten days of January) and for year-round street outreach. People often post publicly about encampments, or about someone sleeping at a station or in a park. Sometimes people post about their own homelessness. PIT Radar turns those scattered, recent, public posts into a ranked, reviewed list of **places** in Nassau and Suffolk.

PIT Radar helps decide **where to send teams**. It does **not count people**. HUD's unsheltered count still comes from observation and surveys on the night of the count; a post is a lead, never a count.

## 2. Users and roles

| Role | Who | Can |
|---|---|---|
| `viewer` | Outreach staff, PIT volunteers' team leads | See map, hotspots, feed; export place-level route sheets |
| `reviewer` | Outreach supervisors, data analyst | Everything a viewer can, plus confirm/dismiss signals, add notes, mark hotspots visited |
| `admin` | Data analyst, CoC program staff | Everything, plus manage users, trigger scans, edit lexicon/sources, see API usage |

## 3. Functional requirements

| ID | Requirement |
|---|---|
| F1 | Scan public sources on a schedule (default every 30 min; "PIT week" mode every 10 min) and on demand (admin). |
| F2 | Cover Reddit, X, TikTok, Instagram, Threads, Bluesky, public Facebook content, Google News, and local news sites, through SocialCrawl, Exa and Firecrawl. |
| F3 | Keep only items **published within the ingest window** (default 30 days; map default view 7 days). Items with no reliable publish date are discarded. |
| F4 | Keep only signals whose location resolves **inside Nassau or Suffolk County**. Queens, Brooklyn and "Long Island City" are excluded. |
| F5 | Separate relevant signals (a person or people experiencing homelessness at an identifiable place, or an encampment) from generic homelessness content (policy, statistics, fundraisers, shelter promotions, animals, metaphors). |
| F6 | For every kept signal, store **verbatim evidence quotes** and the **verbatim location text** from the post, and verify both programmatically. |
| F7 | Cluster signals into **hotspots** with a recency-weighted score and the number of independent sources. |
| F8 | Show a live feed and a 3D interactive map; push new signals to open browsers in real time. |
| F9 | Let reviewers confirm or dismiss signals and mark hotspots visited / not found, with an audit trail. |
| F10 | PIT Planner: group hotspots by town, assign to teams, export GeoJSON/CSV/printable route sheets (place-level only). |
| F11 | Self-reports (people posting about their own homelessness) go to a **separate, town-level list** with a suggested resource reply that staff send manually. Never auto-reply. |
| F12 | Show source health: last scan, per-provider errors, credits/cost used today and this month. |
| F13 | Work on phones: responsive layout, installable PWA, offline view of the last loaded data. |

## 4. Non-goals

- Identifying, describing, following or re-finding specific individuals.
- Monitoring private groups, private accounts, closed Facebook groups, Nextdoor, or anything behind a login.
- Automated replies, DMs or any outbound posting.
- Sharing data with police, code enforcement, sanitation, or any agency for enforcement or "sweeps".
- Replacing HUD PIT methodology or HMIS records.

## 5. Privacy and safety rules (enforced in code and tested)

These are product requirements, not guidelines. Each has a test in [10-testing](10-testing-and-acceptance.md).

| Rule | Requirement |
|---|---|
| **R1 — No media** | Never download, store, display or proxy images or video from posts. Store only `media_present: bool`. |
| **R2 — No poster identity** | Never store poster handles, display names, profile URLs or follower counts. Store the platform, the post URL, and a SHA-256 hash of the platform's post id for de-duplication. |
| **R3 — No descriptions of people** | Never store names, physical descriptions, clothing, age, race/ethnicity, gender, disability or health details of people described in a post. The classifier's `neutral_summary` must describe only the **situation and place** ("Post reports a tent and belongings in the wooded area behind the train station"). |
| **R4 — Minimal evidence** | Keep at most 2 evidence quotes, ≤ 30 words each, chosen to show location/situation/recency. Scrub @handles, emails, phone numbers and URLs from stored quotes. If the only usable quote contains a person's name, keep the signal but drop the quote and set `evidence_redacted = true`. |
| **R5 — Public places only** | Snap every location to a named **public** place (station, park, plaza, library, shopping-center lot, beach, transit center) or, failing that, the hamlet/village centroid. Never store a residential street address. If the post describes a private residence, keep hamlet-level only. |
| **R6 — Retention** | Delete signal-level rows (quotes, summaries, URLs) after `RETENTION_DAYS` (default 30). Hotspots keep only weekly aggregate counts by type. Raw API payloads are never persisted. |
| **R7 — Self-reports are not pins** | Self-reports are never placed on the map. Store town-level only, in their own list. |
| **R8 — Access control** | Every page and API route except `/login` and `/healthz` requires an authenticated session. No public sharing links. `robots: noindex`. |
| **R9 — Purpose limitation** | A banner on every screen and a header on every export reads: "For outreach and PIT planning only. Not for enforcement. Do not share outside LICH outreach." |
| **R10 — Audit** | Log logins, failed logins, exports, reviews and admin changes, with user, time and object. Keep the audit log 1 year. |
| **R11 — Aggregate-first views** | Default map view shows hotspots (aggregates). Individual signal points appear only when zoomed in past street level, and only for logged-in reviewers. |
| **R12 — Human in the loop** | AI verdicts are proposals. Map styling distinguishes "AI-verified, unreviewed" from "staff-confirmed". Dispatch decisions are made by staff. |

## 6. Governance (before go-live)

1. **Written approval** from LICH leadership for this use of public social media (the analyst reports supervisor approval; record it in the CoC's records).
2. **Platform terms.** SocialCrawl, Exa and Firecrawl collect public content from platforms whose terms restrict scraping, and some prohibit using data to track individuals. The place-level design (R1–R7) is chosen with that in mind, but LICH should have counsel or its ED confirm acceptable use. [verify with counsel]
3. **Data-sharing statement** added to LICH's outreach policies: PIT Radar data stays inside outreach.
4. **Key rotation.** Rotate all API keys that were shared in planning conversations.
5. **Pilot in "shadow mode"** for 2 weeks: staff review everything, nobody is dispatched solely on a signal, and precision is measured (see [10-testing](10-testing-and-acceptance.md) §5).

## 7. Quality targets

| Metric | Target |
|---|---|
| Precision of AI-verified map signals (share that staff confirm as real, relevant, in-area, recent) | ≥ 0.90 after the pilot |
| Share of mapped signals older than the selected window | 0 (hard filter) |
| Share of mapped signals outside Nassau/Suffolk | 0 (hard geofence) |
| Evidence-quote verification failures reaching the map | 0 (hard check) |
| Time from post publication to appearing in the feed (when the provider indexes it) | ≤ 1 scan interval after provider availability |
| Classifier cost per day at normal volume | Tracked and capped by `DAILY_LLM_BUDGET_USD` |
