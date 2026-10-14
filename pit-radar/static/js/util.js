// DOM builders, New York time formatting and small helpers.
// All post-derived text is set with textContent (never innerHTML) to rule out XSS from post content.

export const TZ = "America/New_York";

/** h("div.card#id", {attrs, on: {click}}, ...children) -> Element. Strings become text nodes. */
export function h(tag, attrs, ...children) {
  const m = /^([a-z0-9-]+)((?:[.#][\w-]+)*)$/i.exec(tag);
  const el = document.createElement(m ? m[1] : "div");
  if (m && m[2]) {
    for (const part of m[2].match(/[.#][\w-]+/g)) {
      if (part[0] === ".") el.classList.add(part.slice(1)); else el.id = part.slice(1);
    }
  }
  if (attrs && (typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs))) {
    children.unshift(attrs);
    attrs = null;
  }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "on") { for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn); }
    else if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k in el && typeof v !== "string" && k !== "style") el[k] = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

export function svgEl(tag, attrs = {}, ...children) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) el.setAttribute(k, String(v));
  for (const c of children.flat()) if (c !== null && c !== undefined) el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

// ---------------------------------------------------------------- time (always New York)
const fmtDateTime = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" });
const fmtDate = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric", year: "numeric" });
const fmtTime = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric", minute: "2-digit", timeZoneName: "short" });
const fmtDay = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });

export const parseTime = (iso) => (iso ? new Date(iso) : null);
export const nyDateTime = (iso) => (iso ? fmtDateTime.format(new Date(iso)) : "—");
export const nyDate = (iso) => (iso ? fmtDate.format(new Date(iso)) : "—");
export const nyTime = (iso) => (iso ? fmtTime.format(new Date(iso)) : "—");
/** YYYY-MM-DD of an instant in New York. */
export const nyDay = (d) => fmtDay.format(d instanceof Date ? d : new Date(d));

export function relTime(iso, now = Date.now()) {
  if (!iso) return "—";
  const s = Math.round((now - new Date(iso).getTime()) / 1000);
  const a = Math.abs(s);
  const suffix = s >= 0 ? " ago" : " from now";
  if (a < 45) return s >= 0 ? "just now" : "in a moment";
  if (a < 3600) return `${Math.round(a / 60)}m${suffix}`;
  if (a < 86400) return `${Math.round(a / 3600)}h${suffix}`;
  if (a < 86400 * 14) return `${Math.round(a / 86400)}d${suffix}`;
  return nyDate(iso);
}

/** Keep every [data-rel-time] element's text fresh. */
export function startRelativeClock() {
  const tick = () => {
    for (const el of document.querySelectorAll("[data-rel-time]")) {
      el.textContent = relTime(el.dataset.relTime);
    }
  };
  setInterval(tick, 30000);
}

export function timeEl(iso, cls = "") {
  return h(`time${cls ? "." + cls : ""}`, { datetime: iso || "", title: nyDateTime(iso), dataset: { relTime: iso || "" } }, relTime(iso));
}

// ---------------------------------------------------------------- labels
export const TYPE_LABEL = {
  encampment: "Encampment", individual_unsheltered: "Person(s) outside", vehicle_dwelling: "Vehicle dwelling",
  mixed: "Mixed", self_report: "Self-report", unknown: "Possible signal",
};
export const TYPE_COLOR = { // Okabe-Ito, as RGB arrays for deck.gl
  encampment: [230, 159, 0], individual_unsheltered: [86, 180, 233], vehicle_dwelling: [204, 121, 167], mixed: [221, 230, 245],
};
export const STATUS_LABEL = {
  ai_verified: "AI-verified", confirmed: "Staff-confirmed", needs_human_review: "Needs review",
  unlocated: "Unlocated", self_report: "Self-report", dismissed: "Dismissed",
};
export const PLATFORM_LABEL = {
  reddit: "Reddit", x: "X", tiktok: "TikTok", instagram: "Instagram", threads: "Threads", bluesky: "Bluesky",
  facebook: "Facebook", youtube: "YouTube", news: "News", web: "Web",
};
export const DISMISS_LABEL = {
  not_relevant: "Not relevant", outside_area: "Outside Nassau/Suffolk", too_old: "Too old", duplicate: "Duplicate",
  wrong_location: "Wrong location", other: "Other",
};

export function glyph(type) { return h(`span.glyph.${type || "unknown"}`, { "aria-hidden": "true" }); }
export function statusChip(status) { return h(`span.chip.st-${status}`, STATUS_LABEL[status] || status); }

export function meter(conf) {
  const n = Math.max(0, Math.min(5, Math.round((conf || 0) * 5)));
  const el = h("span.meter", { role: "img", "aria-label": `Confidence ${Math.round((conf || 0) * 100)}%`, title: `Confidence ${Math.round((conf || 0) * 100)}%` });
  for (let i = 0; i < 5; i++) el.appendChild(h(i < n ? "i.on" : "i"));
  return el;
}

export function sourceLink(url, label = "Open source ↗") {
  if (!url || !/^https?:\/\//i.test(url)) return null;
  return h("a.src-link", { href: url, target: "_blank", rel: "noopener noreferrer", on: { click: (e) => e.stopPropagation() } }, label);
}

export const fmtNum = (n, d = 0) => (n === null || n === undefined || Number.isNaN(n) ? "—" : Number(n).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d }));
export const fmtUsd = (n) => (n === null || n === undefined ? "—" : `$${Number(n).toFixed(2)}`);

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
export const isMobile = () => window.matchMedia("(max-width: 768px)").matches;

// ---------------------------------------------------------------- toasts
export function toast(message, kind = "info", ms = 4200) {
  let box = document.querySelector(".toasts");
  if (!box) { box = h("div.toasts", { role: "status", "aria-live": "polite" }); document.body.appendChild(box); }
  const t = h(`div.toast.glass.${kind}`, message);
  box.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

export function icon(name) {
  // Small inline SVG icon set (stroke icons). Paths are static constants, never data.
  const P = {
    map: "M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2Zm0 0v14m6-12v14",
    review: "M9 11l3 3 8-8M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9",
    heart: "M12 21s-7-4.35-9.5-8.5C.5 8.5 3 4 7 4c2 0 3.5 1 5 3 1.5-2 3-3 5-3 4 0 6.5 4.5 4.5 8.5C19 16.65 12 21 12 21Z",
    route: "M6 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4Zm12-10a2 2 0 1 0 0-4 2 2 0 0 0 0 4ZM6 15V9a4 4 0 0 1 4-4h2m6 4v6a4 4 0 0 1-4 4h-2",
    pulse: "M3 12h4l3-8 4 16 3-8h4",
    gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7.4-3a7.4 7.4 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7.3 7.3 0 0 0-2-1.2L14.5 2h-5l-.4 2.6a7.3 7.3 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.4l-2 1.6 2 3.4 2.4-1a7.3 7.3 0 0 0 2 1.2l.4 2.6h5l.4-2.6a7.3 7.3 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2Z",
    list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
    search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm10 2-4.35-4.35",
    cube: "M12 2 3 7v10l9 5 9-5V7l-9-5Zm0 0v20M3 7l9 5 9-5",
    compass: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Zm4.2-14.2-2.1 6.3-6.3 2.1 2.1-6.3 6.3-2.1Z",
    play: "M6 4l14 8-14 8V4Z",
    hex: "M12 2l8.66 5v10L12 22l-8.66-5V7L12 2Z",
    x: "M18 6 6 18M6 6l12 12",
    user: "M20 21a8 8 0 0 0-16 0M12 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10Z",
    feed: "M4 6h16M4 12h10M4 18h7",
    refresh: "M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6",
    download: "M12 3v12m0 0 4-4m-4 4-4-4M4 21h16",
    layers: "M12 3 2 8l10 5 10-5-10-5ZM2 12l10 5 10-5M2 16l10 5 10-5",
  };
  return svgEl("svg", { width: 20, height: 20, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" },
    svgEl("path", { d: P[name] || P.map }));
}
