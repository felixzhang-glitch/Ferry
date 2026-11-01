"use strict";
// App-shell only: navigation and shell assets go network-first with an offline
// fallback. API calls, metrics and anything cross-origin are never intercepted.
const VERSION = "ferry-console-v19-pwa-launch-settle";
const SHELL = [
  "/observability/",
  "/static/dashboard.css",
  "/static/appearance.js",
  "/static/notifications.js",
  "/static/dashboard.js",
  "/static/console.js",
  "/static/chat.js",
  "/static/console-icon-180.png",
  "/static/console-icon-192.png",
  "/observability/manifest.webmanifest",
];
// Custom icon URLs from the backend use ?v=hex for cache busting.
// These immutable entries are cached alongside the shell but are keyed by full URL.
const isImmutableIcon = (pathname) => /^\/static\/console-icon-(180|192|512|maskable)\.png$/.test(pathname);

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key.startsWith("ferry-console-") && key !== VERSION).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data?.json() || {}; } catch { /* A malformed push still needs a visible notification. */ }
  const thread = typeof data.thread_id === "string" && /^[a-f0-9]{32}$/.test(data.thread_id) ? data.thread_id : "";
  const bodies = new Set(["回复已完成", "提醒已到", "简报已就绪", "通知已开启"]);
  event.waitUntil(self.registration.showNotification("Ferry", {
    body: bodies.has(data.body) ? data.body : "有新的消息，请打开应用查看。",
    tag: typeof data.tag === "string" && /^[a-f0-9]{64}$/.test(data.tag) ? data.tag : "ferry-message",
    icon: "/static/console-icon-192.png", badge: "/static/console-icon-192.png",
    data: { thread_id: thread },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const thread = event.notification.data?.thread_id;
  const route = typeof thread === "string" && /^[a-f0-9]{32}$/.test(thread) ? `#chat/${thread}` : "#chat";
  const target = new URL(`/observability/${route}`, self.location.origin).href;
  event.waitUntil((async () => {
    const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const client = clients.find((item) => {
      const url = new URL(item.url);
      return url.origin === self.location.origin && /^\/observability\/?$/.test(url.pathname);
    });
    if (client) { await client.navigate(target); await client.focus(); }
    else await self.clients.openWindow(target);
  })());
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/metrics") return;
  // Immutable custom icons: cache by full URL (with query string) so a version
  // mismatch never serves an old avatar for a new ?v= parameter.
  if (isImmutableIcon(url.pathname) && url.search) {
    event.respondWith(
      caches.match(request.url).then((cached) => cached || fetch(request).then((response) => {
        if (response.ok) { const copy = response.clone(); caches.open(VERSION).then((cache) => cache.put(request.url, copy)); }
        return response;
      }).catch(() => caches.match(url.pathname).then((fallback) => fallback || Response.error()))),
    );
    return;
  }
  // Only the exact public shell may be cached, never an arbitrary navigation/download.
  const navigation = request.mode === "navigate" && url.pathname === "/observability/";
  const isShell = navigation || SHELL.includes(url.pathname);
  if (!isShell) return;
  const fallback = navigation ? "/observability/" : url.pathname;
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(VERSION).then((cache) => cache.put(fallback, copy));
        }
        return response;
      })
      .catch(() => caches.match(fallback).then((cached) => cached || Response.error())),
  );
});
