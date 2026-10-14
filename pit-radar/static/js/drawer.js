// Detail drawer (docs/08 §6): hotspot and signal views with evidence, the "Why this was kept" checklist,
// a 30-day timeline, notes (R3 warning), status and team actions.
import { api, isReviewer } from "./api.js";
import { reviewButtons } from "./feed.js";
import { emit, on } from "./store.js";
import {
  PLATFORM_LABEL, TYPE_LABEL, clear, fmtNum, glyph, h, icon, nyDate, nyDateTime, sourceLink, statusChip, timeEl, toast,
} from "./util.js";

let drawer, body, titleEl, current = null;
const FLAG_LABEL = { possible_minors: "Possible minors", medical_concern: "Medical concern", weather_exposure: "Weather exposure", safety_concern: "Safety concern" };
const HOTSPOT_STATUS = [["active", "Active"], ["visited", "Visited"], ["not_found", "Not found"], ["resolved", "Resolved"]];

export function mountDrawer(host) {
  titleEl = h("h2", { id: "drawer-title" });
  const close = h("button.icon-btn", { "aria-label": "Close details (Esc)", on: { click: closeDrawer } }, icon("x"));
  body = h("div");
  drawer = h("aside.drawer", { "aria-labelledby": "drawer-title", "aria-hidden": "true" }, h("div.drawer-head", titleEl, close), body);
  host.append(drawer);
  on("hotspot.updated", (hs) => { if (current && current.kind === "hotspot" && hs && hs.id === current.id && !hs.removed) openHotspot(hs.id, false); });
  on("signal.updated", (s) => { if (current && current.kind === "signal" && s && s.id === current.id) openSignal(s.id, false); });
}

export const isOpen = () => drawer && drawer.classList.contains("open");

export function closeDrawer() {
  if (!drawer) return;
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  current = null;
  emit("drawer:closed", true);
}

function openShell(title) {
  titleEl.textContent = title;
  clear(body);
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
}

export async function openHotspot(id, focus = true) {
  current = { kind: "hotspot", id };
  if (focus) openShell("Loading…");
  let hs;
  try { hs = await api(`/api/hotspots/${id}`); } catch { closeDrawer(); return; }
  if (!current || current.id !== id) return;
  openShell(hs.label);
  const reviewer = isReviewer();
  body.append(
    h("section",
      h("div.card-meta", glyph(hs.primary_type), TYPE_LABEL[hs.primary_type] || hs.primary_type, `· ${hs.town ? hs.town + ", " : ""}${hs.county} County`),
      hs.review_only ? h("p.warn-inline", "Every signal here is awaiting staff review. It is not on viewers' maps yet.") : null,
      h("div.facts",
        fact(fmtNum(hs.signals_7d), "signals, 7 days"), fact(fmtNum(hs.signals_30d), "signals, 30 days"),
        fact(fmtNum(hs.distinct_sources_30d), "independent sources"), fact(fmtNum(hs.confirmed_30d), "staff-confirmed"),
        fact(fmtNum(hs.needs_review_30d), "awaiting review"), fact(fmtNum(hs.score, 2), "score")),
      h("div.gauge", { role: "img", "aria-label": `Score ${fmtNum(hs.score, 2)}` }, gaugeBar(hs.score)),
      h("p.faint", "First seen ", nyDate(hs.first_seen_at), " · last signal ", timeEl(hs.last_signal_at)),
    ),
    h("section", h("h3", "30-day timeline"), timeline(hs.signals || [])),
  );
  if (reviewer) body.append(hotspotActions(hs));
  body.append(h("section", h("h3", `Signals (${(hs.signals || []).length})`),
    (hs.signals || []).length ? (hs.signals || []).map((s) => signalBlock(s, true)) : h("p.muted", "No signals in this window.")));
  if (reviewer) body.append(notesBox(hs));
  body.append(h("section", h("h3", "Export"), h("div.actions",
    h("a.btn.small", { href: `/api/export/route-sheet?team=${encodeURIComponent(hs.assigned_team || "")}`, target: "_blank", rel: "noopener" }, icon("route"), hs.assigned_team ? `Route sheet: ${hs.assigned_team}` : "Route sheet (all)"),
  )));
  emit("drawer:hotspot", hs);
}

export async function openSignal(id, focus = true) {
  current = { kind: "signal", id };
  if (focus) openShell("Loading…");
  let s;
  try { s = await api(`/api/signals/${id}`); } catch { closeDrawer(); return; }
  if (!current || current.id !== id) return;
  openShell(s.place_label || s.town || "Signal");
  body.append(h("section", signalBlock(s, false)));
  if (s.hotspot_id) {
    body.append(h("section", h("button.btn", { on: { click: () => emit("select", { kind: "hotspot", id: s.hotspot_id }) } }, icon("map"), "Open this place's hotspot")));
  }
}

function fact(v, k) { return h("div.fact", h("div.v", v), h("div.k", k)); }

function gaugeBar(score) {
  const span = h("span");
  span.style.width = `${Math.max(4, Math.min(100, (Number(score) || 0) * 20))}%`;
  return span;
}

function timeline(signals) {
  const el = h("div.timeline", { role: "img", "aria-label": `${signals.length} signals in the last 30 days` });
  const now = Date.now();
  const span = 30 * 86400 * 1000;
  for (const s of signals) {
    const t = new Date(s.published_at).getTime();
    const x = Math.max(0, Math.min(1, 1 - (now - t) / span));
    const dot = h(`i.${s.status}`, { title: `${nyDateTime(s.published_at)} · ${s.status_label}` });
    dot.style.left = `${4 + x * 92}%`;
    el.append(dot);
  }
  el.append(h("div.tl-labels", h("span", "30 days ago"), h("span", "today")));
  return el;
}

function checklist(checks) {
  if (!checks || !checks.length) return h("p.muted", "No automated checks recorded (staff review only).");
  return h("ul.checks", checks.map((c) => h("li", h(c.passed ? "span.ok" : "span.no", { "aria-label": c.passed ? "passed" : "failed" }, c.passed ? "✓" : "✕"),
    h("span", c.label, c.detail ? h("span.detail", ` · ${c.detail}`) : null))));
}

export function signalBlock(s, compact) {
  const flags = Object.entries(s.flags || {}).filter(([, v]) => v).map(([k]) => h("span.chip", FLAG_LABEL[k] || k));
  return h("div.sig",
    h("div.card-head", glyph(s.signal_type), h("span.card-title", TYPE_LABEL[s.signal_type] || s.signal_type), statusChip(s.status)),
    h("div.card-meta", nyDateTime(s.published_at), h("span", "·"), PLATFORM_LABEL[s.platform] || s.platform,
      s.people_count_est !== null && s.people_count_est !== undefined ? h("span", `· about ${s.people_count_est} ${s.people_count_est === 1 ? "person" : "people"}`) : null),
    h("p", s.neutral_summary || ""),
    (s.evidence_quotes || []).map((q) => h("blockquote", { title: q.masked ? "Gendered words replaced for privacy" : "" }, `“${q.text}”`)),
    s.evidence_redacted ? h("p.warn-inline", "A quote was withheld by the privacy rules. Open the source to check.") : null,
    s.review_reason ? h("p.warn-inline", `Needs review: ${s.review_reason.replace(/_/g, " ")}`) : null,
    flags.length ? h("div.flags", flags) : null,
    s.decision_basis ? h("p.basis", h("strong", "AI basis: "), s.decision_basis) : null,
    h("details", { open: !compact }, h("summary", "Why this was kept"), checklist(s.checks)),
    h("div.card-foot", s.model ? h("span.model-badge", s.model) : null, h("span.grow"), sourceLink(s.url),
      isReviewer() && ["ai_verified", "needs_human_review", "unlocated"].includes(s.status) ? reviewButtons(s) : null,
      isReviewer() && ["confirmed", "dismissed"].includes(s.status) ? h("button.btn.small", { on: { click: () => reopen(s) } }, "Reopen") : null),
  );
}

async function reopen(s) {
  try {
    const u = await api(`/api/signals/${s.id}/review`, { method: "POST", body: { action: "reopen" } });
    emit("signal.updated", u);
    toast("Reopened for review.", "info", 2500);
  } catch { /* toast shown */ }
}

function hotspotActions(hs) {
  const status = h("select", { "aria-label": "Hotspot status" }, HOTSPOT_STATUS.map(([v, t]) => h("option", { value: v, selected: hs.status === v }, t)));
  status.addEventListener("change", async () => {
    try { await api(`/api/hotspots/${hs.id}/status`, { method: "POST", body: { status: status.value } }); toast("Status updated.", "info", 2000); }
    catch { status.value = hs.status; }
  });
  const team = h("input", { type: "text", value: hs.assigned_team || "", placeholder: "Team (e.g. Team 3)", maxlength: 40, "aria-label": "Assigned team" });
  const save = h("button.btn.small", { on: { click: async () => {
    try { await api(`/api/hotspots/${hs.id}/assign`, { method: "POST", body: { team: team.value.trim() || null } }); toast("Team saved.", "info", 2000); emit("teams:changed", true); }
    catch { /* toast */ }
  } } }, "Assign");
  const confirmAll = h("button.btn.small.good", { on: { click: async () => {
    const pending = (hs.signals || []).filter((s) => ["ai_verified", "needs_human_review"].includes(s.status));
    for (const s of pending) {
      try { await api(`/api/signals/${s.id}/review`, { method: "POST", body: { action: "confirm" }, quiet: true }); } catch { /* skip */ }
    }
    toast(`Confirmed ${pending.length} signal(s).`, "info", 2500);
    openHotspot(hs.id, false);
  } } }, "✓ Confirm all");
  return h("section", h("h3", "Outreach"),
    h("div.actions", h("label.field", "Status", status), h("label.field", "Team", h("div.actions", team, save))),
    h("div.actions", confirmAll));
}

function notesBox(hs) {
  const ta = h("textarea", { maxlength: 2000, "aria-describedby": "notes-warn", "aria-label": "Notes" });
  ta.value = hs.notes || "";
  const err = h("div.field-error", { role: "alert" });
  const save = h("button.btn.small", { on: { click: async () => {
    err.textContent = "";
    try { await api(`/api/hotspots/${hs.id}/notes`, { method: "PUT", body: { notes: ta.value }, quiet: true }); toast("Notes saved.", "info", 2000); }
    catch (e) { err.textContent = e.message; }
  } } }, "Save notes");
  return h("section", h("h3", "Notes"),
    h("p.warn-inline", { id: "notes-warn" }, "Describe the place and situation only. No names or descriptions of people (R3)."),
    ta, err, h("div.actions", save));
}
