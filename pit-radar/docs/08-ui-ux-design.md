# 08 — UI / UX Design

A dark "operations room" interface: a full-bleed **3D map of Nassau and Suffolk** with extruded hotspot columns, a **live feed** that updates in real time, and a **detail drawer** that shows exactly why each signal was kept. It must feel fast and calm, work on a phone in a car at night, and never imply certainty the data doesn't have.

## 1. Principles

1. **Places, not people.** Every visual unit is a place. No photos, avatars, handles or descriptions anywhere (R1–R3).
2. **Show the evidence.** Every signal shows its verbatim quote(s), the source link, the date, and a checklist of the automated checks it passed. Trust comes from transparency, not polish.
3. **Distinguish AI from human.** "AI-verified" and "Staff-confirmed" look different everywhere: map, feed and exports (R12).
4. **Real data only.** Empty states say "No signals in this window" with the last scan time. There are never placeholder or demo cards.
5. **Purpose banner always visible** (R9).

## 2. Design tokens

```css
:root {
  /* surfaces (dark is the default theme) */
  --bg-0: #070B14;  --bg-1: #0D1424;  --bg-2: #141D33;  --bg-3: #1C2744;
  --line: #26324F;  --glass: rgba(13, 20, 36, 0.72);
  /* text */
  --text-1: #E8EEF8; --text-2: #A9B6CF; --text-3: #6F7D99;
  /* interactive */
  --accent: #5AB0FF; --accent-press: #3B8FE0; --focus: #9CCBFF;
  /* signal types: Okabe-Ito colorblind-safe palette, always paired with a shape */
  --type-encampment: #E69F00;  /* orange,      hexagon/tent glyph */
  --type-individual: #56B4E9;  /* sky blue,    circle glyph       */
  --type-vehicle:    #CC79A7;  /* purple-pink, rounded-square glyph */
  /* status */
  --st-confirmed: #009E73; --st-review: #F0E442; --st-dismissed: #6F7D99; --st-ai: var(--accent);
  /* type scale */
  --font-ui: "Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --font-num: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  --fs-xs: 12px; --fs-sm: 13px; --fs-md: 15px; --fs-lg: 18px; --fs-xl: 24px;
  /* shape & motion */
  --r-sm: 8px; --r-md: 12px; --r-lg: 16px;
  --ease: cubic-bezier(.2, .8, .2, 1); --t-fast: 120ms; --t-med: 200ms; --t-slow: 320ms;
}
@media (prefers-color-scheme: light) { /* optional light theme for printing/daytime */ }
```

Self-host the fonts (or use the system stack) so the app has no third-party font dependency. Glass panels: `background: var(--glass); backdrop-filter: blur(14px) saturate(140%); border: 1px solid rgba(255,255,255,.06); box-shadow: 0 10px 30px rgba(0,0,0,.35)`. Check contrast: text on `--bg-2` must meet WCAG AA, and type colors are used for marks and chips, never for body text.

## 3. Layout

### Desktop (≥ 1200 px)

```
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│ ◉ PIT Radar · NY-603   [24h][72h][7d][30d]  [Search a place…]   ● Live · scan 4m ago  PIT ▣ │ 56px top bar
├──┬───────────────────────┬──────────────────────────────────────────────────┬─────────────┤
│▣ │ LIVE FEED      ⟳ ▾ ⚙ │                                                  │ DETAIL       │
│⚑ │ [Type▾][County▾][Src▾]│            3D MAP (MapLibre + deck.gl)           │ (slides in   │
│☰ │ ┌───────────────────┐ │      extruded hotspot columns, county glow,      │  on select)  │
│⌖ │ │⬢ Ronkonkoma Sta.  │ │      outside-scope dimmed, 3D buildings          │              │
│♥ │ │ 3h · reddit       │ │                                                  │ label, score │
│⚙ │ │ Tents beside lot… │ │                                                  │ checks ✓✓✓✓  │
│  │ │ ▮▮▮▮▯ AI-verified │ │  ┌───────────── KPI glass strip ──────────────┐  │ quotes       │
│  │ │ [✓ Confirm][✕]  ↗ │ │  │ 23 active · 6 new 24h · 4 to review · N/S │  │ timeline     │
│  │ └───────────────────┘ │  └────────────────────────────────────────────┘  │ actions      │
├──┴───────────────────────┴──────────────────────────────────────────────────┴─────────────┤
│ For outreach and PIT planning only. Not for enforcement. Do not share outside LICH outreach. │ 28px banner
└───────────────────────────────────────────────────────────────────────────────────────────┘
 64px icon rail: Map · Review queue (badge) · Self-reports · PIT Planner · Sources & Health · Admin
 Feed panel 380px (collapsible) · Detail drawer 420px (closes with Esc)
```

### Mobile (≤ 768 px) — installable PWA

The map is full-screen. A **bottom sheet** has three snap points: *peek* (KPI strip), *half* (feed), *full* (feed + filters). A bottom tab bar holds Map · Feed · Review · Planner. Touch targets are ≥ 44 px. A **"Near me"** sort uses the device location in the browser only, to order by distance; it is never sent to the server or stored. Selecting a card snaps the sheet to half and flies the map to the hotspot.

## 4. The 3D map

**Stack:** MapLibre GL JS (base map) + deck.gl `MapboxOverlay` with `interleaved: true`, so deck layers share MapLibre's depth buffer with the 3D buildings. [verify current versions and the overlay API on the deck.gl docs]

**Base map:** a free vector style with building heights, e.g. OpenFreeMap; recolor it to the dark tokens in a local style JSON. Add a `fill-extrusion` layer from the buildings source (`render_height`), color `#1C2744 → #2A3A63` by height, opacity 0.55. [verify style URL, source-layer names and terms/attribution]

| Layer | Type | Spec |
|---|---|---|
| Outside-scope mask | MapLibre `fill` | Polygon = world minus Nassau ∪ Suffolk, `--bg-0` at 55% opacity. Queens/Brooklyn visibly dim |
| County outline glow | MapLibre `line` ×2 | 6 px blur at 15% + 1.5 px `#5AB0FF` at 70%; county labels at low zoom |
| Hotspot columns | deck `ColumnLayer` | Position = hotspot lat/lon; `elevation = score × k` (k chosen so the top hotspot ≈ 1,200 m at zoom 9–10); radius 140 m (≥ zoom 12) to 450 m (zoom 9); `diskResolution` 24; color = type token (mixed = white 80%); top cap brighter; **ring** (deck `ScatterplotLayer`, stroked) green if confirmed ≥ 1, yellow if any needs review. `transitions: { getElevation: 600 }` so columns grow on update |
| New-signal pulse | deck `ScatterplotLayer` | Hotspots with a signal < 24 h: expanding ring animated by a time uniform, 2.4 s loop. Disabled under `prefers-reduced-motion` |
| Density mode (toggle) | deck `HexagonLayer` | 1 km hexes, extruded, sequential blue→amber range. Aggregate-only view for presentations; hides individual columns |
| Labels | deck `TextLayer` | Top 10 hotspot labels at zoom ≥ 11, with background chip |
| Signal points | deck `ScatterplotLayer` | **Only zoom ≥ 15 and reviewer role** (R11); `precision = public_place` only |

**Camera:**
- Initial view fits the county polygons, pitch 50°, bearing −17°. `maxBounds` is slightly beyond Long Island.
- Selecting an item flies to it (`flyTo`, 1.2 s, then orbits 10°).
- A **Tour** button cycles the top N hotspots every 6 s.
- A **2D/3D** toggle (pitch 0 ↔ 50) and a compass reset.
- Keyboard: `[`/`]` previous/next hotspot, `T` tour, `2`/`3` view mode, `F` feed, `R` review queue, `/` search, `Esc` close drawer.

**Tooltip on hover (glass card):** label · type chip · "last signal 3 h ago" · signals 7 d / 30 d · independent sources · status.

## 5. Live feed

- **Card:** type glyph and color · place label · relative time (auto-updating) · platform icon · 2-line `neutral_summary` · confidence meter (5 segments) · status chip (AI-verified / Staff-confirmed / Needs review) · "Open source ↗" (new tab, `rel="noopener noreferrer"`) · reviewer actions (✓ Confirm, ✕ Dismiss with a reason menu).
- **Real-time:** an SSE `signal.new` event inserts a card at the top with a 200 ms slide and a 2 s accent glow, plus a soft count badge if the user has scrolled away ("3 new ↑"). `aria-live="polite"`, throttled to one announcement per 10 s.
- **Group by hotspot** toggle collapses cards under their hotspot.
- **Filters:** type, county, platform, status, minimum confidence, window. Filter state lives in the URL query string, so views can be bookmarked and shared internally.

## 6. Detail drawer

**Hotspot view:**
- header: label · county · status selector (active/visited/not found/resolved) · assign-team picker · score gauge
- **30-day timeline** strip: dots per signal, colored by status
- member signals, each showing:
  - **evidence quotes** as block quotes with the source link and date
  - the AI's `decision_basis`
  - model badge (e.g. `claude-opus-5-5`)
  - a **"Why this was kept"** checklist: ✓ quotes found in the post · ✓ place found in the post · ✓ inside Nassau/Suffolk · ✓ posted within window · ✓ current/ongoing. A failed check shows ✕ with the reason (needs-review items)
- notes box with the R3 warning inline
- actions: Confirm all / Mark visited / Not found / Export this hotspot.

**Signal view:** the same blocks for one signal.

## 7. Other screens

| Screen | Purpose | Key interactions |
|---|---|---|
| **Review queue** | Fast triage of `ai_verified` (unreviewed) and `needs_human_review` | Split view: list ↔ detail. Keys `J/K` move, `C` confirm, `D` dismiss (then `1–6` for the reason), `O` open source. Progress bar "12 left" |
| **Self-reports** | Town-level list of people posting about their own homelessness (R7) | Card: town · time · summary · source link · **suggested reply** (resources template: 211 Long Island, the county DSS emergency housing line, LICH outreach contact — **[verify each number before launch]**) with a Copy button; staff post it manually. "Mark responded" |
| **PIT Planner** | Turn hotspots into team routes for count night | Towns list with counts. Drag hotspots onto team columns (kanban) and the map colors them by team. "Order route" runs a nearest-neighbor tour. Export route sheet (printable), GeoJSON or CSV. PIT-date countdown. Admins toggle **PIT mode** (faster scans) |
| **Sources & Health** | Is the radar working? | Provider cards: status, last success, last error, credits remaining, today's spend. **Scan funnel** chart (collected → in window → prefiltered → classified → relevant → verified → in area → mapped) for the last scan and 7-day totals. Drop-reasons bar chart. Recent scans table |
| **Admin** | Users, budgets, thresholds, lexicon/sources, SocialCrawl endpoint map | Forms with validation; changes go to the audit log |

Charts follow the same tokens: one hue per series, direct labels, no 3D for statistical charts (3D is reserved for the map).

## 8. PWA and offline

- `manifest.webmanifest`: `name` "PIT Radar — NY-603", `short_name` "PIT Radar", `start_url` "/", `display` "standalone", `background_color` `#070B14`, `theme_color` `#0D1424`, icons 192/512 (maskable).
- Service worker:
  - precache the app shell and vendor libraries
  - **network-first** for `GET /api/hotspots`, `/api/signals` and `/api/stats`, falling back to the last cached response, with an "Offline: showing data from 9:42 PM" banner
  - never cache `/api/auth/*`, `/api/stream` or exports
  - clear the API caches on logout
- Base-map tiles are cached by the browser's normal HTTP cache only.

## 9. Accessibility and quality bar

- WCAG 2.2 AA. Every color encoding is paired with a shape or text. Visible focus rings (`--focus`). Everything is keyboard-operable.
- A **List view** (table of hotspots with the same filters) is an equivalent to the map for screen-reader users.
- `prefers-reduced-motion`: no pulses, no orbit; fly-to becomes jump-to.
- Performance: first meaningful paint < 2 s on a mid-range phone over 4G. Load the vendor bundles from the same origin with long cache headers. Lazy-load the Planner and Admin screens.
- Printing: route sheets have a dedicated print stylesheet (light theme, large type, purpose banner at the top).
