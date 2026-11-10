// Network first, so you always see the newest lines when the Mac is reachable.
// When it isn't (phone offline, Mac asleep), fall back to the last copy seen.
const CACHE = "lines-v16";
const SLOW_MS = 8000;
// One offline copy per endpoint for these, whatever the query (min %, method, scan profile)
const ONE_COPY = ["/api/arbs", "/api/ev", "/api/settings", "/api/bets", "/api/live"];
const SHELL = ["/", "/ev", "/bets", "/history", "/live", "/theme.css", "/report.js", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png", "/favicon-32.png", "/favicon.ico"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Arbitrage alerts pushed by the Mac after a scheduled run.
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "Sportsbook Lines", {
    body: d.body || "",
    tag: d.tag || "arbs",
    renotify: true,
    icon: "/icon-192.png",
    badge: "/icon-192.png",
    data: { url: d.url || "/?view=arbs" },
  }));
});

// Tapping the alert opens the app (or brings it forward) on the Arbitrage tab.
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = new URL(e.notification.data?.url || "/?view=arbs", location.origin).href;
  e.waitUntil((async () => {
    const wins = await clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) {
      if (new URL(w.url).origin === location.origin) {
        await w.focus();
        return w.navigate(target).catch(() => {});
      }
    }
    return clients.openWindow(target);
  })());
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  // Push setup must always talk to the Mac directly. So must the scan status: an
  // old copy saying "not running" would end the page's wait for a scan early.
  if (url.pathname.startsWith("/api/push/") || url.pathname === "/api/status") return;
  // Stats is a desktop page and always live; nothing to keep offline.
  if (url.pathname === "/stats" || url.pathname === "/api/stats") return;
  // Who has access must always be the current list, never a saved one.
  if (url.pathname === "/api/members") return;
  // A forced refresh must hit the books, never come from cache.
  if (url.searchParams.get("refresh") === "1") return;
  const key = ONE_COPY.includes(url.pathname) ? url.origin + url.pathname : e.request;
  const net = fetch(e.request).then((resp) => {
    if (resp.ok) {
      const copy = resp.clone();
      caches.open(CACHE).then((c) => c.put(key, copy));
    }
    return resp;
  });
  // When the Mac is out of reach the request can hang for a minute before it
  // fails, so after SLOW_MS show the saved copy if there is one.
  const slow = new Promise((r) => setTimeout(r, SLOW_MS)).then(() => caches.match(key)).then((hit) => hit || net);
  e.respondWith(
    Promise.race([net, slow])
      .catch(() => caches.match(key).then((hit) => hit || Promise.reject(new Error("offline"))))
  );
});
