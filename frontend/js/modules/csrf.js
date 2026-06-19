/* CSRF wiring for HTMX requests and plain HTML form submissions. */
export function initCsrf() {
    /* HTMX — inject CSRF header on every mutating request */
    document.addEventListener('htmx:configRequest', function (e) {
        var method = (e.detail.verb || 'GET').toUpperCase();
        if (method !== 'GET' && method !== 'HEAD') {
            e.detail.headers['X-CSRF-Token'] = _getCsrfToken();
        }
    });

    /* HTML forms — inject csrf_token hidden field before submission */
    document.addEventListener('submit', function (e) {
        var form = e.target;
        if (!form || form.tagName !== 'FORM') return;
        var method = (form.method || 'GET').toUpperCase();
        if (method === 'GET' || method === 'HEAD') return;
        if (!form.querySelector('input[name="csrf_token"]')) {
            var input = document.createElement('input');
            input.type = 'hidden';
            input.name = 'csrf_token';
            input.value = _getCsrfToken();
            form.appendChild(input);
        }
    });
}
