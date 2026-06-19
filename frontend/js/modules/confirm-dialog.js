/* Confirm dialog via data-confirm attribute (CSP-safe, replaces inline onclick="return confirm(...)") */
export function initConfirmDialog() {
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-confirm]');
        if (!btn) return;
        var msg = btn.getAttribute('data-confirm');
        if (!confirm(msg)) {
            e.preventDefault();
            e.stopImmediatePropagation();
        }
    });
}
