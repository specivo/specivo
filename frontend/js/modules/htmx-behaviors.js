/* CSP-safe HTMX after-request behaviors via data attributes.
   Instead of inline hx-on::after-request="...", elements use:
     data-hx-reload    — reload page on success
     data-hx-redirect  — redirect to URL on success

   A 429 is swapped in like a success: the server answers an htmx request
   over a rate limit with a short note (pages/_rate_limited_inline.html),
   and htmx would otherwise drop every 4xx body and leave the target as it
   was, with nothing to say why. isError stays true, so the request still
   counts as failed and data-hx-reload / data-hx-redirect do not fire. */
export function initHtmxBehaviors() {
    document.addEventListener('htmx:beforeSwap', function (e) {
        if (e.detail.xhr && e.detail.xhr.status === 429) {
            e.detail.shouldSwap = true;
        }
    });
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
