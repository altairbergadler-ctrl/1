const CACHE_NAME = "lossless-archive-v15";
const APP_SHELL = [
  "/",
  "/index.html",
  "/styles.css",
  "/app.js",
  "/manifest.webmanifest",
  "/icon.svg",
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
