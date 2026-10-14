// Sources & Health (docs/08 §7): provider cards, scan funnel (SVG), drop reasons, recent scans, geography.
import { api, isReviewer } from "./api.js";
import { clear, fmtNum, fmtUsd, h, nyDateTime, svgEl, timeEl } from "./util.js";

let page;
const FUNNEL = [["collected", "Collected"], ["in_window", "In time window"], ["prefiltered", "Passed prefilter"], ["classified", "Classified by Claude"],
  ["relevant", "Relevant"], ["verified", "Passed checks"], ["in_area", "In Nassau/Suffolk"], ["mapped", "Mapped"]];
const PROVIDER_NAMES = { socialcrawl: "SocialCrawl", exa: "Exa", firecrawl: "Firecrawl", anthropic: "Claude (Anthropic)", nominatim: "Nominatim geocoder" };
const DROP_LABEL = {
  too_old: "Older than the window", future: "Future timestamp", no_date: "No publish date", no_signal_term: "No homelessness term",
  no_li_place: "No Long Island place", "exclusion:animal": "Animal post", "exclusion:nyc_only": "Queens/Brooklyn/NYC only",
  "exclusion:out_of_area": "Same-name place elsewhere", low_confidence: "Low AI confidence", past_event: "Past event",
  outside_area: "Outside Nassau/Suffolk", duplicate_signal: "Duplicate",
};

export function mountHealth(host) { page = h("div.page"); host.append(page); }

export async function load() {
  clear(page);
  page.append(h("h1", "Sources & Health"), h("p.lead", "Is the radar working? Provider status, what each scan found and dropped, and spend."));
  let health, stats, scans = { items: [] };
  try {
    [health, stats] = await Promise.all([api("/api/health"), api("/api/stats")]);
    if (isReviewer()) scans = await api("/api/scans?limit=20");
  } catch { return; }

  const sched = health.scheduler || {};
  page.append(h("div.grid-3",
    h("div.panel", h("h2", "Scanner"),
      h("p", sched.running ? "Running" : "Stopped", health.scans_paused ? " · paused by an admin" : "", ` · ${health.mode === "pit" ? "PIT mode" : "standard mode"}`),
      h("p.muted", "Last scan ", sched.last_scan_at ? timeEl(sched.last_scan_at) : "—", sched.last_scan_status ? ` (${sched.last_scan_status})` : ""),
      h("p.muted", "Next scan ", sched.next_scan_at ? nyDateTime(sched.next_scan_at) : "—"),
      sched.skip_reason ? h("p.warn-inline", sched.skip_reason) : null),
    geoPanel(health.geo || {}),
    usagePanel(stats.usage)));

  page.append(h("h2", "Providers"), h("div.grid-3", Object.entries(health.providers || {}).map(([k, v]) => providerCard(k, v))));
  page.append(h("div.grid-2",
    h("div.panel.chart", h("h2", "Scan funnel"), funnelChart(stats.funnel_last_scan || {}, stats.funnel_7d || {})),
    h("div.panel.chart", h("h2", "Why items were dropped (7 days)"), dropsChart(stats.drop_reasons_7d || {}))));
  if (isReviewer()) page.append(scansTable(scans.items || []));
}

function providerCard(key, v) {
  const status = v.status || "unknown";
  return h("div.panel.provider",
    h("div.head", h(`span.status-dot.${status}`, { "aria-hidden": "true" }), PROVIDER_NAMES[key] || key, h("span.chip", status)),
    h("p.muted", v.message || (status === "ok" ? "Working" : "")),
    v.credits_remaining !== undefined && v.credits_remaining !== null ? h("p", "Credits remaining ", h("strong.num", fmtNum(v.credits_remaining))) : null,
    v.checked_at ? h("p.faint", "Checked ", timeEl(v.checked_at)) : null);
}

function geoPanel(g) {
  const counts = g.counts || {};
  const lines = Object.entries(counts).map(([kind, byCounty]) => `${kind}: ${Object.values(byCounty || {}).reduce((a, b) => a + (Number(b) || 0), 0)}`);
  return h("div.panel", h("h2", "Geography"), h("p", h(`span.status-dot.${g.state === "ready" ? "ok" : g.state === "failed" ? "unhealthy" : "degraded"}`), " ", g.state || "unknown"),
    h("p.muted", g.message || ""), g.built_at ? h("p.faint", "Built ", nyDateTime(g.built_at)) : null,
    lines.length ? h("p.faint", lines.join(" · ")) : null);
}

function usagePanel(usage) {
  if (!usage) return h("div.panel", h("h2", "Spend"), h("p.muted", "Visible to reviewers and admins."));
  const row = (label, d) => h("tr", h("td", label), h("td.num", fmtNum(d.credits || 0)), h("td.num", fmtNum(d.requests || 0)), h("td.num", fmtUsd(d.cost_usd || 0)));
  const provs = ["socialcrawl", "exa", "firecrawl", "anthropic"];
  return h("div.panel", h("h2", "Spend today (New York day)"),
    h("table.data", h("thead", h("tr", h("th", "Provider"), h("th", "Credits"), h("th", "Requests"), h("th", "$"))),
      h("tbody", provs.map((p) => row(PROVIDER_NAMES[p], (usage.today || {})[p] || {})))),
    h("p.faint", "Month to date: ", provs.map((p) => `${PROVIDER_NAMES[p].split(" ")[0]} ${fmtUsd(((usage.month || {})[p] || {}).cost_usd || 0)}`).join(" · ")));
}

function funnelChart(last, week) {
  const W = 560, rowH = 30, labelW = 170, H = FUNNEL.length * rowH + 30;
  const max = Math.max(1, ...FUNNEL.map(([k]) => Math.max(last[k] || 0, week[k] || 0)));
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Scan funnel: last scan and 7-day totals" });
  FUNNEL.forEach(([k, label], i) => {
    const y = 10 + i * rowH;
    const wWeek = ((week[k] || 0) / max) * (W - labelW - 70);
    const wLast = ((last[k] || 0) / max) * (W - labelW - 70);
    svg.append(svgEl("text", { x: 0, y: y + 15 }, label),
      svgEl("rect", { x: labelW, y: y + 2, width: Math.max(1, wWeek), height: 18, rx: 4, fill: "#26324F" }),
      svgEl("rect", { x: labelW, y: y + 6, width: Math.max(1, wLast), height: 10, rx: 3, fill: "#5AB0FF" }),
      svgEl("text", { x: labelW + Math.max(wWeek, wLast) + 8, y: y + 15, class: "val" }, `${fmtNum(last[k] || 0)} / ${fmtNum(week[k] || 0)}`));
  });
  svg.append(svgEl("text", { x: labelW, y: H - 4 }, "Blue: last scan · gray: 7-day total"));
  return svg;
}

function dropsChart(drops) {
  const rows = Object.entries(drops).sort((a, b) => b[1] - a[1]).slice(0, 12);
  if (!rows.length) return h("p.muted", "Nothing dropped yet.");
  const W = 560, rowH = 26, labelW = 210, H = rows.length * rowH + 10;
  const max = Math.max(1, ...rows.map((r) => r[1]));
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Drop reasons over 7 days" });
  rows.forEach(([k, n], i) => {
    const y = 4 + i * rowH;
    const w = (n / max) * (W - labelW - 60);
    const label = DROP_LABEL[k] || (k.startsWith("not_relevant:") ? `Not relevant: ${k.split(":")[1].replace(/_/g, " ")}` : k.replace(/_/g, " "));
    svg.append(svgEl("text", { x: 0, y: y + 14 }, label), svgEl("rect", { x: labelW, y: y + 3, width: Math.max(1, w), height: 14, rx: 3, fill: "#A9B6CF" }),
      svgEl("text", { x: labelW + w + 8, y: y + 14, class: "val" }, fmtNum(n)));
  });
  return svg;
}

function scansTable(items) {
  return h("div.panel", h("h2", "Recent scans"), items.length ? h("div", { class: "table-wrap" }, h("table.data",
    h("thead", h("tr", h("th", "Started"), h("th", "Mode"), h("th", "Status"), h("th", "Collected"), h("th", "Classified"), h("th", "Stored"), h("th", "Mapped"), h("th", "Notes"))),
    h("tbody", items.map((s) => h("tr", h("td", nyDateTime(s.started_at)), h("td", s.mode), h("td", s.status),
      h("td.num", fmtNum((s.stage_counts || {}).collected || 0)), h("td.num", fmtNum((s.stage_counts || {}).classified || 0)),
      h("td.num", fmtNum((s.stage_counts || {}).stored || 0)), h("td.num", fmtNum((s.stage_counts || {}).mapped || 0)),
      h("td.faint", (s.errors || []).slice(0, 2).join(" · "))))))) : h("p.muted", "No scans yet."));
}
