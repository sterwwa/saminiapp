const CACHE_NAME = "vasya-bot-shell-v3";
const SHELL_URLS = ["/", "/manifest.webmanifest", "/icon.svg", "/apple-touch-icon.png"];

self.addEventListener("install", event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await cache.addAll(SHELL_URLS);
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(key => key.startsWith("vasya-bot-shell-") && key !== CACHE_NAME).map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/auth/")) return;

  if (request.mode === "navigate" && url.pathname === "/") {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        if (response.ok) {
          const cache = await caches.open(CACHE_NAME);
          await cache.put("/", response.clone());
        }
        return response;
      } catch {
        return (await caches.match("/")) || Response.error();
      }
    })());
    return;
  }

  if (SHELL_URLS.includes(url.pathname) && url.pathname !== "/") {
    event.respondWith((async () => {
      const cached = await caches.match(url.pathname);
      if (cached) {
        event.waitUntil(fetch(request).then(async response => {
          if (response.ok) (await caches.open(CACHE_NAME)).put(url.pathname, response.clone());
        }).catch(() => {}));
        return cached;
      }
      return fetch(request);
    })());
  }
});

self.addEventListener("push", event => {
  let payload = {};
  try { payload = event.data ? event.data.json() : {}; }
  catch { payload = { body: event.data ? event.data.text() : "" }; }
  event.waitUntil(self.registration.showNotification(payload.title || "Вася Бот", {
    body: payload.body || "Напоминание о занятии",
    icon: "/apple-touch-icon.png",
    badge: "/icon.svg",
    tag: payload.tag || "vasya-bot-notification",
    data: { url: payload.url || "/" },
    renotify: false,
    requireInteraction: true
  }));
});
self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || "/", self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      if ("focus" in client) {
        await client.focus();
        if ("navigate" in client) await client.navigate(target);
        return;
      }
    }
    if (self.clients.openWindow) await self.clients.openWindow(target);
  })());
});
