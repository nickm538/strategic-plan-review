# 09 — Security, Privacy Operations and Deployment

## 1. Threat model (what we protect, from whom)

| Asset | Threat | Control |
|---|---|---|
| Hotspot map (where vulnerable people may be) | Leak or misuse for harassment, sweeps or enforcement | Login-only (R8); place-level data (R5); no media or identity (R1–R3); purpose banner and export header (R9); audit log (R10); retention (R6); no public sharing links |
| API keys (SocialCrawl, Exa, Firecrawl, Anthropic) | Exposure in code, logs or the browser | Environment variables only; never sent to the front end; redacted in logs; rotated before go-live and at least every 6 months |
| Staff accounts | Password guessing, session theft | Argon2id hashes; lockout after 5 failures/15 min; HttpOnly + Secure + SameSite cookies; CSRF token on writes; 12 h session expiry; logout revokes server-side |
| The classifier | Prompt injection inside posts ("ignore your instructions…") | Posts wrapped in `<post>` tags and declared untrusted data (system prompt); strict JSON schema; verifier V1–V8 (an injected instruction can't make a fake quote pass V1 or a fake place pass the geofence) |
| The app itself | XSS from post text, SSRF from URLs | Render all post-derived text with `textContent` (never `innerHTML`); strict Content Security Policy; the server never fetches arbitrary URLs except through the provider APIs and Nominatim |

## 2. HTTP hardening

- **CSP:** `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob: <tile host>; connect-src 'self' <tile host> <style host>; worker-src 'self' blob:; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`. MapLibre may need `blob:` workers. [verify against the MapLibre CSP notes]
- Headers: `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` (also prevents leaking the app URL when staff open source links), `Permissions-Policy: geolocation=(self)`, `X-Robots-Tag: noindex, nofollow`.
- Rate limits: login (above); API 120 req/min/session; export 20/hour/user.

## 3. Privacy choke point

`app/pipeline/privacy.py` is the **only** path from pipeline objects to the database. It:
- drops author fields (R2) and media URLs (R1)
- hashes `platform_post_id`
- enforces R4 on quotes and R3 on summaries (via verifier V2/V6)
- snaps coordinates (R5)
- strips coordinates for self-reports (R7)
- sets `expires_at`

Unit tests assert that no field outside the allow-list can reach `signals` (docs/10 §1).

## 4. Retention jobs (hourly, `app/pipeline/retention.py`)

1. Delete `signals` where `expires_at < now()`, after rolling their counts into `hotspot_weekly`.
2. Delete `pending_items` where `purge_after < now()`.
3. Delete `query_log` older than 30 days and `items_seen` older than 90 days.
4. Archive hotspots with no signal in 90 days; clear their `notes` and `assigned_team`.
5. Delete `audit_log` and `reviews` older than 1 year.
6. Record what was deleted (counts only) in the scan health panel.

## 5. Deployment on Railway

> Railway specifics below are standard practice but **[verify]** each against current Railway docs at build time (Root Directory, `railway.json` keys, volume behavior, health checks).

**Service setup**
1. In Railway: *New Project → Deploy from GitHub repo* → select `nickm538/strategic-plan-review`. Set **Root Directory** to `pit-radar`. Railway builds `pit-radar/Dockerfile`.
2. Add a **Volume** mounted at `/data` (SQLite file `/data/pit-radar.db`), **or** add the Postgres plugin and set `DATABASE_URL`. Use one replica (the scheduler runs in-process and SQLite is single-writer).
3. Set **Variables** from `config/.env.example`: all API keys, `SESSION_SECRET` (32+ random bytes), `APP_BASE_URL`, budgets, `NOMINATIM_CONTACT_EMAIL`. Set the variables in Railway only; never commit them.
4. Health check path `/healthz`. Generate a Railway domain; optionally map a LICH subdomain.
5. Create the first admin by running `python scripts/create_user.py --name <name> --role admin` in a Railway shell. It prompts for the password and never echoes it.

**`pit-radar/railway.json` (sketch, verify schema):**
```json
{
  "$schema": "https://railway.com/railway.schema.json",
  "build": { "builder": "DOCKERFILE", "dockerfilePath": "Dockerfile" },
  "deploy": {
    "startCommand": "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'",
    "healthcheckPath": "/healthz",
    "healthcheckTimeout": 60,
    "restartPolicyType": "ON_FAILURE",
    "numReplicas": 1
  }
}
```

**Dockerfile (sketch):**
```dockerfile
FROM node:22-slim AS vendor
WORKDIR /v
COPY package.json package-lock.json ./
RUN npm ci --omit=dev        # maplibre-gl + deck.gl only, pinned versions
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=vendor /v/node_modules/maplibre-gl/dist ./static/vendor/maplibre-gl
COPY --from=vendor /v/node_modules/deck.gl/dist.min.js ./static/vendor/deck.gl/dist.min.js
RUN useradd -m app && mkdir -p /data && chown app /data
USER app
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
```
**[verify]** the deck.gl standalone bundle path inside the npm package for the pinned version.

## 6. Monitoring and runbook

| Signal | Where | Action |
|---|---|---|
| Provider unhealthy (401/402/403) | Health panel + SSE `provider.health` + daily email (optional) | Check or rotate the key; top up credits |
| Scan failed or partial 3× in a row | Health panel banner | Check Railway logs (`scan_id`) |
| Budget warning at 80% / stop at 100% | Banner + SSE `budget.warning` | Adjust caps or wait for the reset (midnight America/New_York) |
| Refusal rate > 2%/day | Health panel | Review prompt wording; read refusal categories |
| Precision drop (reviewer dismiss rate > 20% over 7 days) | Health panel | Tune lexicon, thresholds or prompt; check for a new false-positive pattern |

**Incident rule:** if the map or exports are exposed outside LICH, rotate `SESSION_SECRET` (logs everyone out), rotate the API keys, review `audit_log` exports, and notify LICH leadership the same day.

## 7. Using Claude Code to build and run this

The planning sandbox's network policy blocked the provider hosts, and its auto-mode safety check blocked live data collection. To build with Claude Code:
- **Local machine** (recommended for this project): you control permission prompts and network.
- **Cloud environment:** add these to the environment's allowed domains: `www.socialcrawl.dev`, `api.exa.ai`, `api.firecrawl.dev`, `api.anthropic.com`, `nominatim.openstreetmap.org`, `overpass-api.de`, `tigerweb.geo.census.gov`, `www2.census.gov`, `registry.npmjs.org`, plus the base-map tile/style host you choose. Use a permission mode that asks you to approve actions.
