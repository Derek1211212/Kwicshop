// service-worker.js

const CACHE_NAME    = 'swap-chief-cache-v6';
const PLACEHOLDER   = '/static/icons/ss.png';
const STATIC_ASSETS = [
  '/static/manifest.json',
  '/static/icons/ss.png',
  PLACEHOLDER,
  // add any other truly static assets you want pre-cached
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(async cache => {
      // Attempt to cache each asset individually, ignoring failures
      await Promise.all(
        STATIC_ASSETS.map(async path => {
          try {
            const res = await fetch(path);
            if (!res.ok) throw new Error(`Status ${res.status}`);
            await cache.put(path, res.clone());
            console.log('[SW] Cached:', path);
          } catch (err) {
            console.warn('[SW] Failed to cache:', path, err);
          }
        })
      );
      // Activate immediately
      return self.skipWaiting();
    })
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys
          .filter(key => key !== CACHE_NAME)
          .map(oldKey => caches.delete(oldKey))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  // 1) HTML navigations: network-first, cache fallback
  if (req.mode === 'navigate' && req.method === 'GET') {
    event.respondWith(handleNavigation(req));
    return;
  }

  // 2) Static assets: cache-first, then network
  if (req.method === 'GET' && STATIC_ASSETS.includes(url.pathname)) {
    event.respondWith(handleStatic(req));
    return;
  }

  // 3) Images: network-first, placeholder fallback
  if (isImageRequest(req)) {
    event.respondWith(handleImage(req));
    return;
  }

  // 4) Other GET requests: network-first, simple offline fallback
  if (req.method === 'GET' && url.protocol.startsWith('http')) {
    event.respondWith(handleOther(req));
  }
});

// Network-first for navigations
async function handleNavigation(req) {
  try {
    const networkRes = await fetch(req);
    if (req.method === 'GET') {
      const cache = await caches.open(CACHE_NAME);
      cache.put(req, networkRes.clone());
    }
    return networkRes;
  } catch (err) {
    console.warn('[SW] Navigation failed, serve from cache:', req.url);
    return (await caches.match(req)) || new Response('Offline', { status: 503 });
  }
}

// Cache-first for static assets
async function handleStatic(req) {
  const cached = await caches.match(req);
  if (cached) return cached;
  try {
    const networkRes = await fetch(req);
    const cache = await caches.open(CACHE_NAME);
    cache.put(req, networkRes.clone());
    return networkRes;
  } catch (err) {
    console.warn('[SW] Static asset failed:', req.url);
    return new Response('Offline', { status: 503, statusText: 'Service Unavailable' });
  }
}

// Network-first for images, placeholder on failure
async function handleImage(req) {
  try {
    return await fetch(req);
  } catch (err) {
    console.warn('[SW] Image fetch failed:', req.url);
    const placeholder = await caches.match(PLACEHOLDER);
    return placeholder || new Response(null, { status: 404 });
  }
}

// Network-first for other requests
async function handleOther(req) {
  try {
    return await fetch(req);
  } catch (err) {
    console.warn('[SW] Request failed, offline fallback for:', req.url);
    return new Response('Offline', { status: 503, statusText: 'Service Unavailable' });
  }
}

// Helper: detect image requests
function isImageRequest(req) {
  const accept = req.headers.get('Accept') || '';
  const ext = req.url.split('.').pop().split(/\#|\?/)[0];
  return (
    req.destination === 'image' ||
    accept.includes('image') ||
    ['png','jpg','jpeg','gif','webp','svg'].includes(ext.toLowerCase())
  );
}

// — PUSH & NOTIFICATION HANDLERS — //

self.addEventListener('push', event => {
  let data = { title: 'New Notification', body: '', url: '/' };
  try {
    data = event.data.json();
  } catch (e) {
    // No payload or malformed JSON
  }
  const options = {
    body:   data.body,
    icon:   '/static/icons/ss.png',
    icon: data.icon,
    badge:  '/static/icons/ss.png',
    data:   data.url,
    vibrate:[100,50,100],
  };
  event.waitUntil(self.registration.showNotification(data.title, options));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const urlToOpen = event.notification.data || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then(windowClients => {
        for (const client of windowClients) {
          if (client.url === urlToOpen && 'focus' in client) {
            return client.focus();
          }
        }
        return clients.openWindow(urlToOpen);
      })
  );
});
