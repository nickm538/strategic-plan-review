// App state: filters synced to the URL query string (bookmarkable internal views), data caches, events.
const listeners = new Map();

export function on(evt, fn) {
  if (!listeners.has(evt)) listeners.set(evt, new Set());
  listeners.get(evt).add(fn);
  return () => listeners.get(evt).delete(fn);
}
export function emit(evt, data) {
  for (const fn of listeners.get(evt) || []) {
    try { fn(data); } catch (e) { console.error(e); }
  }
}

const FILTER_KEYS = ["window", "county", "type", "status", "source", "min_conf", "group"];
const DEFAULTS = { window: "7d" };

export const state = {
  filters: { ...DEFAULTS },
  view: "map",
  hotspots: new Map(),     // id -> hotspot
  signals: new Map(),      // id -> signal (feed)
  config: null,
  stats: null,
  selected: null,          // {kind: "hotspot"|"signal", id}
};

export function readUrl() {
  const p = new URLSearchParams(window.location.search);
  const f = { ...DEFAULTS };
  for (const k of FILTER_KEYS) if (p.get(k)) f[k] = p.get(k);
  state.filters = f;
  const hash = window.location.hash.replace(/^#/, "");
  if (hash) state.view = hash.split("/")[0];
}

export function setFilters(patch) {
  state.filters = { ...state.filters, ...patch };
  for (const k of Object.keys(state.filters)) if (state.filters[k] === "" || state.filters[k] === null) delete state.filters[k];
  const p = new URLSearchParams();
  for (const k of FILTER_KEYS) if (state.filters[k] && state.filters[k] !== DEFAULTS[k]) p.set(k, state.filters[k]);
  const qs = p.toString();
  history.replaceState(null, "", `${window.location.pathname}${qs ? "?" + qs : ""}${window.location.hash}`);
  emit("filters", state.filters);
}

export function queryString(extra = {}, keys = ["window", "county", "type", "status", "source", "min_conf"]) {
  const p = new URLSearchParams();
  for (const k of keys) if (state.filters[k]) p.set(k, state.filters[k]);
  for (const [k, v] of Object.entries(extra)) if (v !== null && v !== undefined && v !== "") p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : "";
}
