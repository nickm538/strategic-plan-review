// Fetch wrapper: same-origin credentials, CSRF header on writes, 401 -> login, readable errors.
import { toast } from "./util.js";

let csrf = null;
let me = null;

export class ApiError extends Error {
  constructor(status, detail, body) { super(detail); this.status = status; this.body = body; }
}

function detailOf(body, status) {
  if (!body) return `Request failed (${status})`;
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) return body.detail.map((d) => d.msg || String(d)).join("; ");
  return `Request failed (${status})`;
}

export async function api(path, { method = "GET", body, quiet = false, raw = false } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && method !== "HEAD") {
    if (!csrf) await loadMe();
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  let res;
  try {
    res = await fetch(path, { method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (e) {
    if (!quiet) toast("Network error. Check your connection.", "error");
    throw new ApiError(0, "network error");
  }
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    window.location.assign("/login");
    throw new ApiError(401, "Not authenticated");
  }
  if (raw) return res;
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) {
    const msg = detailOf(data, res.status);
    if (!quiet) toast(msg, "error");
    throw new ApiError(res.status, msg, data);
  }
  if (res.headers.get("X-From-Cache") === "1" && typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("pr:offline", { detail: { at: res.headers.get("X-Cached-At") } }));
  }
  return data;
}

export async function loadMe() {
  me = await api("/api/me", { quiet: true });
  csrf = me && me.csrf_token;
  return me;
}

export const currentUser = () => me;
export const role = () => (me ? me.role : "viewer");
export const isReviewer = () => ["reviewer", "admin"].includes(role());
export const isAdmin = () => role() === "admin";

export async function logout() {
  try { await api("/api/auth/logout", { method: "POST", quiet: true }); } catch { /* cookie cleared anyway */ }
  if (navigator.serviceWorker && navigator.serviceWorker.controller) {
    navigator.serviceWorker.controller.postMessage({ type: "logout" });
  }
  if (window.caches) {
    try { for (const k of await caches.keys()) if (k.startsWith("pr-api")) await caches.delete(k); } catch { /* ignore */ }
  }
  window.location.assign("/login");
}
