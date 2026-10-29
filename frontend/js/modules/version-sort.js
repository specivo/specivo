/* Version-list sort select (roadmap). The choice is saved as the viewer's
   preference, so the roadmap and the project versions list both reopen in
   that order. Plain <select data-version-sort> — no inline onchange. */
export function saveVersionSort(value) {
    return spFetch('/api/v1/users/me/preferences/version-sort/?sort=' + encodeURIComponent(value), {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'}
    });
}

export function initVersionSort() {
    document.querySelectorAll('select[data-version-sort]').forEach(function (sel) {
        sel.addEventListener('change', function () {
            var params = new URLSearchParams();
            var show = sel.getAttribute('data-show');
            if (show) params.set('show', show);
            params.set('sort', sel.value);
            var target = window.location.pathname + '?' + params.toString();
            /* Navigate whether or not the save worked: the sort param applies
               the choice to this page either way. */
            saveVersionSort(sel.value).catch(function () {}).then(function () {
                window.location.href = target;
            });
        });
    });
}
