// Admin (docs/08 §7, docs/07 §7): settings, users, self-report reply, scans, SocialCrawl endpoints, geography,
// lexicon/sources overrides, audit log. Every change is audited server-side.
import { api, currentUser } from "./api.js";
import { clear, h, nyDateTime, toast } from "./util.js";

let page;
const SETTING_HELP = {
  ANTHROPIC_MODEL: "Claude model id", CLASSIFIER_EFFORT: "low | medium | high",
  MIN_CONFIDENCE_MAP: "0-1; at or above this and all checks pass, a signal is mapped",
  MIN_CONFIDENCE_REVIEW: "0-1; between this and the map threshold goes to review",
  SCAN_INTERVAL_MIN: "minutes between scans", PIT_SCAN_INTERVAL_MIN: "minutes between scans in PIT mode",
  PIT_MODE_START: "YYYY-MM-DD (New York date)", PIT_MODE_END: "YYYY-MM-DD (New York date)",
  INGEST_MAX_AGE_DAYS: "ignore posts older than this", DAILY_LLM_BUDGET_USD: "hard daily stop for Claude spend",
  PIT_DAILY_LLM_BUDGET_USD: "daily Claude cap in PIT mode",
};

export function mountAdmin(host) { page = h("div.page"); host.append(page); }

export async function load() {
  clear(page);
  page.append(h("h1", "Admin"), h("p.lead", "Changes take effect without a redeploy and are recorded in the audit log."));
  let settings, users;
  try { [settings, users] = await Promise.all([api("/api/admin/settings"), api("/api/admin/users")]); } catch { return; }
  page.append(h("div.grid-2", controlsPanel(settings), usersPanel(users.items || [])));
  page.append(settingsPanel(settings));
  page.append(templatePanel(settings));
  page.append(h("div.grid-2", await endpointsPanel(), geoPanel()));
  page.append(h("div.grid-2", await jsonPanel("Lexicon overrides", "/api/admin/lexicon",
    "Replace whole lists by dotted name, e.g. {\"soft_exclusions\": [\"fundraiser\", \"gala\"]}. Entries starting with re: are regular expressions. Invalid entries are rejected."),
  await jsonPanel("Source overrides", "/api/admin/sources",
    "Enable or disable sources, e.g. {\"subreddits\": [{\"name\": \"longisland\", \"enabled\": true}]}. Lists replace; objects merge.")));
  page.append(await auditPanel());
}

function controlsPanel(s) {
  const pause = h("button.btn", { on: { click: () => put({ SCANS_PAUSED: !s.scans_paused }) } }, s.scans_paused ? "Resume scans" : "Pause scans");
  const pit = h("button.btn", { on: { click: () => put({ PIT_MODE_MANUAL: !s.pit_mode_manual }) } }, s.pit_mode_manual ? "Turn PIT mode off" : "Turn PIT mode on");
  const scan = h("button.btn.primary", { on: { click: async () => {
    try { await api("/api/scans", { method: "POST", body: {} }); toast("Scan started. Watch Sources & Health.", "info"); } catch { /* toast */ }
  } } }, "Run a scan now");
  return h("div.panel", h("h2", "Scanner"),
    h("p.muted", `Mode: ${s.pit_mode ? "PIT" : "standard"}${s.scans_paused ? " · paused" : ""}`), h("div.actions", scan, pause, pit));
}

async function put(body) {
  try { await api("/api/admin/settings", { method: "PUT", body }); toast("Saved.", "info", 1800); load(); } catch { /* toast */ }
}

function settingsPanel(s) {
  const keys = s.overridable.filter((k) => k in s.effective);
  const inputs = {};
  const errBox = h("div.field-error", { role: "alert" });
  const rows = keys.map((k) => {
    const v = s.effective[k];
    const inp = h("input", { type: typeof v === "number" ? "number" : "text", step: typeof v === "number" ? "any" : null, value: v === null || v === undefined ? "" : String(v), "aria-label": k });
    inputs[k] = inp;
    const overridden = k in (s.overrides || {});
    return h("tr", h("td", h("code", k), h("div.faint", SETTING_HELP[k] || "")), h("td", inp),
      h("td", overridden ? h("button.btn.small", { on: { click: () => put({ [k]: null }) } }, "Reset to default") : h("span.faint", "default")));
  });
  const save = h("button.btn.primary", { on: { click: async () => {
    errBox.textContent = "";
    const body = {};
    for (const k of keys) {
      const raw = inputs[k].value.trim();
      const old = s.effective[k];
      if (String(old ?? "") === raw) continue;
      body[k] = typeof old === "number" && raw !== "" ? Number(raw) : raw;
    }
    if (!Object.keys(body).length) { toast("No changes.", "info", 1500); return; }
    try { await api("/api/admin/settings", { method: "PUT", body, quiet: true }); toast("Settings saved.", "info", 2000); load(); }
    catch (e) {
      const errs = (e.body && e.body.errors) || {};
      errBox.textContent = Object.keys(errs).length ? Object.entries(errs).map(([k, v]) => `${k}: ${v}`).join(" · ") : e.message;
    }
  } } }, "Save settings");
  return h("div.panel", h("h2", "Budgets and thresholds"),
    h("table.data", h("thead", h("tr", h("th", "Setting"), h("th", "Value"), h("th", ""))), h("tbody", rows)), errBox, h("div.actions", save));
}

function templatePanel(s) {
  const ta = h("textarea", { "aria-label": "Self-report reply template", maxlength: 1500 });
  ta.value = s.selfreport_reply_template || "";
  const save = h("button.btn", { on: { click: () => put({ SELFREPORT_REPLY_TEMPLATE: ta.value }) } }, "Save reply");
  return h("div.panel", h("h2", "Self-report reply"),
    h("p.warn-inline", "Replace every [... verify] placeholder with a phone number or contact you have confirmed. Do not guess numbers."), ta, h("div.actions", save));
}

function usersPanel(items) {
  const me = currentUser();
  const name = h("input", { type: "text", placeholder: "Name", maxlength: 80, "aria-label": "New user name" });
  const role = h("select", { "aria-label": "Role" }, ["viewer", "reviewer", "admin"].map((r) => h("option", { value: r }, r)));
  const pw = h("input", { type: "password", placeholder: "Temporary password (12+ characters)", autocomplete: "new-password", "aria-label": "Temporary password" });
  const add = h("button.btn.small", { on: { click: async () => {
    try { await api("/api/admin/users", { method: "POST", body: { name: name.value.trim(), role: role.value, password: pw.value } }); toast("User created. Share the password privately.", "info"); load(); }
    catch { /* toast */ }
  } } }, "Add user");
  const rows = items.map((u) => {
    const sel = h("select", { "aria-label": `Role for ${u.name}` }, ["viewer", "reviewer", "admin"].map((r) => h("option", { value: r, selected: u.role === r }, r)));
    sel.addEventListener("change", async () => { try { await api(`/api/admin/users/${u.id}`, { method: "PATCH", body: { role: sel.value } }); toast("Role updated.", "info", 1800); } catch { sel.value = u.role; } });
    const active = h("button.btn.small", { disabled: me && me.name === u.name, on: { click: async () => {
      try { await api(`/api/admin/users/${u.id}`, { method: "PATCH", body: { active: !u.active } }); load(); } catch { /* toast */ }
    } } }, u.active ? "Deactivate" : "Reactivate");
    const reset = h("button.btn.small", { on: { click: async () => {
      const np = window.prompt(`New temporary password for ${u.name} (12+ characters):`);
      if (!np) return;
      try { await api(`/api/admin/users/${u.id}`, { method: "PATCH", body: { new_password: np } }); toast("Password reset; their sessions were signed out.", "info"); } catch { /* toast */ }
    } } }, "Reset password");
    return h("tr", h("td", u.name, u.locked ? h("span.warn-inline", " (locked)") : null), h("td", sel), h("td", u.active ? "active" : "inactive"),
      h("td.faint", u.last_login_at ? nyDateTime(u.last_login_at) : "never"), h("td", h("div.actions", active, reset)));
  });
  return h("div.panel", h("h2", "Users"),
    h("table.data", h("thead", h("tr", h("th", "Name"), h("th", "Role"), h("th", "Status"), h("th", "Last login"), h("th", ""))), h("tbody", rows)),
    h("div.actions", name, role, pw, add));
}

async function endpointsPanel() {
  let data = { resolved: {} };
  try { data = await api("/api/admin/socialcrawl/endpoints"); } catch { /* toast */ }
  const caps = (data.resolved && data.resolved.capabilities) || {};
  const rows = [];
  for (const [cap, plats] of Object.entries(caps)) {
    for (const [plat, ep] of Object.entries(plats || {})) {
      rows.push(h("tr", h("td", cap), h("td", plat), h("td", h("code", (ep && ep.id) || "")), h("td.num", ep && ep.credits !== undefined ? String(ep.credits) : ""),
        h("td", ep && ep.pinned ? "pinned" : "discovered")));
    }
  }
  const run = h("button.btn.small", { on: { click: async () => {
    run.disabled = true;
    try { await api("/api/admin/socialcrawl/discover", { method: "POST" }); toast("Discovery finished.", "info"); load(); } catch { /* toast */ } finally { run.disabled = false; }
  } } }, "Run discovery now");
  return h("div.panel", h("h2", "SocialCrawl endpoints"),
    h("p.muted", data.resolved && data.resolved.resolved_at ? `Resolved ${nyDateTime(data.resolved.resolved_at)}` : "Not resolved yet (needs SOCIALCRAWL_API_KEY)."),
    rows.length ? h("table.data", h("thead", h("tr", h("th", "Capability"), h("th", "Platform"), h("th", "Endpoint"), h("th", "Credits"), h("th", ""))), h("tbody", rows)) : null,
    h("div.actions", run));
}

function geoPanel() {
  const btn = h("button.btn.small", { on: { click: async () => {
    try { await api("/api/admin/geo/rebuild", { method: "POST" }); toast("Geography rebuild started. Progress shows on Sources & Health.", "info"); } catch { /* toast */ }
  } } }, "Rebuild geography");
  return h("div.panel", h("h2", "Geography"),
    h("p.muted", "County boundaries, towns, villages and hamlets come from the Census; LIRR stations and public places from OpenStreetMap. Rebuild after major data updates."),
    h("div.actions", btn));
}

async function jsonPanel(title, path, help) {
  let data = { overrides: {} };
  try { data = await api(path); } catch { /* toast */ }
  const ta = h("textarea", { "aria-label": title, spellcheck: "false" });
  ta.value = JSON.stringify(data.overrides || {}, null, 2);
  const err = h("div.field-error", { role: "alert" });
  const save = h("button.btn.small", { on: { click: async () => {
    err.textContent = "";
    let parsed;
    try { parsed = JSON.parse(ta.value || "{}"); } catch { err.textContent = "That is not valid JSON."; return; }
    try { await api(path, { method: "PUT", body: { overrides: parsed }, quiet: true }); toast("Saved and applied.", "info", 2000); }
    catch (e) {
      const errs = e.body && e.body.errors;
      err.textContent = Array.isArray(errs) ? errs.join(" · ") : e.message;
    }
  } } }, "Validate and save");
  return h("div.panel", h("h2", title), h("p.muted", help), ta, err, h("div.actions", save),
    data.override_errors && data.override_errors.length ? h("p.warn-inline", `Skipped invalid overrides: ${data.override_errors.join("; ")}`) : null);
}

async function auditPanel() {
  let data = { items: [] };
  try { data = await api("/api/admin/audit?limit=100"); } catch { /* toast */ }
  return h("div.panel", h("h2", "Audit log (latest 100)"), h("table.data",
    h("thead", h("tr", h("th", "When"), h("th", "User"), h("th", "Action"), h("th", "Object"))),
    h("tbody", (data.items || []).map((r) => h("tr", h("td", nyDateTime(r.at)), h("td", r.user || "—"), h("td", r.action),
      h("td.faint", [r.object_type, r.object_id].filter(Boolean).join(" ")))))));
}
