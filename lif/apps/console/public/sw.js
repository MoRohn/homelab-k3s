// Labzilla Console service worker: caches the APP SHELL only (brief §6, spec §71).
// - Never touches /api/* (live state, SSE, Ask streams, auth) or any non-GET request: those go straight
//   to the network, untouched, so a cached answer can never pose as the system's current state.
// - index.html is network-first (a deploy shows up on the next load); the cached copy is only an
//   offline fallback so the shell can render "Labzilla unavailable" instead of a browser error page (§62).
// - /assets/* are content-hashed by Vite: cache-first, bounded.
// - Bump VERSION only when this file's caching rules change; activation deletes every other cache.
const VERSION = 'lz-shell-v1';
const INDEX = '/index.html';
const MAX_ASSETS = 80;
const STATIC = /^\/(assets|icons|logo)\/|^\/manifest\.webmanifest$/;

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(VERSION);
      // The first page load isn't controlled yet, so precache the index and the hashed files it names;
      // otherwise an offline reload would find an index whose scripts were never cached.
      const res = await fetch('/', { cache: 'no-cache', credentials: 'same-origin' });
      if (!cacheable(res)) return;
      const html = await res.clone().text();
      await cache.put(INDEX, res);
      const urls = [...new Set(html.match(/\/assets\/[A-Za-z0-9._-]+\.(?:js|css|woff2)/g) || [])];
      await Promise.all(urls.map((u) => cache.add(u).catch(() => undefined)));
    })().catch(() => undefined),
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      for (const key of await caches.keys()) if (key !== VERSION) await caches.delete(key);
      await self.clients.claim();
    })(),
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === '/api' || url.pathname.startsWith('/api/')) return;

  if (req.mode === 'navigate') {
    event.respondWith(networkFirstIndex(req));
  } else if (STATIC.test(url.pathname)) {
    event.respondWith(cacheFirst(req));
  }
  // Anything else (healthz, sw.js itself, …) is left to the browser.
});

function cacheable(res) {
  return !!res && res.ok && res.type === 'basic';
}

async function networkFirstIndex(req) {
  const cache = await caches.open(VERSION);
  try {
    const res = await fetch(req);
    if (cacheable(res) && (res.headers.get('content-type') || '').includes('text/html')) {
      await cache.put(INDEX, res.clone());
    }
    return res;
  } catch (err) {
    const cached = await cache.match(INDEX);
    if (cached) return cached;
    throw err;
  }
}

async function cacheFirst(req) {
  const cache = await caches.open(VERSION);
  const hit = await cache.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  // The server answers unknown paths with index.html (SPA fallback, status 200): never cache that as a
  // script/style, or a stale deploy's missing chunk would be "found" forever.
  if (cacheable(res) && !(res.headers.get('content-type') || '').includes('text/html')) {
    await cache.put(req, res.clone());
    void trim(cache);
  }
  return res;
}

/** Keep the cache bounded: old hashed bundles from earlier deploys are dropped oldest-first. */
async function trim(cache) {
  const keys = (await cache.keys()).filter((k) => new URL(k.url).pathname !== INDEX);
  for (let i = 0; i < keys.length - MAX_ASSETS; i++) await cache.delete(keys[i]);
}
