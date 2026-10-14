# 07 — API (REST + Server-Sent Events)

Base path `/api`. JSON in and out. Every route except `POST /api/auth/login` and `GET /healthz` requires a valid session cookie (R8). Mutating routes also require the `X-CSRF-Token` header, which must match the token issued at login. Roles: `viewer` < `reviewer` < `admin` (docs/01 §2). FastAPI's OpenAPI page (`/api/docs`) is **admin-only** in production.

## 1. Auth

| Method | Path | Role | Body / Notes |
|---|---|---|---|
| POST | `/api/auth/login` | public | `{ "name": "…", "password": "…" }` → sets `pr_session` cookie (HttpOnly, Secure, SameSite=Lax, 12 h). Returns `{ user: {name, role}, csrf_token }`. 5 failed attempts per name per 15 min → 15-min lockout; logs `login_failed` (R10) |
| POST | `/api/auth/logout` | any | Revokes the session |
| GET | `/api/me` | any | `{ name, role }` |

## 2. Read endpoints

Common query params: `window` = `24h` \| `72h` \| `7d` (default) \| `30d`; `county` = `Nassau` \| `Suffolk`; `type` = `encampment` \| `individual_unsheltered` \| `vehicle_dwelling`; `status`; `min_conf` (0–1); `source` (platform); `cursor`, `limit` (≤ 100).

| Method | Path | Role | Returns |
|---|---|---|---|
| GET | `/api/hotspots` | viewer | `{ items: [Hotspot], updated_at }`, sorted by score |
| GET | `/api/hotspots/{id}` | viewer | Hotspot + `signals` (member signals within the window) + `weekly` history |
| GET | `/api/signals` | viewer | `{ items: [Signal], next_cursor }`, newest first. Viewers get only mapped statuses (`ai_verified`, `confirmed`); reviewers also get `needs_human_review` and `unlocated` |
| GET | `/api/signals/{id}` | viewer | One Signal with evidence quotes, decision basis, source link |
| GET | `/api/self-reports` | reviewer | `{ items: [{id, town, county, published_at, neutral_summary, url, suggested_reply}] }`, town-level only (R7) |
| GET | `/api/review-queue` | reviewer | `needs_human_review` + `ai_verified` not yet reviewed, oldest first |
| GET | `/api/stats` | viewer | KPIs: active hotspots (7d), new signals 24h, unreviewed count, by county/type, last scan time, provider health, usage today and this month (credits, LLM $) |
| GET | `/api/scans?limit=20` | reviewer | Recent `scan_runs` with stage counts and drop reasons |
| GET | `/api/places/search?q=` | viewer | Gazetteer lookup for the map search box |
| GET | `/api/config/client` | viewer | Map defaults (center, zoom, pitch, style URL), banner text, feature flags |

### Payload shapes

```json
// Hotspot
{
  "id": 41, "label": "Ronkonkoma LIRR station area", "county": "Suffolk",
  "lat": 40.808, "lon": -73.105, "primary_type": "encampment",
  "score": 3.42, "signals_7d": 3, "signals_30d": 5, "distinct_sources_30d": 3, "confirmed_30d": 1,
  "first_seen_at": "2026-09-28T13:02:00Z", "last_signal_at": "2026-10-13T22:14:00Z",
  "status": "active", "assigned_team": null
}
// Signal
{
  "id": 9001, "hotspot_id": 41, "platform": "reddit", "provider": "socialcrawl",
  "url": "https://www.reddit.com/r/…/comments/…", "published_at": "2026-10-13T22:14:00Z",
  "signal_type": "encampment", "status": "ai_verified", "ai_confidence": 0.86, "model": "claude-opus-5-5",
  "neutral_summary": "Post reports tents in the wooded area beside the station parking lot, described as there for weeks.",
  "evidence_quotes": [{"text": "…", "supports": "location", "masked": false}], "evidence_redacted": false,
  "location_text": "…", "precision": "public_place", "place_label": "Ronkonkoma LIRR station",
  "time_reference": "ongoing", "people_count_est": null,
  "flags": {"possible_minors": false, "medical_concern": false, "weather_exposure": true, "safety_concern": false},
  "decision_basis": "…"
}
```

The coordinates above are illustrative of the shape only; real values come from the gazetteer.

## 3. Write endpoints

| Method | Path | Role | Body |
|---|---|---|---|
| POST | `/api/signals/{id}/review` | reviewer | `{ "action": "confirm" \| "dismiss" \| "needs_followup", "reason": "not_relevant" \| "outside_area" \| "too_old" \| "duplicate" \| "wrong_location" \| "other", "note": "…" }` |
| POST | `/api/hotspots/{id}/status` | reviewer | `{ "status": "visited" \| "not_found" \| "resolved" \| "active", "note": "…" }` |
| POST | `/api/hotspots/{id}/assign` | reviewer | `{ "team": "Team 3" }` |
| POST | `/api/scans` | admin | Trigger a scan now: `{ "mode": "standard" \| "pit" }` |
| PUT | `/api/admin/settings` | admin | Update budgets, thresholds, PIT mode dates |
| GET/POST/DELETE | `/api/admin/users` | admin | Manage users (passwords set via one-time link or CLI) |
| GET | `/api/admin/socialcrawl/endpoints` | admin | Resolved endpoint map from discovery (docs/04 §2.2) for confirmation |

Notes fields show an inline warning in the UI: "Describe the place and situation only — no names or descriptions of people (R3)." The server applies the same denylist check as V6 and rejects notes that fail it, with a friendly message.

## 4. Exports (R9 header on every file)

| Method | Path | Role | Format |
|---|---|---|---|
| GET | `/api/export/hotspots.geojson?window=…` | viewer | FeatureCollection of hotspots (no post URLs, no quotes) |
| GET | `/api/export/hotspots.csv?window=…` | viewer | Same, as CSV, grouped by town |
| GET | `/api/export/route-sheet?team=…` | viewer | Printable HTML route sheet: hotspots for a team, ordered by a simple nearest-neighbor tour |

Every export writes an `audit_log` row (`export`, with filters).

## 5. Live stream — `GET /api/stream` (SSE)

`Content-Type: text/event-stream`. The server sends `: ping` every 20 s to keep proxies open. The client reconnects with `Last-Event-ID`; the server keeps the last 200 events in memory for replay.

| `event:` | `data:` |
|---|---|
| `signal.new` | Signal (viewer-visible statuses only for viewers) |
| `signal.updated` | `{ id, status, reviewed_by, hotspot_id }` |
| `hotspot.updated` | Hotspot |
| `scan.started` | `{ scan_id, mode }` |
| `scan.finished` | `{ scan_id, status, stage_counts, drop_reasons, duration_ms }` |
| `provider.health` | `{ provider, status, message }` |
| `budget.warning` | `{ provider, used, cap }` |

## 6. Health

`GET /healthz` (public, no data): `{ "ok": true, "db": "ok", "scheduler": "running", "last_scan_at": "…" }`. Used by Railway's health check.
