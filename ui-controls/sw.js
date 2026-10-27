/* Managed UI needs live status across model restarts. Chat IndexedDB is untouched. */
const C390_UI_VERSION = '0.2.0';
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil((async () => {
  for (const key of await caches.keys()) {
    if (key.startsWith('workbox-precache-') || key === 'api-cache') await caches.delete(key);
  }
  await self.clients.claim();
  for (const client of await self.clients.matchAll({ type: 'window' })) {
    const url = new URL(client.url);
    // Refresh only a newly opened managed launch URL, not unrelated/old tabs with drafts.
    if (url.origin === self.location.origin && url.searchParams.has('c390-managed') &&
        url.searchParams.get('c390-sw') !== C390_UI_VERSION) {
      url.searchParams.set('c390-sw', C390_UI_VERSION);
      // Do not await navigation inside activate: navigation itself may wait for activation.
      client.navigate(url.href).catch(() => {});
    }
  }
})()));
