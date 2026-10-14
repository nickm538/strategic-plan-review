# 03 — Data Model

Implement with SQLAlchemy 2.x models and Alembic migrations. The DDL below is the reference (Postgres syntax; use SQLAlchemy `JSON` and `DateTime(timezone=True)` so the same models run on SQLite). Every timestamp is stored in UTC.

## 1. Entity overview

| Table | Holds | Retention |
|---|---|---|
| `users`, `sessions` | Staff accounts and active sessions | Until removed; sessions expire after 12 h |
| `scan_runs` | One row per scan, with stage counts and errors | 180 days |
| `query_log` | One row per provider request | 30 days |
| `items_seen` | De-dup memory: hashes and the decision taken, **no content** | 90 days |
| `pending_items` | Prefiltered items waiting for classification (text kept, scrubbed of handles) | **≤ 72 h**, then deleted |
| `places` | Gazetteer (public places, hamlets, villages, towns, stations) | Rebuilt by `scripts/build_geo.py` |
| `signals` | One per post that passed classification (relevant **or** needs human review) | **`RETENTION_DAYS` (30)**, then deleted (R6) |
| `hotspots` | Place-level aggregates shown on the map | Kept while active; archived after 90 days without signals |
| `hotspot_weekly` | Weekly counts per hotspot and type (no post data) | Indefinitely (aggregate) |
| `reviews` | Reviewer actions | 1 year |
| `audit_log` | Security-relevant events (R10) | 1 year |
| `api_usage` | Daily usage and cost per provider | Indefinitely |
| `geocode_cache` | Normalized location text → geocode result | 180 days |
| `scan_lock` | Single-row lock | — |
| `settings` | Admin overrides of env defaults (budgets, thresholds, PIT dates) and the resolved SocialCrawl endpoint map | Until changed |

## 2. DDL (reference)

```sql
CREATE TABLE users (
  id            SERIAL PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,           -- display/login name
  password_hash TEXT NOT NULL,                  -- argon2id
  role          TEXT NOT NULL CHECK (role IN ('viewer','reviewer','admin')),
  active        BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_login_at TIMESTAMPTZ
);

CREATE TABLE sessions (
  id          TEXT PRIMARY KEY,                 -- random 32-byte token (stored hashed)
  user_id     INT NOT NULL REFERENCES users(id),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at  TIMESTAMPTZ NOT NULL,
  revoked     BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE scan_runs (
  id            SERIAL PRIMARY KEY,
  started_at    TIMESTAMPTZ NOT NULL,
  finished_at   TIMESTAMPTZ,
  trigger       TEXT NOT NULL CHECK (trigger IN ('schedule','manual','pit_mode')),
  status        TEXT NOT NULL CHECK (status IN ('running','ok','partial','failed','skipped_locked','skipped_budget')),
  stage_counts  JSONB NOT NULL DEFAULT '{}',    -- {"collected":412,"deduped":301,"in_window":188,"prefiltered":41,"classified":41,"relevant":9,"verified":8,"in_area":7,"stored":7}
  drop_reasons  JSONB NOT NULL DEFAULT '{}',    -- {"too_old":113,"no_date":10,"no_li_place":120,"exclusion":27,"not_relevant":32,"quote_mismatch":1,"outside_area":1}
  errors        JSONB NOT NULL DEFAULT '[]'
);

CREATE TABLE query_log (
  id           BIGSERIAL PRIMARY KEY,
  scan_id      INT NOT NULL REFERENCES scan_runs(id),
  provider     TEXT NOT NULL,                   -- socialcrawl | exa | firecrawl
  endpoint     TEXT NOT NULL,                   -- e.g. 'reddit/search' (SocialCrawl endpoint id) or '/search'
  query_text   TEXT NOT NULL,
  place_id     INT REFERENCES places(id),
  status       TEXT NOT NULL,                   -- ok | empty | error | deferred
  http_status  INT,
  items        INT NOT NULL DEFAULT 0,
  credits      NUMERIC NOT NULL DEFAULT 0,
  cost_usd     NUMERIC NOT NULL DEFAULT 0,
  latency_ms   INT,
  error_type   TEXT,
  at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE items_seen (
  item_hash    TEXT PRIMARY KEY,                -- sha256(canonical_url) ; second key below
  content_hash TEXT NOT NULL,                   -- sha256(normalized text) to catch reposts
  provider     TEXT NOT NULL,
  first_seen   TIMESTAMPTZ NOT NULL,
  decision     TEXT NOT NULL,                   -- too_old | no_date | no_li_place | exclusion | not_relevant | stored | needs_review | ...
  CONSTRAINT items_seen_content UNIQUE (content_hash, provider)
);

CREATE TABLE pending_items (
  id              BIGSERIAL PRIMARY KEY,
  item_hash       TEXT NOT NULL UNIQUE,
  normalized      JSONB NOT NULL,               -- NormalizedItem, handles/emails/phones already scrubbed
  prefilter_score REAL NOT NULL,
  collected_at    TIMESTAMPTZ NOT NULL,
  attempts        INT NOT NULL DEFAULT 0,
  purge_after     TIMESTAMPTZ NOT NULL          -- collected_at + 72h
);

CREATE TABLE places (
  id         SERIAL PRIMARY KEY,
  name       TEXT NOT NULL,
  aliases    JSONB NOT NULL DEFAULT '[]',
  kind       TEXT NOT NULL CHECK (kind IN ('town','city','village','cdp','lirr_station','park','beach','mall','transit','library','hospital','other_public')),
  county     TEXT NOT NULL CHECK (county IN ('Nassau','Suffolk')),
  lat        DOUBLE PRECISION NOT NULL,
  lon        DOUBLE PRECISION NOT NULL,
  parent_id  INT REFERENCES places(id),         -- hamlet/village containing a landmark
  source     TEXT NOT NULL,                     -- census_tigerweb | osm_overpass
  source_id  TEXT NOT NULL,
  UNIQUE (source, source_id)
);

CREATE TABLE hotspots (
  id                 SERIAL PRIMARY KEY,
  place_id           INT REFERENCES places(id),
  label              TEXT NOT NULL,             -- e.g. "Bay Shore LIRR station area"
  county             TEXT NOT NULL,
  lat                DOUBLE PRECISION NOT NULL, -- snapped public-place coordinate (R5)
  lon                DOUBLE PRECISION NOT NULL,
  primary_type       TEXT NOT NULL CHECK (primary_type IN ('encampment','individual_unsheltered','vehicle_dwelling','mixed')),
  score              REAL NOT NULL DEFAULT 0,
  signals_7d         INT NOT NULL DEFAULT 0,
  signals_30d        INT NOT NULL DEFAULT 0,
  distinct_sources_30d INT NOT NULL DEFAULT 0,
  confirmed_30d      INT NOT NULL DEFAULT 0,
  first_seen_at      TIMESTAMPTZ NOT NULL,
  last_signal_at     TIMESTAMPTZ NOT NULL,
  status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','visited','not_found','resolved','archived')),
  assigned_team      TEXT,
  notes              TEXT,                       -- staff notes; UI warns: no names or descriptions (R3)
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE signals (
  id                  BIGSERIAL PRIMARY KEY,
  scan_id             INT NOT NULL REFERENCES scan_runs(id),
  provider            TEXT NOT NULL,            -- socialcrawl | exa | firecrawl
  platform            TEXT NOT NULL,            -- reddit | x | tiktok | instagram | threads | bluesky | facebook | youtube | news | web
  url                 TEXT NOT NULL,            -- canonical public URL (R2: no profile URLs)
  post_id_hash        TEXT NOT NULL,            -- sha256(platform + ':' + platform_post_id)
  published_at        TIMESTAMPTZ NOT NULL,
  collected_at        TIMESTAMPTZ NOT NULL,
  signal_type         TEXT NOT NULL CHECK (signal_type IN ('encampment','individual_unsheltered','vehicle_dwelling','self_report')),
  status              TEXT NOT NULL CHECK (status IN ('ai_verified','confirmed','dismissed','needs_human_review','unlocated','self_report')),
  neutral_summary     TEXT NOT NULL,            -- <= 240 chars, situation + place only (R3)
  evidence_quotes     JSONB NOT NULL,           -- [{"text": "...", "supports": "location|situation|recency|count", "masked": false}], <= 2 (R4; masking per docs/05 V2)
  evidence_redacted   BOOLEAN NOT NULL DEFAULT FALSE,
  location_text       TEXT,                     -- verbatim span from the post
  location_kind       TEXT,                     -- named_place | intersection | street | business | town_only | none
  place_id            INT REFERENCES places(id),
  lat                 DOUBLE PRECISION,         -- snapped (R5); NULL for self_report/unlocated
  lon                 DOUBLE PRECISION,
  precision           TEXT CHECK (precision IN ('public_place','hamlet','town')),
  county              TEXT CHECK (county IN ('Nassau','Suffolk')),
  people_count_est    INT,                      -- only if the post states a number or unambiguous count; else NULL (-1 in the verdict)
  flags               JSONB NOT NULL DEFAULT '{}', -- {"possible_minors":false,"medical_concern":false,"weather_exposure":true,"safety_concern":false}
  time_reference      TEXT NOT NULL,            -- current | ongoing | past | unclear
  ai_confidence       REAL NOT NULL,
  model               TEXT NOT NULL,            -- model that produced the verdict (records fallbacks)
  decision_basis      TEXT NOT NULL,            -- one sentence linking evidence to the decision
  hotspot_id          INT REFERENCES hotspots(id),
  reviewed_by         INT REFERENCES users(id),
  reviewed_at         TIMESTAMPTZ,
  expires_at          TIMESTAMPTZ NOT NULL,     -- collected_at + RETENTION_DAYS (R6)
  UNIQUE (post_id_hash)
);
CREATE INDEX signals_published ON signals (published_at DESC);
CREATE INDEX signals_status ON signals (status);
CREATE INDEX signals_hotspot ON signals (hotspot_id);

CREATE TABLE hotspot_weekly (
  hotspot_id  INT NOT NULL REFERENCES hotspots(id),
  week_start  DATE NOT NULL,
  signal_type TEXT NOT NULL,
  n_signals   INT NOT NULL,
  n_confirmed INT NOT NULL,
  PRIMARY KEY (hotspot_id, week_start, signal_type)
);

CREATE TABLE reviews (
  id          BIGSERIAL PRIMARY KEY,
  object_type TEXT NOT NULL CHECK (object_type IN ('signal','hotspot')),
  object_id   BIGINT NOT NULL,
  user_id     INT NOT NULL REFERENCES users(id),
  action      TEXT NOT NULL,                    -- confirm | dismiss | needs_followup | visited | not_found | resolved | reopen | assign
  reason      TEXT,                             -- for dismiss: not_relevant | outside_area | too_old | duplicate | wrong_location | other
  note        TEXT,
  at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
  id          BIGSERIAL PRIMARY KEY,
  at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  user_id     INT REFERENCES users(id),
  action      TEXT NOT NULL,                    -- login | login_failed | logout | export | review | admin_change | scan_trigger
  object_type TEXT,
  object_id   TEXT,
  details     JSONB NOT NULL DEFAULT '{}',
  ip_hash     TEXT                              -- sha256(ip + daily salt); never raw IP
);

CREATE TABLE api_usage (
  day            DATE NOT NULL,
  provider       TEXT NOT NULL,                 -- socialcrawl | exa | firecrawl | anthropic | nominatim
  requests       INT NOT NULL DEFAULT 0,
  credits        NUMERIC NOT NULL DEFAULT 0,
  input_tokens   BIGINT NOT NULL DEFAULT 0,
  output_tokens  BIGINT NOT NULL DEFAULT 0,
  cache_read_tokens BIGINT NOT NULL DEFAULT 0,
  cache_write_tokens BIGINT NOT NULL DEFAULT 0,
  cost_usd       NUMERIC NOT NULL DEFAULT 0,
  PRIMARY KEY (day, provider)
);

CREATE TABLE geocode_cache (
  query_norm  TEXT PRIMARY KEY,
  result      JSONB NOT NULL,                   -- {"lat":..,"lon":..,"county":..,"display":..,"source":"gazetteer|nominatim","place_id":..} or {"miss":true}
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE scan_lock (
  id          INT PRIMARY KEY CHECK (id = 1),
  holder      TEXT,
  acquired_at TIMESTAMPTZ,
  expires_at  TIMESTAMPTZ
);

-- Runtime settings an admin can change without a redeploy (PUT /api/admin/settings).
-- Environment variables (config/.env.example) are the defaults; a row here overrides them.
-- Secrets are never stored here.
CREATE TABLE settings (
  key         TEXT PRIMARY KEY,                 -- e.g. 'MIN_CONFIDENCE_MAP', 'PIT_MODE_START', 'socialcrawl.resolved_endpoints'
  value       JSONB NOT NULL,
  updated_by  INT REFERENCES users(id),         -- NULL when written by the app (e.g. endpoint discovery)
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## 3. Signal status machine

```
                classifier+verifier
 collected ─────────────────────────────▶ ai_verified ──confirm──▶ confirmed
     │                                        │   ▲                    │
     │ refusal / invalid JSON                 │   └──reopen────────────┤
     ▼                                        └──dismiss──▶ dismissed ◀┘
 needs_human_review ──confirm/dismiss──▶ (confirmed | dismissed)
 relevant but no usable location ───────▶ unlocated   (not mapped; listed in feed by county)
 signal_type = self_report ─────────────▶ self_report (town-level list only, R7)
```

## 4. Hotspot clustering and score

**Assignment.** A new mapped signal joins:
1. an active hotspot with the same `place_id`; else
2. the nearest active hotspot within `HOTSPOT_RADIUS_M`, default 400 m (Nassau and western Suffolk) or 800 m (East End towns: East Hampton, Southampton, Riverhead, Southold, Shelter Island); else
3. a new hotspot at the signal's snapped coordinate.

**Score** (recomputed whenever a member signal changes):

```
score = min(2.0, 1 + 0.25 * (distinct_sources_30d - 1))
        * Σ_over_member_signals_not_dismissed [ status_w * type_w * ai_confidence * 0.5 ** (age_days / HALF_LIFE_DAYS) ]

status_w: confirmed = 1.5, ai_verified = 1.0, needs_human_review = 0.5
type_w:   encampment = 1.5, individual_unsheltered = 1.0, vehicle_dwelling = 1.0
HALF_LIFE_DAYS default 7
```

`primary_type` is the type with the largest weighted share, or `mixed` when no type exceeds 60%. A hotspot with no signals in 90 days becomes `archived`. Its `hotspot_weekly` rows remain, so year-over-year PIT planning can use history without keeping any post data.
