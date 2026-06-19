/* Service Worker registration (PWA) — skip if URL contains credentials (Basic Auth proxy) */
export function initServiceWorker() {
    if ('serviceWorker' in navigator && !location.href.match(/:\/\/[^@]+@/)) {
        navigator.serviceWorker.register('/static/sw.js');
    }
}
