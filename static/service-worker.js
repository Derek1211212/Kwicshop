const CACHE_NAME = 'swap-chief-cache-v3';  // bumped from v2 to v3
const urlsToCache = [
  '/',
  '/static/manifest.json',
  '/static/icons/ss.png',
  // add any other verified assets here
];

// Install → cache assets, then activate immediately
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(urlsToCache))
      .then(() => self.skipWaiting())        // activate new SW immediately
      .catch(err => {
        console.warn('Some resources failed to cache:', err);
      })
  );
});

// Activate → delete any old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys
          .filter(key => key !== CACHE_NAME)
          .map(key => caches.delete(key))
      )
    )
    .then(() => self.clients.claim())        // take control of all pages ASAP
  );
});

// Fetch → serve from cache, fallback to network
self.addEventListener('fetch', event => {
  event.respondWith(
    caches.match(event.request)
      .then(resp => resp || fetch(event.request))
  );
});

//
// —————— PUSH & NOTIFICATION HANDLERS ——————
//

// When a push arrives, show it as a system notification.
// Expects a JSON payload: { title, body, url (optional) }
self.addEventListener('push', event => {
  console.log('[SW] Push event received', event);

  let data = { title: 'New Notification', body: '', url: '/' };
  try {
    data = event.data.json();
  } catch (e) {
    console.warn('[SW] Push event had no JSON payload');
  }

  const options = {
    body: data.body,
    icon: '/static/icons/ss.png',
    badge: '/static/icons/ss.png',
    data: data.url,            // URL to open when clicked
    vibrate: [100, 50, 100],   // optional vibration pattern
    // you can also add image, actions, etc.
  };

  event.waitUntil(
    self.registration.showNotification(data.title, options)
  );
});

// When the user clicks the notification, focus or open the URL.
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const urlToOpen = event.notification.data || '/';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      // Focus an open window/tab if it matches
      for (let client of windowClients) {
        if (client.url === urlToOpen && 'focus' in client) {
          return client.focus();
        }
      }
      // Otherwise open a new window/tab
      if (clients.openWindow) {
        return clients.openWindow(urlToOpen);
      }
    })
  );
});
