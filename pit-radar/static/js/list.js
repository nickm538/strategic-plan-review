// List view: an accessible table equivalent of the map (docs/08 §9), same filters.
import { api } from "./api.js";
import { emit, queryString } from "./store.js";
import { TYPE_LABEL, clear, fmtNum, glyph, h, timeEl } from "./util.js";

let page;
export function mountList(host) { page = h("div.page"); host.append(page); }

export async function load() {
  clear(page);
  page.append(h("h1", "Hotspots"), h("p.lead", "The same places as the map, as a table. Select a row to open its details."));
  let data;
  try { data = await api(`/api/hotspots${queryString({}, ["window", "county", "type"])}`); } catch { return; }
  const items = data.items || [];
  if (!items.length) { page.append(h("div.empty", h("strong", "No hotspots in this window"))); return; }
  page.append(h("table.data", h("caption.sr-only", "Hotspots sorted by score"),
    h("thead", h("tr", ["Place", "Type", "County", "Town", "Score", "7 d", "30 d", "Sources", "Confirmed", "Last signal", "Status", "Team"].map((t) => h("th", { scope: "col" }, t)))),
    h("tbody", items.map((x) => h("tr",
      h("td", h("button.btn.small.ghost", { on: { click: () => emit("select", { kind: "hotspot", id: x.id, view: "map" }) } }, x.label)),
      h("td", glyph(x.primary_type), " ", TYPE_LABEL[x.primary_type] || x.primary_type), h("td", x.county), h("td", x.town || "—"),
      h("td.num", fmtNum(x.score, 2)), h("td.num", fmtNum(x.signals_7d)), h("td.num", fmtNum(x.signals_30d)),
      h("td.num", fmtNum(x.distinct_sources_30d)), h("td.num", fmtNum(x.confirmed_30d)), h("td", timeEl(x.last_signal_at)),
      h("td", x.review_only ? "awaiting review" : x.status), h("td", x.assigned_team || "—"))))));
}
