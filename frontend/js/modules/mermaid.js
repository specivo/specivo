/* Mermaid diagrams in rendered Markdown.

   The server renders a ```mermaid fence as <pre class="sp-mermaid"> holding
   the escaped source. This module draws each one as an SVG next to the pre
   and hides the pre; a diagram that fails keeps its source visible below a
   short translated notice. The vendored library is loaded only once a page
   holds a marker block, including blocks added later (htmx swaps, comments,
   the editor preview), which a MutationObserver picks up.

   The library URL and the notice text come from <meta name="sp-mermaid">
   in base.html, so the vendored version is named in one place. */

var SELECTOR = 'pre.sp-mermaid';
var DONE_ATTR = 'data-sp-mermaid';

var config = null;
var loading = null;
var queue = Promise.resolve();
var renderCount = 0;
/* The library removes any element whose id matches the render id (or its
   scratch container's) before drawing. Stored content can carry ids of its
   own (heading anchors, <a id>), so the prefix holds a random part it cannot
   guess. */
var ID_PREFIX = 'sp-mermaid-' + Math.random().toString(36).slice(2, 10) + '-';
var currentTheme = null;
/* Source and theme of each drawn block, kept so a theme change can draw it again. */
var sources = new WeakMap();
var drawnThemes = new WeakMap();

/* Settings a diagram's own %%{init}%% directive or front matter may not
   change: the library's defaults plus everything that would let stored text
   turn on HTML labels, callbacks or custom sanitizing. */
var SECURE_KEYS = [
    'secure', 'securityLevel', 'startOnLoad', 'maxTextSize', 'suppressErrorRendering', 'maxEdges',
    'htmlLabels', 'flowchart', 'dompurifyConfig', 'themeCSS', 'theme'
];

function readConfig() {
    var meta = document.querySelector('meta[name="sp-mermaid"]');
    if (!meta) return null;
    return {
        src: meta.getAttribute('content'),
        error: meta.getAttribute('data-error') || 'This diagram could not be drawn.'
    };
}

function loadLibrary() {
    if (loading) return loading;
    loading = new Promise(function (resolve, reject) {
        if (window.mermaid) {
            resolve(window.mermaid);
            return;
        }
        var script = document.createElement('script');
        script.src = config.src;
        script.async = true;
        script.onload = function () {
            if (window.mermaid) resolve(window.mermaid);
            else reject(new Error('mermaid did not load'));
        };
        script.onerror = function () {
            reject(new Error('mermaid did not load'));
        };
        document.head.appendChild(script);
    });
    return loading;
}

/* 'dark' when the background behind *pre* is dark, else 'default'. Read from
   the computed background, so it follows any theme, built-in or custom. */
function themeFor(pre) {
    var node = pre.parentElement;
    while (node && node.nodeType === 1) {
        var match = getComputedStyle(node).backgroundColor.match(/[\d.]+/g);
        if (match && match.length >= 3 && (match.length < 4 || parseFloat(match[3]) > 0)) {
            var luminance = (0.2126 * match[0] + 0.7152 * match[1] + 0.0722 * match[2]) / 255;
            return luminance < 0.5 ? 'dark' : 'default';
        }
        node = node.parentElement;
    }
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'default';
}

function initialize(mermaid, theme) {
    mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        theme: theme,
        htmlLabels: false,
        flowchart: { htmlLabels: false },
        suppressErrorRendering: true,
        secure: SECURE_KEYS
    });
    currentTheme = theme;
}

function clearResult(pre) {
    var next = pre.previousElementSibling;
    while (next && (next.classList.contains('sp-mermaid-diagram') || next.classList.contains('sp-mermaid-error'))) {
        var prev = next.previousElementSibling;
        next.remove();
        next = prev;
    }
    pre.hidden = false;
}

function showError(pre, detail) {
    clearResult(pre);
    var notice = document.createElement('p');
    notice.className = 'sp-mermaid-error';
    notice.setAttribute('role', 'note');
    notice.textContent = config.error;
    if (detail) notice.title = String(detail).split('\n')[0];
    pre.parentNode.insertBefore(notice, pre);
}

function draw(mermaid, pre) {
    var theme = themeFor(pre);
    if (theme !== currentTheme) initialize(mermaid, theme);
    drawnThemes.set(pre, theme);
    var source = sources.get(pre);
    renderCount += 1;
    var id = ID_PREFIX + renderCount;
    return mermaid.render(id, source).then(function (result) {
        if (!pre.isConnected) return;
        clearResult(pre);
        var figure = document.createElement('div');
        figure.className = 'sp-mermaid-diagram';
        /* SVG from mermaid.render with securityLevel 'strict': labels are
         * text and the output has been through the library's DOMPurify. */
        figure.innerHTML = result.svg;  /* noqa: XSS */
        pre.parentNode.insertBefore(figure, pre);
        pre.hidden = true;
    }, function (err) {
        if (!pre.isConnected) return;
        showError(pre, err && err.message);
    });
}

/* Draws run one at a time: the library keeps global state per render. */
function enqueue(task) {
    queue = queue.then(task, task);
    return queue;
}

function drawPending() {
    var pending = Array.prototype.filter.call(document.querySelectorAll(SELECTOR), function (pre) {
        return !pre.hasAttribute(DONE_ATTR);
    });
    if (!pending.length) return;
    pending.forEach(function (pre) {
        pre.setAttribute(DONE_ATTR, '');
        sources.set(pre, pre.textContent);
    });
    loadLibrary().then(function (mermaid) {
        pending.forEach(function (pre) {
            enqueue(function () { return draw(mermaid, pre); });
        });
    }, function (err) {
        pending.forEach(function (pre) { showError(pre, err && err.message); });
    });
}

/* Draw a diagram again when the background behind it turned light or dark. */
function redrawOnThemeChange() {
    var mermaid = window.mermaid;
    if (!mermaid || currentTheme === null) return;
    document.querySelectorAll(SELECTOR + '[' + DONE_ATTR + ']').forEach(function (pre) {
        if (!drawnThemes.has(pre) || drawnThemes.get(pre) === themeFor(pre)) return;
        enqueue(function () { return draw(mermaid, pre); });
    });
}

function holdsMarker(node) {
    return node.nodeType === 1 && (node.matches(SELECTOR) || node.querySelector(SELECTOR) !== null);
}

export function initMermaid() {
    config = readConfig();
    if (!config || !config.src) return;
    drawPending();

    var scheduled = false;
    new MutationObserver(function (records) {
        if (scheduled) return;
        var found = records.some(function (record) {
            return Array.prototype.some.call(record.addedNodes, holdsMarker);
        });
        if (!found) return;
        scheduled = true;
        window.requestAnimationFrame(function () {
            scheduled = false;
            drawPending();
        });
    }).observe(document.body, { childList: true, subtree: true });

    var themeAttrs = ['class', 'style', 'data-theme', 'data-bs-theme'];
    var themeObserver = new MutationObserver(redrawOnThemeChange);
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: themeAttrs });
    themeObserver.observe(document.body, { attributes: true, attributeFilter: themeAttrs });
    if (window.matchMedia) {
        var query = window.matchMedia('(prefers-color-scheme: dark)');
        if (query.addEventListener) query.addEventListener('change', redrawOnThemeChange);
    }
}
