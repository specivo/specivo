/* ============================================================
   Global helpers — CSRF, locale-aware dates and autocomplete
   dropdown anchoring.

   Assigned to `window` (not exported) so the component bodies
   migrated verbatim from the old monolith keep calling them by
   bare name, and so inline Alpine components in templates (e.g.
   admin/workflows.html) can call `spFetch(...)` directly.

   Imported for side effects at the top of both bundles
   (alpine-init.js and app.js) before anything uses them.
   ============================================================ */

/* -------------------------------------------------------
   CSRF — read token from cookie, attach to mutating requests
   ------------------------------------------------------- */
window._getCsrfToken = function () {
    var match = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
    return match ? match[1] : '';
};

/* -------------------------------------------------------
   URL prefix — an instance behind STEALTH_PREFIX serves every
   page, asset and API under it. base.html writes it into
   <meta name="sp-base"> ("" without a prefix); every root-relative
   URL the frontend builds goes through spUrl() (spFetch() does it
   itself), so it never leaves the prefix.
   ------------------------------------------------------- */
var spBaseValue = null;

/** The URL prefix of this instance: "" or "/prefix", read once from the page. */
window.spBase = function () {
    if (spBaseValue === null) {
        var meta = document.querySelector('meta[name="sp-base"]');
        spBaseValue = meta ? (meta.getAttribute('content') || '').replace(/\/+$/, '') : '';
    }
    return spBaseValue;
};

/**
 * Give a root-relative application path ("/projects/ACME/") the URL prefix.
 * Idempotent: a path already under the prefix, an absolute URL, a
 * protocol-relative one, a query-only or relative one and a non-string
 * are returned as they are.
 */
window.spUrl = function (path) {
    if (typeof path !== 'string' || path.charAt(0) !== '/' || path.charAt(1) === '/' || path.charAt(1) === '\\') {
        return path;
    }
    var base = spBase();
    if (!base) return path;
    var rest = path.charAt(base.length);
    var under = path.indexOf(base) === 0 && (rest === '' || rest === '/' || rest === '?' || rest === '#');
    return under ? path : base + path;
};

/**
 * Drop-in fetch() wrapper that auto-attaches the X-CSRF-Token header
 * on mutating requests (POST/PATCH/PUT/DELETE), and the URL prefix to a
 * root-relative URL (spUrl), so call sites pass application paths.
 */
window.spFetch = function (url, opts) {
    opts = opts || {};
    var method = (opts.method || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') {
        opts.headers = opts.headers || {};
        if (opts.headers instanceof Headers) {
            opts.headers.set('X-CSRF-Token', _getCsrfToken());
        } else {
            opts.headers['X-CSRF-Token'] = _getCsrfToken();
        }
    }
    return fetch(spUrl(url), opts);
};

/* -------------------------------------------------------
   Locale-aware dates — the server renders the viewer's
   language into <html lang>, so dates follow it instead of
   a hardcoded locale or the browser's own setting.
   ------------------------------------------------------- */
window.spLocale = function () {
    return document.documentElement.lang || undefined;
};

/**
 * Format a date (Date or ISO string) for the page's language with
 * Intl.DateTimeFormat; defaults to a medium date ("Mar 28, 2026").
 * The Gregorian calendar is forced so the year matches the dates the
 * server renders with Babel (Intl would otherwise pick the Buddhist
 * calendar for Thai). Returns '' for a missing or invalid value.
 */
window.spFormatDate = function (value, options) {
    if (!value) return '';
    var d = value instanceof Date ? value : new Date(value);
    if (isNaN(d.getTime())) return '';
    var opts = Object.assign(
        {calendar: 'gregory'},
        options || {year: 'numeric', month: 'short', day: 'numeric'}
    );
    try {
        return new Intl.DateTimeFormat(spLocale(), opts).format(d);
    } catch (_e) {
        /* Unknown language tag: fall back to the browser's locale. */
        return new Intl.DateTimeFormat(undefined, opts).format(d);
    }
};

/* -------------------------------------------------------
   Autocomplete dropdown anchoring
   -------------------------------------------------------
   Positions a `.sp-ac-menu` as `position: fixed` anchored to its
   trigger element so it escapes every `overflow: hidden` ancestor
   (e.g. the rounded `.card` containers in the issue sidebar, which
   would otherwise clip an absolutely-positioned menu). Shared by the
   `tagField` and `entityAutocomplete` Alpine components.

   `root` is the component's positioning element (`.sp-tag-field` /
   `.sp-ac`); `menu` is the `.sp-ac-menu` inside it. The menu opens
   below the trigger, flipping above it when there is not enough room
   below within the viewport. */
window.spAnchorMenu = function (root, menu) {
    if (!root || !menu) return;
    var rect = root.getBoundingClientRect();
    var gap = 4;
    var margin = 8;
    menu.style.position = 'fixed';
    menu.style.left = rect.left + 'px';
    menu.style.right = 'auto';
    menu.style.width = rect.width + 'px';
    // Clear any prior placement so measurements are accurate.
    menu.style.top = '';
    menu.style.bottom = '';
    menu.style.maxHeight = '';
    var menuHeight = menu.offsetHeight;
    var spaceBelow = window.innerHeight - rect.bottom - gap - margin;
    var spaceAbove = rect.top - gap - margin;
    if (menuHeight > spaceBelow && spaceAbove > spaceBelow) {
        // Flip above the trigger.
        menu.style.top = '';
        menu.style.bottom = (window.innerHeight - rect.top + gap) + 'px';
        menu.style.maxHeight = Math.max(0, spaceAbove) + 'px';
    } else {
        menu.style.top = (rect.bottom + gap) + 'px';
        menu.style.maxHeight = Math.max(0, spaceBelow) + 'px';
    }
};

/**
 * Wires up an autocomplete component so its dropdown stays anchored to
 * the trigger while open. `ctx` is the Alpine component (`this`); it
 * must expose `$el` (root) and a boolean `open`. The menu is
 * repositioned whenever `open` becomes true and on scroll/resize while
 * open. Listeners are registered once per component.
 */
window.spBindAnchoredMenu = function (ctx) {
    var root = ctx.$el;
    var menu = root.querySelector('.sp-ac-menu');
    if (!menu) return;
    var reposition = function () {
        if (ctx.open) spAnchorMenu(root, menu);
    };
    ctx.$watch('open', function (isOpen) {
        if (isOpen) {
            // Wait for x-show + x-for to render rows before measuring.
            ctx.$nextTick(function () { spAnchorMenu(root, menu); });
        }
    });
    // Keep the menu glued to the trigger as the user scrolls/resizes.
    window.addEventListener('scroll', reposition, true);
    window.addEventListener('resize', reposition);
};
