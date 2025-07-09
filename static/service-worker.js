const CACHE_NAME    = 'swap-chief-cache-v6';
const PLACEHOLDER   = '/static/icons/sss.png';
const STATIC_ASSETS = [
  '/static/manifest.json',
  '/static/icons/ss.png',
  PLACEHOLDER,
  // Add any other static assets you want pre-cached
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(async cache => {
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

  // 4) Other GET requests: network-first
  if (req.method === 'GET' && url.protocol.startsWith('http')) {
    event.respondWith(handleOther(req));
  }
});

// Navigation: network-first
async function handleNavigation(req) {
  try {
    const networkRes = await fetch(req);
    const cache = await caches.open(CACHE_NAME);
    cache.put(req, networkRes.clone());
    return networkRes;
  } catch (err) {
    console.warn('[SW] Navigation failed, serving from cache:', req.url);
    return (await caches.match(req)) || new Response('Offline', { status: 503 });
  }
}

// Static assets: cache-first
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
    return new Response('Offline', { status: 503 });
  }
}

// Images: network-first, fallback to placeholder
async function handleImage(req) {
  try {
    return await fetch(req);
  } catch (err) {
    console.warn('[SW] Image fetch failed:', req.url);
    const placeholder = await caches.match(PLACEHOLDER);
    return placeholder || new Response(null, { status: 404 });
  }
}

// Other GETs: network-first
async function handleOther(req) {
  try {
    return await fetch(req);
  } catch (err) {
    console.warn('[SW] Request failed, offline fallback for:', req.url);
    return new Response('Offline', { status: 503 });
  }
}

// Image detection helper
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
  let payload = {
    title: 'New Notification',
    body: '',
    icon: '/static/icons/ss.png',
    badge: '/static/icons/ss.png',
    data: { url: '/' }
  };

  try {
    const incoming = event.data.json();
    if (incoming.notification) {
      payload = {
        ...payload,
        ...incoming.notification,
        data: {
          ...payload.data,
          ...(incoming.notification.data || {})
        }
      };
    }
  } catch (e) {
    console.warn('[SW] Malformed push payload');
  }

  const options = {
    body: payload.body,
    icon: payload.icon,
    badge: payload.badge,
    data: payload.data,
    vibrate: [100, 50, 100],
  };

  event.waitUntil(
    self.registration.showNotification(payload.title, options)
  );
});

self.addEventListener('notificationclick', event => {
  event.notification.close();

  // pull out our data
  const { url, type } = event.notification.data || {};

  // for proposal‑status pushes, go to /my-proposals
  const urlToOpen = (type === 'proposal')
    ? '/my-proposals'
    : (url || '/');

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then(windowClients => {
        for (const client of windowClients) {
          if (client.url.endsWith(urlToOpen) && 'focus' in client) {
            return client.focus();
          }
        }
        return clients.openWindow(urlToOpen);
      })
  );
});

