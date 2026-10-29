/* Same-origin navigation targets built from values read out of the DOM.

   Anything the browser is sent to from a data attribute, a form value or a
   server-rendered ``next`` goes through here first, so a value that would
   resolve to another origin or to a ``javascript:`` URL is dropped instead of
   followed. Mirrors ``safe_next_path`` in ``specivo/web/deps.py``.

   No imports and no DOM access beyond ``window.location``, so the module can
   be loaded on its own for testing. */

/* ``value`` if it is a path on this site, otherwise ''.

   It must start with a single ``/``. ``//host`` and ``/\host`` are refused
   because browsers resolve both as another origin, and so is any control
   character: browsers strip tabs and newlines from a URL, which would turn
   ``/\t/host`` into ``//host``. */
export function safeNextPath(value) {
    if (typeof value !== 'string' || value.length === 0 || value.length > 2000) {
        return '';
    }
    if (value.charAt(0) !== '/' || value.charAt(1) === '/' || value.charAt(1) === '\\') {
        return '';
    }
    for (var i = 0; i < value.length; i++) {
        var code = value.charCodeAt(i);
        if (code < 0x20 || code === 0x7f) {
            return '';
        }
    }
    return value;
}

/* A page size as a positive integer, or null for anything else. */
export function pageSize(value) {
    var text = String(value);
    if (!/^[1-9][0-9]{0,3}$/.test(text)) {
        return null;
    }
    return parseInt(text, 10);
}

/* The path and query of ``base`` with ``params`` set, if ``base`` resolves to
   this origin; otherwise null.

   ``base`` may be a path, a path with a query, or a full URL of this page.
   ``params`` replace any parameter of the same name already in ``base``. The
   result always starts with a single ``/``. */
export function sameOriginUrl(base, params, origin) {
    var here = origin || window.location.origin;
    var url;
    try {
        url = new URL(base, here + '/');
    } catch (_e) {
        return null;
    }
    if (url.origin !== here) {
        return null;
    }
    Object.keys(params || {}).forEach(function (name) {
        url.searchParams.set(name, String(params[name]));
    });
    return '/' + url.pathname.replace(/^\/+/, '') + url.search;
}
