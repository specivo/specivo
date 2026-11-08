/* The AI usage tab of an issue (pages/ai_usage/_issue_tab.html).

   The tab is an htmx partial: it is fetched when the tab opens, and its own
   links (sub-issue switch, pages, page sizes) swap a new copy into
   #issue-ai-usage-tab. Two things happen here, both from data attributes, no
   inline script:

   - The chart bundle (spend-chart.min.js, Apache ECharts) is not on the issue
     page. When swapped-in content holds a chart and names the bundle
     (data-sp-chart-src, a path on this site), it is loaded once; it draws
     every chart in the document when it starts, and its own htmx hooks draw
     the charts of later swaps. Pages that load the bundle themselves already
     have its script tag, so it is never loaded twice.
   - After a swap started by a tab link with data-aiu-focus="<id>", that
     heading takes the focus, so keyboard and screen reader users land on what
     they opened (the sessions after a page change). */
import { safeNextPath } from '../lib/safe-url';

const TARGET_ID = 'issue-ai-usage-tab';
const CHART_BUNDLE = /^\/static\/dist\/js\/spend-chart\.min(\.[0-9a-f]{8})?\.js$/;

function chartBundleLoaded() {
    return Array.prototype.some.call(document.scripts, (s) => /\/spend-chart\.min(\.[0-9a-f]{8})?\.js$/.test(s.src));
}

/** Load the chart bundle when *root* holds a chart that is not drawn yet. */
export function loadChartsIn(root) {
    if (!root || !root.querySelector) return;
    const source = root.matches && root.matches('[data-sp-chart-src]') ? root : root.querySelector('[data-sp-chart-src]');
    if (!source || !root.querySelector('[data-sp-spend-chart]') || chartBundleLoaded()) return;
    const src = safeNextPath(source.getAttribute('data-sp-chart-src') || '');
    if (!CHART_BUNDLE.test(src)) return;
    const script = document.createElement('script');
    script.src = src;
    script.defer = true;
    document.head.appendChild(script);
}

export function initIssueAiUsage() {
    let focusId = '';
    document.addEventListener('htmx:beforeRequest', (event) => {
        const elt = event.detail && event.detail.elt;
        focusId = elt && elt.closest && elt.closest(`#${TARGET_ID}`) ? elt.getAttribute('data-aiu-focus') || '' : '';
    });
    document.addEventListener('htmx:load', (event) => {
        loadChartsIn(event.detail && event.detail.elt);
    });
    document.addEventListener('htmx:afterSettle', () => {
        if (!focusId) return;
        const heading = document.getElementById(focusId);
        focusId = '';
        if (heading && heading.closest(`#${TARGET_ID}`)) {
            heading.focus();
            heading.scrollIntoView({ block: 'start' });
        }
    });
}
