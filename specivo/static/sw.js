/* Specivo Service Worker — cache static assets for offline/fast loads */

var CACHE_NAME = 'specivo-v1';
var STATIC_ASSETS = [
  '/static/vendor/bootstrap.5.3.6.min.css',
  '/static/vendor/alpine.3.14.min.js',
  '/static/vendor/htmx.2.0.min.js',
  '/static/vendor/bootstrap.bundle.5.3.6.min.js',
  '/static/img/favicon.svg'
];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      // Build credential-free Request objects to avoid Cache API rejection
      // when the page was loaded with HTTP Basic Auth credentials in the URL.
      var requests = STATIC_ASSETS.map(function (path) {
        return new Request(new URL(path, self.location.origin).href);
      });
      return cache.addAll(requests);
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(
        names.filter(function (n) { return n !== CACHE_NAME; })
             .map(function (n) { return caches.delete(n); })
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', function (event) {
  var url = new URL(event.request.url);

  /* Cache-first for static vendor assets and fonts (immutable) */
  if (url.pathname.startsWith('/static/vendor/') || url.pathname.startsWith('/static/fonts/')) {
    event.respondWith(
      caches.match(event.request).then(function (cached) {
        return cached || fetch(event.request).then(function (response) {
          var clone = response.clone();
          caches.open(CACHE_NAME).then(function (cache) { cache.put(event.request, clone); });
          return response;
        });
      })
    );
    return;
  }

  /* Network-first for everything else (HTML pages, API, app CSS/JS) */
  event.respondWith(
    fetch(event.request).catch(function () {
      return caches.match(event.request);
    })
  );
});
