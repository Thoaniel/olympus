// Minimal service worker: gjør at Olympus kan legges på hjemskjermen som en app. Ingen mellomlagring av svar.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
