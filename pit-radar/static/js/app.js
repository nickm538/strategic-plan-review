// App shell: routing between screens, top bar, live status, keyboard shortcuts, map <-> feed <-> drawer wiring.
import { api, currentUser, isAdmin, isReviewer, loadMe, logout } from "./api.js";
import { closeDrawer, isOpen, mountDrawer, openHotspot, openSignal } from "./drawer.js";
import { mountFeed } from "./feed.js";
import * as M from "./map.js";
import { connect } from "./sse.js";
import { emit, on, readUrl, setFilters, state } from "./store.js";
import { clear, debounce, fmtNum, h, icon, isMobile, relTime, startRelativeClock, toast } from "./util.js";

const SCREENS = [
  { id: "map", label: "Map", icon: "map", key: "1" },
  { id: "list", label: "List", icon: "list", key: null },
  { id: "review", label: "Review", icon: "review", key: "r", min: "reviewer" },
  { id: "selfreports", label: "Self-reports", icon: "heart", min: "reviewer" },
  { id: "planner", label: "PIT Planner", icon: "route" },
  { id: "health", label: "Sources & Health", icon: "pulse" },
  { id: "admin", label: "Admin", icon: "gear", min: "admin" },
];
const lazy = {};
const views = {};
let mainEl, liveEl, reviewBadge, mapBooted = false;

function allowed(s) { return !s.min || (s.min === "reviewer" ? isReviewer() : isAdmin()); }

async function boot() {
  readUrl();
  await loadMe().catch(() => { window.location.assign("/login"); });
  if (!currentUser()) return;
  state.config = await api("/api/config/client");
  buildShell();
  startRelativeClock();
  connect();
  wireEvents();
  refreshStats();
  setInterval(refreshStats, 60000);
  show(SCREENS.some((s) => s.id === state.view && allowed(s)) ? state.view : "map");
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
}

function buildShell() {
  const app = document.getElementById("app");
  clear(app);
  const user = currentUser();
  const windowSeg = h("div.seg.window-seg", { role: "group", "aria-label": "Time window" },
    ["24h", "72h", "7d", "30d"].map((w) => h("button", { "aria-pressed": state.filters.window === w ? "true" : "false", on: { click: () => setFilters({ window: w }) } }, w)));
  on("filters", () => { for (const b of windowSeg.querySelectorAll("button")) b.setAttribute("aria-pressed", b.textContent === state.filters.window ? "true" : "false"); });

  const searchInput = h("input", { type: "search", placeholder: "Search a place…", "aria-label": "Search a place", autocomplete: "off" });
  const results = h("ul.search-results.glass", { hidden: true, role: "listbox" });
  const doSearch = debounce(async () => {
    const q = searchInput.value.trim();
    if (q.length < 2) { results.hidden = true; return; }
    const data = await api(`/api/places/search?q=${encodeURIComponent(q)}`, { quiet: true }).catch(() => ({ items: [] }));
    clear(results);
    for (const p of data.items || []) {
      results.append(h("li", h("button", { role: "option", on: { click: () => { results.hidden = true; searchInput.value = p.label; show("map"); M.flyTo(p.lon, p.lat, { zoom: p.kind === "town" ? 11.5 : 14 }); } } },
        h("span", p.label), h("span.faint", p.county))));
    }
    if (!(data.items || []).length) results.append(h("li.faint", { class: "faint" }, "No match in Nassau or Suffolk"));
    results.hidden = false;
  }, 220);
  searchInput.addEventListener("input", doSearch);
  searchInput.addEventListener("keydown", (e) => { if (e.key === "Escape") { results.hidden = true; searchInput.blur(); } });

  liveEl = h("span.live", { title: "Live updates" }, h("span.dot"), h("span.label", "Connecting…"));
  const pit = state.config.pit_mode ? h("span.pit-badge", "PIT") : null;
  const menu = h("div.menu.glass", { hidden: true, role: "menu" },
    h("div.who", `${user.name} · ${user.role}`),
    h("button", { role: "menuitem", on: { click: () => { menu.hidden = true; passwordDialog(); } } }, "Change password"),
    h("button", { role: "menuitem", on: { click: () => logout() } }, "Sign out"));
  const userBtn = h("button.icon-btn", { "aria-label": "Account menu", "aria-haspopup": "menu", on: { click: () => { menu.hidden = !menu.hidden; } } }, icon("user"));
  document.addEventListener("click", (e) => { if (!menu.hidden && !menu.contains(e.target) && !userBtn.contains(e.target)) menu.hidden = true; });

  const top = h("header.topbar",
    h("div.brand", h("img", { src: "/static/icons/icon-192.png", alt: "" }), h("span", "PIT Radar"), h("small", "NY-603")),
    windowSeg, h("div.search", h("span.search-icon", icon("search")), searchInput, results), h("div.spacer"), liveEl, pit,
    h("div.user-menu", userBtn, menu));

  const rail = h("nav.rail", { "aria-label": "Screens" });
  const tabs = h("nav.tabbar", { "aria-label": "Screens" });
  for (const s of SCREENS.filter(allowed)) {
    const b = h("button.icon-btn", { title: s.label, "aria-label": s.label, dataset: { view: s.id }, on: { click: () => show(s.id) } }, icon(s.icon));
    if (s.id === "review") { reviewBadge = h("span.badge", { hidden: true }); b.append(reviewBadge); }
    rail.append(b);
  }
  for (const id of ["map", "list", "review", "planner"]) {
    const s = SCREENS.find((x) => x.id === id);
    if (!s || !allowed(s)) continue;
    tabs.append(h("button", { dataset: { view: s.id }, on: { click: () => show(s.id) } }, icon(s.icon), s.label));
  }
  mainEl = h("main.main", { id: "main" });
  const banner = h("div.banner", { role: "note" }, state.config.banner);
  app.append(h("div.app", top, rail, mainEl, tabs, banner));
}

function mapView() {
  const feedPanel = h("aside.feed-panel", { "aria-label": "Live feed", dataset: { snap: "half" } });
  const mapDiv = h("div", { id: "map", role: "region", "aria-label": "3D map of Nassau and Suffolk hotspots" });
  const kpis = h("div.kpi-strip.glass", { "aria-live": "polite" });
  const btn = (name, label, fn, pressed) => h("button.icon-btn", { title: label, "aria-label": label, "aria-pressed": pressed === undefined ? null : String(pressed), on: { click: fn } }, icon(name));
  const baseMenu = h("div.menu.glass", { hidden: true, role: "menu" },
    Object.entries(M.BASEMAPS).map(([id, label]) => h("button", { role: "menuitemradio", dataset: { basemap: id },
      on: { click: async () => { baseMenu.hidden = true; await M.setBasemap(id); markBasemap(); } } }, label)));
  const markBasemap = () => { for (const b of baseMenu.querySelectorAll("button")) b.setAttribute("aria-checked", String(b.dataset.basemap === M.currentBasemap())); };
  const baseBtn = btn("layers", "Basemap: streets, dark or aerial photos", () => { markBasemap(); baseMenu.hidden = !baseMenu.hidden; });
  const tools = h("div.map-tools",
    h("div.user-menu", baseBtn, baseMenu),
    btn("feed", "Show or hide the feed (F)", () => toggleFeed()),
    btn("cube", "2D / 3D (2, 3)", (e) => e.currentTarget.setAttribute("aria-pressed", String(M.toggle3D())), true),
    btn("hex", "Density view", (e) => e.currentTarget.setAttribute("aria-pressed", String(M.toggleDensity())), false),
    btn("play", "Tour top hotspots (T)", (e) => e.currentTarget.setAttribute("aria-pressed", String(M.toggleTour())), false),
    btn("compass", "Reset view", () => M.resetView()));
  const legend = h("div.map-legend.glass",
    h("div.row", h("span.glyph.encampment"), "Encampment"), h("div.row", h("span.glyph.individual_unsheltered"), "Person(s) outside"),
    h("div.row", h("span.glyph.vehicle_dwelling"), "Vehicle dwelling"), h("div.row", h("span.glyph.mixed"), "Mixed"),
    h("div.row.faint", "Column height = score · ring: green confirmed, yellow review"));
  const wrap = h("div.map-wrap", mapDiv, tools, legend, kpis);
  const view = h("section.view.view-map", feedPanel, wrap);
  mountDrawer(wrap);
  mainEl.append(view);
  mountFeed(feedPanel);
  views.map = view;
  views.mapKpis = kpis;
  requestAnimationFrame(() => bootMap(mapDiv));
  return view;
}

async function bootMap(mapDiv) {
  if (mapBooted) return;
  mapBooted = true;
  await M.initMap(mapDiv, state.config, currentUser().role);
  const [counties, labels] = await Promise.all([api("/api/geo/counties", { quiet: true }).catch(() => null), api("/api/geo/labels", { quiet: true }).catch(() => null)]);
  if (counties) M.setCounties(counties);
  if (labels) M.setPlaceLabels(labels.items || []);
  await loadHotspots();
}

async function loadHotspots() {
  const p = new URLSearchParams();
  for (const k of ["window", "county", "type"]) if (state.filters[k]) p.set(k, state.filters[k]);
  const data = await api(`/api/hotspots?${p.toString()}`, { quiet: true }).catch(() => null);
  if (!data) return;
  state.hotspots = new Map((data.items || []).map((x) => [x.id, x]));
  M.setHotspots(data.items || []);
}

function toggleFeed() {
  const v = views.map;
  if (!v) return;
  if (isMobile()) {
    const panel = v.querySelector(".feed-panel");
    panel.dataset.snap = panel.dataset.snap === "peek" ? "half" : "peek";
  } else {
    v.classList.toggle("feed-collapsed");
    setTimeout(M.resize, 220);
  }
}

async function show(id) {
  const s = SCREENS.find((x) => x.id === id);
  if (!s || !allowed(s)) id = "map";
  state.view = id;
  history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${id}`);
  for (const b of document.querySelectorAll("[data-view]")) {
    if (b.dataset.view === id) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  }
  for (const [k, el] of Object.entries(views)) if (el instanceof HTMLElement && k !== "mapKpis") el.hidden = k !== id;
  if (id === "map") {
    if (!views.map) mapView();
    views.map.hidden = false;
    setTimeout(M.resize, 30);
    return;
  }
  if (!views[id]) {
    const host = h("section.view");
    mainEl.append(host);
    views[id] = host;
    const mod = lazy[id] || (lazy[id] = await import(`./${id === "selfreports" ? "selfreports" : id}.js`));
    const mount = { list: "mountList", review: "mountReview", selfreports: "mountSelfReports", planner: "mountPlanner", health: "mountHealth", admin: "mountAdmin" }[id];
    mod[mount](host);
  }
  views[id].hidden = false;
  lazy[id].load();
}

async function refreshStats() {
  const s = await api("/api/stats", { quiet: true }).catch(() => null);
  if (!s) return;
  state.stats = s;
  if (reviewBadge) { reviewBadge.hidden = !s.unreviewed; reviewBadge.textContent = s.unreviewed > 99 ? "99+" : String(s.unreviewed || ""); }
  const k = views.mapKpis;
  if (k) {
    clear(k);
    const kpi = (v, label, text = false) => h("div.kpi", h(text ? "div.v.text" : "div.v", v), h("div.k", label));
    k.append(kpi(fmtNum(s.active_hotspots_7d), "active hotspots (7 d)"), kpi(fmtNum(s.new_signals_24h), "new signals (24 h)"),
      s.unreviewed !== null && s.unreviewed !== undefined ? kpi(fmtNum(s.unreviewed), "to review") : null,
      kpi(`${fmtNum(s.by_county.Nassau)} / ${fmtNum(s.by_county.Suffolk)}`, "Nassau / Suffolk"),
      kpi(s.last_scan_at ? relTime(s.last_scan_at) : "—", "last scan", true));
  }
  if (liveEl) liveEl.querySelector(".label").textContent = s.last_scan_at ? `Live · scan ${relTime(s.last_scan_at)}` : "Live · no scan yet";
}

function wireEvents() {
  on("live", (up) => {
    if (!liveEl) return;
    liveEl.classList.toggle("on", up);
    liveEl.classList.toggle("off", !up);
    if (!up) liveEl.querySelector(".label").textContent = "Reconnecting…";
  });
  on("select", async ({ kind, id, hotspotId, view, fromTour }) => {
    if (view === "map" || state.view !== "map") await show("map");
    state.selected = { kind, id };
    if (kind === "hotspot") {
      const hs = state.hotspots.get(id);
      if (hs) M.flyToHotspot(hs);
      if (!fromTour || !isMobile()) openHotspot(id);
    } else {
      const hid = hotspotId || (state.signals.get(id) || {}).hotspot_id;
      const hs = hid && state.hotspots.get(hid);
      if (hs) M.flyToHotspot(hs);
      openSignal(id);
    }
  });
  on("drawer:closed", () => { state.selected = null; M.setSelected(null); });
  on("filters", () => { if (views.map) loadHotspots(); if (state.view !== "map" && lazy[state.view]) lazy[state.view].load(); });
  on("signals", (items) => M.setSignals(items));
  on("hotspot.updated", (hs) => {
    if (!hs || !hs.id) return;
    if (hs.removed) state.hotspots.delete(hs.id); else state.hotspots.set(hs.id, hs);
    M.setHotspots([...state.hotspots.values()]);
  });
  on("scan.finished", (d) => { refreshStats(); if (d && d.status === "failed") toast("The last scan failed. See Sources & Health.", "error"); });
  on("budget.warning", (d) => { if (isReviewer()) toast(`Budget warning: ${d.provider} used ${fmtNum(d.used, 2)} of ${fmtNum(d.cap, 2)}.`, "error", 6000); });
  on("geo.status", () => { if (views.map && !document.hidden) refreshStats(); });
  window.addEventListener("pr:offline", (e) => {
    let bar = document.querySelector(".offline-bar");
    if (!bar) { bar = h("div.offline-bar", { role: "status" }); document.body.append(bar); }
    bar.textContent = `Offline: showing data saved ${e.detail && e.detail.at ? relTime(e.detail.at) : "earlier"}.`;
  });
  window.addEventListener("online", () => { const b = document.querySelector(".offline-bar"); if (b) b.remove(); refreshStats(); });
  document.addEventListener("keydown", onKey);
  window.addEventListener("resize", debounce(M.resize, 150));
}

async function onKey(e) {
  if (e.target.closest && e.target.closest("input, textarea, select, [contenteditable]")) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (state.view === "review" && lazy.review && lazy.review.handleKey(e)) { e.preventDefault(); return; }
  const k = e.key;
  if (k === "Escape" && isOpen()) { closeDrawer(); return; }
  if (k === "/") { e.preventDefault(); const s = document.querySelector(".search input"); if (s) s.focus(); return; }
  if (state.view !== "map") { if (k.toLowerCase() === "r" && isReviewer()) show("review"); return; }
  switch (k) {
    case "[": M.step(-1); break;
    case "]": M.step(1); break;
    case "t": case "T": M.toggleTour(); break;
    case "2": M.set3D(false); break;
    case "3": M.set3D(true); break;
    case "f": case "F": toggleFeed(); break;
    case "r": case "R": if (isReviewer()) show("review"); break;
    default: return;
  }
  e.preventDefault();
}

function passwordDialog() {
  const cur = h("input", { type: "password", autocomplete: "current-password", required: true });
  const nw = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: 12 });
  const err = h("div.field-error", { role: "alert" });
  const dlg = h("dialog", h("form", { method: "dialog" }, h("h2", "Change password"),
    h("label.field", "Current password", cur), h("label.field", "New password (12+ characters)", nw), err,
    h("div.actions", h("button.btn", { value: "cancel", formnovalidate: true }, "Cancel"),
      h("button.btn.primary", { value: "ok", on: { click: async (e) => {
        e.preventDefault();
        err.textContent = "";
        try { await api("/api/me/password", { method: "POST", body: { current_password: cur.value, new_password: nw.value }, quiet: true }); dlg.close(); toast("Password changed. Other sessions were signed out.", "info"); }
        catch (x) { err.textContent = x.message; }
      } } }, "Change"))));
  document.body.append(dlg);
  dlg.addEventListener("close", () => dlg.remove());
  dlg.showModal();
}

boot();
