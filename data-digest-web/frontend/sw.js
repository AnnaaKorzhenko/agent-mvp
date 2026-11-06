// Service worker: лише Web Push ранкового дайджесту. Нічого не кешує — інтерфейс завжди з сервера.
// Push надсилає воркер агентів (агент morning_push) з JSON { title, body, tag? }; клік відкриває «Дайджести».

const DIGESTS_URL = '/#digests';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; }
  catch (e) { data = { body: event.data.text() }; }        // не JSON — показуємо як є
  event.waitUntil(self.registration.showNotification(data.title || 'Data Digest', {
    body: data.body || 'Ранковий дайджест готовий.',
    tag: data.tag || 'morning-digest',                      // новий push замінює старий, а не додає ще один
    icon: '/icon.svg',
    lang: 'uk'
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    // вкладка вже відкрита — показати в ній «Дайджести», інакше відкрити нову
    const tabs = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const tab = tabs.find((c) => new URL(c.url).origin === self.location.origin);
    if (tab) {
      tab.postMessage({ open: 'digests' });
      return tab.focus();
    }
    return self.clients.openWindow(DIGESTS_URL);
  })());
});
