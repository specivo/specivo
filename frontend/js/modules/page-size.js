/* Page-size / per-page selectors across listing pages. Each is a plain
   <select> that navigates or swaps on change (CSP-safe, no inline onchange). */
export function initPageSize() {
    /* Pagination page-size select */
    (function () {
        var sel = document.querySelector('[data-pagination-limit]');
        if (!sel) return;
        sel.addEventListener('change', function () {
            var base = sel.getAttribute('data-base-url') || window.location.pathname;
            window.location.href = base + '?offset=0&limit=' + sel.value;
        });
    })();

    /* Dashboard My Issues page-size select — uses htmx to swap the container */
    (function () {
        document.addEventListener('change', function (e) {
            var sel = e.target.closest('[data-my-issues-limit]');
            if (!sel) return;
            var limit = sel.value;
            var container = document.getElementById('my-issues-container');
            if (!container || typeof htmx === 'undefined') return;
            htmx.ajax('GET', '/partials/dashboard/my-issues/?offset=0&limit=' + limit, {target: container, swap: 'innerHTML'});
        });
    })();

    /* Activity per-page selector — saves preference then reloads */
    (function () {
        var sel = document.querySelector('[data-activity-per-page]');
        if (!sel) return;
        sel.addEventListener('change', function () {
            spFetch('/api/v1/users/me/preferences/activity-per-page/?per_page=' + sel.value, {
                method: 'PATCH',
                headers: {'Content-Type': 'application/json'}
            }).then(function () { window.location.search = ''; });
        });
    })();

    /* Board per-column selector — updates URL query param */
    (function () {
        document.querySelectorAll('[data-board-per-col]').forEach(function (sel) {
            sel.addEventListener('change', function () {
                var href = window.location.href.replace(/board_per_col=\d+/, '').replace(/[?&]$/, '');
                window.location.href = href + (window.location.search ? '&' : '?') + 'board_per_col=' + sel.value;
            });
        });
    })();

    /* Search page size selector */
    (function () {
        var sel = document.querySelector('[data-search-limit]');
        if (!sel) return;
        sel.addEventListener('change', function () {
            var base = sel.getAttribute('data-search-limit');
            window.location = base + '&offset=0&limit=' + sel.value;
        });
    })();
}
