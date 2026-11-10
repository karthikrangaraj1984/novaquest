/* Service worker: makes NovaQuest installable and keeps the app shell available on flaky school Wi-Fi.
   Pages are network-first (always fresh); static files are cache-first. Answers are queued by app.js. */
var CACHE = "nq-static-v1";
var SHELL = ["/static/app.css", "/static/app.js", "/static/icon.svg", "/offline"];
self.addEventListener("install", function (e) { e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); })); self.skipWaiting(); });
self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
  }));
  self.clients.claim();
});
self.addEventListener("fetch", function (e) {
  var req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.indexOf("/static/") === 0) {
    e.respondWith(caches.match(req).then(function (hit) {
      return hit || fetch(req).then(function (res) { var copy = res.clone(); caches.open(CACHE).then(function (c) { c.put(req, copy); }); return res; });
    }));
    return;
  }
  if (req.mode === "navigate") {
    e.respondWith(fetch(req).catch(function () { return caches.match("/offline"); }));
  }
});
