// Review queue (docs/08 §7): split list/detail, keyboard triage J/K C D (1-6) O, progress.
import { api } from "./api.js";
import { signalBlock } from "./drawer.js";
import { review } from "./feed.js";
import { on } from "./store.js";
import { DISMISS_LABEL, PLATFORM_LABEL, TYPE_LABEL, clear, glyph, h, statusChip, timeEl } from "./util.js";

let root, listEl, detailEl, progressEl, countEl;
let items = [];
let idx = 0;
let total = 0;
let startTotal = 0;
let awaitingReason = false;
const REASONS = Object.keys(DISMISS_LABEL);

export function mountReview(host) {
  progressEl = h("span");
  countEl = h("span.num");
  listEl = h("div.review-list", { role: "listbox", "aria-label": "Signals to review" });
  detailEl = h("div.review-detail", { "aria-live": "polite" });
  root = h("div.review", listEl, detailEl);
  host.append(root);
  on("signal.new", (s) => { if (s && (s.status === "needs_human_review" || s.status === "ai_verified")) load(); });
}

export async function load() {
  try {
    const data = await api("/api/review-queue");
    items = data.items || [];
    total = data.total || items.length;
    startTotal = Math.max(startTotal, total);
    idx = Math.min(idx, Math.max(0, items.length - 1));
    render();
  } catch { /* toast */ }
}

function render() {
  clear(listEl);
  const done = Math.max(0, startTotal - total);
  const bar = h("span");
  bar.style.width = `${startTotal ? (done / startTotal) * 100 : 100}%`;
  listEl.append(h("div.panel-head", h("h2", "Review queue"), h("span.num", `${total} left`)),
    h("div.progress", { role: "progressbar", "aria-valuemin": 0, "aria-valuemax": startTotal, "aria-valuenow": done }, bar),
    h("p.faint", h("span.kbd", "J"), "/", h("span.kbd", "K"), " move · ", h("span.kbd", "C"), " confirm · ", h("span.kbd", "D"), " dismiss then ",
      h("span.kbd", "1–6"), " · ", h("span.kbd", "O"), " open source"));
  if (!items.length) {
    listEl.append(h("div.empty", h("strong", "Nothing to review"), "New items appear here as scans find them."));
    clear(detailEl);
    return;
  }
  items.forEach((s, i) => {
    listEl.append(h(`article.card${i === idx ? ".selected" : ""}`, { role: "option", "aria-selected": i === idx ? "true" : "false", tabindex: "-1",
      on: { click: () => { idx = i; render(); root.classList.add("detail-open"); } } },
    h("div.card-head", glyph(s.signal_type), h("span.card-title", s.place_label || s.town || "Location unclear"), statusChip(s.status)),
    h("div.card-meta", timeEl(s.published_at), h("span", `· ${PLATFORM_LABEL[s.platform] || s.platform}`)),
    h("p.card-summary", s.neutral_summary || "")));
  });
  clear(detailEl);
  const s = items[idx];
  detailEl.append(
    h("button.btn.small", { class: "btn small mobile-back", on: { click: () => root.classList.remove("detail-open") } }, "← Back"),
    h("h1", s.place_label || s.town || TYPE_LABEL[s.signal_type] || "Signal"),
    signalBlock(s, false),
  );
  if (awaitingReason) {
    detailEl.append(h("div.panel", h("h2", "Dismiss reason"), h("div.actions",
      REASONS.map((r, i) => h("button.btn.small", { on: { click: () => dismiss(r) } }, `${i + 1}. ${DISMISS_LABEL[r]}`)))));
  }
}

async function act(action, reason) {
  const s = items[idx];
  if (!s) return;
  const updated = await review(s, action, reason);
  if (updated) {
    items.splice(idx, 1);
    total = Math.max(0, total - 1);
    idx = Math.min(idx, Math.max(0, items.length - 1));
    render();
  }
}
async function dismiss(reason) { awaitingReason = false; await act("dismiss", reason); }

export function handleKey(e) {
  if (!items.length) return false;
  const k = e.key.toLowerCase();
  if (awaitingReason && /^[1-6]$/.test(k)) { dismiss(REASONS[Number(k) - 1]); return true; }
  if (k === "escape" && awaitingReason) { awaitingReason = false; render(); return true; }
  if (k === "j") { idx = Math.min(items.length - 1, idx + 1); render(); return true; }
  if (k === "k") { idx = Math.max(0, idx - 1); render(); return true; }
  if (k === "c") { act("confirm"); return true; }
  if (k === "d") { awaitingReason = true; render(); return true; }
  if (k === "o") { const u = items[idx] && items[idx].url; if (u && /^https?:/.test(u)) window.open(u, "_blank", "noopener,noreferrer"); return true; }
  return false;
}
