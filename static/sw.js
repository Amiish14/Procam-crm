// Procam CRM service worker — Phase 14
const CACHE_NAME = 'procam-crm-v2';
const OFFLINE_URL = '/offline';

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache =>
      cache.addAll([OFFLINE_URL, '/static/manifest.json'])
    )
  );
  self.skipWaiting();
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  // Only the CRM's own requests. Fetching another origin from here (Chart.js,
  // SheetJS, fonts, the logo) is subject to this worker's own Content
  // Security Policy, which allows connections to the CRM alone — so
  // handling them here broke those resources once the policy was enforced.
  // Left alone, the page loads them under the page's policy.
  if (new URL(req.url).origin !== self.location.origin) return;
  event.respondWith(
    fetch(req).catch(() =>
      caches.match(req).then(r => r || caches.match(OFFLINE_URL))
    )
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
    ))
  );
  self.clients.claim();
});
