// Live feed (docs/08 §5): cards, filters in the URL, SSE inserts with a "new" badge, group by hotspot.
import { api, isReviewer } from "./api.js";
import { emit, on, queryString, setFilters, state } from "./store.js";
import {
  DISMISS_LABEL, PLATFORM_LABEL, TYPE_LABEL, clear, glyph, h, icon, meter, relTime, sourceLink, statusChip, timeEl, toast,
} from "./util.js";

let listEl, panelEl, badgeEl, liveRegion;
let items = [];
let nextCursor = null;
let pendingNew = 0;
let lastAnnounce = 0;
let loading = false;

export function mountFeed(panel) {
  panelEl = panel;
  const reviewer = isReviewer();
  const sel = (name, label, options) => {
    const s = h("select", { "aria-label": label, on: { change: (e) => setFilters({ [name]: e.target.value }) } },
      options.map(([v, t]) => h("option", { value: v, selected: (state.filters[name] || "") === v }, t)));
    return s;
  };
  const statusOpts = [["", "All statuses"], ["ai_verified", "AI-verified"], ["confirmed", "Staff-confirmed"]];
  if (reviewer) statusOpts.push(["needs_human_review", "Needs review"], ["unlocated", "Unlocated"], ["dismissed", "Dismissed"]);
  const groupBtn = h("button.icon-btn", { title: "Group by hotspot", "aria-label": "Group by hotspot", "aria-pressed": state.filters.group === "1" ? "true" : "false",
    on: { click: () => setFilters({ group: state.filters.group === "1" ? "" : "1" }) } }, icon("list"));
  const refresh = h("button.icon-btn", { title: "Refresh", "aria-label": "Refresh feed", on: { click: () => load(true) } }, icon("refresh"));
  panel.append(
    h("button.sheet-handle", { "aria-label": "Resize feed panel", on: { click: cycleSheet } }),
    h("div.panel-head", h("h2", "Live feed"), groupBtn, refresh),
    h("div.filters",
      sel("type", "Signal type", [["", "All types"], ["encampment", "Encampment"], ["individual_unsheltered", "Person(s) outside"], ["vehicle_dwelling", "Vehicle dwelling"]]),
      sel("county", "County", [["", "Both counties"], ["Nassau", "Nassau"], ["Suffolk", "Suffolk"]]),
      sel("source", "Source", [["", "All sources"], ...Object.entries(PLATFORM_LABEL)]),
      sel("status", "Status", statusOpts),
      sel("min_conf", "Minimum confidence", [["", "Any confidence"], ["0.6", "≥ 60%"], ["0.7", "≥ 70%"], ["0.8", "≥ 80%"]]),
    ),
  );
  badgeEl = h("button.new-badge", { hidden: true, on: { click: () => { listEl.scrollTo({ top: 0, behavior: "smooth" }); hideBadge(); } } });
  listEl = h("div.feed-list", { role: "feed", "aria-busy": "false", "aria-label": "Signals" });
  liveRegion = h("div.sr-only", { "aria-live": "polite" });
  panel.append(badgeEl, listEl, liveRegion);
  listEl.addEventListener("scroll", () => {
    if (listEl.scrollTop < 20) hideBadge();
    if (nextCursor && !loading && listEl.scrollTop + listEl.clientHeight > listEl.scrollHeight - 300) loadMore();
  });
  on("filters", () => load(true));
  on("signal.new", onNew);
  on("signal.updated", onUpdated);
  on("select", ({ kind, id }) => highlight(kind, id));
  load(true);
}

function cycleSheet() {
  const order = ["peek", "half", "full"];
  const cur = panelEl.dataset.snap || "half";
  panelEl.dataset.snap = order[(order.indexOf(cur) + 1) % order.length];
}

export async function load(reset = false) {
  if (loading) return;
  loading = true;
  listEl.setAttribute("aria-busy", "true");
  try {
    const data = await api(`/api/signals${queryString({ limit: 50 })}`);
    items = data.items || [];
    nextCursor = data.next_cursor;
    state.signals = new Map(items.map((s) => [s.id, s]));
    emit("signals", items);
    if (reset) listEl.scrollTop = 0;
    render();
  } catch { /* toast shown */ } finally {
    loading = false;
    listEl.setAttribute("aria-busy", "false");
  }
}

async function loadMore() {
  loading = true;
  try {
    const data = await api(`/api/signals${queryString({ limit: 50, cursor: nextCursor })}`);
    items = items.concat(data.items || []);
    nextCursor = data.next_cursor;
    for (const s of data.items || []) state.signals.set(s.id, s);
    emit("signals", items);
    render();
  } catch { /* toast shown */ } finally { loading = false; }
}

function matchesFilters(s) {
  const f = state.filters;
  if (f.type && s.signal_type !== f.type) return false;
  if (f.county && s.county !== f.county) return false;
  if (f.source && s.platform !== f.source) return false;
  if (f.status && s.status !== f.status) return false;
  if (!f.status && s.status === "dismissed") return false;
  if (f.min_conf && (s.ai_confidence || 0) < Number(f.min_conf)) return false;
  return true;
}

function onNew(s) {
  if (!s || !s.id || state.signals.has(s.id) || !matchesFilters(s)) return;
  items.unshift(s);
  state.signals.set(s.id, s);
  emit("signals", items);
  const card = renderCard(s, true);
  if (state.filters.group === "1") render(); else { removeEmpty(); listEl.prepend(card); }
  if (listEl.scrollTop > 40) { pendingNew += 1; badgeEl.textContent = `${pendingNew} new ↑`; badgeEl.hidden = false; }
  const now = Date.now();
  if (now - lastAnnounce > 10000) {
    lastAnnounce = now;
    liveRegion.textContent = `New signal: ${TYPE_LABEL[s.signal_type] || "signal"} at ${s.place_label || s.town || "a Long Island place"}`;
  }
}

function onUpdated(s) {
  if (!s || !s.id) return;
  if (s.removed || !matchesFilters(s)) {
    items = items.filter((x) => x.id !== s.id);
    state.signals.delete(s.id);
  } else {
    const i = items.findIndex((x) => x.id === s.id);
    if (i >= 0) items[i] = s; else items.unshift(s);
    state.signals.set(s.id, s);
  }
  emit("signals", items);
  render();
}

function hideBadge() { pendingNew = 0; badgeEl.hidden = true; }
function removeEmpty() { const e = listEl.querySelector(".empty"); if (e) e.remove(); }

function render() {
  clear(listEl);
  if (!items.length) {
    const last = state.stats && state.stats.last_scan_at;
    listEl.append(h("div.empty", h("strong", "No signals in this window"),
      last ? h("span", "Last scan ", timeEl(last), ".") : h("span", "No scan has finished yet.")));
    return;
  }
  if (state.filters.group === "1") {
    const groups = new Map();
    for (const s of items) {
      const key = s.hotspot_id ? `h${s.hotspot_id}` : `p${s.place_label || s.town || "unplaced"}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(s);
    }
    for (const [, list] of groups) {
      const first = list[0];
      listEl.append(h("section.group", h("h3", glyph(first.signal_type), first.place_label || first.town || "Unplaced", h("span.faint", `${list.length}`)),
        list.map((s) => renderCard(s))));
    }
    return;
  }
  for (const s of items) listEl.append(renderCard(s));
}

export function renderCard(s, fresh = false) {
  const reviewer = isReviewer();
  const card = h(`article.card${fresh ? ".fresh" : ""}`, {
    tabindex: "0", dataset: { id: String(s.id) }, "aria-label": `${TYPE_LABEL[s.signal_type] || "Signal"} at ${s.place_label || s.town || "unknown place"}`,
    on: {
      click: () => emit("select", { kind: "signal", id: s.id, hotspotId: s.hotspot_id }),
      keydown: (e) => { if (e.key === "Enter") emit("select", { kind: "signal", id: s.id, hotspotId: s.hotspot_id }); },
    },
  },
  h("div.card-head", glyph(s.signal_type), h("span.card-title", s.place_label || s.town || (s.county ? `${s.county} County` : "Location unclear")), statusChip(s.status)),
  h("div.card-meta", timeEl(s.published_at), h("span", "·"), h("span", PLATFORM_LABEL[s.platform] || s.platform),
    s.county ? h("span", `· ${s.county}`) : null, s.evidence_redacted ? h("span.warn-inline", "· quote withheld") : null),
  h("p.card-summary", s.neutral_summary || ""),
  h("div.card-foot", meter(s.ai_confidence), h("span.grow"), sourceLink(s.url),
    reviewer && ["ai_verified", "needs_human_review", "unlocated"].includes(s.status) ? reviewButtons(s) : null));
  if (state.selected && state.selected.kind === "signal" && state.selected.id === s.id) card.classList.add("selected");
  return card;
}

export function reviewButtons(s, after) {
  const wrap = h("span.actions");
  const confirm = h("button.btn.small.good", { title: "Confirm (C)", disabled: s.status === "unlocated",
    on: { click: async (e) => { e.stopPropagation(); await review(s, "confirm"); if (after) after(); } } }, "✓ Confirm");
  const dismiss = h("button.btn.small.bad", { title: "Dismiss (D)", "aria-haspopup": "true",
    on: { click: (e) => { e.stopPropagation(); openDismissMenu(dismiss, s, after); } } }, "✕ Dismiss");
  wrap.append(confirm, dismiss);
  return wrap;
}

export function openDismissMenu(anchor, s, after) {
  const existing = document.querySelector(".dismiss-menu");
  if (existing) existing.remove();
  const menu = h("div.dismiss-menu.glass", { role: "menu" },
    Object.entries(DISMISS_LABEL).map(([k, label], i) => h("button.btn.small.ghost", { role: "menuitem",
      on: { click: async (e) => { e.stopPropagation(); menu.remove(); await review(s, "dismiss", k); if (after) after(); } } }, `${i + 1}. ${label}`)));
  anchor.parentElement.append(menu);
  const first = menu.querySelector("button");
  if (first) first.focus();
  const close = (e) => { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener("click", close, true); } };
  setTimeout(() => document.addEventListener("click", close, true), 0);
}

export async function review(s, action, reason) {
  try {
    const updated = await api(`/api/signals/${s.id}/review`, { method: "POST", body: { action, reason } });
    toast(action === "confirm" ? "Confirmed. It now shows as staff-confirmed." : "Dismissed.", "info", 2500);
    onUpdated(updated);
    emit("reviewed", updated);
    return updated;
  } catch { return null; }
}

function highlight(kind, id) {
  for (const el of listEl.querySelectorAll(".card.selected")) el.classList.remove("selected");
  if (kind === "signal") {
    const el = listEl.querySelector(`.card[data-id="${CSS.escape(String(id))}"]`);
    if (el) { el.classList.add("selected"); el.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
  }
}

export const relativeNow = relTime;
