const CACHE_NAME = "audiofeel-v19";
const APP_SHELL = [
  "/",
  "/index.html",
  "/styles.css",
  "/redesign.css",
  "/app.js",
  "/manifest.webmanifest",
  "/icon.svg",
  "/fonts/inter-cyrillic.woff2",
  "/fonts/inter-latin.woff2",
  "/fonts/literata-cyrillic.woff2",
  "/fonts/literata-latin.woff2",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)),
    )),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  const hasCredentialQuery = ["apiKey", "u", "p", "t", "s"]
    .some((name) => url.searchParams.has(name));
  if (
    request.method !== "GET"
    || url.origin !== self.location.origin
    || url.pathname.startsWith("/api/")
    || url.pathname === "/rest"
    || url.pathname.startsWith("/rest/")
    || hasCredentialQuery
  ) {
    return;
  }
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() => caches.match("/index.html")),
    );
    return;
  }
  event.respondWith(
    fetch(request).then((response) => {
      const copy = response.clone();
      caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
      return response;
    }).catch(() => caches.match(request)),
  );
});

self.addEventListener("push", (event) => {
  if (!event.data) return;
  let payload;
  try {
    payload = event.data.json();
  } catch {
    return;
  }
  if (payload?.type !== "workflow_completed") return;
  event.waitUntil(
    self.registration.showNotification(payload.title || "Music Service", {
      body: payload.body || "Все пачки завершены",
      tag: payload.tag || "workflow-completed",
      data: { url: payload.url || "/#/playlists" },
      icon: "/icon.svg",
      badge: "/icon.svg",
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/#/playlists", self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      const existing = clients.find((client) => new URL(client.url).origin === self.location.origin);
      if (existing) {
        existing.navigate(target);
        return existing.focus();
      }
      return self.clients.openWindow(target);
    }),
  );
});
