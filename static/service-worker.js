const CACHE_NAME = 'swap-chief-cache-v2';
const urlsToCache = [
  '/',
  '/static/manifest.json',
  '/static/icons/ss.png',
  // only include URLs here you’ve verified
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(urlsToCache))
      .catch(err => {
        console.warn('Some resources failed to cache:', err);
      })
  );
});

self.addEventListener('fetch', event => {
  event.respondWith(
    caches.match(event.request)
      .then(resp => resp || fetch(event.request))
  );
});



// —————— PUSH & NOTIFICATION HANDLERS ——————

/**
 * When a push arrives, show it as a system notification.
 * Expects a JSON payload: { title, body, url (optional) }
 */
self.addEventListener('push', event => {
  let data = { title: 'New Notification', body: '', url: '/' };
  try {
    data = event.data.json();
  } catch (e) { /* malformed or no payload */ }

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

/**
 * When the user clicks the notification, focus or open the URL.
 */
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const urlToOpen = event.notification.data || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      // If an open window/tab matches, focus it
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
