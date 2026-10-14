// Self-reports (R7): town-level list, suggested reply with Copy, Mark responded. Never on the map.
import { api } from "./api.js";
import { PLATFORM_LABEL, clear, h, sourceLink, timeEl, toast } from "./util.js";

let page;

export function mountSelfReports(host) {
  page = h("div.page");
  host.append(page);
}

export async function load() {
  clear(page);
  page.append(h("h1", "Self-reports"),
    h("p.lead", "People posting about their own homelessness, listed by town only. They are never placed on the map. Reply from your own account if appropriate."));
  let data;
  try { data = await api("/api/self-reports"); } catch { return; }
  if (data.template_needs_review) {
    page.append(h("p.warn-inline", "The suggested reply still has [verify] placeholders. An admin should fill in verified phone numbers under Admin → Self-report reply."));
  }
  const items = data.items || [];
  if (!items.length) { page.append(h("div.empty", h("strong", "No self-reports in the last 30 days"))); return; }
  const grid = h("div.grid-2");
  for (const r of items) {
    const reply = h("textarea", { readonly: true, "aria-label": "Suggested reply" });
    reply.value = r.suggested_reply;
    const copy = h("button.btn.small", { on: { click: async () => {
      try { await navigator.clipboard.writeText(reply.value); toast("Reply copied.", "info", 1800); }
      catch { reply.select(); toast("Select the text and copy it.", "info", 2500); }
    } } }, "Copy reply");
    const done = h("button.btn.small.good", { disabled: r.responded, on: { click: async () => {
      try { await api(`/api/self-reports/${r.id}/responded`, { method: "POST" }); done.disabled = true; done.textContent = "Responded ✓"; } catch { /* toast */ }
    } } }, r.responded ? "Responded ✓" : "Mark responded");
    grid.append(h("article.panel",
      h("div.card-head", h("span.card-title", r.town || "Town unknown"), h("span.chip", r.county || "")),
      h("div.card-meta", timeEl(r.published_at), h("span", `· ${PLATFORM_LABEL[r.platform] || r.platform}`)),
      h("p", r.neutral_summary || ""), reply, h("div.actions", copy, done, sourceLink(r.url))));
  }
  page.append(grid);
}
