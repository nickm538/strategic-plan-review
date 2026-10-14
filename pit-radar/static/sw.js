// Service worker (docs/08 §8): precache the shell + vendor libraries; network-first for key API reads with an
// offline fallback; never cache auth, the live stream or exports; clear API caches on logout.
const VERSION = "pr-v1";
const SHELL = `${VERSION}-shell`;
const API = "pr-api";
const PRECACHE = [
  "/static/css/app.css", "/static/js/app.js", "/static/js/api.js", "/static/js/store.js", "/static/js/sse.js",
  "/static/js/util.js", "/static/js/map.js", "/static/js/feed.js", "/static/js/drawer.js", "/static/js/list.js",
  "/static/js/review.js", "/static/js/selfreports.js", "/static/js/planner.js", "/static/js/health.js", "/static/js/admin.js",
  "/static/vendor/maplibre-gl/maplibre-gl-csp.js", "/static/vendor/maplibre-gl/maplibre-gl-csp-worker.js",
  "/static/vendor/maplibre-gl/maplibre-gl.css", "/static/vendor/deck.gl/dist.min.js",
  "/static/icons/icon-192.png", "/static/icons/icon-512.png", "/manifest.webmanifest",
];
const NETWORK_FIRST = [/^\/api\/hotspots(\?|$)/, /^\/api\/signals(\?|$)/, /^\/api\/stats(\?|$)/, /^\/api\/config\/client$/,
  /^\/api\/geo\/(counties|labels)$/, /^\/api\/me$/];
const NEVER = [/^\/api\/auth\//, /^\/api\/stream/, /^\/api\/export\//, /^\/api\/admin\//];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(PRECACHE)).catch(() => {}).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k.startsWith("pr-v") && k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("message", (e) => {
  if (e.data && e.data.type === "logout") e.waitUntil(caches.delete(API));
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;           // map tiles: browser HTTP cache only
  const path = url.pathname;
  if (NEVER.some((r) => r.test(path))) return;
  if (NETWORK_FIRST.some((r) => r.test(path + url.search) || r.test(path))) {
    e.respondWith(networkFirst(req));
    return;
  }
  if (path.startsWith("/static/")) {
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(SHELL).then((c) => c.put(req, copy)); }
      return res;
    })));
  }
});

async function networkFirst(req) {
  try {
    const res = await fetch(req);
    if (res.ok) {
      const headers = new Headers(res.headers);
      headers.set("X-Cached-At", new Date().toISOString());
      const body = await res.clone().arrayBuffer();
      const cache = await caches.open(API);
      await cache.put(req, new Response(body, { status: res.status, headers }));
    }
    return res;
  } catch (err) {
    const hit = await (await caches.open(API)).match(req);
    if (!hit) throw err;
    const headers = new Headers(hit.headers);
    headers.set("X-From-Cache", "1");
    return new Response(await hit.arrayBuffer(), { status: 200, headers });
  }
}
