// Live updates over Server-Sent Events. The browser resends Last-Event-ID on reconnect; the server replays.
import { emit } from "./store.js";

let es = null;
let retryMs = 2000;
let connected = false;

const EVENTS = ["hello", "signal.new", "signal.updated", "hotspot.updated", "scan.started", "scan.finished",
  "provider.health", "budget.warning", "geo.status", "maintenance.finished", "session.expired"];

export function connect() {
  if (es) es.close();
  es = new EventSource("/api/stream", { withCredentials: true });
  es.onopen = () => { connected = true; retryMs = 2000; emit("live", true); };
  es.onerror = () => {
    connected = false;
    emit("live", false);
    if (es.readyState === EventSource.CLOSED) {
      setTimeout(connect, retryMs);
      retryMs = Math.min(retryMs * 2, 60000);
    }
  };
  for (const name of EVENTS) {
    es.addEventListener(name, (e) => {
      let data = {};
      try { data = JSON.parse(e.data || "{}"); } catch { return; }
      if (name === "session.expired") { window.location.assign("/login"); return; }
      emit(name, data);
    });
  }
}

export const isLive = () => connected;
