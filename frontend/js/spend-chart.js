/* ============================================================
   Spend chart entry — bundled on its own into spend-chart.min.js.

   Holds Apache ECharts (tree-shaken, see lib/echarts.js), so only
   the AI spend report pages load it (report.html, chart_js block).
   The script is deferred, so the document is parsed when it runs.
   ============================================================ */
import { initSpendCharts, watchHtmx } from './modules/spend-chart';

watchHtmx();
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => initSpendCharts());
} else {
    initSpendCharts();
}
