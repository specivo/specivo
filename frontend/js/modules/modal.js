/* Generic modal open/close via data attributes (CSP-safe) */
export function initModal() {
    document.addEventListener('click', function (e) {
        var opener = e.target.closest('[data-open-modal]');
        if (opener) {
            var id = opener.getAttribute('data-open-modal');
            var el = document.getElementById(id);
            if (el) el.style.display = 'flex';
            return;
        }
        var closer = e.target.closest('[data-close-modal]');
        if (closer) {
            var cid = closer.getAttribute('data-close-modal');
            var cel = document.getElementById(cid);
            if (cel) cel.style.display = 'none';
            return;
        }
        /* Backdrop click to close */
        if (e.target.hasAttribute && e.target.hasAttribute('data-modal-backdrop')) {
            e.target.style.display = 'none';
        }
    });
}
