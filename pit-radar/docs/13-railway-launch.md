# 13 — Launch on Railway (step by step)

Total time: about 20 minutes, plus a few minutes for the first geography build. You need admin access to the
GitHub repo `nickm538/strategic-plan-review` and a Railway account.

## 1. Create the service

1. In Railway: **New Project → Deploy from GitHub repo** → choose `nickm538/strategic-plan-review`.
2. Open the new service → **Settings**:
   - **Source → Branch:** the branch you are deploying (for now `claude/gracious-hypatia-rsvh1z`, or `main` after you merge).
   - **Root Directory:** `pit-radar`
   - Railway finds `pit-radar/railway.json`, builds `pit-radar/Dockerfile`, and uses `/healthz` as the health check.
3. **Volume:** service → **Settings → Volumes → Add volume**, mount path **`/data`**. The database and the built
   geography live here and survive redeploys. (Optional: use Railway Postgres instead and set `DATABASE_URL`;
   keep the volume anyway for the geography files.)
4. Keep **one replica**: the scheduler runs inside the app.

## 2. Paste the variables (service → Variables)

Required:

| Variable | What to paste |
|---|---|
| `SOCIALCRAWL_API_KEY` | A new key from your SocialCrawl dashboard |
| `EXA_API_KEY` | A new key from your Exa dashboard |
| `FIRECRAWL_API_KEY` | A new key from your Firecrawl dashboard |
| `ANTHROPIC_API_KEY` | A new key from console.anthropic.com |
| `SESSION_SECRET` | 48+ random characters. Generate one: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `NOMINATIM_CONTACT_EMAIL` | A LICH email address (required by the OpenStreetMap geocoder's usage policy) |
| `APP_BASE_URL` | The Railway URL from step 3, e.g. `https://pit-radar-production.up.railway.app` |
| `BOOTSTRAP_ADMIN_NAME`, `BOOTSTRAP_ADMIN_PASSWORD` | Your first sign-in (see step 4) |

Every other variable in `config/.env.example` has a working default. Budgets, thresholds and PIT dates can be
changed later in the app (Admin screen) without a redeploy. **Rotate keys first:** the keys pasted in the planning
chat must be replaced with new ones.

Any provider key can be left blank to start; the app skips that provider and says so on Sources & Health.

## 3. Domain and first deploy

1. Service → **Settings → Networking → Generate Domain**. Copy it into `APP_BASE_URL`.
2. Deploy (Railway redeploys automatically when variables change). Watch **Deployments → Logs** for
   `PIT Radar started` and a passing health check.

## 4. Create the first admin (your sign-in)

The app is login-only and has no default account. Create yours with two more variables (service → Variables):

| Variable | Value |
|---|---|
| `BOOTSTRAP_ADMIN_NAME` | The name you will sign in with, e.g. `Nick` |
| `BOOTSTRAP_ADMIN_PASSWORD` | A password of 12+ characters that only you know |

Railway redeploys; on startup the app creates that admin **only if no users exist yet** (it can never add or change
an account after that). Then open your Railway URL and sign in with exactly that name and password. You can delete
both variables afterwards. Add other staff on the Admin screen (roles: viewer, reviewer, admin).

Alternative, if you use the Railway CLI: `railway ssh`, then `python scripts/create_user.py --name "Your Name" --role admin`.

## 5. What happens on first boot

1. **Geography build (2–5 minutes):** the app downloads Nassau/Suffolk boundaries, towns, villages and hamlets from the
   U.S. Census (TIGERweb) and LIRR stations plus public places from OpenStreetMap, checks every point against the
   county boundaries, and saves it to `/data/geo`. Sources & Health → Geography shows progress, then "ready". If a
   source is down it retries automatically (1, 5, 15, 60 minutes).
2. **SocialCrawl endpoint check:** it confirms the pinned endpoints against SocialCrawl's free catalog.
3. **First scan about 1 minute after geography is ready**, then every 60 minutes (every 15 in PIT mode).

## 6. Confirm it works

- `https://<your-domain>/healthz` → `{"ok": true, ...}`
- Sign in → **Sources & Health**: each provider shows green "ok" (or a clear reason, like "API key rejected").
- After the first scan: the **scan funnel** shows how many posts were collected, filtered, classified and mapped.
- The map: basemap switcher (top-right layers button) → *Streets 3D* (default), *Dark 3D*, or *Aerial photos (NYS)*.
  Zoom to street level for street names and 3D buildings; the locate button centers on your position (it never
  leaves your browser).

## 7. PIT mode

Admin → Settings → set `PIT_MODE_START` and `PIT_MODE_END` (New York dates). Between those dates the app scans every
15 minutes with higher budgets. Or turn it on immediately from the PIT Planner.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| Deploy fails, log says `SESSION_SECRET` too short | Set a 48+ character `SESSION_SECRET` |
| Health check fails right after the first deploy | Check that the volume is mounted at `/data`; the log line names the problem |
| Geography "failed" | Census or OpenStreetMap was down; it retries automatically, or Admin → Rebuild geography |
| A provider shows "unhealthy: API key rejected" | Re-paste that key in Variables (no spaces); Railway redeploys |
| "Out of credits" | Top up the provider, or lower its budget on the Admin screen |
| No signals after several scans | Sources & Health → "Why items were dropped" shows where posts were filtered; that is normal in quiet weeks |
| Map shows only counties and columns | The basemap host was unreachable from that browser/network; try another basemap from the layers button |

## 9. Costs

Default hard caps (change on the Admin screen): SocialCrawl 40 credits per scan and 900 per day; Exa and Firecrawl
6 searches per scan each; Claude $5 per day (PIT mode: 60 / 4,000 credits, 10 searches, $20 per day). Today's and
this month's spend are on Sources & Health.
