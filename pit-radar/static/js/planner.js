// PIT Planner (docs/08 §7): towns list, team kanban (drag and drop + keyboard), route sheets, exports,
// PIT-date countdown (New York time), admin PIT-mode toggle.
import { api, isAdmin, isReviewer } from "./api.js";
import { emit, state } from "./store.js";
import { TYPE_LABEL, TZ, clear, fmtNum, glyph, h, icon, nyDate, nyDay, toast } from "./util.js";

let page;
const UNASSIGNED = "__none__";
const SWATCHES = ["#5AB0FF", "#E69F00", "#CC79A7", "#009E73", "#F0E442", "#56B4E9", "#D55E00", "#A9B6CF"];

export function mountPlanner(host) { page = h("div.page"); host.append(page); }

export async function load() {
  clear(page);
  let hs, cfg;
  try {
    [hs, cfg] = await Promise.all([api("/api/hotspots?window=30d"), api("/api/config/client")]);
  } catch { return; }
  const hotspots = hs.items || [];
  const reviewer = isReviewer();
  page.append(h("h1", "PIT Planner"),
    h("p.lead", "Turn hotspots into team routes for count night. Assign each place to a team, then print the route sheet."));

  const top = h("div.grid-3");
  top.append(countdownPanel(cfg), exportsPanel());
  if (isAdmin()) top.append(pitModePanel(cfg));
  page.append(top);

  // towns
  const towns = new Map();
  for (const x of hotspots) {
    const key = `${x.town || "Unknown town"}|${x.county}`;
    if (!towns.has(key)) towns.set(key, { town: x.town || "Unknown town", county: x.county, n: 0, signals: 0 });
    const t = towns.get(key); t.n += 1; t.signals += x.signals_30d;
  }
  const townRows = [...towns.values()].sort((a, b) => b.signals - a.signals);
  page.append(h("div.panel", h("h2", "Towns"), townRows.length
    ? h("table.data", h("thead", h("tr", h("th", "Town"), h("th", "County"), h("th", "Hotspots"), h("th", "Signals (30 d)"))),
      h("tbody", townRows.map((t) => h("tr", h("td", t.town), h("td", t.county), h("td.num", fmtNum(t.n)), h("td.num", fmtNum(t.signals))))))
    : h("p.muted", "No hotspots in the last 30 days.")));

  // kanban
  const teams = new Set(hotspots.map((x) => x.assigned_team).filter(Boolean));
  const extra = (state.plannerTeams || []).filter((t) => !teams.has(t));
  const lanes = [UNASSIGNED, ...[...teams].sort(), ...extra];
  const board = h("div.kanban", { role: "list" });
  const addInput = h("input", { type: "text", placeholder: "New team name", maxlength: 40, "aria-label": "New team name" });
  const add = h("button.btn.small", { disabled: !reviewer, on: { click: () => {
    const name = addInput.value.trim();
    if (!name) return;
    state.plannerTeams = [...(state.plannerTeams || []), name];
    load();
  } } }, "Add team");
  page.append(h("div.panel", h("h2", "Teams"), h("p.muted", reviewer
    ? "Drag a place onto a team, or focus a place and press 0-9 to move it (0 = unassigned)."
    : "Team assignments (reviewers can change them)."),
  reviewer ? h("div.actions", addInput, add) : null, board));

  lanes.forEach((lane, i) => {
    const color = SWATCHES[i % SWATCHES.length];
    const members = hotspots.filter((x) => (lane === UNASSIGNED ? !x.assigned_team : x.assigned_team === lane));
    const laneEl = h("section.lane", { role: "listitem", dataset: { team: lane }, "aria-label": lane === UNASSIGNED ? "Unassigned" : lane },
      h("h3", h("span.swatch", { class: "swatch" }), lane === UNASSIGNED ? "Unassigned" : lane, h("span.faint", `${members.length}`)),
      lane !== UNASSIGNED ? h("a.btn.small", { href: `/api/export/route-sheet?team=${encodeURIComponent(lane)}&window=30d`, target: "_blank", rel: "noopener" }, icon("route"), "Order route & print") : null,
      members.map((x) => miniCard(x, lanes, reviewer)));
    laneEl.querySelector(".swatch").style.background = color;
    if (reviewer) {
      laneEl.addEventListener("dragover", (e) => { e.preventDefault(); laneEl.classList.add("drop-target"); });
      laneEl.addEventListener("dragleave", () => laneEl.classList.remove("drop-target"));
      laneEl.addEventListener("drop", (e) => {
        e.preventDefault(); laneEl.classList.remove("drop-target");
        const id = Number(e.dataTransfer.getData("text/plain"));
        if (id) assign(id, lane);
      });
    }
    board.append(laneEl);
  });
}

function miniCard(x, lanes, reviewer) {
  const el = h("div.mini", { draggable: reviewer ? "true" : "false", tabindex: "0", dataset: { id: String(x.id) },
    "aria-label": `${x.label}, ${TYPE_LABEL[x.primary_type] || x.primary_type}, ${x.signals_30d} signals` },
  h("div.card-head", glyph(x.primary_type), h("strong.card-title", x.label)),
  h("span.faint", `${x.town || ""}${x.town ? " · " : ""}${x.signals_30d} signal(s) · score ${fmtNum(x.score, 1)}`));
  el.addEventListener("click", () => emit("select", { kind: "hotspot", id: x.id, view: "map" }));
  if (reviewer) {
    el.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/plain", String(x.id)); el.setAttribute("aria-grabbed", "true"); });
    el.addEventListener("dragend", () => el.removeAttribute("aria-grabbed"));
    el.addEventListener("keydown", (e) => {
      if (/^[0-9]$/.test(e.key)) { const lane = lanes[Number(e.key)]; if (lane !== undefined) assign(x.id, lane); }
    });
  }
  return el;
}

async function assign(id, lane) {
  try {
    await api(`/api/hotspots/${id}/assign`, { method: "POST", body: { team: lane === UNASSIGNED ? null : lane } });
    toast(lane === UNASSIGNED ? "Unassigned." : `Assigned to ${lane}.`, "info", 1800);
    load();
  } catch { /* toast */ }
}

function countdownPanel(cfg) {
  const start = cfg.pit_mode_start;
  const today = nyDay(new Date());
  let text = "No PIT dates set";
  let sub = "An admin sets PIT_MODE_START and PIT_MODE_END under Admin → Settings.";
  if (start) {
    const days = Math.round((Date.parse(`${start}T12:00:00Z`) - Date.parse(`${today}T12:00:00Z`)) / 86400000);
    text = days > 0 ? `${days} day${days === 1 ? "" : "s"}` : days === 0 ? "Today" : "In progress";
    sub = `PIT mode ${nyDate(`${start}T12:00:00Z`)} to ${cfg.pit_mode_end ? nyDate(`${cfg.pit_mode_end}T12:00:00Z`) : "?"} (${TZ.replace("_", " ")} time)`;
  }
  return h("div.panel", h("h2", "PIT countdown"), h("div.countdown", text), h("p.muted", sub),
    cfg.pit_mode ? h("span.pit-badge", "PIT mode on: faster scans") : null);
}

function exportsPanel() {
  const a = (href, label) => h("a.btn.small", { href, target: "_blank", rel: "noopener" }, icon("download"), label);
  return h("div.panel", h("h2", "Exports"), h("p.muted", "Hotspot-level only. Every file carries the outreach-only notice and is logged."),
    h("div.actions", a("/api/export/route-sheet?window=30d", "Route sheet (all)"), a("/api/export/hotspots.csv?window=30d", "CSV"),
      a("/api/export/hotspots.geojson?window=30d", "GeoJSON")));
}

function pitModePanel(cfg) {
  const btn = h("button.btn", { on: { click: async () => {
    try {
      const r = await api("/api/admin/settings", { method: "PUT", body: { PIT_MODE_MANUAL: !cfg.pit_mode } });
      toast(r.pit_mode ? "PIT mode on." : "PIT mode off.", "info", 2000);
      load();
    } catch { /* toast */ }
  } } }, cfg.pit_mode ? "Turn PIT mode off" : "Turn PIT mode on now");
  return h("div.panel", h("h2", "PIT mode"), h("p.muted", "Scans every 15 minutes with higher budgets. Turns on automatically between the PIT dates."), btn);
}
