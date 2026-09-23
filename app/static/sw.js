/* Service worker for the "Café do escritório" PWA shell.
   Caches only the static app shell (HTML/CSS/JS/icons) so the app opens
   instantly and works offline. It NEVER caches anything under /api: those
   responses are dynamic and tied to the session cookie, so serving a cached
   API response could show one person the data of another. */
"use strict";

const CACHE_VERSION = "v8";
const CACHE_NAME = "cafe-shell-" + CACHE_VERSION;

const SHELL_URLS = [
  "/",
  "/static/style.css?v=8",
  "/static/app.js?v=8",
  "/static/manifest.json",
  "/static/icon.svg",
  "/static/icons/icon-180.png",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-512-maskable.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      // { cache: "reload" } bypasses the browser HTTP cache for the precache
      // fetch itself, otherwise install() could seed the new cache with the
      // same stale response the HTTP cache is already holding.
      .then((cache) => cache.addAll(SHELL_URLS.map((u) => new Request(u, { cache: "reload" }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Never touch the API: those are dynamic responses tied to the caller's session.
  if (url.pathname.startsWith("/api")) return;
  if (event.request.method !== "GET") return;

  // Network-first: the LAN is fast and is the source of truth, so a fresh
  // deploy is visible immediately. The cache only kicks in when the network
  // request fails, so the app still works offline. { cache: "no-cache" }
  // also makes this bypass the browser's own HTTP cache, so the request
  // always revalidates with the server (via ETag) instead of the browser
  // silently answering from heuristic freshness before the SW even sees it.
  event.respondWith(
    fetch(event.request, { cache: "no-cache" })
      .then((resp) => {
        if (resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE_NAME).then((c) => c.put(event.request, copy));
        }
        return resp;
      })
      .catch(() => caches.match(event.request))
  );
});

/* ---------- web push ---------- */

self.addEventListener("push", (event) => {
  let dados = {};
  if (event.data) {
    try { dados = event.data.json(); } catch { dados = { corpo: event.data.text() }; }
  }
  const titulo = dados.titulo || "Café do escritório";
  const opcoes = {
    body: dados.corpo || "",
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png",
    data: { url: dados.url || "/" },
  };
  event.waitUntil(self.registration.showNotification(titulo, opcoes));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const destino = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((janelas) => {
      for (const janela of janelas) {
        if (janela.url.startsWith(self.location.origin) && "focus" in janela) return janela.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(destino);
    })
  );
});
