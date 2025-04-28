// service-worker.js

const CACHE_NAME = 'swap-chief-cache-v4';  // bump this on each deploy
const STATIC_ASSETS = [
  '/static/manifest.json',
  '/static/icons/ss.png',
  // add any other long-lived assets here
];

// 1) Install → cache static assets, then take over immediately
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

// 2) Activate → remove old caches, and claim clients
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE_NAME)
            .map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// 3) Fetch → network-first for navigations, cache-first for static assets
self.addEventListener('fetch', event => {
  const req = event.request;

  // A) Navigation requests (HTML pages)
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)                                  // try network
        .then(res => {
          // optionally update cache so offline still has a fallback
          const copy = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req))           // fallback to cache if offline
    );
    return;
  }

  // B) Static asset requests
  if (STATIC_ASSETS.some(path => req.url.endsWith(path))) {
    event.respondWith(
      caches.match(req).then(cached => {
        return cached || fetch(req).then(networkRes => {
          // update cache for next time
          caches.open(CACHE_NAME).then(c => c.put(req, networkRes.clone()));
          return networkRes;
        });
      })
    );
    return;
  }

  // C) Everything else: just go to network
  event.respondWith(fetch(req));
});


// —————— PUSH & NOTIFICATION HANDLERS ——————

self.addEventListener('push', event => {
  let data = { title: 'New Notification', body: '', url: '/' };
  try { data = event.data.json() } catch (e) {}

  const options = {
    body: data.body,
    icon: '/static/icons/ss.png',
    badge: '/static/icons/ss.png',
    data: data.url,
    vibrate: [100, 50, 100]
  };

  event.waitUntil(
    self.registration.showNotification(data.title, options)
  );
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const urlToOpen = event.notification.data || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then(windows => {
        for (let win of windows) {
          if (win.url === urlToOpen && 'focus' in win) {
            return win.focus();
          }
        }
        return clients.openWindow(urlToOpen);
      })
  );
});
