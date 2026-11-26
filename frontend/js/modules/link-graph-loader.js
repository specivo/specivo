/* Loads the link graph bundle (link-graph.min.js) when a graph is first opened.

   The issue's Graph tab and the wiki page's Graph block hold a graph that is
   not drawn until the reader opens it, so the page itself loads no chart
   library. Opening the tab (issueTabs dispatches ``sp-show-tab-graph``) or the
   block (a <details data-sp-graph-block> toggled open) marks its graph with
   data-sp-graph-open and loads the bundle named in its data-sp-graph-src, a
   path on this site. A bundle already on the page is told with an
   ``sp-link-graph-open`` event instead; on start it draws every graph that is
   marked open, so a graph opened while the bundle loads is not missed. */
import { safeNextPath } from '../lib/safe-url';

const BUNDLE = /^\/static\/dist\/js\/link-graph\.min(\.[0-9a-f]{8})?\.js$/;
let requested = false;

function bundleLoaded() {
    return Boolean(window.spLinkGraph)
        || Array.prototype.some.call(document.scripts, (s) => /\/link-graph\.min(\.[0-9a-f]{8})?\.js$/.test(s.src));
}

function loadBundle(el) {
    if (requested || bundleLoaded()) return;
    const src = safeNextPath(el.getAttribute('data-sp-graph-src') || '');
    // The path carries the URL prefix (STEALTH_PREFIX); the bundle check is on the rest.
    const base = spBase();
    if (src.indexOf(base) !== 0 || !BUNDLE.test(src.slice(base.length))) return;
    requested = true;
    const script = document.createElement('script');
    script.src = src;
    script.defer = true;
    document.head.appendChild(script);
}

/** Open the graph inside *container* (or *container* itself): draw it, loading the bundle first if needed. */
export function openGraphIn(container) {
    if (!container || !container.querySelector) return;
    const el = container.matches('[data-sp-link-graph]') ? container : container.querySelector('[data-sp-link-graph]');
    if (!el || el.hasAttribute('data-sp-graph-open')) return;
    el.setAttribute('data-sp-graph-open', '');
    if (window.spLinkGraph) {
        el.dispatchEvent(new CustomEvent('sp-link-graph-open', { bubbles: true }));
    } else {
        loadBundle(el);
    }
}

export function initLinkGraphLoader() {
    // The event comes from the tab link (or the tab bar, on a #graph address): the graph is in the tab bar's panels.
    document.addEventListener('sp-show-tab-graph', (event) => {
        const from = event.target instanceof Element ? event.target : null;
        openGraphIn(from && (from.closest('.activity-tabs') || from));
    });
    // ``toggle`` does not bubble: listen in the capture phase.
    document.addEventListener('toggle', (event) => {
        const block = event.target;
        if (block instanceof HTMLDetailsElement && block.open && block.hasAttribute('data-sp-graph-block')) {
            openGraphIn(block);
        }
    }, true);
}
