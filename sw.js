/* APEX service worker: web push notifications.
 * Served from /sw.js (scope "/"). Kept intentionally small: no offline caching, so a new
 * deploy of index.html is always picked up immediately. */

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

// Safari (macOS / iOS) revokes push permission if a push doesn't show a notification,
// so there we always show it. Elsewhere we skip it while the app is open and focused.
const IS_SAFARI = /^((?!chrome|android|crios|fxios|edg).)*safari/i.test(self.navigator.userAgent);

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (_) {
    data = { body: event.data ? event.data.text() : '' };
  }

  event.waitUntil((async () => {
    if (!IS_SAFARI) {
      const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
      if (windows.some((w) => w.focused && w.visibilityState === 'visible')) return;
    }
    await self.registration.showNotification(data.title || 'APEX', {
      body: data.body || '',
      icon: '/icons/icon-192.png',
      badge: '/icons/icon-192.png',
      tag: data.tag || undefined,
      renotify: Boolean(data.tag),
      data: { url: data.url || '/' },
    });
  })());
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';

  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const w of windows) {
      if (new URL(w.url).origin === self.location.origin) {
        await w.focus();
        w.postMessage({ type: 'apex-open-url', url });
        return;
      }
    }
    await self.clients.openWindow(url);
  })());
});
