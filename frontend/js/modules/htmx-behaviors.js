/* CSP-safe HTMX after-request behaviors via data attributes.
   Instead of inline hx-on::after-request="...", elements use:
     data-hx-reload    — reload page on success
     data-hx-redirect  — redirect to URL on success */
export function initHtmxBehaviors() {
    document.addEventListener('htmx:afterRequest', function (e) {
        if (!e.detail.successful) return;
        var el = e.detail.elt;
        if (el.hasAttribute('data-hx-reload')) {
            window.location.reload();
        } else if (el.hasAttribute('data-hx-redirect')) {
            window.location.href = el.getAttribute('data-hx-redirect');
        }
    });
}
