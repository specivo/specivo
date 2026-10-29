/* Page-size / per-page selectors across listing pages. Each is a plain
   <select> that navigates or swaps on change (CSP-safe, no inline onchange).

   The URLs come from data attributes and option values, so every target is
   rebuilt with sameOriginUrl/pageSize: a value that is not a small positive
   integer, or a base that is not on this origin, does nothing. */
import { pageSize, sameOriginUrl } from '../lib/safe-url';

function go(base, params) {
    var target = sameOriginUrl(base, params);
    if (target) window.location.assign(target);
}

export function initPageSize() {
    /* Pagination page-size select */
    (function () {
        var sel = document.querySelector('[data-pagination-limit]');
        if (!sel) return;
        sel.addEventListener('change', function () {
            var limit = pageSize(sel.value);
            if (limit === null) return;
            var base = sel.getAttribute('data-base-url') || window.location.pathname;
            go(base, {offset: 0, limit: limit});
        });
    })();

    /* Dashboard My Issues page-size select — uses htmx to swap the container */
    (function () {
        document.addEventListener('change', function (e) {
            var sel = e.target.closest('[data-my-issues-limit]');
            if (!sel) return;
            var limit = pageSize(sel.value);
            if (limit === null) return;
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
            var perPage = pageSize(sel.value);
            if (perPage === null) return;
            spFetch('/api/v1/users/me/preferences/activity-per-page/?per_page=' + perPage, {
                method: 'PATCH',
                headers: {'Content-Type': 'application/json'}
            }).then(function () { window.location.search = ''; });
        });
    })();

    /* Board per-column selector — updates URL query param */
    (function () {
        document.querySelectorAll('[data-board-per-col]').forEach(function (sel) {
            sel.addEventListener('change', function () {
                var perCol = pageSize(sel.value);
                if (perCol === null) return;
                go(window.location.href, {board_per_col: perCol});
            });
        });
    })();

    /* Search page size selector */
    (function () {
        var sel = document.querySelector('[data-search-limit]');
        if (!sel) return;
        sel.addEventListener('change', function () {
            var limit = pageSize(sel.value);
            if (limit === null) return;
            go(sel.getAttribute('data-search-limit'), {offset: 0, limit: limit});
        });
    })();
}
