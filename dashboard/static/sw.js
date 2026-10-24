// Minimal service worker: makes the dashboard installable as a phone app.
// Live data is never cached — every screen always shows fresh numbers.
const SHELL = 'kalshi-shell-v1';
const OFFLINE = `<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<body style="background:#000;color:#ededed;font-family:-apple-system,Segoe UI,Roboto,sans-serif;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0;text-align:center">
<div><div style="font-size:42px">⚠️</div><h2>Can't reach the bot</h2>
<p style="color:#8a8a90">Check your phone's connection.<br>The bot keeps running on the server.</p>
<button onclick="location.reload()" style="background:#22c55e;color:#000;border:0;border-radius:10px;
padding:12px 22px;font-weight:700;font-size:16px">Try again</button></div>`;

self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  if (e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request).catch(() =>
      new Response(OFFLINE, {headers: {'Content-Type': 'text/html'}})));
  }
});
